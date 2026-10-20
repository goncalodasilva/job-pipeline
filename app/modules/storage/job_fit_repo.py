"""Fit source snapshots, batched fact context and append-only assessment writes (M4.5)."""

import json
import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.domain.errors import VersionConflict
from app.domain.fact_selection import FactRecord
from app.domain.facts import FactContent, FactStatus, Privacy
from app.domain.fit import Requirement
from app.modules.storage import facts_repo, job_summary_repo, roles_repo


@dataclass(frozen=True)
class FitInputs:
    job: dict[str, Any]
    summary_id: str | None
    requirements: tuple[Requirement, ...]
    facts: tuple[FactRecord, ...]


def inputs(conn: sqlite3.Connection, job_id: str) -> FitInputs:
    job = job_summary_repo.job(conn, job_id)
    summary_id = current_summary(conn, job_id)
    requirements = (
        tuple(
            Requirement(*row)
            for row in conn.execute(
                "SELECT id,kind,text FROM job_requirements WHERE summary_id=? ORDER BY position",
                (summary_id,),
            )
        )
        if summary_id
        else ()
    )
    roles = {
        role.id: " at ".join(part for part in (role.title, role.employer) if part)
        for role in roles_repo.list_roles(conn)
    }
    skills = defaultdict(list)
    for fact_id, name in conn.execute(
        "SELECT fs.fact_id,s.name FROM fact_skills fs JOIN skills s ON s.id=fs.skill_id "
        "ORDER BY s.name"
    ):
        skills[fact_id].append(name)
    records = tuple(
        FactRecord(
            row.number,
            row.current.version,
            True,
            FactStatus(row.status),
            Privacy(row.privacy),
            FactContent(
                row.current.text, row.current.situation, row.current.action, row.current.result
            ),
            roles.get(row.role_id or "") or None,
            tuple(skills[row.id]),
        )
        for row in facts_repo.list_facts(conn)
    )
    return FitInputs(job, summary_id, requirements, records)


def current_summary(conn: sqlite3.Connection, job_id: str) -> str | None:
    row = conn.execute(
        "SELECT id FROM job_summaries WHERE job_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    return row[0] if row else None


def store(
    conn: sqlite3.Connection,
    *,
    id: str,
    job_id: str,
    summary_id: str,
    result: dict[str, Any],
    band: str,
    flagged: bool,
    fact_refs: Sequence[str],
    provider: str,
    model: str,
    prompt_id: str,
    prompt_version: str,
    task_id: str,
    now: str,
) -> str:
    conn.execute(
        "INSERT INTO job_assessments (id,job_id,summary_id,result_json,fit_band,"
        "deal_breaker,fact_refs_json,provider,model,prompt_id,prompt_version,task_id,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            id,
            job_id,
            summary_id,
            json.dumps(result, ensure_ascii=False),
            band,
            int(flagged),
            json.dumps(list(fact_refs)),
            provider,
            model,
            prompt_id,
            prompt_version,
            task_id,
            now,
        ),
    )
    # An older paid result remains historical; it must never overwrite a newer summary's band.
    if current_summary(conn, job_id) == summary_id:
        current = job_summary_repo.job(conn, job_id)
        cursor = conn.execute(
            "UPDATE jobs SET fit_band=?,deal_breaker=?,updated_at=?,version=version+1 "
            "WHERE id=? AND version=?",
            (band, int(flagged), now, job_id, current["version"]),
        )
        if cursor.rowcount != 1:
            raise VersionConflict()
    return f"job_assessment:{id}"


def view_rows(conn: sqlite3.Connection, job_id: str, summary_id: str | None) -> tuple[Any, Any]:
    assessment = conn.execute(
        "SELECT fit_band,deal_breaker,result_json,provider FROM job_assessments "
        "WHERE job_id=? AND summary_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
        (job_id, summary_id),
    ).fetchone()
    task = conn.execute(
        "SELECT id,status,error,attempt FROM tasks WHERE dedupe_key=? "
        "ORDER BY CASE WHEN status IN ('queued','running') THEN 0 ELSE 1 END, "
        "updated_at DESC,created_at DESC,rowid DESC LIMIT 1",
        (f"assess_fit:{job_id}",),
    ).fetchone()
    return assessment, task
