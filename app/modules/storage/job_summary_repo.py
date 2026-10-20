"""Summary inputs and atomic result writes. Callers own transactions (M4.4)."""

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from app.domain.errors import JobNotFound, VersionConflict
from app.domain.ids import IdFactory
from app.domain.job_normalization import company_key, normalize_value
from app.domain.jobs import merge_fields
from app.modules.storage import job_intake_repo, jobs_repo


def job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    cursor = conn.execute(
        "SELECT j.*, c.name AS company FROM jobs j "
        "LEFT JOIN companies c ON c.id=j.company_id WHERE j.id=?",
        (job_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise JobNotFound()
    found = dict(zip((column[0] for column in cursor.description), row, strict=True))
    found["locations"] = json.loads(found["locations"])
    found["user_corrected"] = json.loads(found["user_corrected"])
    return found


def controlled(conn: sqlite3.Connection) -> dict[str, dict[str, list[str]]]:
    lists: dict[str, dict[str, list[str]]] = {}
    for field, value, aliases in conn.execute(
        "SELECT field,value,aliases FROM controlled_values ORDER BY field,value"
    ):
        lists.setdefault(field, {})[value] = json.loads(aliases)
    return lists


def store(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    task_id: str,
    data: Mapping[str, Any],
    summary_id: str,
    provider: str,
    model: str,
    prompt_id: str,
    prompt_version: str,
    ids: IdFactory,
    now: str,
) -> str:
    current = job(conn, job_id)
    lists = controlled(conn)
    fields = dict(data)
    for field, values in lists.items():
        value = fields.get(field)
        if isinstance(value, str):
            fields[field] = normalize_value(value, values)
    merged = merge_fields(current, fields, current["user_corrected"])
    for group in ("salary", "experience"):
        low, high = group + "_min", group + "_max"
        low_value, high_value = merged[low], merged[high]
        if (
            isinstance(low_value, int | float)
            and isinstance(high_value, int | float)
            and low_value > high_value
        ):
            # A user-corrected bound wins; discard only its conflicting generated peer.
            if low in current["user_corrected"]:
                merged[high] = None
            else:
                merged[low] = None
    company_id = None
    name = merged.get("company")
    if isinstance(name, str) and name.strip() and company_key(name):
        key = company_key(name)
        row = conn.execute("SELECT id FROM companies WHERE name_key=?", (key,)).fetchone()
        company_id = row[0] if row else ids.new()
        if row is None:
            jobs_repo.insert_company(conn, id=company_id, name=name, name_key=key, now=now)
    conn.execute(
        "INSERT INTO job_summaries (id,job_id,summary,fields_json,provider,model,"
        "prompt_id,prompt_version,task_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            summary_id,
            job_id,
            data["summary"],
            json.dumps(dict(data), ensure_ascii=False),
            provider,
            model,
            prompt_id,
            prompt_version,
            task_id,
            now,
        ),
    )
    for position, requirement in enumerate(data["requirements"], 1):
        conn.execute(
            "INSERT INTO job_requirements (id,summary_id,position,kind,text) VALUES (?,?,?,?,?)",
            (ids.new(), summary_id, position, requirement["kind"], requirement["text"]),
        )
    for field, values in lists.items():
        value = merged.get(field)
        if isinstance(value, str) and value and value not in values:
            conn.execute(
                "INSERT OR IGNORE INTO field_value_queue "
                "(id,field,value,created_at,updated_at) VALUES (?,?,?,?,?)",
                (ids.new(), field, value, now, now),
            )
    cursor = conn.execute(
        "UPDATE jobs SET company_id=?,title=?,industry=?,department=?,work_mode=?,"
        "locations=?,employment_type=?,seniority=?,experience_min=?,experience_max=?,"
        "experience_text=?,salary_min=?,salary_max=?,salary_currency=?,salary_period=?,"
        "posted_date=?,updated_at=?,version=version+1 WHERE id=? AND version=?",
        (
            company_id,
            merged["title"] or current["title"],
            merged["industry"],
            merged["department"],
            merged["work_mode"],
            json.dumps(merged["locations"] or []),
            merged["employment_type"],
            merged["seniority"],
            merged["experience_min"],
            merged["experience_max"],
            merged["experience_text"],
            merged["salary_min"],
            merged["salary_max"],
            merged["salary_currency"],
            merged["salary_period"],
            merged["posted_date"],
            now,
            job_id,
            current["version"],
        ),
    )
    if cursor.rowcount != 1:
        raise VersionConflict()
    _merge_by_company_title(conn, job_id, company_id, merged["title"], now)
    return f"job_summary:{summary_id}"


def view_rows(
    conn: sqlite3.Connection, job_id: str
) -> tuple[tuple[Any, ...] | None, tuple[Any, ...] | None]:
    summary = conn.execute(
        "SELECT summary,provider FROM job_summaries WHERE job_id=? "
        "ORDER BY created_at DESC,rowid DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    task = conn.execute(
        "SELECT id,status,error,attempt FROM tasks WHERE dedupe_key=? "
        "ORDER BY CASE WHEN status IN ('queued','running') THEN 0 ELSE 1 END, "
        "updated_at DESC,created_at DESC,rowid DESC LIMIT 1",
        (f"summarize_job:{job_id}",),
    ).fetchone()
    return summary, task


def _merge_by_company_title(
    conn: sqlite3.Connection, job_id: str, company_id: str | None, title: object, now: str
) -> None:
    if company_id is None or not isinstance(title, str) or not title.strip():
        return
    all_jobs = {item.id: item for item in job_intake_repo.all_jobs(conn)}
    current = all_jobs[job_id]
    root = current.merged_into or current.id
    pairs = job_intake_repo.dismissed_pairs(conn)
    for other_id, other_title in conn.execute(
        "SELECT id,title FROM jobs WHERE company_id=? AND id<>? ORDER BY found_at,rowid",
        (company_id, job_id),
    ):
        other = all_jobs[other_id]
        target = other.merged_into or other.id
        if target == root or normalize_value(title, {other_title: []}) != other_title:
            continue
        members = [item for item in all_jobs.values() if (item.merged_into or item.id) == root]
        targets = [item for item in all_jobs.values() if (item.merged_into or item.id) == target]
        if any(tuple(sorted((a.id, b.id))) in pairs for a in members for b in targets):
            continue
        for member in members:
            job_intake_repo.set_merge(conn, member, target, now)
        return
