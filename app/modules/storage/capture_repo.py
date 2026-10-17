"""Rows of the ``capture_notes`` table (migration 0003; ADR-090 item 10). Callers own the
transaction. Times are UTC ISO 8601 text from the Clock.

A note is open while it is neither deleted nor turned into a fact. Deleting is soft
(``deleted_at``) so it can be undone. Each change applies only from the state it changes
(a delete only to an open note, an undo only to a deleted one), so repeating one changes
nothing; ``version`` is bumped on every change.
"""

import sqlite3
from dataclasses import dataclass

# Literal SQL only (no string building).
_INSERT = (
    "INSERT INTO capture_notes (id, text, resolved_fact_id, deleted_at, created_at, updated_at, "
    "version) VALUES (?, ?, NULL, NULL, ?, ?, 1)"
)
_SELECT_BY_ID = (
    "SELECT id, text, resolved_fact_id, deleted_at, created_at FROM capture_notes WHERE id = ?"
)
_SELECT_OPEN = (
    "SELECT id, text, resolved_fact_id, deleted_at, created_at FROM capture_notes "
    "WHERE resolved_fact_id IS NULL AND deleted_at IS NULL ORDER BY created_at, rowid"
)
_DELETE = (
    "UPDATE capture_notes SET deleted_at = ?, updated_at = ?, version = version + 1 "
    "WHERE id = ? AND deleted_at IS NULL AND resolved_fact_id IS NULL"
)
_UNDO_DELETE = (
    "UPDATE capture_notes SET deleted_at = NULL, updated_at = ?, version = version + 1 "
    "WHERE id = ? AND deleted_at IS NOT NULL"
)
_RESOLVE = (
    "UPDATE capture_notes SET resolved_fact_id = ?, updated_at = ?, version = version + 1 "
    "WHERE id = ? AND resolved_fact_id IS NULL AND deleted_at IS NULL"
)


@dataclass(frozen=True)
class StoredNote:
    id: str
    text: str
    resolved_fact_id: str | None
    deleted_at: str | None
    created_at: str

    @property
    def open(self) -> bool:
        return self.resolved_fact_id is None and self.deleted_at is None


def insert_note(conn: sqlite3.Connection, *, note_id: str, text: str, now: str) -> None:
    conn.execute(_INSERT, (note_id, text, now, now))


def get_note(conn: sqlite3.Connection, note_id: str) -> StoredNote | None:
    row = conn.execute(_SELECT_BY_ID, (note_id,)).fetchone()
    return None if row is None else StoredNote(*row)


def list_open(conn: sqlite3.Connection) -> tuple[StoredNote, ...]:
    """The open notes, oldest first."""
    return tuple(StoredNote(*row) for row in conn.execute(_SELECT_OPEN).fetchall())


def delete_note(conn: sqlite3.Connection, note_id: str, *, now: str) -> bool:
    """Soft-delete an open note; False when it isn't open."""
    return conn.execute(_DELETE, (now, now, note_id)).rowcount == 1


def undo_delete(conn: sqlite3.Connection, note_id: str, *, now: str) -> bool:
    """Bring a deleted note back; False when it isn't deleted."""
    return conn.execute(_UNDO_DELETE, (now, note_id)).rowcount == 1


def resolve_note(conn: sqlite3.Connection, note_id: str, *, fact_id: str, now: str) -> bool:
    """Record the fact an open note was turned into; False when it isn't open."""
    return conn.execute(_RESOLVE, (fact_id, now, note_id)).rowcount == 1
