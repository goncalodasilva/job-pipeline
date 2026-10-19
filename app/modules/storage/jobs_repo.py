"""Rows of the ``companies`` and ``jobs`` tables (migration 0004; M4.2). Callers own the
transaction. Times are UTC ISO 8601 text from the Clock; ``snooze_until`` is a local
'YYYY-MM-DD' date. Updates check the row's version and raise ``VersionConflict`` when no row
matches, which also covers a missing job: callers check the job exists first."""

import json
import sqlite3
from dataclasses import dataclass

from app.domain.errors import VersionConflict

# Literal SQL only (no string building).
_INSERT_COMPANY = (
    "INSERT INTO companies (id, name, name_key, created_at, updated_at, version) "
    "VALUES (?, ?, ?, ?, ?, 1)"
)
_INSERT_JOB = (
    "INSERT INTO jobs (id, company_id, title, snapshot_text, captured_at, found_at, "
    "created_at, updated_at, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)"
)
_SELECT_JOB = (
    "SELECT id, company_id, title, triage_state, previous_state, snooze_until, removal_reason, "
    "removal_text, user_corrected, merged_into_job_id, found_at, version FROM jobs WHERE id = ?"
)
_UPDATE_TRIAGE = (
    "UPDATE jobs SET triage_state = ?, previous_state = ?, snooze_until = ?, "
    "removal_reason = ?, removal_text = ?, updated_at = ?, version = version + 1 "
    "WHERE id = ? AND version = ?"
)


@dataclass(frozen=True)
class StoredJob:
    id: str
    company_id: str | None
    title: str
    triage_state: str
    previous_state: str | None
    snooze_until: str | None
    removal_reason: str | None
    removal_text: str | None
    user_corrected: tuple[str, ...]
    merged_into_job_id: str | None
    found_at: str
    version: int


def insert_company(
    conn: sqlite3.Connection, *, id: str, name: str, name_key: str, now: str
) -> None:
    conn.execute(_INSERT_COMPANY, (id, name, name_key, now, now))


def insert_job(
    conn: sqlite3.Connection,
    *,
    id: str,
    title: str,
    snapshot_text: str,
    captured_at: str,
    found_at: str,
    now: str,
    company_id: str | None = None,
) -> None:
    """Store a new job as New, with no summary fields yet."""
    conn.execute(
        _INSERT_JOB, (id, company_id, title, snapshot_text, captured_at, found_at, now, now)
    )


def get_job(conn: sqlite3.Connection, job_id: str) -> StoredJob | None:
    row = conn.execute(_SELECT_JOB, (job_id,)).fetchone()
    if row is None:
        return None
    corrected = json.loads(row[8])
    return StoredJob(
        id=row[0],
        company_id=row[1],
        title=row[2],
        triage_state=row[3],
        previous_state=row[4],
        snooze_until=row[5],
        removal_reason=row[6],
        removal_text=row[7],
        user_corrected=tuple(name for name in corrected if isinstance(name, str)),
        merged_into_job_id=row[9],
        found_at=row[10],
        version=row[11],
    )


def update_job_triage(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    expected_version: int,
    triage_state: str,
    previous_state: str | None,
    snooze_until: str | None,
    removal_reason: str | None,
    removal_text: str | None,
    now: str,
) -> None:
    """Write a job's triage if it still has ``expected_version``; ``VersionConflict`` (and
    nothing written) when it doesn't.

    A missing job also surfaces as ``VersionConflict``: the UPDATE matches no row either
    way. Callers that need to tell the two apart (for a "not found" answer) read the job
    with ``get_job`` first, in the same transaction."""
    cursor = conn.execute(
        _UPDATE_TRIAGE,
        (
            triage_state,
            previous_state,
            snooze_until,
            removal_reason,
            removal_text,
            now,
            job_id,
            expected_version,
        ),
    )
    if cursor.rowcount != 1:
        raise VersionConflict(detail="jobs row changed or missing")
