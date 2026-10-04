"""Rows of the ``settings`` table (spec 2.7, 2.8). Callers own the transaction."""

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class StoredSetting:
    key: str
    value: str  # JSON text
    version: int
    updated_at: str


def get_setting(conn: sqlite3.Connection, key: str) -> StoredSetting | None:
    row = conn.execute(
        "SELECT key, value, version, updated_at FROM settings WHERE key = ?", (key,)
    ).fetchone()
    return None if row is None else StoredSetting(row[0], row[1], row[2], row[3])


def insert_setting(conn: sqlite3.Connection, key: str, value: str, now: str) -> bool:
    """Store a new setting at version 1; False when the key already exists."""
    cursor = conn.execute(
        "INSERT INTO settings (key, value, created_at, updated_at, version) "
        "VALUES (?, ?, ?, ?, 1) ON CONFLICT (key) DO NOTHING",
        (key, value, now, now),
    )
    return cursor.rowcount == 1


def update_setting(
    conn: sqlite3.Connection, key: str, value: str, *, expected_version: int, now: str
) -> bool:
    """Replace the value if the row still has ``expected_version``; False when it doesn't."""
    cursor = conn.execute(
        "UPDATE settings SET value = ?, updated_at = ?, version = version + 1 "
        "WHERE key = ? AND version = ?",
        (value, now, key, expected_version),
    )
    return cursor.rowcount == 1
