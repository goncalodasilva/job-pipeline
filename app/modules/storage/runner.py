"""Forward-only SQL migrations with a backup before upgrading (spec 2.7; ADR-004).

Migration files are named ``NNNN_description.sql``. Each runs in its own transaction together
with its ``schema_migrations`` row, so a failed migration leaves nothing behind. A migration
file must not contain its own ``BEGIN``, ``COMMIT`` or ``ROLLBACK``; the runner refuses one
that ends the transaction early.
"""

import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.domain.clock import Clock
from app.modules.storage.database import Database, create_private_file

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
DEFAULT_KEEP_BACKUPS = 5

_FILE_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_BACKUP_NAME = re.compile(r"^app-\d{8}T\d{6}Z-before-\d{4}\.db$")

# The runner needs this table before it can record migration 0001, so it creates it.
_SCHEMA_MIGRATIONS = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


class MigrationError(Exception):
    """The migrations folder is invalid, or a migration failed and was rolled back."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path


@dataclass(frozen=True)
class MigrationResult:
    applied: tuple[int, ...]
    backup: Path | None


def load_migrations(folder: Path) -> list[Migration]:
    """Return the folder's migrations by version; any misnamed ``.sql`` file is an error."""
    migrations: dict[int, Migration] = {}
    for path in folder.glob("*.sql"):
        match = _FILE_NAME.match(path.name)
        if match is None:
            raise MigrationError(f"migration file name must be NNNN_name.sql: {path.name}")
        version = int(match.group(1))
        if version in migrations:
            raise MigrationError(f"two migrations share number {version:04d}")
        migrations[version] = Migration(version=version, name=match.group(2), path=path)
    return [migrations[version] for version in sorted(migrations)]


def apply_migrations(
    db: Database,
    folder: Path,
    backup_dir: Path,
    clock: Clock,
    keep_backups: int = DEFAULT_KEEP_BACKUPS,
) -> MigrationResult:
    if keep_backups < 1:
        raise ValueError("keep_backups must be at least 1")
    migrations = load_migrations(folder)
    with db.transaction() as conn:
        conn.execute(_SCHEMA_MIGRATIONS)
    with db.exclusive() as conn:
        done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        pending = _pending(migrations, done)
        if not pending:
            return MigrationResult(applied=(), backup=None)
        backup = None
        if done:  # an existing database: keep a copy before changing it
            backup = _backup(conn, backup_dir, clock, pending[0].version)
            _prune_backups(backup_dir, keep_backups)
        for migration in pending:
            _apply(conn, migration, clock)
    return MigrationResult(applied=tuple(m.version for m in pending), backup=backup)


def _pending(migrations: list[Migration], done: set[int]) -> list[Migration]:
    known = {migration.version for migration in migrations}
    unknown = done - known
    if unknown:
        raise MigrationError(
            f"the database has migration {max(unknown):04d}, which this app version lacks"
        )
    pending = [migration for migration in migrations if migration.version not in done]
    if pending and done and pending[0].version < max(done):
        raise MigrationError(
            f"migration {pending[0].version:04d} is numbered below one already applied"
        )
    return pending


def _apply(conn: sqlite3.Connection, migration: Migration, clock: Clock) -> None:
    script = migration.path.read_text(encoding="utf-8")
    try:
        # executescript cannot take parameters, so the transaction spans two calls.
        conn.executescript(f"BEGIN IMMEDIATE;\n{script}")
        if not conn.in_transaction:
            raise MigrationError(f"migration {migration.version:04d} ended its own transaction")
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (migration.version, migration.name, clock.now().isoformat()),
        )
        conn.execute("COMMIT")
    except sqlite3.Error as error:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise MigrationError(f"migration {migration.version:04d} failed and was undone") from error
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _backup(conn: sqlite3.Connection, backup_dir: Path, clock: Clock, before: int) -> Path:
    """Copy the database with the sqlite3 backup API, which includes rows still in the WAL.

    The copy is a full copy of personal data, so the folder and file are owner-only.
    """
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    stamp = clock.now().strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"app-{stamp}-before-{before:04d}.db"
    partial = target.with_suffix(".partial")
    _remove_partial(partial)
    try:
        create_private_file(partial)
        destination = sqlite3.connect(partial)
        try:
            _backup_copy(conn, destination)
        finally:
            destination.close()
        partial.replace(target)
    except BaseException:
        _remove_partial(partial)
        raise
    return target


def _backup_copy(source: sqlite3.Connection, destination: sqlite3.Connection) -> None:
    source.backup(destination)


def _remove_partial(partial: Path) -> None:
    for path in (partial, partial.with_name(partial.name + "-journal")):
        path.unlink(missing_ok=True)


def _prune_backups(backup_dir: Path, keep: int) -> None:
    """Delete the oldest backups the runner made; other files in the folder are left alone."""
    backups = sorted(p for p in backup_dir.iterdir() if _BACKUP_NAME.match(p.name))
    for old in backups[: max(len(backups) - keep, 0)]:
        old.unlink()
