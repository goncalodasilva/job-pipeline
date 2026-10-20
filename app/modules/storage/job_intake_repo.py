"""Intake lookups and merge links. Callers own the transaction (M4.3)."""

import sqlite3
from dataclasses import dataclass

from app.domain.errors import VersionConflict


@dataclass(frozen=True)
class IntakeJob:
    id: str
    snapshot: str
    merged_into: str | None
    version: int


def all_jobs(conn: sqlite3.Connection) -> list[IntakeJob]:
    return [
        IntakeJob(*row)
        for row in conn.execute(
            "SELECT id, snapshot_text, merged_into_job_id, version FROM jobs "
            "ORDER BY found_at, rowid"
        )
    ]


def source_job(conn: sqlite3.Connection, canonical: str) -> str | None:
    row = conn.execute(
        "SELECT job_id FROM job_sources WHERE canonical_url = ? ORDER BY rowid DESC LIMIT 1",
        (canonical,),
    ).fetchone()
    return row[0] if row else None


def insert_source(
    conn: sqlite3.Connection,
    *,
    id: str,
    job_id: str,
    url: str | None,
    canonical: str | None,
    kind: str,
    now: str,
) -> None:
    conn.execute(
        "INSERT INTO job_sources (id, job_id, url, canonical_url, kind, found_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (id, job_id, url, canonical, kind, now, now),
    )


def set_merge(conn: sqlite3.Connection, job: IntakeJob, target: str | None, now: str) -> None:
    cursor = conn.execute(
        "UPDATE jobs SET merged_into_job_id = ?, updated_at = ?, version = version + 1 "
        "WHERE id = ? AND version = ?",
        (target, now, job.id, job.version),
    )
    if cursor.rowcount != 1:
        raise VersionConflict()


def dismissed_pairs(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    return set(conn.execute("SELECT job_a_id, job_b_id FROM job_pair_dismissals"))


def dismiss(conn: sqlite3.Connection, first: str, second: str, now: str) -> None:
    a, b = sorted((first, second))
    conn.execute(
        "INSERT OR IGNORE INTO job_pair_dismissals (job_a_id, job_b_id, created_at) "
        "VALUES (?, ?, ?)",
        (a, b, now),
    )
