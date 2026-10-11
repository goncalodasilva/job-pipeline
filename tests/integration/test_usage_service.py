"""The usage service (M2.15; AC-M2.6, AC-M2.7, AC-M2.8, AC-M2.10; spec 3.7; ADR-076 items 3-8,
ADR-078).

``app.services.usage.UsageService(db, clock, ids, tz)`` keeps per-service request budgets and
the usage records they count:

- ``load_budgets() -> Versioned[Mapping[str, Budget]]`` and
  ``save_budget(service, budget | None, *, expected_version)``: one typed setting for every
  service, ``VersionConflict`` on a stale version, ``None`` removes the budget.
- ``reserve(service, *, tier, task_id=None, task_type=None, skip_budget=False) -> str``: the
  block check (``evaluate`` on the effective count, tier caps included) and the insert run in
  one ``BEGIN IMMEDIATE`` transaction; blocked raises ``BudgetBlocked`` (display name, reset
  date) and writes nothing. ``skip_budget`` is the connection test: counted, never blocked.
- ``complete(record_id, *, success, tokens)``: the first completion wins.
- ``status(service) -> UsageStatus``: ``budget``, ``cycle``, ``local_count``,
  ``provider_count``, ``effective`` (``EffectiveCount``), ``unfinished_count``,
  ``provider_snapshots``.
- ``preflight(service, *, tier) -> Decision``: no writes.
- ``acknowledge(service, threshold, cycle_start)``: only the threshold the current decision
  offers, in the current cycle; idempotent; anything else raises ``ValueError``.
- ``save_snapshot(snapshot) -> bool``: latest per (service, window); counts outside SQLite's
  INTEGER range, unknown services and older observations are refused (False), never clamped.
- ``breakdown(service, *, cycle=None) -> tuple[row, ...]``: by tier and task type.

Services: ``claude_subscription``, ``claude_api_key``, ``web_search``, ``fake`` (ADR-076 item 4).
Real migrated SQLite in a temp folder, ``FrozenClock``, ``SequentialIdFactory`` and a fixed
``ZoneInfo`` whose offset is not zero, so a wall-time comparison would put records in the wrong
cycle. No sleeps (the race lines threads up on a ``threading.Barrier``). The new modules are
imported inside the tests so each fails on its own while they are missing. All data is fictional.
"""

import re
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.domain.budgets import Allow, Blocked, Budget, CapBlocked, Note, Warn, cycle_bounds
from app.domain.errors import (
    BudgetBlocked,
    BudgetNoticeRefused,
    TierCapReached,
    UsageLimitsUnreadable,
    VersionConflict,
)
from app.modules.ai import ProviderUsageSnapshot, TokenUsage
from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory

pytestmark = pytest.mark.integration

TZ = ZoneInfo("America/New_York")
# 10:00 local on 20 February 2026 (UTC-5): inside the cycle 15 February - 15 March.
START = datetime(2026, 2, 20, 15, 0, 0, tzinfo=UTC)
CYCLE_START = date(2026, 2, 15)
RESET = date(2026, 3, 15)
# Local midnight on 15 March 2026 is 04:00 UTC (daylight time began on 8 March).
NEXT_CYCLE_AT = datetime(2026, 3, 15, 4, 0, 0, tzinfo=UTC)
NEXT_RESET = date(2026, 4, 15)
SERVICE = "claude_api_key"
SERVICE_NAME = "Claude API key"
OTHER_SERVICE = "fake"
TASK_TYPE = "practice"
SQLITE_INT_MAX = 2**63 - 1
BARRIER_TIMEOUT_S = 10
TABLES = ("usage_records", "budget_notices", "provider_usage_snapshots", "settings")


# --- fixtures and helpers --------------------------------------------------------


class LockedIds:
    """A thread-safe ``SequentialIdFactory`` for the race test."""

    def __init__(self, prefix: str) -> None:
        self._ids = SequentialIdFactory(prefix)
        self._lock = threading.Lock()

    def new(self) -> str:
        with self._lock:
            return self._ids.new()


def _open_migrated(path: Path, backups: Path) -> Database:
    database = Database.open(path)
    apply_migrations(database, MIGRATIONS_DIR, backups, FrozenClock(START))
    return database


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(START)


@pytest.fixture
def ids() -> SequentialIdFactory:
    return SequentialIdFactory("usage")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "app.db"


@pytest.fixture
def db(tmp_path: Path, db_path: Path) -> Iterator[Database]:
    database = _open_migrated(db_path, tmp_path / "backups")
    try:
        yield database
    finally:
        database.close()


def _service(db: Database, clock: Any, ids: Any) -> Any:
    from app.services.usage import UsageService

    return UsageService(db, clock, ids, TZ)


@pytest.fixture
def usage(db: Database, clock: FrozenClock, ids: SequentialIdFactory) -> Any:
    return _service(db, clock, ids)


def _budget(**overrides: Any) -> Budget:
    values: dict[str, Any] = {
        "allowance": 10,
        "first_warning": 5,
        "second_warning": 8,
        "block_at": 10,
        "reset_day": 15,
    }
    values.update(overrides)
    return Budget(**values)


def _set_budget(usage: Any, budget: Budget | None, service: str = SERVICE) -> None:
    version = usage.load_budgets().version
    usage.save_budget(service, budget, expected_version=version)


def _reserve_n(
    usage: Any,
    count: int,
    *,
    tier: str = "fast",
    service: str = SERVICE,
    task_type: str = TASK_TYPE,
) -> list[str]:
    return [usage.reserve(service, tier=tier, task_type=task_type) for _ in range(count)]


def _rows(db: Database, table: str) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = db.connection.execute(
        f"SELECT * FROM {table} ORDER BY rowid"  # noqa: S608 - fixed table names
    ).fetchall()
    return rows


def _all_rows(db: Database) -> dict[str, list[tuple[object, ...]]]:
    return {table: _rows(db, table) for table in TABLES}


def _record(db: Database, record_id: str) -> dict[str, object]:
    cursor = db.connection.execute("SELECT * FROM usage_records WHERE id = ?", (record_id,))
    row = cursor.fetchone()
    assert row is not None
    names = [column[0] for column in cursor.description]
    return dict(zip(names, row, strict=True))


def _usage_count(db: Database) -> int:
    count: int = db.connection.execute("SELECT count(*) FROM usage_records").fetchone()[0]
    return count


def _notices(db: Database) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = db.connection.execute(
        "SELECT service, cycle_start, threshold FROM budget_notices ORDER BY rowid"
    ).fetchall()
    return rows


def _requests_snapshot(
    used: int,
    *,
    observed_at: datetime = START,
    service: str = SERVICE,
    window: str = "month",
    limit: int | None = None,
) -> ProviderUsageSnapshot:
    return ProviderUsageSnapshot(
        service=service,
        window=window,
        unit="requests",
        used=used,
        limit=limit,
        utilization=None,
        resets_at=None,
        observed_at=observed_at,
    )


def _fraction_snapshot(utilization: float, *, service: str = SERVICE) -> ProviderUsageSnapshot:
    return ProviderUsageSnapshot(
        service=service,
        window="five_hour",
        unit="fraction",
        used=None,
        limit=None,
        utilization=utilization,
        resets_at=START + timedelta(hours=3),
        observed_at=START,
    )


def _task(db: Database, clock: FrozenClock, target: str = "run-1") -> str:
    from app.services.tasks import TaskService

    ref = TaskService(db, clock, SequentialIdFactory("task"), lambda _change: None).enqueue(
        TASK_TYPE, target
    )
    task_id: str = ref.id
    return task_id


# --- budget settings ------------------------------------------------------------


def test_no_budget_by_default(usage: Any) -> None:
    loaded = usage.load_budgets()

    assert dict(loaded.value) == {}
    assert loaded.version == 0


def test_budget_saved_per_service(usage: Any) -> None:
    _set_budget(usage, _budget())

    loaded = usage.load_budgets()
    assert dict(loaded.value) == {SERVICE: _budget()}
    assert usage.status(SERVICE).budget == _budget()
    assert usage.status(OTHER_SERVICE).budget is None


def test_budget_save_stale_version_conflict(usage: Any) -> None:
    stale = usage.load_budgets().version
    usage.save_budget(SERVICE, _budget(), expected_version=stale)

    with pytest.raises(VersionConflict):
        usage.save_budget(OTHER_SERVICE, _budget(reset_day=1), expected_version=stale)

    assert dict(usage.load_budgets().value) == {SERVICE: _budget()}


def test_remove_budget_returns_to_no_limit(usage: Any) -> None:
    _set_budget(usage, _budget())
    _reserve_n(usage, 10)
    assert usage.preflight(SERVICE, tier="fast") == Blocked(RESET)

    _set_budget(usage, None)

    assert dict(usage.load_budgets().value) == {}
    assert usage.preflight(SERVICE, tier="fast") == Allow()
    usage.reserve(SERVICE, tier="fast", task_type=TASK_TYPE)


def test_budget_for_unknown_service_refused(usage: Any) -> None:
    with pytest.raises(ValueError):
        usage.save_budget("not_a_service", _budget(), expected_version=0)
    assert usage.load_budgets().version == 0


# --- reserve and complete (AC-M2.6) ---------------------------------------------


def test_reserve_inserts_unfinished_record_with_service_tier_and_task(
    db: Database, usage: Any, clock: FrozenClock
) -> None:
    task_id = _task(db, clock)

    record_id = usage.reserve(SERVICE, tier="thorough", task_id=task_id, task_type=TASK_TYPE)

    row = _record(db, record_id)
    assert record_id.startswith("usage-")
    assert row["service"] == SERVICE
    assert row["tier"] == "thorough"
    assert row["task_id"] == task_id
    assert row["task_type"] == TASK_TYPE
    assert row["success"] is None
    assert row["input_tokens"] is None
    assert row["output_tokens"] is None
    assert row["requested_at"] == START.isoformat()
    assert row["version"] == 1


def test_complete_records_outcome_and_tokens(db: Database, usage: Any, clock: FrozenClock) -> None:
    ok_id, failed_id = _reserve_n(usage, 2)
    clock.advance(timedelta(seconds=30))

    usage.complete(ok_id, success=True, tokens=TokenUsage(input_tokens=120, output_tokens=45))
    usage.complete(failed_id, success=False, tokens=None)

    ok = _record(db, ok_id)
    assert (ok["success"], ok["input_tokens"], ok["output_tokens"]) == (1, 120, 45)
    assert ok["updated_at"] == clock.now().isoformat()
    assert ok["version"] == 2
    failed = _record(db, failed_id)
    assert (failed["success"], failed["input_tokens"], failed["output_tokens"]) == (0, None, None)


def test_complete_twice_keeps_first_outcome(db: Database, usage: Any) -> None:
    (record_id,) = _reserve_n(usage, 1)
    usage.complete(record_id, success=True, tokens=TokenUsage(input_tokens=10, output_tokens=2))

    usage.complete(record_id, success=False, tokens=TokenUsage(input_tokens=99, output_tokens=99))

    row = _record(db, record_id)
    assert (row["success"], row["input_tokens"], row["output_tokens"]) == (1, 10, 2)
    assert row["version"] == 2


def test_complete_with_out_of_range_tokens_completes_without_tokens(
    db: Database, usage: Any
) -> None:
    (record_id,) = _reserve_n(usage, 1)

    usage.complete(record_id, success=True, tokens=TokenUsage(input_tokens=2**63, output_tokens=-1))

    row = _record(db, record_id)
    assert (row["success"], row["input_tokens"], row["output_tokens"]) == (1, None, None)


def test_ac_m2_6_unfinished_record_counts_toward_budget(
    tmp_path: Path, db_path: Path, clock: FrozenClock
) -> None:
    first = _open_migrated(db_path, tmp_path / "backups")
    try:
        usage = _service(first, clock, SequentialIdFactory("usage"))
        _set_budget(usage, _budget())
        done = _reserve_n(usage, 3)
        for record_id in done:
            usage.complete(record_id, success=True, tokens=None)
        _reserve_n(usage, 7)  # in flight when the process dies: never completed
    finally:
        first.close()

    reopened = Database.open(db_path)
    try:
        after = _service(reopened, clock, SequentialIdFactory("after"))
        status = after.status(SERVICE)
        assert status.local_count == 10
        assert status.unfinished_count == 7
        assert after.preflight(SERVICE, tier="fast") == Blocked(RESET)
        with pytest.raises(BudgetBlocked):
            after.reserve(SERVICE, tier="fast", task_type=TASK_TYPE)
    finally:
        reopened.close()


def test_failed_record_counts_toward_budget(usage: Any) -> None:
    _set_budget(usage, _budget())
    for record_id in _reserve_n(usage, 4):
        usage.complete(record_id, success=False, tokens=None)

    assert usage.status(SERVICE).local_count == 4
    assert usage.preflight(SERVICE, tier="fast") == Warn("first", 5, 10, RESET)


# --- the block (AC-M2.7) ----------------------------------------------------------


def test_reserve_blocked_raises_budget_blocked_and_writes_nothing(db: Database, usage: Any) -> None:
    _set_budget(usage, _budget())
    _reserve_n(usage, 10)
    before = _all_rows(db)

    with pytest.raises(BudgetBlocked) as caught:
        usage.reserve(SERVICE, tier="fast", task_type=TASK_TYPE)

    assert caught.value.service == SERVICE_NAME
    assert caught.value.resets_on == RESET
    assert caught.value.user_message == (
        "Monthly limit reached for Claude API key. Resets on 15 March 2026."
    )
    assert _all_rows(db) == before


def test_ac_m2_7_blocked_status_has_reset_date(usage: Any) -> None:
    _set_budget(usage, _budget())
    _reserve_n(usage, 9)
    assert usage.preflight(SERVICE, tier="fast") == Warn("second", 10, 10, RESET)

    _reserve_n(usage, 1)

    assert usage.preflight(SERVICE, tier="fast") == Blocked(RESET)
    status = usage.status(SERVICE)
    assert status.cycle.start_date == CYCLE_START
    assert status.cycle.reset_date == RESET
    assert status.effective.value == 10
    with pytest.raises(BudgetBlocked) as caught:
        usage.reserve(SERVICE, tier="thorough", task_type=TASK_TYPE)
    assert caught.value.resets_on == RESET


def test_block_lifts_in_next_cycle(usage: Any, clock: FrozenClock) -> None:
    _set_budget(usage, _budget())
    _reserve_n(usage, 10)

    clock.set(NEXT_CYCLE_AT)

    assert usage.preflight(SERVICE, tier="fast") == Allow()
    usage.reserve(SERVICE, tier="fast", task_type=TASK_TYPE)
    assert usage.status(SERVICE).local_count == 1


def test_block_is_per_service(usage: Any) -> None:
    _set_budget(usage, _budget())
    _reserve_n(usage, 10)

    usage.reserve(OTHER_SERVICE, tier="fast", task_type=TASK_TYPE)

    assert usage.status(OTHER_SERVICE).local_count == 1
    assert usage.status(SERVICE).local_count == 10


@pytest.mark.parametrize("connections", ["shared", "separate"])
def test_reserve_block_check_is_atomic_across_threads(
    tmp_path: Path, db_path: Path, clock: FrozenClock, connections: str
) -> None:
    block_at = 5
    workers = 8
    first = _open_migrated(db_path, tmp_path / "backups")
    opened: list[Database] = []
    try:
        setup = _service(first, clock, LockedIds("setup"))
        _set_budget(
            setup,
            _budget(allowance=5, first_warning=2, second_warning=3, block_at=block_at),
        )
        _reserve_n(setup, block_at - 2)
        shared = _service(first, clock, LockedIds("shared"))

        def service_for(index: int) -> Any:
            if connections == "shared":
                return shared
            database = Database.open(db_path)
            opened.append(database)
            return _service(database, clock, LockedIds(f"worker{index}"))

        services = [service_for(index) for index in range(workers)]
        barrier = threading.Barrier(workers, timeout=BARRIER_TIMEOUT_S)
        outcomes: list[str] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def run(service: Any) -> Callable[[], None]:
            def reserve() -> None:
                try:
                    barrier.wait()
                    try:
                        service.reserve(SERVICE, tier="fast", task_type=TASK_TYPE)
                        outcome = "reserved"
                    except BudgetBlocked:
                        outcome = "blocked"
                    with lock:
                        outcomes.append(outcome)
                except BaseException as exc:  # reported below, never swallowed
                    with lock:
                        errors.append(exc)

            return reserve

        threads = [threading.Thread(target=run(service)) for service in services]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=BARRIER_TIMEOUT_S * 2)
            assert not thread.is_alive()

        assert errors == []
        assert sorted(outcomes) == ["blocked"] * (workers - 2) + ["reserved"] * 2
        assert _usage_count(first) == block_at
    finally:
        for database in opened:
            database.close()
        first.close()


def test_reserve_on_capped_tier_raises_budget_blocked_and_writes_nothing(
    db: Database, usage: Any
) -> None:
    _set_budget(usage, _budget(thorough_cap=3))
    _reserve_n(usage, 3, tier="thorough")
    before = _all_rows(db)

    with pytest.raises(BudgetBlocked):
        usage.reserve(SERVICE, tier="thorough", task_type=TASK_TYPE)

    assert _all_rows(db) == before
    usage.reserve(SERVICE, tier="fast", task_type=TASK_TYPE)


def test_thorough_cap_preflight_offers_fast_after_checking_fast(usage: Any) -> None:
    _set_budget(usage, _budget(thorough_cap=3))
    _reserve_n(usage, 3, tier="thorough")

    assert usage.preflight(SERVICE, tier="thorough") == CapBlocked(
        tier="thorough", offer_fast=True, reset_date=RESET
    )
    assert usage.preflight(SERVICE, tier="fast") == Allow()


def test_connection_test_reservation_skips_budget_but_counts(db: Database, usage: Any) -> None:
    _set_budget(usage, _budget())
    _reserve_n(usage, 10)

    record_id = usage.reserve(SERVICE, tier="fast", skip_budget=True)

    assert _record(db, record_id)["service"] == SERVICE
    assert usage.status(SERVICE).local_count == 11
    assert usage.preflight(SERVICE, tier="fast") == Blocked(RESET)


def test_connection_test_reservation_never_warns(db: Database, usage: Any) -> None:
    _set_budget(usage, _budget())
    _reserve_n(usage, 4)

    usage.reserve(SERVICE, tier="fast", skip_budget=True)

    assert _notices(db) == []
    assert usage.status(SERVICE).local_count == 5


# --- warnings once per threshold per cycle (AC-M2.7) ------------------------------


def test_ac_m2_7_dialog_once_per_threshold_per_cycle(db: Database, usage: Any) -> None:
    _set_budget(usage, _budget())
    _reserve_n(usage, 3)
    assert usage.preflight(SERVICE, tier="fast") == Allow()

    _reserve_n(usage, 1)
    assert usage.preflight(SERVICE, tier="fast") == Warn("first", 5, 10, RESET)
    usage.acknowledge(SERVICE, "first", CYCLE_START)
    assert usage.preflight(SERVICE, tier="fast") == Note(5, 10, RESET)

    _reserve_n(usage, 3)
    assert usage.preflight(SERVICE, tier="fast") == Warn("second", 8, 10, RESET)
    usage.acknowledge(SERVICE, "second", CYCLE_START)
    assert usage.preflight(SERVICE, tier="fast") == Note(8, 10, RESET)

    _reserve_n(usage, 1)
    assert usage.preflight(SERVICE, tier="fast") == Note(9, 10, RESET)
    assert _notices(db) == [
        (SERVICE, "2026-02-15", "first"),
        (SERVICE, "2026-02-15", "second"),
    ]


def test_ac_m2_7_new_cycle_warns_again(db: Database, usage: Any, clock: FrozenClock) -> None:
    _set_budget(usage, _budget())
    _reserve_n(usage, 4)
    usage.acknowledge(SERVICE, "first", CYCLE_START)
    assert usage.preflight(SERVICE, tier="fast") == Note(5, 10, RESET)

    clock.set(NEXT_CYCLE_AT)
    assert usage.status(SERVICE).local_count == 0
    _reserve_n(usage, 4)

    assert usage.preflight(SERVICE, tier="fast") == Warn("first", 5, 10, NEXT_RESET)
    usage.acknowledge(SERVICE, "first", date(2026, 3, 15))
    assert _notices(db) == [
        (SERVICE, "2026-02-15", "first"),
        (SERVICE, "2026-03-15", "first"),
    ]


def test_acknowledge_is_idempotent(db: Database, usage: Any) -> None:
    _set_budget(usage, _budget())
    _reserve_n(usage, 4)

    usage.acknowledge(SERVICE, "first", CYCLE_START)
    usage.acknowledge(SERVICE, "first", CYCLE_START)

    assert _notices(db) == [(SERVICE, "2026-02-15", "first")]


def test_acknowledge_threshold_not_reached_refused(db: Database, usage: Any) -> None:
    _set_budget(usage, _budget())
    _reserve_n(usage, 2)

    with pytest.raises(BudgetNoticeRefused):
        usage.acknowledge(SERVICE, "first", CYCLE_START)
    assert _notices(db) == []


def test_acknowledge_other_threshold_than_offered_refused(db: Database, usage: Any) -> None:
    _set_budget(usage, _budget())
    _reserve_n(usage, 4)  # the decision offers "first"

    with pytest.raises(BudgetNoticeRefused):
        usage.acknowledge(SERVICE, "second", CYCLE_START)
    assert _notices(db) == []
    assert usage.preflight(SERVICE, tier="fast") == Warn("first", 5, 10, RESET)


def test_acknowledge_threshold_reached_before_first_was_acknowledged(
    db: Database, usage: Any
) -> None:
    # Two requests raced past both thresholds before the first dialog was confirmed.
    _set_budget(usage, _budget())
    _reserve_n(usage, 7)
    assert usage.preflight(SERVICE, tier="fast") == Warn("second", 8, 10, RESET)

    usage.acknowledge(SERVICE, "first", CYCLE_START)
    usage.acknowledge(SERVICE, "second", CYCLE_START)

    assert _notices(db) == [
        (SERVICE, "2026-02-15", "first"),
        (SERVICE, "2026-02-15", "second"),
    ]
    assert usage.status(SERVICE).shown_thresholds == frozenset({"first", "second"})


def test_acknowledge_for_another_cycle_refused(
    db: Database, usage: Any, clock: FrozenClock
) -> None:
    _set_budget(usage, _budget())
    clock.set(NEXT_CYCLE_AT)
    _reserve_n(usage, 4)

    for stale in (CYCLE_START, date(2026, 4, 15), date(2026, 3, 14)):
        with pytest.raises(BudgetNoticeRefused):
            usage.acknowledge(SERVICE, "first", stale)
    assert _notices(db) == []


@pytest.mark.parametrize(
    ("service", "threshold"),
    [(OTHER_SERVICE, "first"), ("not_a_service", "first"), (SERVICE, "third"), (SERVICE, "")],
)
def test_acknowledge_without_budget_or_unknown_values_refused(
    db: Database, usage: Any, service: str, threshold: str
) -> None:
    _set_budget(usage, _budget())
    _reserve_n(usage, 4)
    _reserve_n(usage, 4, service=OTHER_SERVICE)  # no budget there: nothing to acknowledge

    with pytest.raises(BudgetNoticeRefused):
        usage.acknowledge(service, threshold, CYCLE_START)
    assert _notices(db) == []


def test_preflight_writes_nothing(db: Database, usage: Any) -> None:
    _set_budget(usage, _budget())
    _reserve_n(usage, 4)
    before = _all_rows(db)

    for tier in ("fast", "thorough"):
        usage.preflight(SERVICE, tier=tier)
    usage.status(SERVICE)
    usage.breakdown(SERVICE)

    assert _all_rows(db) == before


# --- cycles in the local time zone (ADR-076 item 8) ---------------------------------


def test_cycle_bounds_compared_as_utc_instants(usage: Any, clock: FrozenClock) -> None:
    _set_budget(usage, _budget())
    # 23:30 local on 14 March: still the February cycle, although 15 March in UTC.
    clock.set(NEXT_CYCLE_AT - timedelta(minutes=30))
    _reserve_n(usage, 2)
    assert usage.status(SERVICE).cycle.start_date == CYCLE_START
    assert usage.status(SERVICE).local_count == 2

    clock.set(NEXT_CYCLE_AT)
    _reserve_n(usage, 1)

    status = usage.status(SERVICE)
    assert status.cycle.start_date == date(2026, 3, 15)
    assert status.cycle.reset_date == NEXT_RESET
    assert status.local_count == 1


# --- provider-reported usage (AC-M2.8) ----------------------------------------------


def test_ac_m2_8_provider_snapshot_higher_than_local_is_used(usage: Any) -> None:
    _set_budget(usage, _budget())
    _reserve_n(usage, 2)

    assert usage.save_snapshot(_requests_snapshot(7)) is True

    status = usage.status(SERVICE)
    assert status.local_count == 2
    assert status.provider_count == 7
    assert (status.effective.value, status.effective.source) == (7, "provider")
    assert usage.preflight(SERVICE, tier="fast") == Warn("second", 8, 10, RESET)

    usage.save_snapshot(_requests_snapshot(10, observed_at=START + timedelta(minutes=1)))
    with pytest.raises(BudgetBlocked):
        usage.reserve(SERVICE, tier="fast", task_type=TASK_TYPE)


def test_local_count_higher_than_provider_is_used(usage: Any) -> None:
    _set_budget(usage, _budget())
    _reserve_n(usage, 6)
    usage.save_snapshot(_requests_snapshot(3))

    status = usage.status(SERVICE)
    assert status.provider_count == 3
    assert (status.effective.value, status.effective.source) == (6, "local")


def test_snapshot_of_other_window_unit_or_service_not_compared(usage: Any) -> None:
    _set_budget(usage, _budget())
    _reserve_n(usage, 1)
    usage.save_snapshot(_requests_snapshot(9, window="five_hour"))
    usage.save_snapshot(_requests_snapshot(9, service=OTHER_SERVICE))
    usage.save_snapshot(_fraction_snapshot(0.95))

    status = usage.status(SERVICE)
    assert (status.effective.value, status.effective.source) == (1, "local")
    assert usage.preflight(SERVICE, tier="fast") == Allow()
    # The plan's utilization is information only (ADR-076 item 6), but it is shown.
    assert _fraction_snapshot(0.95) in status.provider_snapshots


def test_snapshot_from_previous_cycle_not_compared(usage: Any) -> None:
    _set_budget(usage, _budget())
    # 10 February local: the January cycle.
    usage.save_snapshot(_requests_snapshot(9, observed_at=datetime(2026, 2, 10, 15, tzinfo=UTC)))
    _reserve_n(usage, 1)

    status = usage.status(SERVICE)
    assert status.provider_count is None
    assert (status.effective.value, status.effective.source) == (1, "local")


def test_snapshot_upserted_per_service_and_window(db: Database, usage: Any) -> None:
    usage.save_snapshot(_requests_snapshot(3))
    later = _requests_snapshot(5, observed_at=START + timedelta(minutes=5), limit=100)

    assert usage.save_snapshot(later) is True

    rows = db.connection.execute(
        'SELECT service, "window", used, limit_count, observed_at, version '
        "FROM provider_usage_snapshots"
    ).fetchall()
    assert rows == [(SERVICE, "month", 5, 100, later.observed_at.isoformat(), 2)]
    assert later in usage.status(SERVICE).provider_snapshots


def test_older_snapshot_does_not_replace_newer(db: Database, usage: Any) -> None:
    usage.save_snapshot(_requests_snapshot(5, observed_at=START))

    assert usage.save_snapshot(_requests_snapshot(9, observed_at=START - timedelta(1))) is False

    assert db.connection.execute("SELECT used FROM provider_usage_snapshots").fetchall() == [(5,)]


@pytest.mark.parametrize(
    ("used", "limit"),
    [(SQLITE_INT_MAX + 1, None), (1, SQLITE_INT_MAX + 1), (10**30, 10**30)],
    ids=["used_too_large", "limit_too_large", "both_huge"],
)
def test_snapshot_outside_sqlite_integer_range_refused(
    db: Database, usage: Any, used: int, limit: int | None
) -> None:
    usage.save_snapshot(_requests_snapshot(4))
    before = _all_rows(db)

    saved = usage.save_snapshot(
        _requests_snapshot(used, limit=limit, observed_at=START + timedelta(minutes=1))
    )

    assert saved is False
    assert _all_rows(db) == before


def test_snapshot_at_sqlite_integer_max_saved(db: Database, usage: Any) -> None:
    assert usage.save_snapshot(_requests_snapshot(SQLITE_INT_MAX, limit=SQLITE_INT_MAX)) is True

    assert db.connection.execute(
        "SELECT used, limit_count FROM provider_usage_snapshots"
    ).fetchall() == [(SQLITE_INT_MAX, SQLITE_INT_MAX)]


def test_snapshot_for_unknown_service_refused(db: Database, usage: Any) -> None:
    assert usage.save_snapshot(_requests_snapshot(3, service="not_a_service")) is False
    assert _rows(db, "provider_usage_snapshots") == []


# --- no budget (AC-M2.10) -----------------------------------------------------------


def test_ac_m2_10_service_without_budget_never_blocked_or_warned(db: Database, usage: Any) -> None:
    _set_budget(usage, _budget(), service=OTHER_SERVICE)  # a budget elsewhere changes nothing
    usage.save_snapshot(_requests_snapshot(10**6))

    for tier in ("fast", "thorough"):
        _reserve_n(usage, 30, tier=tier)
        assert usage.preflight(SERVICE, tier=tier) == Allow()

    status = usage.status(SERVICE)
    assert status.budget is None
    assert status.local_count == 60
    with pytest.raises(BudgetNoticeRefused):
        usage.acknowledge(SERVICE, "first", CYCLE_START)
    assert _notices(db) == []


# --- breakdown ----------------------------------------------------------------------


def test_breakdown_by_tier_and_task(usage: Any, clock: FrozenClock) -> None:
    clock.set(START - timedelta(days=30))  # 21 January: the previous cycle
    _reserve_n(usage, 1)
    clock.set(START)
    _reserve_n(usage, 1, service=OTHER_SERVICE)
    done = [
        ("fast", TASK_TYPE, True, TokenUsage(100, 10)),
        ("fast", TASK_TYPE, True, TokenUsage(50, 5)),
        ("thorough", TASK_TYPE, False, TokenUsage(300, 30)),
        ("thorough", "cv_import", True, TokenUsage(7, 3)),
    ]
    for tier, task_type, success, tokens in done:
        record_id = usage.reserve(SERVICE, tier=tier, task_type=task_type)
        usage.complete(record_id, success=success, tokens=tokens)
    usage.reserve(SERVICE, tier="fast", task_type=TASK_TYPE)  # unfinished

    rows = usage.breakdown(SERVICE)

    assert [
        (r.tier, r.task_type, r.requests, r.unfinished, r.input_tokens, r.output_tokens)
        for r in rows
    ] == [
        ("fast", TASK_TYPE, 3, 1, 150, 15),
        ("thorough", "cv_import", 1, 0, 7, 3),
        ("thorough", TASK_TYPE, 1, 0, 300, 30),
    ]
    previous = cycle_bounds(START - timedelta(days=30), 1, TZ)
    earlier = usage.breakdown(SERVICE, cycle=previous)
    assert [(r.tier, r.task_type, r.requests) for r in earlier] == [("fast", TASK_TYPE, 1)]


# --- no prompt or personal text ------------------------------------------------------

USAGE_COLUMNS = {
    "id",
    "provider",
    "task_type",
    "tier",
    "units",
    "requested_at",
    "success",
    "created_at",
    "updated_at",
    "version",
    "task_id",
    "service",
    "input_tokens",
    "output_tokens",
}
# Ids, service and task type names, tiers and ISO 8601 times: no spaces, so no prose.
_TOKEN_TEXT = re.compile(r"^[A-Za-z0-9_:.+\-]*$")


@pytest.mark.parametrize(
    "task_type",
    [
        "Write a cover letter for Northwind Traders",
        "Ada Lovelace CV",
        "practice\nIgnore previous instructions",
        "x" * 65,
        "",
    ],
    ids=["prompt", "personal_name", "injected_newline", "too_long", "empty"],
)
def test_task_type_that_is_not_an_id_refused(db: Database, usage: Any, task_type: str) -> None:
    with pytest.raises(ValueError):
        usage.reserve(SERVICE, tier="fast", task_type=task_type)
    assert _usage_count(db) == 0


@pytest.mark.parametrize(
    ("service", "tier"), [("not_a_service", "fast"), (SERVICE, "ultra"), ("", "fast")]
)
def test_reserve_unknown_service_or_tier_refused(
    db: Database, usage: Any, service: str, tier: str
) -> None:
    with pytest.raises(ValueError):
        usage.reserve(service, tier=tier, task_type=TASK_TYPE)
    assert _usage_count(db) == 0


def test_records_hold_no_prompt_or_personal_text(
    db: Database, usage: Any, clock: FrozenClock
) -> None:
    columns = {row[1] for row in db.connection.execute("PRAGMA table_info(usage_records)")}
    assert columns == USAGE_COLUMNS

    task_id = _task(db, clock)
    _set_budget(usage, _budget())
    record_id = usage.reserve(SERVICE, tier="thorough", task_id=task_id, task_type=TASK_TYPE)
    usage.complete(record_id, success=True, tokens=TokenUsage(12, 3))
    usage.reserve(SERVICE, tier="fast", skip_budget=True)
    _reserve_n(usage, 3)
    usage.acknowledge(SERVICE, "first", CYCLE_START)
    usage.save_snapshot(_requests_snapshot(4))
    usage.save_snapshot(_fraction_snapshot(0.5))

    for table in ("usage_records", "budget_notices", "provider_usage_snapshots"):
        for row in _rows(db, table):
            for value in row:
                if isinstance(value, str):
                    assert _TOKEN_TEXT.match(value), (table, value)


# --- review follow-ups: connection test, unreadable budgets, caps, snapshots -----------

UNREADABLE = '{"budgets": {"claude_api_key": {"allowance": "ten"}}}'


def _store_unreadable_budgets(db: Database) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO settings (key, value, created_at, updated_at, version) "
            "VALUES ('usage_budgets', ?, ?, ?, 3)",
            (UNREADABLE, START.isoformat(), START.isoformat()),
        )


@pytest.mark.parametrize("task_type", [TASK_TYPE, "cv_import"])
def test_skip_budget_for_other_than_the_connection_test_refused(
    db: Database, usage: Any, task_type: str
) -> None:
    _set_budget(usage, _budget())
    _reserve_n(usage, 10)
    before = _all_rows(db)

    with pytest.raises(ValueError):
        usage.reserve(SERVICE, tier="fast", task_type=task_type, skip_budget=True)

    assert _all_rows(db) == before


def test_connection_test_task_type_named_explicitly_skips_budget(db: Database, usage: Any) -> None:
    _set_budget(usage, _budget())
    _reserve_n(usage, 10)

    record_id = usage.reserve(SERVICE, tier="fast", task_type="connection_test", skip_budget=True)

    assert _record(db, record_id)["task_type"] == "connection_test"


def test_unreadable_budgets_fail_closed(db: Database, usage: Any) -> None:
    _store_unreadable_budgets(db)
    before = _all_rows(db)

    for tier in ("fast", "thorough"):
        with pytest.raises(UsageLimitsUnreadable) as caught:
            usage.reserve(SERVICE, tier=tier, task_type=TASK_TYPE)
        with pytest.raises(UsageLimitsUnreadable):
            usage.preflight(SERVICE, tier=tier)
    assert caught.value.user_message == (
        "Your usage limits couldn't be read. Open Settings > Usage budgets and save them again."
    )
    assert _all_rows(db) == before
    with pytest.raises(BudgetNoticeRefused):
        usage.acknowledge(SERVICE, "first", CYCLE_START)


def test_unreadable_budgets_still_allow_the_connection_test(db: Database, usage: Any) -> None:
    _store_unreadable_budgets(db)

    record_id = usage.reserve(SERVICE, tier="fast", skip_budget=True)

    assert _record(db, record_id)["task_type"] == "connection_test"
    status = usage.status(SERVICE)
    assert status.budgets_unreadable is True
    assert status.budget is None
    assert status.local_count == 1


def test_readable_budgets_status_not_flagged(usage: Any) -> None:
    assert usage.status(SERVICE).budgets_unreadable is False
    _set_budget(usage, _budget())
    assert usage.status(SERVICE).budgets_unreadable is False


def test_save_budget_replaces_unreadable_setting_with_only_the_new_value(
    db: Database, usage: Any
) -> None:
    _store_unreadable_budgets(db)
    loaded = usage.load_budgets()
    assert (dict(loaded.value), loaded.version) == ({}, 3)

    usage.save_budget(OTHER_SERVICE, _budget(reset_day=1), expected_version=3)

    reloaded = usage.load_budgets()
    assert (dict(reloaded.value), reloaded.version) == ({OTHER_SERVICE: _budget(reset_day=1)}, 4)
    assert usage.status(SERVICE).budgets_unreadable is False
    assert usage.preflight(SERVICE, tier="fast") == Allow()


def test_thorough_cap_raises_tier_cap_reached_offering_fast(db: Database, usage: Any) -> None:
    _set_budget(usage, _budget(thorough_cap=3))
    _reserve_n(usage, 3, tier="thorough")
    before = _all_rows(db)

    with pytest.raises(TierCapReached) as caught:
        usage.reserve(SERVICE, tier="thorough", task_type=TASK_TYPE)

    error = caught.value
    assert (error.service, error.tier, error.resets_on, error.offer_fast) == (
        SERVICE_NAME,
        "thorough",
        RESET,
        True,
    )
    assert error.user_message == (
        "The Thorough limit for Claude API key is reached. It resets on 15 March 2026; "
        "you can run this once on Fast."
    )
    assert _all_rows(db) == before


def test_fast_cap_raises_tier_cap_reached_without_offer(usage: Any) -> None:
    _set_budget(usage, _budget(fast_cap=2))
    _reserve_n(usage, 2, tier="fast")

    with pytest.raises(TierCapReached) as caught:
        usage.reserve(SERVICE, tier="fast", task_type=TASK_TYPE)

    assert (caught.value.tier, caught.value.offer_fast) == ("fast", False)
    usage.reserve(SERVICE, tier="thorough", task_type=TASK_TYPE)


def test_overall_block_is_not_a_tier_cap(usage: Any) -> None:
    _set_budget(usage, _budget(thorough_cap=3))
    _reserve_n(usage, 10, tier="fast")

    with pytest.raises(BudgetBlocked) as caught:
        usage.reserve(SERVICE, tier="thorough", task_type=TASK_TYPE)

    assert type(caught.value) is BudgetBlocked


def test_unreadable_stored_snapshot_ignored(db: Database, usage: Any) -> None:
    _set_budget(usage, _budget())
    usage.save_snapshot(_requests_snapshot(9))
    usage.save_snapshot(_fraction_snapshot(0.5))
    with db.transaction() as conn:
        conn.execute(
            "UPDATE provider_usage_snapshots SET resets_at = 'not a time' "
            "WHERE \"window\" = 'month'"
        )
    _reserve_n(usage, 1)

    status = usage.status(SERVICE)

    assert status.provider_snapshots == (_fraction_snapshot(0.5),)
    assert status.provider_count is None
    assert (status.effective.value, status.effective.source) == (1, "local")


def test_month_snapshot_with_other_reset_not_compared(usage: Any) -> None:
    _set_budget(usage, _budget())
    _reserve_n(usage, 1)
    misaligned = ProviderUsageSnapshot(
        service=SERVICE,
        window="month",
        unit="requests",
        used=9,
        limit=None,
        utilization=None,
        resets_at=NEXT_CYCLE_AT + timedelta(days=1),
        observed_at=START,
    )
    usage.save_snapshot(misaligned)

    status = usage.status(SERVICE)
    assert status.provider_count is None
    assert (status.effective.value, status.effective.source) == (1, "local")
    assert misaligned in status.provider_snapshots


def test_month_snapshot_resetting_with_the_cycle_compared(usage: Any) -> None:
    _set_budget(usage, _budget())
    _reserve_n(usage, 1)
    aligned = ProviderUsageSnapshot(
        service=SERVICE,
        window="month",
        unit="requests",
        used=9,
        limit=None,
        utilization=None,
        # The same instant as the cycle end, written in another offset.
        resets_at=NEXT_CYCLE_AT.astimezone(ZoneInfo("Asia/Tokyo")),
        observed_at=START,
    )
    usage.save_snapshot(aligned)

    status = usage.status(SERVICE)
    assert status.provider_count == 9
    assert (status.effective.value, status.effective.source) == (9, "provider")


def test_status_reports_thresholds_shown_this_cycle(usage: Any, clock: FrozenClock) -> None:
    _set_budget(usage, _budget())
    assert usage.status(SERVICE).shown_thresholds == frozenset()
    _reserve_n(usage, 4)
    usage.acknowledge(SERVICE, "first", CYCLE_START)

    assert usage.status(SERVICE).shown_thresholds == frozenset({"first"})
    clock.set(NEXT_CYCLE_AT)
    assert usage.status(SERVICE).shown_thresholds == frozenset()


# --- M2.23 review: tier caps, information-only snapshots, one read for the panel ----------------


def test_status_without_budget_or_caps_reports_no_tier_caps(usage: Any) -> None:
    _reserve_n(usage, 2, tier="thorough")
    assert usage.status(SERVICE).tier_caps == ()

    _set_budget(usage, _budget())
    assert usage.status(SERVICE).tier_caps == ()


def test_status_reports_each_set_cap_with_its_tier_count(usage: Any) -> None:
    from app.domain.budgets import TierUsage

    _set_budget(usage, _budget(thorough_cap=3))
    _reserve_n(usage, 4, tier="fast")
    _reserve_n(usage, 2, tier="thorough")
    _reserve_n(usage, 5, tier="thorough", service=OTHER_SERVICE)  # another service's own

    assert usage.status(SERVICE).tier_caps == (TierUsage("thorough", 3, 2),)

    _set_budget(usage, _budget(fast_cap=4, thorough_cap=3))
    assert usage.status(SERVICE).tier_caps == (
        TierUsage("fast", 4, 4),
        TierUsage("thorough", 3, 2),
    )

    _set_budget(usage, _budget(fast_cap=9))
    assert usage.status(SERVICE).tier_caps == (TierUsage("fast", 9, 4),)


@pytest.mark.parametrize("used", [2, 3])
def test_status_cap_reached_agrees_with_preflight_and_reserve(usage: Any, used: int) -> None:
    """The status says a cap is reached exactly when the next request on that tier is
    refused for it: one rule, not two."""
    _set_budget(usage, _budget(thorough_cap=3))
    _reserve_n(usage, used, tier="thorough")

    (cap,) = usage.status(SERVICE).tier_caps
    refused = isinstance(usage.preflight(SERVICE, tier="thorough"), CapBlocked)

    assert (cap.count, cap.reached) == (used, used == 3)
    assert refused is cap.reached
    if cap.reached:
        with pytest.raises(TierCapReached):
            usage.reserve(SERVICE, tier="thorough", task_type=TASK_TYPE)
    else:
        usage.reserve(SERVICE, tier="thorough", task_type=TASK_TYPE)


def test_tier_count_starts_again_in_the_next_cycle(usage: Any, clock: FrozenClock) -> None:
    from app.domain.budgets import TierUsage

    _set_budget(usage, _budget(thorough_cap=3))
    _reserve_n(usage, 3, tier="thorough")
    clock.set(NEXT_CYCLE_AT)

    assert usage.status(SERVICE).tier_caps == (TierUsage("thorough", 3, 0),)


def test_ac_m2_8_compared_snapshot_is_not_information_only(usage: Any) -> None:
    """ADR-079: the one snapshot that is compared with the local count is not listed as
    information; every other one is, whatever its unit or window."""
    _set_budget(usage, _budget())
    compared = _requests_snapshot(7)
    week = _requests_snapshot(9, window="week")
    fraction = _fraction_snapshot(0.25)
    for snapshot in (compared, week, fraction):
        assert usage.save_snapshot(snapshot)

    status = usage.status(SERVICE)

    assert status.provider_count == 7
    assert compared in status.provider_snapshots
    assert compared not in status.information_only
    assert set(status.information_only) == {week, fraction}


def test_information_only_drops_a_snapshot_whose_reset_has_passed(
    usage: Any, clock: FrozenClock
) -> None:
    """A window that has reset says nothing about now: it is dropped once ``resets_at`` is
    reached (the injected clock), while one without a reset time stays."""
    fraction = _fraction_snapshot(0.25)  # resets three hours after START
    week = _requests_snapshot(9, window="week")  # no reset time
    usage.save_snapshot(fraction)
    usage.save_snapshot(week)
    assert fraction.resets_at is not None

    clock.set(fraction.resets_at - timedelta(seconds=1))
    assert set(usage.status(SERVICE).information_only) == {fraction, week}

    clock.set(fraction.resets_at)
    status = usage.status(SERVICE)
    assert status.information_only == (week,)
    assert fraction in status.provider_snapshots, "still stored; only no longer shown"


def test_reports_read_status_and_breakdown_without_the_write_lock(
    db: Database, usage: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The usage panel's read: every service's status and breakdown from one read snapshot.
    It never opens a write transaction and writes nothing."""
    _set_budget(usage, _budget(thorough_cap=3))
    _reserve_n(usage, 2, tier="thorough")
    _reserve_n(usage, 1, service=OTHER_SERVICE)
    usage.save_snapshot(_fraction_snapshot(0.25))
    expected = [
        (usage.status(service), usage.breakdown(service)) for service in (SERVICE, OTHER_SERVICE)
    ]
    before = _all_rows(db)
    opened: list[str] = []
    real_read = db.read

    def no_write_lock() -> None:
        raise AssertionError("a report never takes the write lock")

    def counted_read() -> Any:
        opened.append("read")
        return real_read()

    monkeypatch.setattr(db, "transaction", no_write_lock)
    monkeypatch.setattr(db, "read", counted_read)

    reports = usage.reports((SERVICE, OTHER_SERVICE))

    assert [(report.status, report.breakdown) for report in reports] == expected
    assert opened == ["read"], "one read snapshot for every service"
    assert _all_rows(db) == before


def test_reports_refuse_an_unknown_service(usage: Any) -> None:
    with pytest.raises(ValueError):
        usage.reports((SERVICE, "fictional_service"))


# --- check_start (M2.24): preflight for a started action, with its confirmation -------------


def test_check_start_reports_the_decision_and_the_cycle_and_writes_nothing(
    db: Database, usage: Any
) -> None:
    from app.services.usage import StartCheck

    assert usage.check_start(SERVICE, tier="fast") == StartCheck(Allow(), date(2026, 2, 1))
    _set_budget(usage, _budget())
    _reserve_n(usage, 4)
    before = _all_rows(db)

    assert usage.check_start(SERVICE, tier="fast") == StartCheck(
        Warn("first", 5, 10, RESET), CYCLE_START, block_at=10
    )
    assert _all_rows(db) == before


def test_check_start_stores_only_the_warning_that_is_due(db: Database, usage: Any) -> None:
    """Both warnings are reached and none confirmed: the second is due. Naming the first
    (which ``acknowledge`` would accept), another cycle or anything else stores nothing."""
    from app.services.usage import Confirmation, StartCheck

    _set_budget(usage, _budget())
    _reserve_n(usage, 7)
    due = StartCheck(Warn("second", 8, 10, RESET), CYCLE_START, block_at=10)

    for other in (("first", CYCLE_START), ("second", RESET), ("block", CYCLE_START), ("", RESET)):
        named = Confirmation(SERVICE, *other)
        assert usage.check_start(SERVICE, tier="fast", confirming=named) == due
        assert _notices(db) == []

    genuine = Confirmation(SERVICE, "second", CYCLE_START)
    confirmed = usage.check_start(SERVICE, tier="fast", confirming=genuine)
    assert confirmed == StartCheck(Note(8, 10, RESET), CYCLE_START, acknowledged=True, block_at=10)
    assert _notices(db) == [(SERVICE, "2026-02-15", "second")]
    # Confirmed once: the same post again finds no warning due and stores nothing more.
    again = usage.check_start(SERVICE, tier="fast", confirming=genuine)
    assert again == StartCheck(Note(8, 10, RESET), CYCLE_START, block_at=10)
    assert _notices(db) == [(SERVICE, "2026-02-15", "second")]


@pytest.mark.parametrize("named", ["fake", "claude_subscription", "fictional_service", "", "FAKE"])
def test_check_start_refuses_a_confirmation_that_names_another_service(
    db: Database, usage: Any, named: str
) -> None:
    """Two services are at the same warning in the same cycle, so only the service's name
    tells their confirmations apart: one that names anything but the service being checked
    stores nothing for either, and the warning stays due."""
    from app.services.usage import Confirmation, StartCheck

    other = "fake"
    for service in (SERVICE, other):
        _set_budget(usage, _budget(), service)
        _reserve_n(usage, 4, service=service)
    due = StartCheck(Warn("first", 5, 10, RESET), CYCLE_START, block_at=10)
    assert usage.check_start(other, tier="fast") == due

    forged = Confirmation(named, "first", CYCLE_START)
    assert usage.check_start(SERVICE, tier="fast", confirming=forged) == due
    assert _notices(db) == []

    # The same warning, named for the service being checked, is accepted.
    genuine = Confirmation(SERVICE, "first", CYCLE_START)
    assert usage.check_start(SERVICE, tier="fast", confirming=genuine).acknowledged
    assert _notices(db) == [(SERVICE, "2026-02-15", "first")]
    assert usage.check_start(other, tier="fast") == due, "the other service is still asked"


def test_check_start_reports_the_blocking_point_of_the_budget(usage: Any) -> None:
    """What the warning's wording needs: the blocking point, whatever the decision is; None
    for a service without a budget."""
    assert usage.check_start(SERVICE, tier="fast").block_at is None
    _set_budget(usage, _budget(allowance=12, block_at=9))
    assert usage.check_start(SERVICE, tier="fast").block_at == 9
    _reserve_n(usage, 4)
    checked = usage.check_start(SERVICE, tier="fast")
    assert checked.decision == Warn("first", 5, 12, RESET)
    assert checked.block_at == 9
    assert usage.check_start("fake", tier="fast").block_at is None, "another service: no budget"


def test_check_start_never_confirms_past_a_block(db: Database, usage: Any) -> None:
    from app.services.usage import Confirmation, StartCheck

    _set_budget(usage, _budget())
    _reserve_n(usage, 10)

    checked = usage.check_start(
        SERVICE, tier="fast", confirming=Confirmation(SERVICE, "second", CYCLE_START)
    )

    assert checked == StartCheck(Blocked(RESET), CYCLE_START, block_at=10)
    assert _notices(db) == []


@pytest.mark.parametrize(
    ("fast_used", "on_fast"), [(0, Allow()), (2, CapBlocked("fast", False, RESET))]
)
def test_check_start_on_a_capped_thorough_tier_says_what_fast_would_meet(
    usage: Any, fast_used: int, on_fast: Any
) -> None:
    from app.services.usage import StartCheck

    _set_budget(usage, _budget(fast_cap=2, thorough_cap=3))
    _reserve_n(usage, 3, tier="thorough")
    _reserve_n(usage, fast_used, tier="fast")

    assert usage.check_start(SERVICE, tier="thorough") == StartCheck(
        CapBlocked("thorough", True, RESET), CYCLE_START, fast=on_fast, block_at=10
    )
    if fast_used:  # the Fast cap itself offers nothing, so nothing else is checked
        assert usage.check_start(SERVICE, tier="fast") == StartCheck(
            on_fast, CYCLE_START, block_at=10
        )


def test_check_start_with_unreadable_budgets_raises(db: Database, usage: Any) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO settings (key, value, created_at, updated_at, version) "
            "VALUES ('usage_budgets', ?, ?, ?, 3)",
            (
                '{"budgets": {"claude_api_key": {"allowance": "ten"}}}',
                START.isoformat(),
                START.isoformat(),
            ),
        )
    from app.services.usage import Confirmation

    with pytest.raises(UsageLimitsUnreadable):
        usage.check_start(
            SERVICE, tier="fast", confirming=Confirmation(SERVICE, "first", CYCLE_START)
        )
    assert _notices(db) == []
