"""Recognise this app's database without changing it (step 1; ADR-049 item 3)."""

import sqlite3
from pathlib import Path


def is_workspace_database(path: Path) -> bool:
    """Whether ``path`` is a SQLite file holding this app's ``schema_migrations`` table.

    Never creates or modifies anything. It reads the file as immutable first, which writes no
    journal, WAL or shared-memory files. Only when the WAL and shared-memory files both
    already exist (a database in use keeps recent changes there) does it read again in plain
    read-only mode, which then creates nothing new. Any error means "no".
    """
    if not path.is_file():
        return False
    if _has_migrations_table(path, "mode=ro&immutable=1"):
        return True
    in_use = all(path.with_name(path.name + suffix).is_file() for suffix in ("-wal", "-shm"))
    return in_use and _has_migrations_table(path, "mode=ro")


def _has_migrations_table(path: Path, query: str) -> bool:
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?{query}", uri=True)
    except sqlite3.Error:
        return False
    try:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
    except sqlite3.Error:
        return False
    finally:
        connection.close()
    return row is not None
