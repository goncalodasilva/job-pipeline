"""One consistent snapshot of what an inbox card shows; the queries do not grow with the queue."""

import json
import sqlite3
from typing import Any

from app.domain.errors import JobNotFound
from app.modules.storage import job_summary_repo


def card(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    """The job with its current summary, requirements and their fit matches, the sources of
    the card (including merged duplicates) and whether a summary or fit task is active.
    Raises ``JobNotFound`` for an unknown job or a duplicate merged into another card."""
    job = job_summary_repo.job(conn, job_id)
    if job["merged_into_job_id"] is not None:
        raise JobNotFound()
    summary = conn.execute(
        "SELECT id,summary,provider FROM job_summaries WHERE job_id=? "
        "ORDER BY created_at DESC,rowid DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    summary_id = summary[0] if summary else None
    requirements = conn.execute(
        "SELECT id,text FROM job_requirements WHERE summary_id=? ORDER BY position",
        (summary_id,),
    ).fetchall()
    assessment = conn.execute(
        "SELECT result_json,provider FROM job_assessments WHERE job_id=? AND summary_id=? "
        "ORDER BY created_at DESC,rowid DESC LIMIT 1",
        (job_id, summary_id),
    ).fetchone()
    active = conn.execute(
        "SELECT type FROM tasks WHERE dedupe_key IN (?,?) AND status IN ('queued','running')",
        (f"summarize_job:{job_id}", f"assess_fit:{job_id}"),
    ).fetchall()
    sources = conn.execute(
        "SELECT s.url,s.kind FROM job_sources s JOIN jobs j ON j.id=s.job_id "
        "WHERE j.id=? OR j.merged_into_job_id=? ORDER BY s.found_at,s.rowid",
        (job_id, job_id),
    ).fetchall()
    # The status of the job's application once it has left Preparing (None otherwise).
    moved_on = conn.execute(
        "SELECT s.label FROM applications a JOIN application_statuses s ON s.key=a.status_key "
        "WHERE a.job_id=? AND s.phase<>'preparing'",
        (job_id,),
    ).fetchone()
    matches: dict[str, bool] = {}
    if assessment:
        matches = {
            item["requirement_id"]: bool(item["met"])
            for item in json.loads(assessment[0]).get("requirements", [])
        }
    return {
        "job": job,
        "summary": {"text": summary[1], "practice": summary[2] == "fake"} if summary else None,
        "requirements": tuple(
            {"text": text, "met": matches.get(id_) if assessment else None}
            for id_, text in requirements
        ),
        "fit_practice": bool(assessment and assessment[1] == "fake"),
        "processing": any(row[0] == "summarize_job" for row in active),
        "active": bool(active),
        "moved_on": moved_on[0] if moved_on else None,
        "sources": tuple((row[0], row[1]) for row in sources),
    }
