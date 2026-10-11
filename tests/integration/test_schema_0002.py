"""Migration 0002 (M2.5; AC-M2.1, AC-M2.6; ADR-027, ADR-076 items 4, 5, 7; ADR-078).

0002 adds the unique partial index on active ``tasks.dedupe_key``, links ``usage_records`` to
``tasks`` (``task_id``, ON DELETE SET NULL), records the service and token counts on usage
records, and creates ``provider_usage_snapshots`` (latest row per service and window) and
``budget_notices`` (append-only, one row per service, cycle and threshold, the cycle as a local
``YYYY-MM-DD`` date). Services are non-blank free text in the schema; the domain validates the
set (ADR-076 item 4), and a trigger makes every new usage record name its service. Tables aren't
STRICT (ADR-027), so whole-number and real columns check ``typeof`` themselves, as in 0001.

Tests check behavior through SQL (inserts accepted or refused) and the schema through PRAGMAs,
never through index names. All values are fictional.
"""

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

import pytest

from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations, load_migrations
from tests.fakes.clock import FrozenClock

pytestmark = pytest.mark.integration

START = datetime(2026, 2, 7, 10, 30, 0, tzinfo=UTC)
STAMP = "2026-02-07T10:30:00+00:00"
LATER = "2026-02-07T11:00:00+00:00"
RESETS = "2026-03-01T00:00:00+00:00"
# Cycles start at local midnight (ADR-076 item 8), so a notice stores the local calendar date.
CYCLE_FEB = "2026-02-01"
CYCLE_MAR = "2026-03-01"

ACTIVE_STATES = ("queued", "running")
TERMINAL_STATES = ("succeeded", "failed", "cancelled", "interrupted")
SERVICES = ("claude_subscription", "claude_api_key", "web_search", "fake")
SQLITE_INTEGER_MAX = 2**63 - 1

MIGRATION_HASHES = Path(__file__).parents[1] / "fixtures" / "migration_hashes.json"


# --- helpers ----------------------------------------------------------------


def _migration_0002() -> Path:
    found = [m.path for m in load_migrations(MIGRATIONS_DIR) if m.version == 2]
    assert found, "migration 0002 is missing from the migrations folder"
    return found[0]


def _folder_up_to(tmp_path: Path, version: int) -> Path:
    folder = tmp_path / f"migrations-up-to-{version:04d}"
    folder.mkdir()
    for migration in load_migrations(MIGRATIONS_DIR):
        if migration.version <= version:
            shutil.copy(migration.path, folder / migration.path.name)
    return folder


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database.open(tmp_path / "data" / "app.db")
    try:
        apply_migrations(database, MIGRATIONS_DIR, tmp_path / "backups", FrozenClock(START))
        yield database
    finally:
        database.close()


def columns(conn: sqlite3.Connection, table: str) -> dict[str, dict[str, object]]:
    return {
        row[1]: {"type": row[2].upper(), "notnull": row[3], "default": row[4]}
        for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    }


class IndexInfo(NamedTuple):
    name: str
    unique: int
    partial: int
    columns: list[str]


def indexes(conn: sqlite3.Connection, table: str) -> list[IndexInfo]:
    """Each index on ``table`` (PRAGMA index_list) with its key columns."""
    found = []
    for row in conn.execute(f'PRAGMA index_list("{table}")').fetchall():
        name = row[1]
        keys = [info[2] for info in conn.execute(f'PRAGMA index_info("{name}")').fetchall()]
        found.append(IndexInfo(name=name, unique=row[2], partial=row[4], columns=keys))
    return found


def insert_task(
    conn: sqlite3.Connection, task_id: str, status: str, dedupe_key: str = "practice:demo-1"
) -> None:
    conn.execute(
        "INSERT INTO tasks (id, type, status, dedupe_key, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, "practice", status, dedupe_key, STAMP, STAMP),
    )


def insert_usage(conn: sqlite3.Connection, usage_id: str, **extra: object) -> None:
    row: dict[str, object] = {
        "id": usage_id,
        "provider": "fake",
        "task_type": "practice",
        "requested_at": STAMP,
        "created_at": STAMP,
        "updated_at": STAMP,
        **extra,
    }
    names = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO usage_records ({names}) VALUES ({marks})", tuple(row.values()))  # noqa: S608


SNAPSHOT_REQUESTS: dict[str, object] = {
    "id": "snap-0001",
    "service": "fake",
    "window": "month",
    "unit": "requests",
    "used": 12,
    "limit_count": 100,
    "utilization": None,
    "resets_at": RESETS,
    "observed_at": STAMP,
    "created_at": STAMP,
    "updated_at": STAMP,
}
SNAPSHOT_FRACTION: dict[str, object] = {
    "id": "snap-0002",
    "service": "claude_subscription",
    "window": "five_hour",
    "unit": "fraction",
    "used": None,
    "limit_count": None,
    "utilization": 0.25,
    "resets_at": RESETS,
    "observed_at": STAMP,
    "created_at": STAMP,
    "updated_at": STAMP,
}


def insert_snapshot(conn: sqlite3.Connection, row: dict[str, object]) -> None:
    names = ", ".join(f'"{name}"' for name in row)
    marks = ", ".join("?" for _ in row)
    conn.execute(
        f"INSERT INTO provider_usage_snapshots ({names}) VALUES ({marks})",  # noqa: S608
        tuple(row.values()),
    )


def insert_notice(
    conn: sqlite3.Connection,
    notice_id: str,
    service: str = "fake",
    cycle_start: str = CYCLE_FEB,
    threshold: str = "first",
) -> None:
    conn.execute(
        "INSERT INTO budget_notices (id, service, cycle_start, threshold, shown_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (notice_id, service, cycle_start, threshold, STAMP),
    )


# --- upgrade from 0001 ------------------------------------------------------


def test_0002_upgrade_keeps_0001_rows_and_backs_up(tmp_path: Path) -> None:
    _migration_0002()
    backups = tmp_path / "backups"
    with Database.open(tmp_path / "data" / "app.db") as database:
        apply_migrations(database, _folder_up_to(tmp_path, 1), backups, FrozenClock(START))
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO settings (key, value, created_at, updated_at)"
                " VALUES ('ui.theme', '\"light\"', ?, ?)",
                (STAMP, STAMP),
            )
            conn.execute(
                "INSERT INTO setup_state (step_id, status, completed_at, created_at, updated_at)"
                " VALUES ('welcome', 'done', ?, ?, ?)",
                (STAMP, STAMP, STAMP),
            )
            insert_task(conn, "task-0001", "succeeded", "practice:old")
            insert_task(conn, "task-0002", "queued", "practice:waiting")
            insert_usage(conn, "usage-0001", tier="fast", units=1, success=1)
        before = {
            table: sorted(database.connection.execute(f"SELECT * FROM {table}").fetchall())  # noqa: S608
            for table in ("settings", "setup_state", "tasks", "usage_records")
        }

        later = FrozenClock(datetime(2026, 2, 8, 8, 0, 0, tzinfo=UTC))
        result = apply_migrations(database, MIGRATIONS_DIR, backups, later)
        conn = database.connection

        assert 2 in result.applied
        assert result.backup is not None
        assert result.backup.name == "app-20260208T080000Z-before-0002.db"
        assert result.backup.is_file()

        for table in ("settings", "setup_state", "tasks"):
            after = sorted(conn.execute(f"SELECT * FROM {table}").fetchall())  # noqa: S608
            assert after == before[table], f"{table} rows changed during the upgrade"

        old_usage_columns = "id, provider, task_type, tier, units, requested_at, success,"
        old_usage_columns += " created_at, updated_at, version"
        usage = conn.execute(f"SELECT {old_usage_columns} FROM usage_records").fetchall()  # noqa: S608
        assert sorted(usage) == before["usage_records"]
        new_values = conn.execute(
            "SELECT task_id, service, input_tokens, output_tokens FROM usage_records"
        ).fetchone()
        assert tuple(new_values) == (None, None, None, None)
        # The service trigger guards inserts only: a 0001-era row can still be completed.
        conn.execute("UPDATE usage_records SET success = 0 WHERE id = 'usage-0001'")

    with sqlite3.connect(result.backup) as copy:
        assert [r[0] for r in copy.execute("SELECT version FROM schema_migrations")] == [1]
        assert copy.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2


TABLES_AT_0001 = {"schema_migrations", "settings", "setup_state", "tasks", "usage_records"}


def test_0002_adds_exactly_two_tables(tmp_path: Path) -> None:
    # Up to 0002 only, so later migrations adding tables don't break this check.
    with Database.open(tmp_path / "data" / "app.db") as database:
        apply_migrations(
            database, _folder_up_to(tmp_path, 2), tmp_path / "backups", FrozenClock(START)
        )
        rows = database.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    assert {row[0] for row in rows} == TABLES_AT_0001 | {
        "provider_usage_snapshots",
        "budget_notices",
    }


# --- dedupe index (AC-M2.1) -------------------------------------------------


def test_active_dedupe_index_is_unique_and_partial(db: Database) -> None:
    matches = [
        index for index in indexes(db.connection, "tasks") if index.columns == ["dedupe_key"]
    ]
    assert matches, "no index on tasks(dedupe_key)"
    assert any(index.unique == 1 and index.partial == 1 for index in matches)


@pytest.mark.parametrize(
    ("first", "second"),
    [("queued", "queued"), ("queued", "running"), ("running", "queued"), ("running", "running")],
)
def test_ac_m2_1_index_rejects_second_active_task_with_same_key(
    db: Database, first: str, second: str
) -> None:
    conn = db.connection
    insert_task(conn, "task-0001", first)
    with pytest.raises(sqlite3.IntegrityError):
        insert_task(conn, "task-0002", second)
    insert_task(conn, "task-0003", second, dedupe_key="practice:demo-2")


@pytest.mark.parametrize("finished", TERMINAL_STATES)
def test_index_allows_same_key_after_first_finished(db: Database, finished: str) -> None:
    conn = db.connection
    insert_task(conn, "task-0001", finished)
    insert_task(conn, "task-0002", finished)
    insert_task(conn, "task-0003", "queued")
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 3


@pytest.mark.parametrize("finished", TERMINAL_STATES)
def test_index_refuses_moving_finished_task_back_beside_an_active_one(
    db: Database, finished: str
) -> None:
    # Retry (ADR-076 item 11) moves a finished row back to queued; the index must refuse it
    # while another task with the same key is active.
    conn = db.connection
    insert_task(conn, "task-0001", finished)
    insert_task(conn, "task-0002", "running")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE tasks SET status = 'queued' WHERE id = 'task-0001'")


# --- usage_records additions (AC-M2.6, ADR-076 items 4, 5) ------------------


def test_usage_records_new_columns(db: Database) -> None:
    found = columns(db.connection, "usage_records")
    for name, kind in (
        ("task_id", "TEXT"),
        ("service", "TEXT"),
        ("input_tokens", "INTEGER"),
        ("output_tokens", "INTEGER"),
    ):
        assert name in found, f"usage_records lacks {name}"
        assert found[name]["type"] == kind
        assert found[name]["notnull"] == 0, f"usage_records.{name} must allow NULL"
    links = db.connection.execute("PRAGMA foreign_key_list(usage_records)").fetchall()
    task_links = [row for row in links if row[3] == "task_id"]
    assert len(task_links) == 1
    assert task_links[0][2] == "tasks"
    assert task_links[0][4] in (None, "id")
    assert task_links[0][6] == "SET NULL"


def test_usage_task_id_set_null_when_task_deleted(db: Database) -> None:
    conn = db.connection
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    insert_task(conn, "task-0001", "succeeded")
    insert_usage(conn, "usage-0001", task_id="task-0001", service="fake")
    insert_usage(conn, "usage-0002", service="fake")

    with pytest.raises(sqlite3.IntegrityError):
        insert_usage(conn, "usage-0003", task_id="task-9999", service="fake")

    conn.execute("DELETE FROM tasks WHERE id = 'task-0001'")
    rows = conn.execute("SELECT id, task_id FROM usage_records ORDER BY id").fetchall()
    assert [tuple(row) for row in rows] == [("usage-0001", None), ("usage-0002", None)]


@pytest.mark.parametrize("service", [*SERVICES, "openai_fictional"])
def test_usage_service_accepts_known_and_unknown_names(db: Database, service: str) -> None:
    # The schema only requires a name; the domain validates the set (ADR-076 item 4).
    insert_usage(db.connection, "usage-0001", service=service)
    stored = db.connection.execute("SELECT service FROM usage_records").fetchone()
    assert tuple(stored) == (service,)


@pytest.mark.parametrize("service", ["", "   ", " \t\n\r", None])
def test_usage_service_rejects_blank_or_null_on_insert(db: Database, service: str | None) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        insert_usage(db.connection, "usage-0001", service=service)
    assert db.connection.execute("SELECT count(*) FROM usage_records").fetchone() == (0,)


def test_usage_insert_without_service_column_refused(db: Database) -> None:
    # ADD COLUMN can't be NOT NULL, so a trigger makes every new row name its service.
    with pytest.raises(sqlite3.IntegrityError):
        insert_usage(db.connection, "usage-0001")
    assert db.connection.execute("SELECT count(*) FROM usage_records").fetchone() == (0,)


def test_usage_update_cannot_blank_the_service(db: Database) -> None:
    conn = db.connection
    insert_usage(conn, "usage-0001", service="fake")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE usage_records SET service = '  ' WHERE id = 'usage-0001'")


@pytest.mark.parametrize("column", ["input_tokens", "output_tokens"])
def test_usage_token_counts_are_null_or_non_negative_integers(db: Database, column: str) -> None:
    conn = db.connection
    for index, bad in enumerate((-1, 1.5, "abc")):
        with pytest.raises(sqlite3.IntegrityError):
            insert_usage(conn, f"bad-{index:04d}", service="fake", **{column: bad})
    insert_usage(conn, "usage-0001", service="fake", **{column: 0})
    insert_usage(conn, "usage-0002", service="fake", **{column: 1234})
    insert_usage(conn, "usage-0003", service="fake", **{column: None})
    stored = conn.execute(
        f"SELECT {column}, typeof({column}) FROM usage_records ORDER BY id"  # noqa: S608
    ).fetchall()
    assert [tuple(row) for row in stored] == [(0, "integer"), (1234, "integer"), (None, "null")]


# --- indexes for panel lists and budget counts ------------------------------


@pytest.mark.parametrize(
    ("table", "leading"),
    [
        ("usage_records", ["task_id"]),
        ("usage_records", ["service", "requested_at"]),
        ("tasks", ["status", "updated_at"]),
        ("tasks", ["created_at"]),
    ],
    ids=["usage.task_id", "usage.service_requested_at", "tasks.status_updated_at", "tasks.created"],
)
def test_0002_indexes_exist(db: Database, table: str, leading: list[str]) -> None:
    found = indexes(db.connection, table)
    assert any(
        index.columns[: len(leading)] == leading and index.partial == 0 for index in found
    ), f"no full index on {table}({', '.join(leading)}); found {found}"


# --- provider_usage_snapshots (ADR-078) -------------------------------------


def test_snapshot_table_columns(db: Database) -> None:
    # No raw payloads, session ids, uuids, status, model names or free text (ADR-078).
    found = columns(db.connection, "provider_usage_snapshots")
    assert set(found) == {
        "id",
        "service",
        "window",
        "unit",
        "used",
        "limit_count",
        "utilization",
        "resets_at",
        "observed_at",
        "created_at",
        "updated_at",
        "version",
    }
    for name in ("service", "window", "unit", "observed_at", "created_at", "updated_at"):
        assert found[name]["notnull"] == 1, f"provider_usage_snapshots.{name} must be NOT NULL"
    for name in ("used", "limit_count", "utilization", "resets_at"):
        assert found[name]["notnull"] == 0, f"provider_usage_snapshots.{name} must allow NULL"
    assert found["used"]["type"] == "INTEGER"
    assert found["limit_count"]["type"] == "INTEGER"
    assert found["utilization"]["type"] == "REAL"
    assert found["version"]["type"] == "INTEGER"
    assert found["version"]["notnull"] == 1
    assert str(found["version"]["default"]) == "1"


def test_snapshot_accepts_valid_rows_and_updates_in_place(db: Database) -> None:
    conn = db.connection
    insert_snapshot(conn, SNAPSHOT_REQUESTS)
    insert_snapshot(conn, SNAPSHOT_FRACTION)
    insert_snapshot(
        conn,
        {**SNAPSHOT_REQUESTS, "id": "snap-0003", "window": "day", "used": 0, "limit_count": None},
    )
    insert_snapshot(conn, {**SNAPSHOT_FRACTION, "id": "snap-0004", "window": "seven_day"})
    insert_snapshot(
        conn,
        {**SNAPSHOT_FRACTION, "id": "snap-0005", "window": "edge", "utilization": 0.0},
    )
    insert_snapshot(
        conn,
        {**SNAPSHOT_FRACTION, "id": "snap-0006", "window": "full", "utilization": 1.0},
    )
    insert_snapshot(
        conn,
        {
            **SNAPSHOT_REQUESTS,
            "id": "snap-0007",
            "window": "max",
            "used": SQLITE_INTEGER_MAX,
            "limit_count": SQLITE_INTEGER_MAX,
        },
    )
    conn.execute(
        "UPDATE provider_usage_snapshots SET used = 13, observed_at = ?, updated_at = ?,"
        " version = version + 1 WHERE id = 'snap-0001'",
        (LATER, LATER),
    )
    row = conn.execute(
        "SELECT used, typeof(used), version FROM provider_usage_snapshots WHERE id = 'snap-0001'"
    ).fetchone()
    assert tuple(row) == (13, "integer", 2)
    stored = conn.execute(
        "SELECT utilization, typeof(utilization) FROM provider_usage_snapshots"
        " WHERE id = 'snap-0002'"
    ).fetchone()
    assert tuple(stored) == (0.25, "real")


BAD_SNAPSHOTS: list[tuple[str, dict[str, object]]] = [
    ("unit-unknown", {**SNAPSHOT_REQUESTS, "unit": "tokens"}),
    ("unit-uppercase", {**SNAPSHOT_REQUESTS, "unit": "REQUESTS"}),
    ("unit-null", {**SNAPSHOT_REQUESTS, "unit": None}),
    ("used-negative", {**SNAPSHOT_REQUESTS, "used": -1}),
    ("used-fraction", {**SNAPSHOT_REQUESTS, "used": 1.5}),
    ("used-text", {**SNAPSHOT_REQUESTS, "used": "abc"}),
    ("limit-negative", {**SNAPSHOT_REQUESTS, "limit_count": -5}),
    ("limit-fraction", {**SNAPSHOT_REQUESTS, "limit_count": 2.5}),
    ("limit-text", {**SNAPSHOT_REQUESTS, "limit_count": "many"}),
    ("utilization-above-1", {**SNAPSHOT_FRACTION, "utilization": 1.0001}),
    ("utilization-below-0", {**SNAPSHOT_FRACTION, "utilization": -0.01}),
    ("utilization-infinite", {**SNAPSHOT_FRACTION, "utilization": float("inf")}),
    ("utilization-text", {**SNAPSHOT_FRACTION, "utilization": "half"}),
    # SQLite stores NaN as NULL, so a fraction snapshot then lacks its value.
    ("utilization-nan", {**SNAPSHOT_FRACTION, "utilization": float("nan")}),
    ("service-empty", {**SNAPSHOT_REQUESTS, "service": ""}),
    ("service-blank", {**SNAPSHOT_REQUESTS, "service": "   "}),
    ("service-null", {**SNAPSHOT_REQUESTS, "service": None}),
    ("window-empty", {**SNAPSHOT_REQUESTS, "window": ""}),
    ("window-blank", {**SNAPSHOT_REQUESTS, "window": " \t"}),
    ("window-null", {**SNAPSHOT_REQUESTS, "window": None}),
    ("requests-without-used", {**SNAPSHOT_REQUESTS, "used": None}),
    ("fraction-without-utilization", {**SNAPSHOT_FRACTION, "utilization": None}),
    ("observed-at-null", {**SNAPSHOT_REQUESTS, "observed_at": None}),
    ("version-zero", {**SNAPSHOT_REQUESTS, "version": 0}),
    ("version-text", {**SNAPSHOT_REQUESTS, "version": "abc"}),
]


@pytest.mark.parametrize(
    "row", [row for _, row in BAD_SNAPSHOTS], ids=[name for name, _ in BAD_SNAPSHOTS]
)
def test_snapshot_checks_reject_bad_values(db: Database, row: dict[str, object]) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        insert_snapshot(db.connection, row)
    assert db.connection.execute("SELECT count(*) FROM provider_usage_snapshots").fetchone() == (0,)


def test_snapshot_checks_reject_duplicate_service_and_window(db: Database) -> None:
    conn = db.connection
    insert_snapshot(conn, SNAPSHOT_REQUESTS)
    with pytest.raises(sqlite3.IntegrityError):
        insert_snapshot(conn, {**SNAPSHOT_REQUESTS, "id": "snap-0099", "used": 50})
    insert_snapshot(conn, {**SNAPSHOT_REQUESTS, "id": "snap-0100", "window": "five_hour"})
    insert_snapshot(conn, {**SNAPSHOT_REQUESTS, "id": "snap-0101", "service": "claude_api_key"})


@pytest.mark.parametrize("column", ["used", "limit_count"])
def test_snapshot_checks_reject_counts_beyond_sqlite_integer_range(
    db: Database, column: str
) -> None:
    # The bound is SQLite's INTEGER range (ADR-078, user decision). Python refuses to bind
    # 2**63; a literal one past the range becomes REAL in SQLite, which the typeof check
    # must refuse rather than store an inexact count.
    conn = db.connection
    with pytest.raises(OverflowError):
        insert_snapshot(conn, {**SNAPSHOT_REQUESTS, column: SQLITE_INTEGER_MAX + 1})
    other = "limit_count" if column == "used" else "used"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            f'INSERT INTO provider_usage_snapshots (id, service, "window", unit, {column},'  # noqa: S608
            f" {other}, observed_at, created_at, updated_at) VALUES ('snap-0200', 'fake',"
            " 'month', 'requests', 9223372036854775808, 1, ?, ?, ?)",
            (STAMP, STAMP, STAMP),
        )
    assert db.connection.execute("SELECT count(*) FROM provider_usage_snapshots").fetchone() == (0,)


# --- budget_notices (ADR-076 item 7) ----------------------------------------


def test_budget_notice_table_is_append_only_shape(db: Database) -> None:
    found = columns(db.connection, "budget_notices")
    assert set(found) == {"id", "service", "cycle_start", "threshold", "shown_at"}
    for name in ("service", "cycle_start", "threshold", "shown_at"):
        assert found[name]["notnull"] == 1, f"budget_notices.{name} must be NOT NULL"


def test_budget_notice_unique_per_threshold_and_cycle(db: Database) -> None:
    conn = db.connection
    insert_notice(conn, "notice-0001")
    with pytest.raises(sqlite3.IntegrityError):
        insert_notice(conn, "notice-0002")

    insert_notice(conn, "notice-0003", threshold="second")
    insert_notice(conn, "notice-0004", cycle_start=CYCLE_MAR)
    insert_notice(conn, "notice-0005", service="claude_api_key")
    with pytest.raises(sqlite3.IntegrityError):
        insert_notice(conn, "notice-0006", threshold="second")

    count = conn.execute("SELECT count(*) FROM budget_notices").fetchone()[0]
    assert count == 4


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("threshold", "third"),
        ("threshold", "blocked"),
        ("threshold", "FIRST"),
        ("threshold", ""),
        ("service", ""),
        ("service", "   "),
        ("service", " \t\n\r"),
        ("cycle_start", "2026-02-01T00:00:00+00:00"),
        ("cycle_start", "2026-2-1"),
        ("cycle_start", "2026/02/01"),
        ("cycle_start", "2026-02-01 "),
        ("cycle_start", ""),
    ],
)
def test_budget_notice_rejects_unknown_threshold_blank_service_or_bad_cycle(
    db: Database, field: str, bad: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        insert_notice(db.connection, "notice-0001", **{field: bad})
    assert db.connection.execute("SELECT count(*) FROM budget_notices").fetchone() == (0,)


@pytest.mark.parametrize("service", [*SERVICES, "openai_fictional"])
def test_budget_notice_accepts_known_and_unknown_services(db: Database, service: str) -> None:
    # The schema only requires a name; the domain validates the set (ADR-076 item 4).
    insert_notice(db.connection, "notice-0001", service=service, threshold="first")
    insert_notice(db.connection, "notice-0002", service=service, threshold="second")


def test_budget_notice_cycle_start_is_a_local_date(db: Database) -> None:
    conn = db.connection
    insert_notice(conn, "notice-0001", cycle_start="2026-02-01")
    insert_notice(conn, "notice-0002", cycle_start="2026-12-31")
    stored = conn.execute("SELECT cycle_start FROM budget_notices ORDER BY id").fetchall()
    assert [row[0] for row in stored] == ["2026-02-01", "2026-12-31"]


# --- migration hashes -------------------------------------------------------


def test_migration_hashes_include_0002() -> None:
    path = _migration_0002()
    recorded: dict[str, str] = json.loads(MIGRATION_HASHES.read_text(encoding="utf-8"))
    assert path.name in recorded, f"{MIGRATION_HASHES.name} lacks {path.name}"
    assert recorded[path.name] == hashlib.sha256(path.read_bytes()).hexdigest()
