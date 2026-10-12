"""Rows of the ``roles`` table (migration 0003; ADR-089 item 11). Callers own the
transaction. Times are UTC ISO 8601 text from the Clock."""

import sqlite3
from dataclasses import dataclass

# Literal SQL only (no string building).
_INSERT = (
    "INSERT INTO roles (id, employer, title, start_date, end_date, status, created_at, "
    "updated_at, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)"
)
_SELECT_BY_ID = (
    "SELECT id, employer, title, start_date, end_date, status, version FROM roles WHERE id = ?"
)
_SELECT_ALL = (
    "SELECT id, employer, title, start_date, end_date, status, version FROM roles "
    "ORDER BY created_at, rowid"
)
_CONFIRM = (
    "UPDATE roles SET status = 'confirmed', updated_at = ?, version = version + 1 "
    "WHERE id = ? AND status = 'draft'"
)


@dataclass(frozen=True)
class StoredRole:
    id: str
    employer: str
    title: str
    start_date: str | None
    end_date: str | None
    status: str
    version: int


def insert_role(
    conn: sqlite3.Connection,
    *,
    role_id: str,
    employer: str,
    title: str,
    start_date: str | None,
    end_date: str | None,
    status: str,
    now: str,
) -> None:
    conn.execute(_INSERT, (role_id, employer, title, start_date, end_date, status, now, now))


def get_role(conn: sqlite3.Connection, role_id: str) -> StoredRole | None:
    row = conn.execute(_SELECT_BY_ID, (role_id,)).fetchone()
    return None if row is None else StoredRole(*row)


def list_roles(conn: sqlite3.Connection) -> list[StoredRole]:
    """Every role, oldest first."""
    return [StoredRole(*row) for row in conn.execute(_SELECT_ALL).fetchall()]


def confirm_role(conn: sqlite3.Connection, role_id: str, *, now: str) -> bool:
    """Confirm a draft role; False (and nothing written) when it is confirmed or unknown."""
    return conn.execute(_CONFIRM, (now, role_id)).rowcount == 1
