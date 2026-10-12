"""Rows of the ``cv_imports`` table (migration 0003; ADR-090 item 7). Callers own the
transaction. Times are UTC ISO 8601 text from the Clock. No client file name is stored."""

import sqlite3
from dataclasses import dataclass

# Literal SQL only (no string building).
_INSERT = (
    "INSERT INTO cv_imports (id, kind, stored_name, state, task_id, created_at, updated_at, "
    "version) VALUES (?, ?, ?, 'uploaded', NULL, ?, ?, 1)"
)
_SELECT_BY_ID = "SELECT id, kind, stored_name, state, task_id, version FROM cv_imports WHERE id = ?"
_MARK_DONE = (
    "UPDATE cv_imports SET state = 'done', task_id = ?, updated_at = ?, version = version + 1 "
    "WHERE id = ? AND state = 'uploaded'"
)
_DELETE = "DELETE FROM cv_imports WHERE id = ?"
_SELECT_LISTED = (
    "SELECT id, kind, state, created_at, task_id FROM cv_imports "
    "ORDER BY created_at DESC, rowid DESC"
)
_SELECT_STORED_NAMES = "SELECT stored_name FROM cv_imports"


@dataclass(frozen=True)
class StoredImport:
    id: str
    kind: str
    stored_name: str
    state: str
    task_id: str | None
    version: int


@dataclass(frozen=True)
class ListedImport:
    """An import as a list shows it: without its file name."""

    id: str
    kind: str
    state: str
    created_at: str
    task_id: str | None


def insert_import(
    conn: sqlite3.Connection, *, import_id: str, kind: str, stored_name: str, now: str
) -> None:
    """A new upload, not yet imported."""
    conn.execute(_INSERT, (import_id, kind, stored_name, now, now))


def get_import(conn: sqlite3.Connection, import_id: str) -> StoredImport | None:
    row = conn.execute(_SELECT_BY_ID, (import_id,)).fetchone()
    return None if row is None else StoredImport(*row)


def mark_done(conn: sqlite3.Connection, import_id: str, *, task_id: str, now: str) -> bool:
    """Mark an uploaded import done by ``task_id``; False (and nothing written) when it is
    gone or already done."""
    return conn.execute(_MARK_DONE, (task_id, now, import_id)).rowcount == 1


def delete_import(conn: sqlite3.Connection, import_id: str) -> bool:
    """Delete the row (its drafts keep their text and lose the link); False when unknown."""
    return conn.execute(_DELETE, (import_id,)).rowcount == 1


def list_imports(conn: sqlite3.Connection) -> tuple[ListedImport, ...]:
    """Every import, uploaded or done, newest first."""
    return tuple(ListedImport(*row) for row in conn.execute(_SELECT_LISTED).fetchall())


def stored_names(conn: sqlite3.Connection) -> frozenset[str]:
    """The file name of every import row."""
    return frozenset(row[0] for row in conn.execute(_SELECT_STORED_NAMES).fetchall())
