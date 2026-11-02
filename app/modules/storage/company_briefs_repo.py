"""Company-brief persistence (M8.3). Callers own the short transaction.

A brief is only ever added: older rows are kept and the newest by ``found_at`` is the one shown.
"""

import json
import sqlite3
from dataclasses import dataclass

from app.domain.company_brief import BriefSection


@dataclass(frozen=True)
class CompanyFields:
    """What a brief asks about: the company's own stored words and its version."""

    id: str
    name: str
    website: str
    industry: str
    version: int


@dataclass(frozen=True)
class StoredBrief:
    id: str
    company_id: str
    sections: tuple[BriefSection, ...]
    sources: tuple[str, ...]
    found_at: str
    provider: str | None
    model: str | None


@dataclass(frozen=True)
class LatestTask:
    id: str
    status: str
    error: str | None
    result_ref: str | None


def company(conn: sqlite3.Connection, company_id: str) -> CompanyFields | None:
    row = conn.execute(
        "SELECT id, name, coalesce(website,''), coalesce(industry,''), version "
        "FROM companies WHERE id=?",
        (company_id,),
    ).fetchone()
    return CompanyFields(*row) if row else None


def latest(conn: sqlite3.Connection, company_id: str) -> StoredBrief | None:
    row = conn.execute(
        "SELECT id, company_id, content, sources, found_at, provider, model FROM company_briefs "
        "WHERE company_id=? ORDER BY found_at DESC, created_at DESC, id DESC LIMIT 1",
        (company_id,),
    ).fetchone()
    if row is None:
        return None
    content, sources = json.loads(row[2]), json.loads(row[3])
    return StoredBrief(
        row[0],
        row[1],
        tuple(
            BriefSection(s["heading"], s["text"], s["source_url"], s["source_date"])
            for s in content.get("sections", [])
        ),
        tuple(sources),
        row[4],
        row[5],
        row[6],
    )


def insert(
    conn: sqlite3.Connection,
    *,
    brief_id: str,
    company_id: str,
    sections: tuple[BriefSection, ...],
    sources: tuple[str, ...],
    found_at: str,
    provider: str,
    model: str,
    prompt_id: str,
    prompt_version: str,
) -> None:
    content = {
        "sections": [
            {
                "heading": s.heading,
                "text": s.text,
                "source_url": s.source_url,
                "source_date": s.source_date,
            }
            for s in sections
        ]
    }
    conn.execute(
        "INSERT INTO company_briefs(id, company_id, content, sources, found_at, provider, model, "
        "prompt_id, prompt_version, created_at, updated_at, version) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
        (
            brief_id,
            company_id,
            json.dumps(content, ensure_ascii=False),
            json.dumps(list(sources), ensure_ascii=False),
            found_at,
            provider,
            model,
            prompt_id,
            prompt_version,
            found_at,
            found_at,
        ),
    )


def latest_task(conn: sqlite3.Connection, task_type: str, company_id: str) -> LatestTask | None:
    row = conn.execute(
        "SELECT id, status, error, result_ref FROM tasks WHERE type=? AND dedupe_key=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (task_type, task_type + ":" + company_id),
    ).fetchone()
    return LatestTask(*row) if row else None
