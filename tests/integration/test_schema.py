"""Migration 0001 and schema equality (M0.12, AC-M0.4, spec 2.7, ADR-027, ADR-038).

Spec 2.7: a fresh database and a database upgraded step by step must end with identical
schemas. The step-by-step path replays the real migrations folder one file at a time, so the
check stays meaningful as later milestones add migrations. All values are fictional.
"""

import hashlib
import json
import re
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations, load_migrations
from tests.fakes.clock import FrozenClock

pytestmark = pytest.mark.integration

START = datetime(2026, 1, 3, 9, 0, 0, tzinfo=UTC)
STAMP = "2026-01-03T09:00:00+00:00"

EXPECTED_TABLES = {"schema_migrations", "settings", "setup_state", "tasks", "usage_records"}
MUTABLE_TABLES = (
    "settings",
    "setup_state",
    "tasks",
    "usage_records",
    "provider_usage_snapshots",
)
TASK_STATES = ("queued", "running", "succeeded", "failed", "cancelled", "interrupted")
SETUP_STATES = ("pending", "done", "skipped")


# --- helpers ----------------------------------------------------------------


def _normalize_sql(sql: str | None) -> str | None:
    if sql is None:
        return None
    return re.sub(r"\s+", " ", sql).strip()


def _user_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        " ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


def schema_snapshot(conn: sqlite3.Connection) -> dict[str, object]:
    master = sorted(
        (row[0], row[1], row[2], _normalize_sql(row[3]))
        for row in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master")
        if not row[1].startswith("sqlite_")
    )
    per_table: dict[str, object] = {}
    for table in _user_tables(conn):
        quoted = '"' + table.replace('"', '""') + '"'
        per_table[table] = {
            "columns": conn.execute(f"PRAGMA table_info({quoted})").fetchall(),
            "indexes": sorted(conn.execute(f"PRAGMA index_list({quoted})").fetchall()),
            "foreign_keys": conn.execute(f"PRAGMA foreign_key_list({quoted})").fetchall(),
        }
    return {"master": master, "tables": per_table}


def recorded_versions(conn: sqlite3.Connection) -> list[int]:
    return [
        row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")
    ]


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database.open(tmp_path / "data" / "app.db")
    try:
        apply_migrations(database, MIGRATIONS_DIR, tmp_path / "backups", FrozenClock(START))
        yield database
    finally:
        database.close()


def insert_task(conn: sqlite3.Connection, task_id: str, status: str) -> None:
    conn.execute(
        "INSERT INTO tasks (id, type, status, dedupe_key, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, "demo.example", status, f"demo:{task_id}", STAMP, STAMP),
    )


# --- tests ------------------------------------------------------------------


def test_ac_m0_4_schema_fresh_equals_upgraded_step_by_step(tmp_path: Path) -> None:
    migrations = load_migrations(MIGRATIONS_DIR)
    versions = [m.version for m in migrations]
    assert 1 in versions, "0001_initial.sql is missing from the migrations folder"

    with Database.open(tmp_path / "fresh" / "app.db") as fresh:
        apply_migrations(fresh, MIGRATIONS_DIR, tmp_path / "fresh-backups", FrozenClock(START))
        fresh_schema = schema_snapshot(fresh.connection)
        fresh_versions = recorded_versions(fresh.connection)

    with Database.open(tmp_path / "stepped" / "app.db") as stepped:
        for index, migration in enumerate(migrations):
            partial = tmp_path / f"partial-{index:04d}"
            partial.mkdir()
            for earlier in migrations[: index + 1]:
                shutil.copy(earlier.path, partial / earlier.path.name)
            result = apply_migrations(
                stepped, partial, tmp_path / "stepped-backups", FrozenClock(START)
            )
            assert result.applied == (migration.version,)
        stepped_schema = schema_snapshot(stepped.connection)
        stepped_versions = recorded_versions(stepped.connection)

    assert fresh_schema == stepped_schema
    assert fresh_versions == versions
    assert stepped_versions == versions


@pytest.fixture
def db_at_0001(tmp_path: Path) -> Iterator[Database]:
    # Later migrations add tables, columns and the dedupe index; this checks 0001 alone.
    only_0001 = tmp_path / "migrations-0001"
    only_0001.mkdir()
    shutil.copy(MIGRATIONS_DIR / "0001_initial.sql", only_0001 / "0001_initial.sql")
    database = Database.open(tmp_path / "data-0001" / "app.db")
    try:
        apply_migrations(database, only_0001, tmp_path / "backups-0001", FrozenClock(START))
        yield database
    finally:
        database.close()


def test_0001_creates_expected_tables(db_at_0001: Database) -> None:
    conn = db_at_0001.connection
    assert set(_user_tables(conn)) == EXPECTED_TABLES
    assert 1 in recorded_versions(conn)

    names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master")]
    assert not [name for name in names if "fts" in name.lower()]
    virtual = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND sql LIKE '%VIRTUAL%'"
    ).fetchall()
    assert virtual == []

    migration_columns = [
        (row[1], row[2], row[3])
        for row in conn.execute("PRAGMA table_info(schema_migrations)").fetchall()
    ]
    assert [c[0] for c in migration_columns] == ["version", "name", "applied_at"]

    usage_columns = {row[1] for row in conn.execute("PRAGMA table_info(usage_records)")}
    assert "task_id" not in usage_columns

    task_columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    assert "dedupe_key" in task_columns
    for index in conn.execute("PRAGMA index_list(tasks)").fetchall():
        name, unique = index[1], index[2]
        if unique:
            covered = {row[2] for row in conn.execute(f'PRAGMA index_info("{name}")')}
            assert "dedupe_key" not in covered, f"unique index {name} covers dedupe_key (M2)"


def test_0001_sql_declares_schema_migrations_if_not_exists() -> None:
    path = MIGRATIONS_DIR / "0001_initial.sql"
    assert path.is_file(), "0001_initial.sql is missing"
    sql = _normalize_sql(path.read_text(encoding="utf-8")) or ""
    assert re.search(r"CREATE TABLE IF NOT EXISTS schema_migrations\b", sql, flags=re.IGNORECASE), (
        "0001 must declare schema_migrations with IF NOT EXISTS (ADR-038)"
    )


@pytest.mark.parametrize("table", MUTABLE_TABLES)
def test_mutable_tables_have_created_updated_version(db: Database, table: str) -> None:
    columns = {
        row[1]: {"type": row[2].upper(), "notnull": row[3], "default": row[4]}
        for row in db.connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for name in ("created_at", "updated_at", "version"):
        assert name in columns, f"{table} lacks {name}"
        assert columns[name]["notnull"] == 1, f"{table}.{name} must be NOT NULL"
    assert columns["version"]["type"] == "INTEGER"
    assert str(columns["version"]["default"]) == "1"


def test_tasks_status_check_rejects_unknown_state(db: Database) -> None:
    conn = db.connection
    for index, status in enumerate(TASK_STATES):
        insert_task(conn, f"task-{index:04d}", status)
    stored = {row[0] for row in conn.execute("SELECT status FROM tasks")}
    assert stored == set(TASK_STATES)
    version = conn.execute("SELECT version FROM tasks WHERE id = 'task-0000'").fetchone()[0]
    assert version == 1

    for index, status in enumerate(("done", "QUEUED", "")):
        with pytest.raises(sqlite3.IntegrityError):
            insert_task(conn, f"bad-{index:04d}", status)


STAMP = "2026-01-03T09:00:00+00:00"
VALID_ROWS: dict[str, dict[str, object]] = {
    "settings": {"key": "theme", "value": '"light"', "created_at": STAMP, "updated_at": STAMP},
    "setup_state": {
        "step_id": "welcome",
        "status": "pending",
        "created_at": STAMP,
        "updated_at": STAMP,
    },
    "tasks": {
        "id": "task-0001",
        "type": "fictional_task",
        "status": "queued",
        "dedupe_key": "fictional_task:1",
        "created_at": STAMP,
        "updated_at": STAMP,
    },
    "usage_records": {
        "id": "usage-0001",
        "provider": "fake",
        "task_type": "fictional_task",
        "service": "fake",  # required on new rows since 0002
        "requested_at": STAMP,
        "created_at": STAMP,
        "updated_at": STAMP,
    },
}
WHOLE_NUMBER_COLUMNS = [
    ("settings", "version", 2),
    ("setup_state", "version", 2),
    ("tasks", "version", 2),
    ("tasks", "attempt", 2),
    ("tasks", "progress", 50),
    ("tasks", "cancel_requested", 1),
    ("usage_records", "version", 2),
    ("usage_records", "units", 3),
    ("usage_records", "success", 1),
]


def insert_row(conn: sqlite3.Connection, table: str, row: dict[str, object]) -> None:
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(row.values()))  # noqa: S608


@pytest.mark.parametrize(
    ("table", "column", "good"),
    WHOLE_NUMBER_COLUMNS,
    ids=[f"{table}.{column}" for table, column, _ in WHOLE_NUMBER_COLUMNS],
)
def test_whole_number_columns_reject_text_and_fractions(
    db: Database, table: str, column: str, good: int
) -> None:
    # Tables aren't STRICT (ADR-027), so 0001 checks typeof() itself: text such as 'abc'
    # would otherwise pass a numeric CHECK because SQLite sorts text above every number.
    conn = db.connection
    for bad in ("abc", 1.5):
        with pytest.raises(sqlite3.IntegrityError):
            insert_row(conn, table, {**VALID_ROWS[table], column: bad})

    insert_row(conn, table, {**VALID_ROWS[table], column: good})
    stored = conn.execute(f"SELECT {column}, typeof({column}) FROM {table}").fetchone()  # noqa: S608
    assert tuple(stored) == (good, "integer")


def test_setup_state_status_check(db: Database) -> None:
    conn = db.connection
    for status in SETUP_STATES:
        conn.execute(
            "INSERT INTO setup_state (step_id, status, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (f"step-{status}", status, STAMP, STAMP),
        )
    for status in ("complete", "DONE", ""):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO setup_state (step_id, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?)",
                (f"bad-{status or 'empty'}", status, STAMP, STAMP),
            )


def test_settings_value_must_be_json(db: Database) -> None:
    conn = db.connection
    for index, value in enumerate(('{"a": 1}', '"text"', "42")):
        conn.execute(
            "INSERT INTO settings (key, value, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (f"demo.key{index}", value, STAMP, STAMP),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO settings (key, value, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("demo.bad", "not json", STAMP, STAMP),
        )


def test_sqlite_build_supports_fts5_and_json() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE docs USING fts5(body)")
        conn.execute("INSERT INTO docs (body) VALUES ('fictional analyst role in Lisbon')")
        conn.execute("INSERT INTO docs (body) VALUES ('imaginary baker position')")
        hits = conn.execute("SELECT body FROM docs WHERE docs MATCH 'analyst'").fetchall()
        assert hits == [("fictional analyst role in Lisbon",)]
        value = conn.execute("""SELECT json_extract('{"a": {"b": 7}}', '$.a.b')""").fetchone()
        assert value == (7,)
    finally:
        conn.close()


def test_0001_schema_migrations_matches_the_runner_table() -> None:
    # The runner creates the table first, so 0001's IF NOT EXISTS copy never runs in the app;
    # compare the two declarations directly so they cannot drift apart.
    from app.modules.storage.runner import _SCHEMA_MIGRATIONS

    def columns(script: str) -> list[tuple[object, ...]]:
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.executescript(script)
            return [tuple(row) for row in conn.execute("PRAGMA table_info(schema_migrations)")]

    initial = (MIGRATIONS_DIR / "0001_initial.sql").read_text(encoding="utf-8")
    assert columns(initial) == columns(_SCHEMA_MIGRATIONS)


MIGRATION_HASHES = Path(__file__).parents[1] / "fixtures" / "migration_hashes.json"


def test_committed_migrations_are_never_edited() -> None:
    # Migrations are forward-only and never edited after commit (.claude/rules/database.md).
    # A new migration gets its SHA-256 added here once; changing an existing one fails.
    recorded: dict[str, str] = json.loads(MIGRATION_HASHES.read_text(encoding="utf-8"))
    actual = {
        migration.path.name: hashlib.sha256(migration.path.read_bytes()).hexdigest()
        for migration in load_migrations(MIGRATIONS_DIR)
    }

    missing = sorted(set(actual) - set(recorded))
    assert not missing, f"record new migrations in {MIGRATION_HASHES.name}: " + ", ".join(
        f'"{name}": "{actual[name]}"' for name in missing
    )
    assert sorted(set(recorded) - set(actual)) == [], "a recorded migration was deleted"
    changed = sorted(name for name in actual if actual[name] != recorded[name])
    assert changed == [], f"committed migrations were edited: {changed}; fix forward instead"
