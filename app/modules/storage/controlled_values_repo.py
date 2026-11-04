"""Rows of ``controlled_values`` for the Lists editor (M9.2 part D; ADR-131 items 6-7; migration
0004). Callers own the transaction. ``jobs`` is read only, to count the jobs that hold a value.

Only industry, seniority and employment type are editable: ``work_mode`` is tied to the
``WorkMode`` type of the job preferences. A field name is checked against the lists below before
it reaches SQL, and every user value is bound.
"""

import json
import sqlite3
from collections.abc import Sequence
from typing import Any

from app.domain.controlled_values import EDITABLE_FIELDS, LISTED_FIELDS
from app.domain.errors import VersionConflict


def _editable(field: str) -> None:
    if field not in EDITABLE_FIELDS:
        raise ValueError(f"{field!r} is not an editable list")


def _listed(field: str) -> None:
    if field not in LISTED_FIELDS:
        raise ValueError(f"{field!r} is not a list")


def add_value(conn: sqlite3.Connection, *, value_id: str, field: str, value: str, now: str) -> None:
    """Insert a value with no aliases at version 1. The text is stored as given. A second row
    with the same field and value is an ``sqlite3.IntegrityError``."""
    _editable(field)
    conn.execute(
        "INSERT INTO controlled_values (id, field, value, aliases, created_at, updated_at) "
        "VALUES (?, ?, ?, '[]', ?, ?)",
        (value_id, field, value, now, now),
    )


def get_value(conn: sqlite3.Connection, value_id: str) -> dict[str, Any] | None:
    """The row with this id (any field), or None."""
    row = conn.execute(
        "SELECT id, field, value, aliases, version FROM controlled_values WHERE id = ?",
        (value_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "field": row[1],
        "value": row[2],
        "aliases": list(json.loads(row[3])),
        "version": row[4],
    }


def set_aliases(
    conn: sqlite3.Connection,
    value_id: str,
    aliases: Sequence[str],
    *,
    expected_version: int,
    now: str,
) -> bool:
    """Replace the alias list and bump ``version``. False, and nothing changes, when the row is
    missing or the version is stale. ``ValueError`` for a row of a field that is not editable."""
    row = conn.execute("SELECT field FROM controlled_values WHERE id = ?", (value_id,)).fetchone()
    if row is None:
        return False
    _editable(row[0])
    cursor = conn.execute(
        "UPDATE controlled_values SET aliases = ?, updated_at = ?, version = version + 1 "
        "WHERE id = ? AND version = ?",
        (json.dumps(list(aliases)), now, value_id, expected_version),
    )
    return cursor.rowcount == 1


def jobs_holding(conn: sqlite3.Connection, field: str, value: str) -> int:
    """How many jobs hold ``value`` in column ``field``, whatever their triage state and
    whether or not the field was corrected by the user."""
    _listed(field)
    row = conn.execute(
        f"SELECT count(*) FROM jobs WHERE {field} = ?",  # noqa: S608 - field checked above
        (value,),
    ).fetchone()
    return int(row[0])


def delete_unused(conn: sqlite3.Connection, value_id: str, *, expected_version: int) -> int:
    """Delete the value only when no job holds it for its field, in one statement with the
    version check. Returns how many jobs hold it (0 means it was deleted). ``LookupError`` for an
    unknown id, ``ValueError`` for a field that is not editable, ``VersionConflict`` for a stale
    version (checked before the use count)."""
    row = conn.execute(
        "SELECT field, value, version FROM controlled_values WHERE id = ?", (value_id,)
    ).fetchone()
    if row is None:
        raise LookupError(value_id)
    field, value, version = row
    _editable(field)
    if version != expected_version:
        raise VersionConflict(detail=f"controlled value {value_id} changed elsewhere")
    cursor = conn.execute(
        f"DELETE FROM controlled_values WHERE id = ? AND version = ? AND NOT EXISTS "  # noqa: S608
        f"(SELECT 1 FROM jobs WHERE jobs.{field} = controlled_values.value)",
        (value_id, expected_version),
    )
    return 0 if cursor.rowcount == 1 else jobs_holding(conn, field, value)


def values_with_use(conn: sqlite3.Connection, field: str) -> tuple[dict[str, Any], ...]:
    """The rows of a field by value: id, field, value, aliases (a list), version and the number
    of jobs that hold the value."""
    _listed(field)
    rows = conn.execute(
        f"SELECT id, value, aliases, version, "  # noqa: S608 - field checked above
        f"(SELECT count(*) FROM jobs WHERE jobs.{field} = controlled_values.value) "
        "FROM controlled_values WHERE field = ? ORDER BY value",
        (field,),
    ).fetchall()
    return tuple(
        {
            "id": id_,
            "field": field,
            "value": value,
            "aliases": list(json.loads(aliases)),
            "version": version,
            "jobs": int(jobs),
        }
        for id_, value, aliases, version, jobs in rows
    )
