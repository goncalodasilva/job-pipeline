"""Rows of the ``skills`` table (migration 0003; ADR-090 item 9). Callers own the
transaction. ``aliases`` is a JSON array; times are UTC ISO 8601 text from the Clock."""

import sqlite3
from dataclasses import dataclass

# Literal SQL only (no string building).
_INSERT = (
    "INSERT INTO skills (id, name, normalized_name, aliases, created_at, updated_at, version) "
    "VALUES (?, ?, ?, ?, ?, ?, 1)"
)
_SELECT_ALL = "SELECT id, name, aliases, version FROM skills ORDER BY normalized_name"
_SET_ALIASES = (
    "UPDATE skills SET aliases = ?, updated_at = ?, version = version + 1 "
    "WHERE id = ? AND version = ?"
)


@dataclass(frozen=True)
class StoredSkill:
    id: str
    name: str
    aliases: str  # a JSON array
    version: int


def insert_skill(
    conn: sqlite3.Connection,
    *,
    skill_id: str,
    name: str,
    normalized_name: str,
    aliases: str,
    now: str,
) -> None:
    conn.execute(_INSERT, (skill_id, name, normalized_name, aliases, now, now))


def list_skills(conn: sqlite3.Connection) -> list[StoredSkill]:
    """Every skill, in alphabetical order."""
    return [StoredSkill(*row) for row in conn.execute(_SELECT_ALL).fetchall()]


def set_aliases(
    conn: sqlite3.Connection, skill_id: str, aliases: str, *, expected_version: int, now: str
) -> bool:
    """Replace a skill's aliases; False when the row is missing or stale."""
    cursor = conn.execute(_SET_ALIASES, (aliases, now, skill_id, expected_version))
    return cursor.rowcount == 1
