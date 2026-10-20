"""Detail projections and versioned corrections; callers own the transaction."""

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from app.domain.errors import VersionConflict
from app.domain.facts import FactRef
from app.domain.ids import IdFactory
from app.domain.job_normalization import company_key
from app.modules.storage import facts_repo, job_summary_repo, jobs_repo


def matches(conn: sqlite3.Connection, job_id: str) -> tuple[dict[str, Any], ...]:
    summary = conn.execute(
        "SELECT id FROM job_summaries WHERE job_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    if not summary:
        return ()
    row = conn.execute(
        "SELECT result_json,fact_refs_json FROM job_assessments WHERE job_id=? AND summary_id=? "
        "ORDER BY created_at DESC,rowid DESC LIMIT 1",
        (job_id, summary[0]),
    ).fetchone()
    reported = (
        {item["requirement_id"]: item for item in json.loads(row[0]).get("requirements", [])}
        if row
        else {}
    )
    sent = set(json.loads(row[1])) if row else set()
    facts = {fact.number: fact for fact in facts_repo.list_facts(conn)}
    found = []
    for id_, kind, text in conn.execute(
        "SELECT id,kind,text FROM job_requirements WHERE summary_id=? ORDER BY position",
        (summary[0],),
    ):
        item = reported.get(id_, {})
        citations = []
        refs = item.get("fact_ids", [])
        met = bool(item.get("met") and refs and all(ref in sent for ref in refs))
        for ref in refs if met else ():
            parsed = FactRef.parse(ref)
            fact = facts.get(parsed.number)
            if fact:
                current = fact.current
                wording = "\n".join(
                    part
                    for part in (current.text, current.situation, current.action, current.result)
                    if part
                )
                citations.append(
                    {
                        "id": fact.id,
                        "ref": ref,
                        "wording": wording,
                        "changed": current.version != parsed.version,
                        "unavailable": fact.status != "confirmed" or fact.privacy == "never_use",
                    }
                )
            else:
                citations.append(
                    {
                        "id": None,
                        "ref": ref,
                        "wording": "Cited fact is no longer available.",
                        "changed": True,
                        "unavailable": True,
                    }
                )
        found.append(
            {
                "text": text,
                "kind": kind.replace("_", " "),
                "met": met,
                "assessed": row is not None,
                "citations": tuple(citations),
            }
        )
    return tuple(found)


def sources(conn: sqlite3.Connection, job_id: str) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "id": id_,
            "job_id": source_job,
            "url": url,
            "kind": kind,
            "title": title,
            "merged": source_job != job_id,
        }
        for id_, source_job, url, kind, title in conn.execute(
            "SELECT s.id,j.id,s.url,s.kind,j.title FROM job_sources s JOIN jobs j ON j.id=s.job_id "
            "WHERE j.id=? OR j.merged_into_job_id=? ORDER BY s.found_at,s.rowid",
            (job_id, job_id),
        )
    )


def update(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    expected_version: int,
    values: Mapping[str, Any],
    ids: IdFactory,
    now: str,
) -> None:
    current = job_summary_repo.job(conn, job_id)
    corrected = set(current["user_corrected"])
    changed = {field: value for field, value in values.items() if value != current[field]}
    if not changed:
        return
    corrected.update(field for field in changed if field != "notes")
    merged = {**current, **changed}
    company_id = current["company_id"]
    if "company" in changed:
        name = changed["company"]
        company_id = None
        if name:
            key = company_key(name)
            row = conn.execute("SELECT id FROM companies WHERE name_key=?", (key,)).fetchone()
            company_id = row[0] if row else ids.new()
            if row is None:
                jobs_repo.insert_company(conn, id=company_id, name=name, name_key=key, now=now)
    cursor = conn.execute(
        "UPDATE jobs SET company_id=?,title=?,industry=?,department=?,work_mode=?,locations=?,"
        "employment_type=?,seniority=?,experience_min=?,experience_max=?,experience_text=?,"
        "salary_min=?,salary_max=?,salary_currency=?,salary_period=?,posted_date=?,notes=?,"
        "user_corrected=?,updated_at=?,version=version+1 WHERE id=? AND version=?",
        (
            company_id,
            merged["title"],
            merged["industry"],
            merged["department"],
            merged["work_mode"],
            json.dumps(merged["locations"], ensure_ascii=False),
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
            merged["notes"],
            json.dumps(sorted(corrected)),
            now,
            job_id,
            expected_version,
        ),
    )
    if cursor.rowcount != 1:
        raise VersionConflict()
