"""SQLite database wrapper and migration runner (M0.11, AC-M0.4, spec 2.7).

Every test uses its own fictional migration folder under ``tmp_path``; the real
``0001_initial.sql`` is covered by task M0.12.
"""

import os
import shutil
import sqlite3
import stat
import threading
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.modules.storage import (
    Database,
    Migration,
    MigrationError,
    MigrationResult,
    apply_migrations,
    load_migrations,
)
from tests.fakes.clock import FrozenClock

pytestmark = pytest.mark.integration

START = datetime(2026, 1, 3, 9, 0, 0, tzinfo=UTC)

WIDGETS_SQL = """
CREATE TABLE widgets (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
"""

GADGETS_SQL = """
CREATE TABLE gadgets (
    id INTEGER PRIMARY KEY,
    widget_id INTEGER NOT NULL REFERENCES widgets(id),
    label TEXT NOT NULL
);
"""

# Creates a table and inserts a row, then fails halfway through.
BROKEN_SQL = """
CREATE TABLE gadgets (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL
);
INSERT INTO widgets (name) VALUES ('Half-done');
THIS IS NOT VALID SQL;
"""


# --- helpers ----------------------------------------------------------------


def write_migration(folder: Path, filename: str, sql: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    path.write_text(sql, encoding="utf-8")
    return path


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {row[0] for row in rows}


def recorded_versions(conn: sqlite3.Connection) -> list[int]:
    return [
        row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")
    ]


def backup_files(backup_dir: Path) -> list[str]:
    if not backup_dir.exists():
        return []
    return sorted(p.name for p in backup_dir.iterdir() if p.is_file())


def backup_name(instant: datetime, version: int) -> str:
    return f"app-{instant.strftime('%Y%m%dT%H%M%SZ')}-before-{version:04d}.db"


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(START)


@pytest.fixture
def migrations_dir(tmp_path: Path) -> Path:
    return tmp_path / "migrations"


@pytest.fixture
def backup_dir(tmp_path: Path) -> Path:
    return tmp_path / "data" / "backups"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "app.db"


@pytest.fixture
def db(db_path: Path) -> Iterator[Database]:
    database = Database.open(db_path)
    try:
        yield database
    finally:
        database.close()


# --- AC-M0.4 ----------------------------------------------------------------


def test_ac_m0_4_empty_db_gets_schema(
    db: Database, db_path: Path, migrations_dir: Path, backup_dir: Path, clock: FrozenClock
) -> None:
    write_migration(migrations_dir, "0001_widgets.sql", WIDGETS_SQL)
    write_migration(migrations_dir, "0002_gadgets.sql", GADGETS_SQL)

    result = apply_migrations(db, migrations_dir, backup_dir, clock)

    assert isinstance(result, MigrationResult)
    assert result.applied == (1, 2)
    assert result.backup is None
    assert backup_files(backup_dir) == []

    assert db.path == db_path
    assert db_path.exists()
    assert {"widgets", "gadgets", "schema_migrations"} <= table_names(db.connection)

    rows = db.connection.execute(
        "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
    ).fetchall()
    assert [row[0] for row in rows] == [1, 2]
    for _, name, applied_at in rows:
        assert name
        applied = datetime.fromisoformat(applied_at)
        assert applied.utcoffset() == timedelta(0)
        assert applied == clock.now()


def test_ac_m0_4_rerun_is_noop_and_makes_no_backup(
    db_path: Path, migrations_dir: Path, backup_dir: Path, clock: FrozenClock
) -> None:
    write_migration(migrations_dir, "0001_widgets.sql", WIDGETS_SQL)
    write_migration(migrations_dir, "0002_gadgets.sql", GADGETS_SQL)

    with Database.open(db_path) as first:
        apply_migrations(first, migrations_dir, backup_dir, clock)
        before = first.connection.execute(
            "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        schema_before = first.connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()

    clock.advance(timedelta(hours=1))
    with Database.open(db_path) as second:
        result = apply_migrations(second, migrations_dir, backup_dir, clock)
        after = second.connection.execute(
            "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        schema_after = second.connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()

    assert result.applied == ()
    assert result.backup is None
    assert backup_files(backup_dir) == []
    assert after == before
    assert schema_after == schema_before


def test_ac_m0_4_existing_db_backed_up_before_new_migration(
    db_path: Path, migrations_dir: Path, backup_dir: Path, clock: FrozenClock
) -> None:
    write_migration(migrations_dir, "0001_widgets.sql", WIDGETS_SQL)
    with Database.open(db_path) as db:
        apply_migrations(db, migrations_dir, backup_dir, clock)
        with db.transaction() as conn:
            conn.execute("INSERT INTO widgets (name) VALUES ('Sprocket')")

    write_migration(migrations_dir, "0002_gadgets.sql", GADGETS_SQL)
    clock.advance(timedelta(hours=1))
    with Database.open(db_path) as db:
        result = apply_migrations(db, migrations_dir, backup_dir, clock)
        assert "gadgets" in table_names(db.connection)
        assert recorded_versions(db.connection) == [1, 2]

    expected = backup_dir / backup_name(clock.now(), 2)
    assert result.applied == (2,)
    assert result.backup == expected
    assert expected.exists()
    assert backup_files(backup_dir) == [expected.name]

    with closing(sqlite3.connect(expected)) as snapshot:
        assert "gadgets" not in table_names(snapshot)
        assert recorded_versions(snapshot) == [1]
        assert snapshot.execute("SELECT name FROM widgets").fetchall() == [("Sprocket",)]


# --- backups ----------------------------------------------------------------


def test_backup_includes_uncheckpointed_wal_rows(
    db: Database, db_path: Path, migrations_dir: Path, backup_dir: Path, clock: FrozenClock
) -> None:
    write_migration(migrations_dir, "0001_widgets.sql", WIDGETS_SQL)
    apply_migrations(db, migrations_dir, backup_dir, clock)

    db.connection.execute("PRAGMA wal_autocheckpoint = 0")
    with db.transaction() as conn:
        conn.executemany(
            "INSERT INTO widgets (name) VALUES (?)",
            [("Cog",), ("Flange",), ("Gizmo",)],
        )

    wal_file = db_path.with_name(db_path.name + "-wal")
    assert wal_file.exists() and wal_file.stat().st_size > 0

    # The main file alone does not yet hold the new rows: they live in the WAL.
    main_only = db_path.parent / "main-only-copy.db"
    shutil.copyfile(db_path, main_only)
    with closing(sqlite3.connect(main_only)) as copy:
        if "widgets" in table_names(copy):
            assert copy.execute("SELECT COUNT(*) FROM widgets").fetchone()[0] == 0

    write_migration(migrations_dir, "0002_gadgets.sql", GADGETS_SQL)
    clock.advance(timedelta(minutes=5))
    result = apply_migrations(db, migrations_dir, backup_dir, clock)

    assert result.backup is not None
    standalone = result.backup.parent / "standalone" / result.backup.name
    standalone.parent.mkdir()
    shutil.copyfile(result.backup, standalone)
    with closing(sqlite3.connect(standalone)) as snapshot:
        names = [row[0] for row in snapshot.execute("SELECT name FROM widgets ORDER BY id")]
    assert names == ["Cog", "Flange", "Gizmo"]


def test_backups_keep_last_five(
    db: Database, migrations_dir: Path, backup_dir: Path, clock: FrozenClock
) -> None:
    write_migration(migrations_dir, "0001_widgets.sql", WIDGETS_SQL)
    apply_migrations(db, migrations_dir, backup_dir, clock)

    made: list[str] = []
    for version in range(2, 9):
        write_migration(
            migrations_dir,
            f"{version:04d}_step_{version}.sql",
            f"CREATE TABLE step_{version} (id INTEGER PRIMARY KEY);",
        )
        clock.advance(timedelta(minutes=1))
        result = apply_migrations(db, migrations_dir, backup_dir, clock)
        assert result.applied == (version,)
        assert result.backup is not None
        assert result.backup.exists()
        made.append(result.backup.name)

    assert len(made) == 7
    assert made == sorted(made)
    assert backup_files(backup_dir) == made[-5:]


def test_backups_respect_keep_backups_argument(
    db: Database, migrations_dir: Path, backup_dir: Path, clock: FrozenClock
) -> None:
    write_migration(migrations_dir, "0001_widgets.sql", WIDGETS_SQL)
    apply_migrations(db, migrations_dir, backup_dir, clock, keep_backups=2)

    made: list[str] = []
    for version in range(2, 5):
        write_migration(
            migrations_dir,
            f"{version:04d}_step_{version}.sql",
            f"CREATE TABLE step_{version} (id INTEGER PRIMARY KEY);",
        )
        clock.advance(timedelta(minutes=1))
        result = apply_migrations(db, migrations_dir, backup_dir, clock, keep_backups=2)
        assert result.backup is not None
        made.append(result.backup.name)

    assert backup_files(backup_dir) == made[-2:]


# --- loading and ordering ---------------------------------------------------


def test_migrations_apply_in_numeric_order(
    db: Database, migrations_dir: Path, backup_dir: Path, clock: FrozenClock
) -> None:
    log_insert = "INSERT INTO apply_log (version) VALUES ({v});"
    # Created deliberately out of order; only the number decides.
    write_migration(migrations_dir, "0010_ten.sql", log_insert.format(v=10))
    write_migration(migrations_dir, "0002_two.sql", log_insert.format(v=2))
    write_migration(
        migrations_dir,
        "0001_log.sql",
        "CREATE TABLE apply_log (\n"
        "    seq INTEGER PRIMARY KEY AUTOINCREMENT,\n"
        "    version INTEGER NOT NULL\n"
        ");\n" + log_insert.format(v=1),
    )
    write_migration(migrations_dir, "0003_three.sql", log_insert.format(v=3))
    # Files that are not migrations are ignored.
    (migrations_dir / "__init__.py").write_text("", encoding="utf-8")
    (migrations_dir / "README.md").write_text("Fictional notes.\n", encoding="utf-8")

    loaded = load_migrations(migrations_dir)
    assert all(isinstance(m, Migration) for m in loaded)
    assert [m.version for m in loaded] == [1, 2, 3, 10]
    assert [m.path.name for m in loaded] == [
        "0001_log.sql",
        "0002_two.sql",
        "0003_three.sql",
        "0010_ten.sql",
    ]

    result = apply_migrations(db, migrations_dir, backup_dir, clock)

    assert result.applied == (1, 2, 3, 10)
    order = [row[0] for row in db.connection.execute("SELECT version FROM apply_log ORDER BY seq")]
    assert order == [1, 2, 3, 10]
    assert recorded_versions(db.connection) == [1, 2, 3, 10]


def test_duplicate_migration_number_rejected(
    db: Database, migrations_dir: Path, backup_dir: Path, clock: FrozenClock
) -> None:
    write_migration(migrations_dir, "0001_widgets.sql", WIDGETS_SQL)
    write_migration(migrations_dir, "0001_gadgets.sql", GADGETS_SQL)

    with pytest.raises(MigrationError):
        load_migrations(migrations_dir)

    with pytest.raises(MigrationError):
        apply_migrations(db, migrations_dir, backup_dir, clock)

    assert not {"widgets", "gadgets"} & table_names(db.connection)


@pytest.mark.parametrize("filename", ["1_widgets.sql", "0002-gadgets.sql", "0002_Gadgets.sql"])
def test_misnamed_sql_file_rejected(
    filename: str, migrations_dir: Path, backup_dir: Path, clock: FrozenClock, db: Database
) -> None:
    # A migration that silently never runs is worse than a loud start-up error.
    write_migration(migrations_dir, "0001_widgets.sql", WIDGETS_SQL)
    write_migration(migrations_dir, filename, GADGETS_SQL)

    with pytest.raises(MigrationError):
        load_migrations(migrations_dir)
    with pytest.raises(MigrationError):
        apply_migrations(db, migrations_dir, backup_dir, clock)
    assert "widgets" not in table_names(db.connection)


def test_failed_migration_rolls_back_and_is_not_recorded(
    db: Database, migrations_dir: Path, backup_dir: Path, clock: FrozenClock
) -> None:
    write_migration(migrations_dir, "0001_widgets.sql", WIDGETS_SQL)
    write_migration(
        migrations_dir,
        "0002_broken.sql",
        BROKEN_SQL,
    )
    write_migration(migrations_dir, "0003_later.sql", "CREATE TABLE later (id INTEGER);\n")

    with pytest.raises(MigrationError):
        apply_migrations(db, migrations_dir, backup_dir, clock)

    conn = db.connection
    assert not conn.in_transaction
    tables = table_names(conn)
    assert "widgets" in tables
    assert "gadgets" not in tables
    assert "later" not in tables
    assert conn.execute("SELECT COUNT(*) FROM widgets").fetchone()[0] == 0
    assert recorded_versions(conn) == [1]


# --- Database ---------------------------------------------------------------


def test_pragmas_wal_fk_busy_timeout(tmp_path: Path) -> None:
    nested = tmp_path / "not" / "yet" / "there" / "app.db"
    with Database.open(nested) as db:
        assert nested.exists()
        conn = db.connection
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000

        with db.transaction() as tx:
            tx.execute("CREATE TABLE parents (id INTEGER PRIMARY KEY)")
            tx.execute(
                "CREATE TABLE children (id INTEGER PRIMARY KEY, "
                "parent_id INTEGER NOT NULL REFERENCES parents(id))"
            )

        with pytest.raises(sqlite3.IntegrityError), db.transaction() as tx:
            tx.execute("INSERT INTO children (parent_id) VALUES (999)")

        assert conn.execute("SELECT COUNT(*) FROM children").fetchone()[0] == 0


def test_transaction_rolls_back_on_exception(db: Database, db_path: Path) -> None:
    with db.transaction() as conn:
        conn.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        conn.execute("INSERT INTO widgets (name) VALUES ('Kept')")

    class Boom(Exception):
        pass

    with pytest.raises(Boom), db.transaction() as conn:
        conn.execute("INSERT INTO widgets (name) VALUES ('Discarded')")
        raise Boom

    assert not db.connection.in_transaction
    with closing(sqlite3.connect(db_path)) as other:
        names = [row[0] for row in other.execute("SELECT name FROM widgets ORDER BY id")]
    assert names == ["Kept"]


def test_transaction_takes_write_lock_up_front(db: Database, db_path: Path) -> None:
    with db.transaction() as conn:
        assert conn.in_transaction
        with closing(sqlite3.connect(db_path, timeout=0.1, isolation_level=None)) as other:
            other.execute("PRAGMA busy_timeout = 100")
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                other.execute("BEGIN IMMEDIATE")


# --- review regressions -------------------------------------------------------


@pytest.mark.parametrize("ending", ["COMMIT;", "END;", "ROLLBACK;"])
def test_migration_that_ends_its_own_transaction_is_not_recorded(
    ending: str, db: Database, migrations_dir: Path, backup_dir: Path, clock: FrozenClock
) -> None:
    write_migration(migrations_dir, "0001_widgets.sql", WIDGETS_SQL)
    write_migration(
        migrations_dir, "0002_sneaky.sql", f"CREATE TABLE sneaky (id INTEGER);\n{ending}\n"
    )

    with pytest.raises(MigrationError):
        apply_migrations(db, migrations_dir, backup_dir, clock)

    assert not db.connection.in_transaction
    assert recorded_versions(db.connection) == [1]


def test_failed_commit_rolls_back_and_database_stays_usable(db: Database) -> None:
    with db.transaction() as tx:
        tx.execute("CREATE TABLE parents (id INTEGER PRIMARY KEY)")
        tx.execute(
            "CREATE TABLE children (id INTEGER PRIMARY KEY, parent_id INTEGER "
            "REFERENCES parents(id) DEFERRABLE INITIALLY DEFERRED)"
        )

    with pytest.raises(sqlite3.IntegrityError), db.transaction() as tx:
        tx.execute("INSERT INTO children (parent_id) VALUES (99)")  # fails only at COMMIT

    assert not db.connection.in_transaction
    with db.transaction() as tx:
        tx.execute("INSERT INTO parents (id) VALUES (1)")
    assert db.connection.execute("SELECT COUNT(*) FROM children").fetchone()[0] == 0


def test_threads_take_turns_in_transactions(db: Database) -> None:
    with db.transaction() as tx:
        tx.execute("CREATE TABLE marks (who TEXT NOT NULL)")
    inside = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def first() -> None:
        try:
            with db.transaction() as tx:
                tx.execute("INSERT INTO marks VALUES ('first')")
                inside.set()
                release.wait(timeout=5)
                raise RuntimeError("first rolls back")
        except RuntimeError:
            pass
        except BaseException as error:  # pragma: no cover - surfaced below
            errors.append(error)

    def second() -> None:
        try:
            with db.transaction() as tx:
                tx.execute("INSERT INTO marks VALUES ('second')")
        except BaseException as error:  # pragma: no cover - surfaced below
            errors.append(error)

    one = threading.Thread(target=first)
    one.start()
    assert inside.wait(timeout=5)
    two = threading.Thread(target=second)
    two.start()
    release.set()
    one.join(timeout=5)
    two.join(timeout=5)

    assert errors == []
    rows = [row[0] for row in db.connection.execute("SELECT who FROM marks")]
    assert rows == ["second"]


def test_unknown_applied_version_rejected(
    db: Database, migrations_dir: Path, backup_dir: Path, clock: FrozenClock
) -> None:
    write_migration(migrations_dir, "0001_widgets.sql", WIDGETS_SQL)
    write_migration(migrations_dir, "0002_gadgets.sql", GADGETS_SQL)
    apply_migrations(db, migrations_dir, backup_dir, clock)
    (migrations_dir / "0002_gadgets.sql").unlink()  # an older app opening a newer database

    with pytest.raises(MigrationError):
        apply_migrations(db, migrations_dir, backup_dir, clock)


def test_new_migration_below_applied_version_rejected(
    db: Database, migrations_dir: Path, backup_dir: Path, clock: FrozenClock
) -> None:
    write_migration(migrations_dir, "0001_widgets.sql", WIDGETS_SQL)
    write_migration(migrations_dir, "0003_gadgets.sql", GADGETS_SQL)
    apply_migrations(db, migrations_dir, backup_dir, clock)
    write_migration(migrations_dir, "0002_late.sql", "CREATE TABLE late (id INTEGER);\n")

    with pytest.raises(MigrationError):
        apply_migrations(db, migrations_dir, backup_dir, clock)
    assert "late" not in table_names(db.connection)


def test_keep_backups_must_be_positive(
    db: Database, migrations_dir: Path, backup_dir: Path, clock: FrozenClock
) -> None:
    write_migration(migrations_dir, "0001_widgets.sql", WIDGETS_SQL)

    with pytest.raises(ValueError):
        apply_migrations(db, migrations_dir, backup_dir, clock, keep_backups=0)


def test_pruning_leaves_other_files_and_failed_backup_leaves_no_partial(
    db: Database,
    migrations_dir: Path,
    backup_dir: Path,
    clock: FrozenClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_migration(migrations_dir, "0001_widgets.sql", WIDGETS_SQL)
    apply_migrations(db, migrations_dir, backup_dir, clock, keep_backups=1)
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "app-manual.db").write_text("kept", encoding="utf-8")
    (backup_dir / "app-0-old.db").write_text("kept", encoding="utf-8")
    for version in (2, 3):
        clock.advance(timedelta(hours=1))
        write_migration(
            migrations_dir,
            f"{version:04d}_t{version}.sql",
            f"CREATE TABLE t{version} (id INTEGER);\n",
        )
        apply_migrations(db, migrations_dir, backup_dir, clock, keep_backups=1)

    names = backup_files(backup_dir)
    assert "app-manual.db" in names
    assert "app-0-old.db" in names
    assert [n for n in names if "-before-" in n] == [backup_name(clock.now(), 3)]

    clock.advance(timedelta(hours=1))
    write_migration(migrations_dir, "0004_t4.sql", "CREATE TABLE t4 (id INTEGER);\n")

    def failing_backup(source: sqlite3.Connection, destination: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("disk full (fictional)")

    monkeypatch.setattr("app.modules.storage.runner._backup_copy", failing_backup, raising=True)
    with pytest.raises(sqlite3.OperationalError):
        apply_migrations(db, migrations_dir, backup_dir, clock, keep_backups=1)
    assert not [n for n in backup_files(backup_dir) if "partial" in n]
    assert "t4" not in table_names(db.connection)


def test_database_and_backups_are_private_on_posix(
    db_path: Path, migrations_dir: Path, backup_dir: Path, clock: FrozenClock
) -> None:
    write_migration(migrations_dir, "0001_widgets.sql", WIDGETS_SQL)
    with Database.open(db_path) as db:
        apply_migrations(db, migrations_dir, backup_dir, clock)
        clock.advance(timedelta(hours=1))
        write_migration(migrations_dir, "0002_gadgets.sql", GADGETS_SQL)
        result = apply_migrations(db, migrations_dir, backup_dir, clock)
        assert result.backup is not None

        if os.name != "posix":  # Windows has no owner-only mode bits; ACLs protect the folder
            return
        assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700
        for path in (db_path, db_path.with_name("app.db-wal"), result.backup):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name
        assert stat.S_IMODE(backup_dir.stat().st_mode) == 0o700


# --- close() and transactions in other threads (M2.8 code review) -------------------------


def test_close_waits_for_another_threads_transaction(tmp_path: Path) -> None:
    db = Database.open(tmp_path / "app.db")
    inside = threading.Event()
    release = threading.Event()
    outcome: list[str] = []

    def hold_transaction() -> None:
        with db.transaction() as conn:
            conn.execute("CREATE TABLE fictional (id INTEGER)")
            inside.set()
            release.wait(10)
            conn.execute("INSERT INTO fictional VALUES (1)")
        outcome.append("committed")

    holder = threading.Thread(target=hold_transaction)
    holder.start()
    assert inside.wait(10)
    closer = threading.Thread(target=db.close)
    closer.start()
    closer.join(0.2)
    assert closer.is_alive(), "close() must wait while another thread is in a transaction"

    release.set()
    holder.join(10)
    closer.join(10)
    assert not closer.is_alive()
    assert outcome == ["committed"]
    with pytest.raises(sqlite3.ProgrammingError):
        db.connection.execute("SELECT 1")
    with closing(sqlite3.connect(tmp_path / "app.db")) as check:
        assert check.execute("SELECT count(*) FROM fictional").fetchone()[0] == 1


def test_close_inside_own_transaction_returns(tmp_path: Path) -> None:
    db = Database.open(tmp_path / "app.db")
    finished = threading.Event()

    def close_inside() -> None:
        try:
            with db.transaction():
                db.close()
        except sqlite3.ProgrammingError:
            pass  # the commit can't run on a closed connection
        finally:
            finished.set()

    worker = threading.Thread(target=close_inside, daemon=True)
    worker.start()
    assert finished.wait(10), "close() inside the same thread's transaction must not hang"
    with pytest.raises(sqlite3.ProgrammingError):
        db.connection.execute("SELECT 1")
