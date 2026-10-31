"""Batched session source reads. Callers own the read snapshot; no writes or providers."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from app.domain.fact_selection import FactRecord
from app.domain.facts import FactContent, FactStatus, Privacy
from app.modules.storage import facts_repo, roles_repo


@dataclass(frozen=True)
class SessionFact:
    id: str
    role_id: str | None
    type: str
    record: FactRecord


def facts(conn: sqlite3.Connection) -> tuple[SessionFact, ...]:
    roles = {
        r.id: " at ".join(p for p in (r.title, r.employer) if p)
        for r in roles_repo.list_roles(conn)
    }
    skills: dict[str, list[str]] = {}
    for fid, name in conn.execute(
        "SELECT fs.fact_id,s.name FROM fact_skills fs "
        "JOIN skills s ON s.id=fs.skill_id ORDER BY s.name"
    ):
        skills.setdefault(fid, []).append(name)
    return tuple(
        SessionFact(
            row.id,
            row.role_id,
            row.type,
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
                tuple(skills.get(row.id, ())),
            ),
        )
        for row in facts_repo.list_facts(conn)
    )


def interviews(conn: sqlite3.Connection, now: str) -> dict[str, datetime]:
    return {
        jid: datetime.fromisoformat(stamp)
        for jid, stamp in conn.execute(
            "SELECT a.job_id,min(e.starts_at) FROM events e "
            "JOIN applications a ON a.id=e.application_id "
            "WHERE e.type='interview' AND e.status='scheduled' "
            "AND e.starts_at>=? GROUP BY a.job_id",
            (now,),
        )
    }


def job_labels(conn: sqlite3.Connection) -> dict[str, str]:
    """Labels for focus choices in one read, rather than a lookup per saved job."""
    return dict(conn.execute("SELECT id,title FROM jobs"))
