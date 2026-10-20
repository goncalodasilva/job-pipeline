"""Applications persistence (M5.2). Callers own the write transaction and history write."""

import sqlite3
from dataclasses import dataclass

from app.domain.errors import VersionConflict


@dataclass(frozen=True)
class StoredApplication:
    id: str
    job_id: str
    status_key: str
    applied_at: str | None
    created_at: str
    updated_at: str
    version: int


def get_application(conn: sqlite3.Connection, application_id: str) -> StoredApplication | None:
    row = conn.execute(
        "SELECT id, job_id, status_key, applied_at, created_at, updated_at, version "
        "FROM applications WHERE id = ?",
        (application_id,),
    ).fetchone()
    return None if row is None else StoredApplication(*row)


def update_application(
    conn: sqlite3.Connection,
    application_id: str,
    *,
    status_key: str,
    applied_at: str | None,
    now: str,
    expected_version: int,
) -> None:
    """Keep the first applied date even when a later caller supplies None or another date."""
    result = conn.execute(
        "UPDATE applications SET status_key = ?, applied_at = COALESCE(applied_at, ?), "
        "updated_at = ?, version = version + 1 WHERE id = ? AND version = ?",
        (status_key, applied_at, now, application_id, expected_version),
    )
    if result.rowcount != 1:
        raise VersionConflict()
