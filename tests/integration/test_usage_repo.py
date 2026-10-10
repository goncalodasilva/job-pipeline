"""The usage repository (M2.15; AC-M2.6, AC-M2.7, AC-M2.8; ADR-076 items 4, 5, 7; ADR-078).

``app.modules.storage.usage_repo`` holds plain functions on a ``sqlite3.Connection`` that run
inside the caller's transaction, like ``settings_repo`` and ``tasks_repo``. Times are UTC ISO
8601 text from the Clock; cycle bounds are passed as UTC text, ``[start, end)``.

- ``insert_record(conn, *, record_id, service, tier, task_type, task_id, now)``
- ``complete_record(conn, record_id, *, success, input_tokens, output_tokens, now) -> bool``:
  only an unfinished record changes (True); bumps ``version`` and ``updated_at``.
- ``count_requests(conn, service, *, start, end, tier=None) -> int``: finished or not.
- ``count_unfinished(conn, service, *, start, end) -> int``
- ``breakdown(conn, service, *, start, end) -> list[row]``: rows with ``tier``, ``task_type``,
  ``requests``, ``unfinished``, ``input_tokens``, ``output_tokens``, ordered by tier, task type.
- ``insert_notice(conn, *, notice_id, service, cycle_start: date, threshold, now) -> bool``:
  False when that (service, cycle, threshold) is already stored.
- ``shown_thresholds(conn, service, cycle_start: date) -> frozenset[str]``
- ``upsert_snapshot(conn, *, snapshot_id, service, window, unit, used, limit_count,
  utilization, resets_at, observed_at, now) -> bool``: one row per (service, window), updated
  in place (ADR-078 d).
- ``list_snapshots(conn, service) -> list[row]`` with the same fields and ``version``.

Real migrated SQLite in a temp folder, ``FrozenClock`` and ``SequentialIdFactory``. The new
module is imported inside the tests so each fails on its own while it is missing. All values
are fictional.
"""

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory

pytestmark = pytest.mark.integration

START = datetime(2026, 2, 20, 15, 0, 0, tzinfo=UTC)
CYCLE_START_UTC = datetime(2026, 2, 15, 5, 0, 0, tzinfo=UTC).isoformat()
CYCLE_END_UTC = datetime(2026, 3, 15, 4, 0, 0, tzinfo=UTC).isoformat()
SERVICE = "claude_api_key"
OTHER_SERVICE = "fake"
TASK_TYPE = "practice"


def _repo() -> ModuleType:
    from app.modules.storage import usage_repo

    return usage_repo


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(START)


@pytest.fixture
def ids() -> SequentialIdFactory:
    return SequentialIdFactory("usage")


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database.open(tmp_path / "data" / "app.db")
    apply_migrations(database, MIGRATIONS_DIR, tmp_path / "backups", FrozenClock(START))
    try:
        yield database
    finally:
        database.close()


def _insert(
    db: Database,
    ids: SequentialIdFactory,
    at: datetime = START,
    *,
    service: str = SERVICE,
    tier: str = "fast",
    task_type: str = TASK_TYPE,
) -> str:
    record_id = ids.new()
    with db.transaction() as conn:
        _repo().insert_record(
            conn,
            record_id=record_id,
            service=service,
            tier=tier,
            task_type=task_type,
            task_id=None,
            now=at.isoformat(),
        )
    return record_id


def _complete(db: Database, record_id: str, *, success: bool, tokens: Any = (None, None)) -> bool:
    with db.transaction() as conn:
        changed: bool = _repo().complete_record(
            conn,
            record_id,
            success=success,
            input_tokens=tokens[0],
            output_tokens=tokens[1],
            now=(START + timedelta(seconds=30)).isoformat(),
        )
    return changed


def _count(db: Database, service: str = SERVICE, **kwargs: Any) -> int:
    with db.transaction() as conn:
        count: int = _repo().count_requests(
            conn, service, start=CYCLE_START_UTC, end=CYCLE_END_UTC, **kwargs
        )
    return count


def _raw(db: Database, record_id: str) -> dict[str, object]:
    cursor = db.connection.execute("SELECT * FROM usage_records WHERE id = ?", (record_id,))
    row = cursor.fetchone()
    assert row is not None
    names = [column[0] for column in cursor.description]
    return dict(zip(names, row, strict=True))


def _snapshot_args(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "snapshot_id": "snap-1",
        "service": SERVICE,
        "window": "month",
        "unit": "requests",
        "used": 3,
        "limit_count": None,
        "utilization": None,
        "resets_at": None,
        "observed_at": START.isoformat(),
        "now": START.isoformat(),
    }
    values.update(overrides)
    return values


# --- records ------------------------------------------------------------------------


def test_insert_record_is_unfinished_and_counted(db: Database, ids: SequentialIdFactory) -> None:
    record_id = _insert(db, ids)

    row = _raw(db, record_id)
    assert row["service"] == SERVICE
    assert row["tier"] == "fast"
    assert row["task_type"] == TASK_TYPE
    assert row["success"] is None
    assert row["requested_at"] == START.isoformat()
    assert row["created_at"] == START.isoformat()
    assert row["version"] == 1
    assert _count(db) == 1
    with db.transaction() as conn:
        assert (
            _repo().count_unfinished(conn, SERVICE, start=CYCLE_START_UTC, end=CYCLE_END_UTC) == 1
        )


def test_complete_record_only_changes_unfinished_record(
    db: Database, ids: SequentialIdFactory
) -> None:
    record_id = _insert(db, ids)

    assert _complete(db, record_id, success=True, tokens=(120, 45)) is True
    assert _complete(db, record_id, success=False, tokens=(1, 1)) is False

    row = _raw(db, record_id)
    assert (row["success"], row["input_tokens"], row["output_tokens"]) == (1, 120, 45)
    assert row["updated_at"] == (START + timedelta(seconds=30)).isoformat()
    assert row["version"] == 2
    assert _count(db) == 1


def test_complete_unknown_record_changes_nothing(db: Database) -> None:
    assert _complete(db, "usage-9999", success=True) is False


def test_count_bounds_are_half_open(db: Database, ids: SequentialIdFactory) -> None:
    _insert(db, ids, datetime.fromisoformat(CYCLE_START_UTC))  # counted: start is inclusive
    _insert(db, ids, datetime.fromisoformat(CYCLE_START_UTC) - timedelta(microseconds=1))
    _insert(db, ids, datetime.fromisoformat(CYCLE_END_UTC) - timedelta(microseconds=1))
    _insert(db, ids, datetime.fromisoformat(CYCLE_END_UTC))  # next cycle: end is exclusive

    assert _count(db) == 2


def test_count_filters_by_service_and_tier(db: Database, ids: SequentialIdFactory) -> None:
    _insert(db, ids, tier="fast")
    _insert(db, ids, tier="thorough")
    _insert(db, ids, tier="thorough")
    _insert(db, ids, service=OTHER_SERVICE, tier="thorough")

    assert _count(db) == 3
    assert _count(db, tier="thorough") == 2
    assert _count(db, tier="fast") == 1
    assert _count(db, OTHER_SERVICE) == 1


def test_finished_and_unfinished_records_all_count(db: Database, ids: SequentialIdFactory) -> None:
    ok, failed, _open = (_insert(db, ids) for _ in range(3))
    _complete(db, ok, success=True)
    _complete(db, failed, success=False)

    assert _count(db) == 3
    with db.transaction() as conn:
        assert (
            _repo().count_unfinished(conn, SERVICE, start=CYCLE_START_UTC, end=CYCLE_END_UTC) == 1
        )


def test_insert_runs_in_callers_transaction(db: Database, ids: SequentialIdFactory) -> None:
    with pytest.raises(RuntimeError), db.transaction() as conn:
        _repo().insert_record(
            conn,
            record_id=ids.new(),
            service=SERVICE,
            tier="fast",
            task_type=TASK_TYPE,
            task_id=None,
            now=START.isoformat(),
        )
        raise RuntimeError("roll back")

    assert db.connection.execute("SELECT count(*) FROM usage_records").fetchone()[0] == 0


def test_breakdown_groups_by_tier_and_task_type(db: Database, ids: SequentialIdFactory) -> None:
    for tokens in ((100, 10), (50, 5)):
        _complete(db, _insert(db, ids), success=True, tokens=tokens)
    _insert(db, ids)  # unfinished: counted, no tokens yet
    _complete(db, _insert(db, ids, tier="thorough", task_type="cv_import"), success=False)
    _insert(db, ids, service=OTHER_SERVICE)
    _insert(db, ids, datetime.fromisoformat(CYCLE_END_UTC))

    with db.transaction() as conn:
        rows = _repo().breakdown(conn, SERVICE, start=CYCLE_START_UTC, end=CYCLE_END_UTC)

    assert [
        (r.tier, r.task_type, r.requests, r.unfinished, r.input_tokens, r.output_tokens)
        for r in rows
    ] == [
        ("fast", TASK_TYPE, 3, 1, 150, 15),
        ("thorough", "cv_import", 1, 0, 0, 0),
    ]


# --- notices ------------------------------------------------------------------------


def test_insert_notice_once_per_service_cycle_and_threshold(db: Database) -> None:
    cycle = date(2026, 2, 15)

    def insert(notice_id: str, service: str, cycle_start: date, threshold: str) -> bool:
        with db.transaction() as conn:
            inserted: bool = _repo().insert_notice(
                conn,
                notice_id=notice_id,
                service=service,
                cycle_start=cycle_start,
                threshold=threshold,
                now=START.isoformat(),
            )
        return inserted

    assert insert("n-1", SERVICE, cycle, "first") is True
    assert insert("n-2", SERVICE, cycle, "first") is False
    assert insert("n-3", SERVICE, cycle, "second") is True
    assert insert("n-4", SERVICE, date(2026, 3, 15), "first") is True
    assert insert("n-5", OTHER_SERVICE, cycle, "first") is True

    with db.transaction() as conn:
        assert _repo().shown_thresholds(conn, SERVICE, cycle) == frozenset({"first", "second"})
        assert _repo().shown_thresholds(conn, SERVICE, date(2026, 3, 15)) == frozenset({"first"})
        assert _repo().shown_thresholds(conn, SERVICE, date(2026, 1, 15)) == frozenset()
    stored = db.connection.execute(
        "SELECT cycle_start FROM budget_notices WHERE id = 'n-1'"
    ).fetchone()
    assert stored == ("2026-02-15",)


# --- snapshots ----------------------------------------------------------------------


def test_upsert_snapshot_updates_row_in_place(db: Database) -> None:
    with db.transaction() as conn:
        assert _repo().upsert_snapshot(conn, **_snapshot_args()) is True
    later = (START + timedelta(minutes=5)).isoformat()
    with db.transaction() as conn:
        assert (
            _repo().upsert_snapshot(
                conn,
                **_snapshot_args(
                    snapshot_id="snap-2", used=5, limit_count=100, observed_at=later, now=later
                ),
            )
            is True
        )

    rows = db.connection.execute(
        'SELECT id, service, "window", used, limit_count, observed_at, created_at, updated_at, '
        "version FROM provider_usage_snapshots"
    ).fetchall()
    assert rows == [
        ("snap-1", SERVICE, "month", 5, 100, later, START.isoformat(), later, 2),
    ]


def test_snapshots_kept_per_window_and_listed_per_service(db: Database) -> None:
    resets = (START + timedelta(hours=3)).isoformat()
    with db.transaction() as conn:
        _repo().upsert_snapshot(conn, **_snapshot_args())
        _repo().upsert_snapshot(
            conn,
            **_snapshot_args(
                snapshot_id="snap-2",
                window="five_hour",
                unit="fraction",
                used=None,
                utilization=0.4,
                resets_at=resets,
            ),
        )
        _repo().upsert_snapshot(conn, **_snapshot_args(snapshot_id="snap-3", service="fake"))

    with db.transaction() as conn:
        listed = _repo().list_snapshots(conn, SERVICE)

    assert sorted((s.window, s.unit, s.used, s.utilization, s.resets_at) for s in listed) == [
        ("five_hour", "fraction", None, 0.4, resets),
        ("month", "requests", 3, None, None),
    ]
