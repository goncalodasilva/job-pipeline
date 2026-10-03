"""The SQLite connection and its transactions (spec 2.7)."""

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Self

BUSY_TIMEOUT_MS = 5000


def create_private_file(path: Path) -> None:
    """Create ``path`` (if missing) readable only by its owner; it will hold personal data."""
    os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
    os.chmod(path, 0o600)


class Database:
    """One SQLite connection in WAL mode; writes go through ``transaction()``.

    The connection runs with ``isolation_level=None``, so the sqlite3 module never opens
    transactions on its own: every write transaction is an explicit ``BEGIN IMMEDIATE``.
    Transaction state belongs to the connection, so ``transaction()`` holds a lock and
    threads take turns. ``connection`` bypasses that lock: use it only for reads from the
    thread that owns the database (start-up, migrations, tests).
    """

    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self.path = path
        self._connection = connection
        self._lock = threading.RLock()

    @classmethod
    def open(cls, path: Path) -> "Database":
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # SQLite gives the -wal and -shm files the main file's mode.
        create_private_file(path)
        connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        try:
            connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise sqlite3.OperationalError(f"WAL journal mode unavailable (got {mode})")
            connection.execute("PRAGMA foreign_keys = ON")
        except BaseException:
            connection.close()
            raise
        return cls(path, connection)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    @contextmanager
    def exclusive(self) -> Iterator[sqlite3.Connection]:
        """Hold the connection without opening a transaction (the migration runner)."""
        with self._lock:
            yield self._connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Take the write lock up front, commit on success and roll back on any error."""
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
                self._connection.execute("COMMIT")
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
