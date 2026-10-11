"""The tasks repository (M2.6; AC-M2.1; spec 5 M2 reliability rules 1, 2, 5, 6; ADR-038,
ADR-076 items 10, 11).

``app.modules.storage.tasks_repo`` holds plain functions on a ``sqlite3.Connection`` that run
inside the caller's transaction, like ``settings_repo``: insert-or-get on the unique partial
index of active dedupe keys (0002), conditional status transitions that bump ``version`` and
``updated_at``, heartbeats for running tasks, the cancel-request flag, and reads.

Real migrated SQLite in a temp folder, ``FrozenClock`` and ``SequentialIdFactory``; no sleeps
(the claim race lines threads up with a ``threading.Barrier``). Imports of the new modules are
inside the tests so each fails on its own while they are missing. All values are fictional.
"""

import sqlite3
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory

pytestmark = pytest.mark.integration

START = datetime(2026, 2, 7, 10, 30, 0, tzinfo=UTC)
TASK_TYPE = "practice"
BARRIER_TIMEOUT_S = 10

TASK_COLUMNS = (
    "id",
    "type",
    "status",
    "dedupe_key",
    "attempt",
    "progress",
    "heartbeat_at",
    "cancel_requested",
    "input_ref",
    "result_ref",
    "error",
    "created_at",
    "updated_at",
    "finished_at",
    "version",
)


# --- fixtures and helpers --------------------------------------------------------


def _repo() -> ModuleType:
    from app.modules.storage import tasks_repo

    return tasks_repo


def _status(value: str) -> Any:
    from app.domain.tasks import TaskStatus

    return TaskStatus(value)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(START)


@pytest.fixture
def ids() -> SequentialIdFactory:
    return SequentialIdFactory("task")


def _open_migrated(path: Path, backups: Path) -> Database:
    database = Database.open(path)
    apply_migrations(database, MIGRATIONS_DIR, backups, FrozenClock(START))
    return database


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = _open_migrated(tmp_path / "data" / "app.db", tmp_path / "backups")
    try:
        yield database
    finally:
        database.close()


def _now(clock: FrozenClock) -> str:
    return clock.now().isoformat()


def _new_row(
    clock: FrozenClock, ids: SequentialIdFactory, target: str = "run-1", **overrides: Any
) -> Any:
    from app.domain.tasks import dedupe_key

    now = _now(clock)
    fields: dict[str, Any] = {
        "id": ids.new(),
        "type": TASK_TYPE,
        "status": _status("queued"),
        "dedupe_key": dedupe_key(TASK_TYPE, target),
        "attempt": 1,
        "progress": 0,
        "heartbeat_at": None,
        "cancel_requested": False,
        "input_ref": None,
        "result_ref": None,
        "error": None,
        "created_at": now,
        "updated_at": now,
        "finished_at": None,
        "version": 1,
    }
    fields.update(overrides)
    return _repo().TaskRow(**fields)


def _insert(db: Database, row: Any) -> tuple[Any, bool]:
    with db.transaction() as conn:
        result: tuple[Any, bool] = _repo().insert_or_get_active(conn, row)
    return result


def _transition(
    db: Database, task_id: str, sources: tuple[str, ...], target: str, now: str, **kwargs: Any
) -> bool:
    with db.transaction() as conn:
        changed: bool = _repo().transition(
            conn,
            task_id,
            tuple(_status(s) for s in sources),
            _status(target),
            now=now,
            **kwargs,
        )
    return changed


def _get(db: Database, task_id: str) -> Any:
    with db.transaction() as conn:
        return _repo().get(conn, task_id)


def _raw(db: Database, task_id: str) -> dict[str, object]:
    cursor = db.connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    row = cursor.fetchone()
    assert row is not None
    names = [column[0] for column in cursor.description]
    assert sorted(names) == sorted(TASK_COLUMNS)
    return dict(zip(names, row, strict=True))


def _count(db: Database) -> int:
    count: int = db.connection.execute("SELECT count(*) FROM tasks").fetchone()[0]
    return count


def _queued(db: Database, clock: FrozenClock, ids: SequentialIdFactory, target: str) -> str:
    row, created = _insert(db, _new_row(clock, ids, target))
    assert created is True
    task_id: str = row.id
    return task_id


def _running(db: Database, clock: FrozenClock, ids: SequentialIdFactory, target: str) -> str:
    task_id = _queued(db, clock, ids, target)
    assert _transition(db, task_id, ("queued",), "running", _now(clock)) is True
    return task_id


# --- TaskRow ----------------------------------------------------------------------


def test_task_row_mirrors_the_task_columns() -> None:
    import dataclasses

    row_type = _repo().TaskRow
    assert dataclasses.is_dataclass(row_type)
    assert [f.name for f in dataclasses.fields(row_type)] == list(TASK_COLUMNS)


def test_get_returns_row_as_inserted_and_none_when_missing(
    db: Database, clock: FrozenClock, ids: SequentialIdFactory
) -> None:
    row = _new_row(clock, ids, input_ref="input-ref-1")
    stored, created = _insert(db, row)
    assert created is True
    assert stored == row

    loaded = _get(db, row.id)
    assert loaded == row
    assert loaded.status == "queued"
    assert loaded.cancel_requested is False
    assert loaded.version == 1
    assert _get(db, "task-9999") is None


# --- insert or get (AC-M2.1) ---------------------------------------------------------


def test_ac_m2_1_insert_duplicate_active_returns_existing_row(
    db: Database, clock: FrozenClock, ids: SequentialIdFactory
) -> None:
    first = _new_row(clock, ids, "run-1")
    stored_first, created_first = _insert(db, first)
    assert created_first is True

    clock.advance(timedelta(seconds=5))
    duplicate = _new_row(clock, ids, "run-1")
    assert duplicate.id != first.id
    existing, created = _insert(db, duplicate)

    assert created is False
    assert existing.id == first.id
    assert existing == stored_first
    assert _count(db) == 1
    assert _get(db, duplicate.id) is None


def test_ac_m2_1_insert_duplicate_of_running_task_returns_it(
    db: Database, clock: FrozenClock, ids: SequentialIdFactory
) -> None:
    running_id = _running(db, clock, ids, "run-1")

    existing, created = _insert(db, _new_row(clock, ids, "run-1"))

    assert created is False
    assert existing.id == running_id
    assert existing.status == "running"
    assert _count(db) == 1


@pytest.mark.parametrize("ending", ["succeeded", "failed", "cancelled", "interrupted"])
def test_insert_after_task_finished_creates_new_row(
    db: Database, clock: FrozenClock, ids: SequentialIdFactory, ending: str
) -> None:
    first_id = _running(db, clock, ids, "run-1")
    assert _transition(db, first_id, ("running",), ending, _now(clock)) is True

    second, created = _insert(db, _new_row(clock, ids, "run-1"))

    assert created is True
    assert second.id != first_id
    assert _count(db) == 2


def test_insert_with_existing_id_still_raises(
    db: Database, clock: FrozenClock, ids: SequentialIdFactory
) -> None:
    # Only the active dedupe-key conflict means "already there"; any other constraint
    # failure (here the primary key, with a different dedupe key) must not be swallowed.
    first_id = _running(db, clock, ids, "run-1")
    assert _transition(db, first_id, ("running",), "succeeded", _now(clock)) is True

    clash = _new_row(clock, ids, "run-2", id=first_id)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(db, clash)
    assert _count(db) == 1


def test_duplicate_insert_keeps_caller_transaction_usable(
    db: Database, clock: FrozenClock, ids: SequentialIdFactory
) -> None:
    repo = _repo()
    with db.transaction() as conn:
        _, created_a = repo.insert_or_get_active(conn, _new_row(clock, ids, "run-1"))
        _, created_dup = repo.insert_or_get_active(conn, _new_row(clock, ids, "run-1"))
        _, created_b = repo.insert_or_get_active(conn, _new_row(clock, ids, "run-2"))

    assert (created_a, created_dup, created_b) == (True, False, True)
    assert _count(db) == 2


def test_repo_functions_never_commit_on_their_own(
    db: Database, clock: FrozenClock, ids: SequentialIdFactory
) -> None:
    repo = _repo()
    task_id = _queued(db, clock, ids, "run-1")

    class Abort(Exception):
        pass

    with pytest.raises(Abort), db.transaction() as conn:
        repo.insert_or_get_active(conn, _new_row(clock, ids, "run-2"))
        repo.transition(conn, task_id, (_status("queued"),), _status("running"), now=_now(clock))
        raise Abort

    assert _count(db) == 1
    assert _raw(db, task_id)["status"] == "queued"
    assert _raw(db, task_id)["version"] == 1


# --- transitions (rule 1) ------------------------------------------------------------


def test_transition_succeeds_only_from_expected_state(
    db: Database, clock: FrozenClock, ids: SequentialIdFactory
) -> None:
    task_id = _queued(db, clock, ids, "run-1")

    # Wrong expected state: nothing changes.
    assert _transition(db, task_id, ("running",), "succeeded", _now(clock)) is False
    assert _raw(db, task_id)["status"] == "queued"
    assert _raw(db, task_id)["version"] == 1

    assert _transition(db, task_id, ("queued",), "running", _now(clock)) is True
    assert _raw(db, task_id)["status"] == "running"

    # The same claim a second time loses: the row is no longer queued.
    assert _transition(db, task_id, ("queued",), "running", _now(clock)) is False

    # Several expected states are accepted.
    assert _transition(db, task_id, ("queued", "running"), "interrupted", _now(clock)) is True
    assert _raw(db, task_id)["status"] == "interrupted"

    # A finished task can't be moved by an ordinary transition from an active state.
    assert _transition(db, task_id, ("queued", "running"), "cancelled", _now(clock)) is False
    assert _raw(db, task_id)["status"] == "interrupted"

    # An unknown id changes nothing.
    assert _transition(db, "task-9999", ("queued",), "running", _now(clock)) is False


def test_version_bumped_on_every_transition(
    db: Database, clock: FrozenClock, ids: SequentialIdFactory
) -> None:
    task_id = _queued(db, clock, ids, "run-1")
    assert _raw(db, task_id)["version"] == 1

    clock.advance(timedelta(seconds=1))
    assert _transition(db, task_id, ("queued",), "running", _now(clock)) is True
    assert _raw(db, task_id)["version"] == 2
    assert _raw(db, task_id)["updated_at"] == _now(clock)

    clock.advance(timedelta(seconds=1))
    assert _transition(db, task_id, ("queued",), "running", _now(clock)) is False
    assert _raw(db, task_id)["version"] == 2  # a lost update bumps nothing

    clock.advance(timedelta(seconds=1))
    assert _transition(db, task_id, ("running",), "succeeded", _now(clock)) is True
    assert _raw(db, task_id)["version"] == 3
    assert _raw(db, task_id)["updated_at"] == _now(clock)

    other_id = _queued(db, clock, ids, "run-2")
    clock.advance(timedelta(seconds=1))
    assert _transition(db, other_id, ("queued",), "cancelled", _now(clock)) is True
    assert _raw(db, other_id)["version"] == 2
    assert _raw(db, other_id)["updated_at"] == _now(clock)


def test_transition_sets_heartbeat_on_claim_and_finished_at_on_end(
    db: Database, clock: FrozenClock, ids: SequentialIdFactory
) -> None:
    created_at = _now(clock)
    task_id = _queued(db, clock, ids, "run-1")
    assert _raw(db, task_id)["finished_at"] is None

    # 0001 has no started_at: entering running starts the heartbeat, so the watchdog (M2.10)
    # times a task that never beats from its claim.
    clock.advance(timedelta(seconds=2))
    claimed_at = _now(clock)
    assert _transition(db, task_id, ("queued",), "running", claimed_at) is True
    claimed = _raw(db, task_id)
    assert claimed["heartbeat_at"] == claimed_at
    assert claimed["finished_at"] is None
    assert claimed["created_at"] == created_at

    clock.advance(timedelta(seconds=3))
    ended_at = _now(clock)
    assert _transition(db, task_id, ("running",), "succeeded", ended_at) is True
    ended = _raw(db, task_id)
    assert ended["finished_at"] == ended_at
    assert ended["created_at"] == created_at


def test_transition_stores_error_result_and_progress_when_given(
    db: Database, clock: FrozenClock, ids: SequentialIdFactory
) -> None:
    failed_id = _running(db, clock, ids, "run-1")
    assert (
        _transition(db, failed_id, ("running",), "failed", _now(clock), error="provider_timeout")
        is True
    )
    failed = _raw(db, failed_id)
    assert failed["error"] == "provider_timeout"
    assert failed["result_ref"] is None

    done_id = _running(db, clock, ids, "run-2")
    assert (
        _transition(
            db,
            done_id,
            ("running",),
            "succeeded",
            _now(clock),
            result_ref="draft:fictional-1",
            progress=100,
        )
        is True
    )
    done = _raw(db, done_id)
    assert done["result_ref"] == "draft:fictional-1"
    assert done["progress"] == 100
    assert done["error"] is None


def test_transition_without_optional_values_leaves_them_alone(
    db: Database, clock: FrozenClock, ids: SequentialIdFactory
) -> None:
    task_id = _running(db, clock, ids, "run-1")
    with db.transaction() as conn:
        assert _repo().heartbeat(conn, task_id, _now(clock), progress=40) is True

    assert _transition(db, task_id, ("running",), "interrupted", _now(clock)) is True
    row = _raw(db, task_id)
    assert row["progress"] == 40
    assert row["error"] is None
    assert row["result_ref"] is None


# --- the claim race (rule 1, rule 9) -----------------------------------------------------


@pytest.mark.parametrize("connections", ["shared", "separate"])
def test_two_threads_claim_same_task_one_wins(
    tmp_path: Path, clock: FrozenClock, ids: SequentialIdFactory, connections: str
) -> None:
    path = tmp_path / "data" / "app.db"
    first = _open_migrated(path, tmp_path / "backups")
    second = first if connections == "shared" else Database.open(path)
    try:
        task_id = _queued(first, clock, ids, "run-1")
        barrier = threading.Barrier(2, timeout=BARRIER_TIMEOUT_S)
        outcomes: list[bool] = []
        errors: list[BaseException] = []
        outcomes_lock = threading.Lock()
        now = _now(clock)

        def claim(database: Database) -> Callable[[], None]:
            def run() -> None:
                try:
                    # Line up outside the transaction: the shared Database serialises
                    # transactions with a lock, so waiting inside one would deadlock.
                    barrier.wait()
                    with database.transaction() as conn:
                        won = _repo().transition(
                            conn, task_id, (_status("queued"),), _status("running"), now=now
                        )
                    with outcomes_lock:
                        outcomes.append(won)
                except BaseException as exc:  # reported below, never swallowed
                    with outcomes_lock:
                        errors.append(exc)

            return run

        threads = [
            threading.Thread(target=claim(first)),
            threading.Thread(target=claim(second)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=BARRIER_TIMEOUT_S * 2)
            assert not thread.is_alive()

        assert errors == []
        assert sorted(outcomes) == [False, True]
        row = _raw(first, task_id)
        assert row["status"] == "running"
        assert row["version"] == 2
    finally:
        if second is not first:
            second.close()
        first.close()


# --- heartbeat (rule 5) ---------------------------------------------------------------


def test_heartbeat_only_updates_running_task(
    db: Database, clock: FrozenClock, ids: SequentialIdFactory
) -> None:
    repo = _repo()
    queued_id = _queued(db, clock, ids, "run-1")
    running_id = _running(db, clock, ids, "run-2")
    done_id = _running(db, clock, ids, "run-3")
    assert _transition(db, done_id, ("running",), "succeeded", _now(clock)) is True
    before_queued = _raw(db, queued_id)
    before_running = _raw(db, running_id)
    before_done = _raw(db, done_id)

    clock.advance(timedelta(seconds=30))
    beat_at = _now(clock)
    with db.transaction() as conn:
        assert repo.heartbeat(conn, running_id, beat_at, progress=25) is True
        assert repo.heartbeat(conn, queued_id, beat_at, progress=25) is False
        assert repo.heartbeat(conn, done_id, beat_at, progress=25) is False
        assert repo.heartbeat(conn, "task-9999", beat_at) is False

    running = _raw(db, running_id)
    assert running["heartbeat_at"] == beat_at
    assert running["progress"] == 25
    assert running["status"] == "running"
    # A heartbeat isn't a state change: version and updated_at stay put.
    assert running["version"] == 2
    assert running["updated_at"] == before_running["updated_at"]
    assert _raw(db, queued_id) == before_queued
    assert _raw(db, done_id) == before_done


def test_heartbeat_without_progress_keeps_progress(
    db: Database, clock: FrozenClock, ids: SequentialIdFactory
) -> None:
    repo = _repo()
    task_id = _running(db, clock, ids, "run-1")
    with db.transaction() as conn:
        assert repo.heartbeat(conn, task_id, _now(clock), progress=60) is True

    clock.advance(timedelta(seconds=10))
    with db.transaction() as conn:
        assert repo.heartbeat(conn, task_id, _now(clock)) is True

    row = _raw(db, task_id)
    assert row["progress"] == 60
    assert row["heartbeat_at"] == _now(clock)


@pytest.mark.parametrize("progress", [-1, 101, 1000])
def test_heartbeat_refuses_progress_outside_0_to_100(
    db: Database, clock: FrozenClock, ids: SequentialIdFactory, progress: int
) -> None:
    # Clamping is the caller's job (M2.10); the repo refuses out-of-range values plainly,
    # before the schema's CHECK would, and leaves the row untouched.
    task_id = _running(db, clock, ids, "run-1")
    before = _raw(db, task_id)
    with pytest.raises(ValueError), db.transaction() as conn:
        _repo().heartbeat(conn, task_id, _now(clock), progress=progress)
    assert _raw(db, task_id) == before


# --- cancel requests (rule 6) ---------------------------------------------------------


def test_request_cancel_sets_flag_on_running_task_only(
    db: Database, clock: FrozenClock, ids: SequentialIdFactory
) -> None:
    repo = _repo()
    running_id = _running(db, clock, ids, "run-1")
    done_id = _running(db, clock, ids, "run-2")
    assert _transition(db, done_id, ("running",), "succeeded", _now(clock)) is True
    before_done = _raw(db, done_id)

    with db.transaction() as conn:
        assert repo.request_cancel(conn, running_id, _now(clock)) is True
        assert repo.request_cancel(conn, done_id, _now(clock)) is False
        assert repo.request_cancel(conn, "task-9999", _now(clock)) is False

    assert _raw(db, running_id)["cancel_requested"] == 1
    assert _raw(db, running_id)["status"] == "running"  # the runner ends it (M2.11)
    assert _get(db, running_id).cancel_requested is True
    assert _raw(db, done_id) == before_done


# --- lists --------------------------------------------------------------------------


def test_list_recent_orders_by_updated_at_newest_first_with_limit(
    db: Database, clock: FrozenClock, ids: SequentialIdFactory
) -> None:
    repo = _repo()
    a = _queued(db, clock, ids, "run-a")
    clock.advance(timedelta(seconds=1))
    b = _queued(db, clock, ids, "run-b")
    clock.advance(timedelta(seconds=1))
    c = _queued(db, clock, ids, "run-c")
    clock.advance(timedelta(seconds=1))
    # Touching the oldest task moves it to the top.
    assert _transition(db, a, ("queued",), "running", _now(clock)) is True
    clock.advance(timedelta(seconds=1))
    assert _transition(db, a, ("running",), "failed", _now(clock), error="provider_error") is True

    with db.transaction() as conn:
        everything = repo.list_recent(conn, 10)
        top_two = repo.list_recent(conn, 2)

    assert [row.id for row in everything] == [a, c, b]
    assert [row.id for row in top_two] == [a, c]
    assert everything[0].status == "failed"


def test_list_active_returns_only_queued_and_running(
    db: Database, clock: FrozenClock, ids: SequentialIdFactory
) -> None:
    repo = _repo()
    queued_id = _queued(db, clock, ids, "run-1")
    running_id = _running(db, clock, ids, "run-2")
    for target, ending in [("run-3", "succeeded"), ("run-4", "failed"), ("run-5", "interrupted")]:
        ended = _running(db, clock, ids, target)
        assert _transition(db, ended, ("running",), ending, _now(clock)) is True
    cancelled_id = _queued(db, clock, ids, "run-6")
    assert _transition(db, cancelled_id, ("queued",), "cancelled", _now(clock)) is True

    with db.transaction() as conn:
        active = repo.list_active(conn)

    assert sorted(row.id for row in active) == sorted([queued_id, running_id])
    assert {row.status for row in active} == {"queued", "running"}


def test_lists_are_empty_on_a_fresh_database(db: Database) -> None:
    repo = _repo()
    with db.transaction() as conn:
        assert list(repo.list_recent(conn, 10)) == []
        assert list(repo.list_active(conn)) == []


# --- refused transitions and retry (rule 1; ADR-076 item 11) ----------------------------


@pytest.mark.parametrize(
    ("sources", "target"),
    [
        (("succeeded",), "running"),
        (("failed",), "queued"),
        (("interrupted", "cancelled"), "queued"),
        (("queued",), "succeeded"),
        (("running",), "running"),
        (("queued", "running"), "queued"),
    ],
)
def test_transition_refuses_pairs_the_domain_disallows(
    db: Database,
    clock: FrozenClock,
    ids: SequentialIdFactory,
    sources: tuple[str, ...],
    target: str,
) -> None:
    task_id = _running(db, clock, ids, "run-1")
    assert _transition(db, task_id, ("running",), "failed", _now(clock)) is True
    before = _raw(db, task_id)

    with pytest.raises(ValueError):
        _transition(db, task_id, sources, target, _now(clock))
    assert _raw(db, task_id) == before


@pytest.mark.parametrize("ending", ["failed", "cancelled", "interrupted"])
def test_requeue_for_retry_resets_the_row_as_next_attempt(
    db: Database, clock: FrozenClock, ids: SequentialIdFactory, ending: str
) -> None:
    repo = _repo()
    task_id = _running(db, clock, ids, "run-1")
    with db.transaction() as conn:
        assert repo.heartbeat(conn, task_id, _now(clock), progress=70) is True
        assert repo.request_cancel(conn, task_id, _now(clock)) is True
    assert _transition(db, task_id, ("running",), ending, _now(clock), error="boom") is True
    db.connection.execute(
        "UPDATE tasks SET result_ref = 'draft:fictional-old' WHERE id = ?", (task_id,)
    )
    before = _raw(db, task_id)
    assert before["heartbeat_at"] is not None

    clock.advance(timedelta(seconds=5))
    with db.transaction() as conn:
        assert repo.requeue_for_retry(conn, task_id, _now(clock)) is True

    row = _raw(db, task_id)
    assert row["status"] == "queued"
    assert row["attempt"] == 2
    assert row["error"] is None
    assert row["progress"] == 0
    assert row["cancel_requested"] == 0
    assert row["finished_at"] is None
    assert row["result_ref"] is None
    assert row["heartbeat_at"] is None
    assert row["updated_at"] == _now(clock)
    assert row["version"] == int(str(before["version"])) + 1
    assert row["created_at"] == before["created_at"]


def test_requeue_for_retry_refuses_other_states(
    db: Database, clock: FrozenClock, ids: SequentialIdFactory
) -> None:
    repo = _repo()
    queued_id = _queued(db, clock, ids, "run-1")
    running_id = _running(db, clock, ids, "run-2")
    done_id = _running(db, clock, ids, "run-3")
    assert _transition(db, done_id, ("running",), "succeeded", _now(clock)) is True
    before = {task_id: _raw(db, task_id) for task_id in (queued_id, running_id, done_id)}

    with db.transaction() as conn:
        for task_id in (queued_id, running_id, done_id, "task-9999"):
            assert repo.requeue_for_retry(conn, task_id, _now(clock)) is False

    for task_id, row in before.items():
        assert _raw(db, task_id) == row


def test_requeue_for_retry_refused_while_key_is_active(
    db: Database, clock: FrozenClock, ids: SequentialIdFactory
) -> None:
    repo = _repo()
    failed_id = _running(db, clock, ids, "run-1")
    assert _transition(db, failed_id, ("running",), "failed", _now(clock)) is True
    _queued(db, clock, ids, "run-1")
    before = _raw(db, failed_id)

    with db.transaction() as conn:
        assert repo.requeue_for_retry(conn, failed_id, _now(clock)) is False
    assert _raw(db, failed_id) == before


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "running"},
        {"status": "succeeded"},
        {"attempt": 2},
        {"cancel_requested": True},
        {"finished_at": START.isoformat()},
    ],
)
def test_insert_refuses_rows_that_are_not_fresh_queued_tasks(
    db: Database, clock: FrozenClock, ids: SequentialIdFactory, overrides: dict[str, Any]
) -> None:
    if "status" in overrides:
        overrides = {**overrides, "status": _status(overrides["status"])}
    row = _new_row(clock, ids, "run-1", **overrides)
    with pytest.raises(ValueError):
        _insert(db, row)
    assert _count(db) == 0


@pytest.mark.parametrize("target", ["failed", "cancelled", "interrupted"])
def test_transition_accepts_result_ref_only_on_success(
    db: Database, clock: FrozenClock, ids: SequentialIdFactory, target: str
) -> None:
    task_id = _running(db, clock, ids, "run-1")
    before = _raw(db, task_id)
    with pytest.raises(ValueError):
        _transition(db, task_id, ("running",), target, _now(clock), result_ref="draft:x")
    assert _raw(db, task_id) == before

    queued_id = _queued(db, clock, ids, "run-2")
    with pytest.raises(ValueError):
        _transition(db, queued_id, ("queued",), "running", _now(clock), result_ref="draft:x")
    assert _raw(db, queued_id)["status"] == "queued"


def test_tasks_repo_is_re_exported_by_the_storage_module() -> None:
    import app.modules.storage as storage

    assert "tasks_repo" in storage.__all__
    assert storage.tasks_repo is _repo()


# --- claim the oldest queued task (M2.7) ------------------------------------------------


def _claim(db: Database, now: str) -> Any:
    with db.transaction() as conn:
        return _repo().claim_next(conn, now)


def test_claim_next_takes_oldest_by_updated_at_with_rowid_tie_break(
    db: Database, clock: FrozenClock, ids: SequentialIdFactory
) -> None:
    first = _queued(db, clock, ids, "run-1")
    clock.advance(timedelta(seconds=10))
    later = _queued(db, clock, ids, "run-2")
    clock.set(START)  # same updated_at as the first, inserted after it
    tie = _queued(db, clock, ids, "run-3")
    clock.set(START + timedelta(minutes=1))
    now = _now(clock)

    claimed = [_claim(db, now) for _ in range(3)]

    assert [row.id for row in claimed] == [first, tie, later]
    for row in claimed:
        assert row.status == "running"
        assert row.heartbeat_at == now
        assert row.updated_at == now
        assert row.version == 2
        assert _raw(db, row.id)["status"] == "running"
    assert _claim(db, now) is None


def test_claim_next_skips_running_and_finished_tasks(
    db: Database, clock: FrozenClock, ids: SequentialIdFactory
) -> None:
    assert _claim(db, _now(clock)) is None
    running_id = _running(db, clock, ids, "run-1")
    done_id = _running(db, clock, ids, "run-2")
    assert _transition(db, done_id, ("running",), "succeeded", _now(clock)) is True
    before = (_raw(db, running_id), _raw(db, done_id))

    assert _claim(db, _now(clock)) is None
    assert (_raw(db, running_id), _raw(db, done_id)) == before


@pytest.mark.parametrize("connections", ["shared", "separate"])
def test_two_threads_claim_next_one_gets_the_task(
    tmp_path: Path, clock: FrozenClock, ids: SequentialIdFactory, connections: str
) -> None:
    path = tmp_path / "data" / "app.db"
    first = _open_migrated(path, tmp_path / "backups")
    second = first if connections == "shared" else Database.open(path)
    try:
        task_id = _queued(first, clock, ids, "run-1")
        barrier = threading.Barrier(2, timeout=BARRIER_TIMEOUT_S)
        outcomes: list[Any] = []
        errors: list[BaseException] = []
        lock = threading.Lock()
        now = _now(clock)

        def claim(database: Database) -> Callable[[], None]:
            def run() -> None:
                try:
                    barrier.wait()
                    row = _claim(database, now)
                    with lock:
                        outcomes.append(row)
                except BaseException as exc:  # reported below, never swallowed
                    with lock:
                        errors.append(exc)

            return run

        threads = [threading.Thread(target=claim(first)), threading.Thread(target=claim(second))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=BARRIER_TIMEOUT_S * 2)
            assert not thread.is_alive()

        assert errors == []
        winners = [row for row in outcomes if row is not None]
        assert len(outcomes) == 2
        assert len(winners) == 1
        assert winners[0].id == task_id
        assert _raw(first, task_id)["version"] == 2
    finally:
        if second is not first:
            second.close()


def test_interrupt_stale_falls_back_to_updated_at_without_heartbeat(
    db: Database, clock: FrozenClock, ids: SequentialIdFactory
) -> None:
    """A running row with no heartbeat at all (written by older code or by hand) is judged
    by ``updated_at`` instead of never being stale."""
    task_id = _running(db, clock, ids, "run-1")
    db.connection.execute("UPDATE tasks SET heartbeat_at = NULL WHERE id = ?", (task_id,))
    claimed_at = _now(clock)

    with db.transaction() as conn:
        assert _repo().interrupt_stale(conn, TASK_TYPE, claimed_at, _now(clock)) == []
    clock.advance(timedelta(minutes=3))
    cutoff = (clock.now() - timedelta(minutes=2)).isoformat()
    with db.transaction() as conn:
        assert _repo().interrupt_stale(conn, TASK_TYPE, cutoff, _now(clock)) == [task_id]
    assert _get(db, task_id).status == _status("interrupted")
