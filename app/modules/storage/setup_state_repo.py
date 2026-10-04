"""Rows of the ``setup_state`` table (spec 2.7; ADR-018). Callers own the transaction."""

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class StoredStep:
    step_id: str
    status: str
    completed_at: str | None
    version: int


def list_steps(conn: sqlite3.Connection) -> list[StoredStep]:
    rows = conn.execute(
        "SELECT step_id, status, completed_at, version FROM setup_state ORDER BY step_id"
    ).fetchall()
    return [StoredStep(row[0], row[1], row[2], row[3]) for row in rows]


def upsert_step(
    conn: sqlite3.Connection, step_id: str, status: str, *, completed_at: str | None, now: str
) -> None:
    conn.execute(
        "INSERT INTO setup_state (step_id, status, completed_at, created_at, updated_at, version) "
        "VALUES (?, ?, ?, ?, ?, 1) "
        "ON CONFLICT (step_id) DO UPDATE SET status = excluded.status, "
        "completed_at = excluded.completed_at, updated_at = excluded.updated_at, "
        "version = version + 1",
        (step_id, status, completed_at, now, now),
    )
