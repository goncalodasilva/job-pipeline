"""The task service (M2.7; AC-M2.1, AC-M2.2; spec 5 M2 reliability rules 1, 2, 6; ADR-034,
ADR-038, ADR-076 items 10, 11).

``app.services.tasks.TaskService(db, clock, ids, on_change)`` is the one place that changes
task rows: ``enqueue`` (an active duplicate returns the existing task), ``request_cancel``
(queued ends cancelled at once, running only gets the flag), ``retry`` (same row, attempt + 1;
an active duplicate wins; refused from running, queued or succeeded with ``TaskNotRetryable``),
``get``, ``recent`` and, for the runner, ``claim_next``, ``heartbeat`` and ``finish``.
``on_change(TaskChange(id, type, status))`` fires after the commit, once per committed change,
never for a no-op or a rolled-back call, and a raising callback can't undo the change.
``tasks.error`` holds the domain error's kind (its class name), never its text.
Other services enqueue through the ``TaskEnqueuer`` protocol in ``app.domain.tasks`` (ADR-034).

Real migrated SQLite in a temp folder, ``FrozenClock`` and ``SequentialIdFactory``; no sleeps
(concurrent enqueues line up on a ``threading.Barrier``). Imports of the new modules are inside
the tests so each fails on its own while they are missing. All values are fictional.
"""

import logging
import os
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.domain.errors import AppError, NetworkUnavailable
from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory
from tests.leak_scan import fictional_secret

pytestmark = pytest.mark.integration

START = datetime(2026, 2, 7, 10, 30, 0, tzinfo=UTC)
TASK_TYPE = "practice"
BARRIER_TIMEOUT_S = 10
REPO_ROOT = Path(__file__).resolve().parents[2]


# --- fixtures and helpers --------------------------------------------------------


def _status(value: str) -> Any:
    from app.domain.tasks import TaskStatus

    return TaskStatus(value)


class Recorder:
    """Collects ``TaskChange`` events; optionally runs a probe inside the callback."""

    def __init__(self) -> None:
        self.changes: list[Any] = []
        self.probe: Callable[[Any], None] | None = None

    def __call__(self, change: Any) -> None:
        self.changes.append(change)
        if self.probe is not None:
            self.probe(change)

    def triples(self) -> list[tuple[str, str, str]]:
        return [(c.id, c.type, str(c.status)) for c in self.changes]


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
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "app.db"


@pytest.fixture
def db(tmp_path: Path, db_path: Path) -> Iterator[Database]:
    database = _open_migrated(db_path, tmp_path / "backups")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def events() -> Recorder:
    return Recorder()


def _service(db: Database, clock: Any, ids: Any, on_change: Callable[[Any], None]) -> Any:
    from app.services.tasks import TaskService

    return TaskService(db, clock, ids, on_change)


@pytest.fixture
def service(db: Database, clock: FrozenClock, ids: SequentialIdFactory, events: Recorder) -> Any:
    return _service(db, clock, ids, events)


def _raw(db: Database, task_id: str) -> dict[str, object]:
    cursor = db.connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    row = cursor.fetchone()
    assert row is not None
    names = [column[0] for column in cursor.description]
    return dict(zip(names, row, strict=True))


def _count(db: Database) -> int:
    count: int = db.connection.execute("SELECT count(*) FROM tasks").fetchone()[0]
    return count


def _enqueue(service: Any, target: str = "run-1", **kwargs: Any) -> str:
    ref = service.enqueue(TASK_TYPE, target, **kwargs)
    assert ref.created is True
    task_id: str = ref.id
    return task_id


def _running(service: Any, target: str = "run-1") -> str:
    task_id = _enqueue(service, target)
    claimed = service.claim_next()
    assert claimed is not None
    assert claimed.id == task_id
    return task_id


def _ended(service: Any, ending: str, target: str = "run-1") -> str:
    task_id = _running(service, target)
    error = NetworkUnavailable() if ending == "failed" else None
    assert service.finish(task_id, _status(ending), error=error) is True
    return task_id


def _snapshot(db: Database) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = db.connection.execute(
        "SELECT * FROM tasks ORDER BY rowid"
    ).fetchall()
    return rows


# --- enqueue (AC-M2.1) ------------------------------------------------------------


def test_enqueue_creates_queued_task_and_publishes_it(
    db: Database, service: Any, events: Recorder
) -> None:
    ref = service.enqueue(TASK_TYPE, "run-1", input_ref="input-ref-1")

    assert ref.created is True
    row = service.get(ref.id)
    assert row is not None
    assert row.status == "queued"
    assert row.type == TASK_TYPE
    assert row.dedupe_key == f"{TASK_TYPE}:run-1"
    assert row.attempt == 1
    assert row.input_ref == "input-ref-1"
    assert events.triples() == [(ref.id, TASK_TYPE, "queued")]


def test_ac_m2_1_duplicate_enqueue_returns_existing_id(
    db: Database, service: Any, clock: FrozenClock, events: Recorder
) -> None:
    first = service.enqueue(TASK_TYPE, "run-1")
    clock.advance(timedelta(seconds=5))
    second = service.enqueue(TASK_TYPE, "run-1")

    assert first.created is True
    assert second.created is False
    assert second.id == first.id
    assert _count(db) == 1
    # The duplicate changed nothing, so it published nothing.
    assert events.triples() == [(first.id, TASK_TYPE, "queued")]


def test_ac_m2_1_duplicate_of_running_task_returns_its_id(service: Any, db: Database) -> None:
    running_id = _running(service, "run-1")

    again = service.enqueue(TASK_TYPE, "run-1")

    assert again.created is False
    assert again.id == running_id
    assert _count(db) == 1


@pytest.mark.parametrize("ending", ["succeeded", "failed", "cancelled", "interrupted"])
def test_ac_m2_1_enqueue_after_finish_creates_new_task(
    service: Any, db: Database, ending: str
) -> None:
    first_id = _ended(service, ending, "run-1")

    again = service.enqueue(TASK_TYPE, "run-1")

    assert again.created is True
    assert again.id != first_id
    assert _count(db) == 2
    assert service.get(first_id).status == ending


def test_enqueue_different_targets_are_independent(service: Any, db: Database) -> None:
    a = service.enqueue(TASK_TYPE, "run-1")
    b = service.enqueue(TASK_TYPE, "run-2")
    assert a.created is True
    assert b.created is True
    assert a.id != b.id
    assert _count(db) == 2


@pytest.mark.parametrize("connections", ["shared", "separate"])
def test_ac_m2_1_concurrent_enqueues_create_one_row(
    tmp_path: Path, db_path: Path, clock: FrozenClock, connections: str
) -> None:
    first_db = _open_migrated(db_path, tmp_path / "backups")
    second_db = first_db if connections == "shared" else Database.open(db_path)
    events = Recorder()
    events_lock = threading.Lock()

    def on_change(change: Any) -> None:
        with events_lock:
            events(change)

    services = [
        _service(first_db, clock, SequentialIdFactory("left"), on_change),
        _service(second_db, clock, SequentialIdFactory("right"), on_change),
    ]
    try:
        barrier = threading.Barrier(2, timeout=BARRIER_TIMEOUT_S)
        refs: list[Any] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def enqueue(task_service: Any) -> Callable[[], None]:
            def run() -> None:
                try:
                    barrier.wait()
                    ref = task_service.enqueue(TASK_TYPE, "run-1")
                    with lock:
                        refs.append(ref)
                except BaseException as exc:  # reported below, never swallowed
                    with lock:
                        errors.append(exc)

            return run

        threads = [threading.Thread(target=enqueue(s)) for s in services]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=BARRIER_TIMEOUT_S * 2)
            assert not thread.is_alive()

        assert errors == []
        assert len(refs) == 2
        assert refs[0].id == refs[1].id
        assert sorted(ref.created for ref in refs) == [False, True]
        assert _count(first_db) == 1
        assert len(events.changes) == 1
        assert events.changes[0].id == refs[0].id
    finally:
        if second_db is not first_db:
            second_db.close()
        first_db.close()


# --- cancel requests ---------------------------------------------------------------


def test_cancel_queued_task_ends_cancelled_without_running(
    service: Any, db: Database, events: Recorder
) -> None:
    task_id = _enqueue(service)
    events.changes.clear()

    row = service.request_cancel(task_id)

    assert row is not None
    assert row.id == task_id
    assert row.status == "cancelled"
    stored = _raw(db, task_id)
    assert stored["status"] == "cancelled"
    assert stored["finished_at"] is not None
    assert stored["heartbeat_at"] is None
    assert events.triples() == [(task_id, TASK_TYPE, "cancelled")]
    # It never runs: the runner finds nothing to claim.
    assert service.claim_next() is None
    assert service.get(task_id).status == "cancelled"


def test_cancel_running_sets_flag_only(service: Any, db: Database, events: Recorder) -> None:
    task_id = _running(service)
    events.changes.clear()

    row = service.request_cancel(task_id)

    assert row is not None
    assert row.status == "running"
    assert row.cancel_requested is True
    stored = _raw(db, task_id)
    assert stored["status"] == "running"
    assert stored["cancel_requested"] == 1
    assert stored["finished_at"] is None
    # The flag is a committed change, so the panel hears about it once (still running).
    assert events.triples() == [(task_id, TASK_TYPE, "running")]


@pytest.mark.parametrize("ending", ["succeeded", "failed", "cancelled", "interrupted"])
def test_cancel_finished_task_changes_nothing_and_publishes_nothing(
    service: Any, db: Database, events: Recorder, ending: str
) -> None:
    task_id = _ended(service, ending)
    before = _snapshot(db)
    events.changes.clear()

    row = service.request_cancel(task_id)

    assert row is not None
    assert row.status == ending
    assert _snapshot(db) == before
    assert events.changes == []


def test_cancel_missing_task_returns_none(service: Any, events: Recorder) -> None:
    assert service.request_cancel("task-9999") is None
    assert events.changes == []


# --- retry (ADR-076 item 11) -------------------------------------------------------


@pytest.mark.parametrize("ending", ["interrupted", "failed", "cancelled"])
def test_retry_interrupted_requeues_with_attempt_2(
    service: Any, db: Database, events: Recorder, clock: FrozenClock, ending: str
) -> None:
    task_id = _ended(service, ending)
    clock.advance(timedelta(minutes=3))
    events.changes.clear()

    row = service.retry(task_id)

    assert row.id == task_id
    assert row.status == "queued"
    assert row.attempt == 2
    stored = _raw(db, task_id)
    assert stored["status"] == "queued"
    assert stored["attempt"] == 2
    assert stored["error"] is None
    assert stored["finished_at"] is None
    assert stored["cancel_requested"] == 0
    assert stored["updated_at"] == clock.now().isoformat()
    assert _count(db) == 1
    assert events.triples() == [(task_id, TASK_TYPE, "queued")]
    # The same row runs again.
    claimed = service.claim_next()
    assert claimed is not None
    assert claimed.id == task_id


def test_retry_returns_active_duplicate_instead(
    service: Any, db: Database, events: Recorder
) -> None:
    old_id = _ended(service, "interrupted", "run-1")
    newer = service.enqueue(TASK_TYPE, "run-1")
    assert newer.created is True
    before = _raw(db, old_id)
    events.changes.clear()

    row = service.retry(old_id)

    assert row.id == newer.id
    assert row.status == "queued"
    assert _raw(db, old_id) == before
    assert _count(db) == 2
    assert events.changes == []


@pytest.mark.parametrize("state", ["running", "succeeded", "queued"])
def test_retry_refused_for_running_or_succeeded(
    service: Any, db: Database, events: Recorder, state: str
) -> None:
    from app.domain.errors import TaskNotRetryable

    if state == "queued":
        task_id = _enqueue(service)
    elif state == "running":
        task_id = _running(service)
    else:
        task_id = _ended(service, "succeeded")
    before = _snapshot(db)
    events.changes.clear()

    with pytest.raises(TaskNotRetryable) as caught:
        service.retry(task_id)

    assert isinstance(caught.value, AppError)
    assert caught.value.user_message.strip()
    assert _snapshot(db) == before
    assert events.changes == []


def test_retry_missing_task_returns_none(service: Any, db: Database, events: Recorder) -> None:
    before = _snapshot(db)

    assert service.retry("task-9999") is None

    assert _snapshot(db) == before
    assert events.changes == []


def test_retry_twice_runs_attempt_3(service: Any) -> None:
    task_id = _ended(service, "failed")
    service.retry(task_id)
    assert service.claim_next().id == task_id
    assert service.finish(task_id, _status("interrupted")) is True

    row = service.retry(task_id)

    assert row.attempt == 3


# --- change events after commit ------------------------------------------------------


def test_change_published_after_commit_only(
    db: Database, db_path: Path, clock: FrozenClock
) -> None:
    observer = Database.open(db_path)
    seen: list[tuple[str, object, bool]] = []
    events = Recorder()

    def probe(change: Any) -> None:
        # Another connection only sees committed data (WAL), so if the transaction were
        # still open it would see the old status or no row at all.
        row = observer.connection.execute(
            "SELECT status FROM tasks WHERE id = ?", (change.id,)
        ).fetchone()
        seen.append(
            (str(change.status), None if row is None else row[0], db.connection.in_transaction)
        )

    events.probe = probe

    class RepeatingIds:
        """Hands out the same id twice, so the second insert breaks the primary key."""

        def new(self) -> str:
            return "task-0001"

    service = _service(db, clock, RepeatingIds(), events)
    try:
        task_id = service.enqueue(TASK_TYPE, "run-1").id
        claimed = service.claim_next()
        assert claimed is not None
        assert service.request_cancel(task_id) is not None
        assert service.finish(task_id, _status("cancelled")) is True

        assert seen == [
            ("queued", "queued", False),
            ("running", "running", False),
            ("running", "running", False),
            ("cancelled", "cancelled", False),
        ]

        # A failing transaction (primary key clash on a different target) rolls back and
        # publishes nothing.
        before = _snapshot(db)
        events.changes.clear()
        with pytest.raises((sqlite3.IntegrityError, AppError)):
            service.enqueue(TASK_TYPE, "run-2")
        assert events.changes == []
        assert _snapshot(db) == before
        assert db.connection.in_transaction is False
    finally:
        observer.close()


def test_no_op_calls_publish_nothing(service: Any, events: Recorder) -> None:
    queued_id = _enqueue(service, "run-1")
    events.changes.clear()

    assert service.heartbeat(queued_id) is False
    assert service.finish(queued_id, _status("succeeded")) is False
    assert service.enqueue(TASK_TYPE, "run-1").created is False
    assert service.get(queued_id) is not None
    service.recent(10)

    assert events.changes == []


def test_raising_on_change_keeps_committed_change(
    db: Database, clock: FrozenClock, ids: SequentialIdFactory, caplog: pytest.LogCaptureFixture
) -> None:
    marker = "fictional-callback-detail-7Q2Z"

    def explode(change: Any) -> None:
        raise RuntimeError(marker)

    service = _service(db, clock, ids, explode)
    caplog.set_level(logging.DEBUG)

    ref = service.enqueue(TASK_TYPE, "run-1")
    claimed = service.claim_next()

    assert ref.created is True
    assert _raw(db, ref.id)["status"] == "running"
    assert claimed is not None
    assert claimed.id == ref.id
    assert db.connection.in_transaction is False
    assert "RuntimeError" in caplog.text
    assert marker not in caplog.text
    for record in caplog.records:
        assert record.exc_info is None or marker not in str(record.exc_info[1])


# --- errors stored as their kind ---------------------------------------------------------


def test_error_stored_as_kind_without_text(
    service: Any, db: Database, events: Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    secret = fictional_secret("sk-ant-oat01-", "M2.7 task error detail")
    task_id = _running(service)
    events.changes.clear()
    caplog.set_level(logging.DEBUG)

    finished = service.finish(
        task_id, _status("failed"), error=NetworkUnavailable(detail=f"upstream said {secret.value}")
    )

    assert finished is True
    stored = _raw(db, task_id)
    assert stored["status"] == "failed"
    assert stored["error"] == "NetworkUnavailable"
    assert service.get(task_id).error == "NetworkUnavailable"
    for value in stored.values():
        assert secret.value not in str(value)
        assert NetworkUnavailable.message not in str(value)
    assert secret.value not in caplog.text
    assert events.triples() == [(task_id, TASK_TYPE, "failed")]


def test_error_kind_of_error_with_custom_message(service: Any, db: Database) -> None:
    from app.domain.errors import BudgetBlocked

    task_id = _running(service)
    error = BudgetBlocked("Fictional Search", datetime(2026, 3, 1, tzinfo=UTC).date())

    assert service.finish(task_id, _status("failed"), error=error) is True

    stored = _raw(db, task_id)
    assert stored["error"] == "BudgetBlocked"
    assert "Fictional Search" not in str(stored)


def test_error_stored_as_its_declared_kind(service: Any, db: Database) -> None:
    class FictionalRenamedError(AppError):
        kind = "FictionalStableKind"

    task_id = _running(service)

    assert service.finish(task_id, _status("failed"), error=FictionalRenamedError()) is True

    assert _raw(db, task_id)["error"] == "FictionalStableKind"


# --- runner side: claim, heartbeat, finish ---------------------------------------------------


def test_claim_next_takes_oldest_queued_first(
    service: Any, clock: FrozenClock, events: Recorder
) -> None:
    first = _enqueue(service, "run-1")
    clock.advance(timedelta(seconds=10))
    later = _enqueue(service, "run-2")
    clock.set(START)  # same updated_at as the first; inserted later, so after it
    tie = _enqueue(service, "run-3")
    clock.set(START + timedelta(minutes=1))
    events.changes.clear()

    order = [service.claim_next().id for _ in range(3)]

    assert order == [first, tie, later]
    assert service.claim_next() is None
    assert events.triples() == [
        (first, TASK_TYPE, "running"),
        (tie, TASK_TYPE, "running"),
        (later, TASK_TYPE, "running"),
    ]
    for task_id in order:
        assert service.get(task_id).status == "running"


def test_claim_next_on_empty_queue_returns_none(service: Any, events: Recorder) -> None:
    assert service.claim_next() is None
    _ended(service, "succeeded")
    events.changes.clear()
    assert service.claim_next() is None
    assert events.changes == []


def test_claim_next_puts_retried_task_behind_older_queued(service: Any, clock: FrozenClock) -> None:
    old = _ended(service, "interrupted", "run-1")
    clock.advance(timedelta(seconds=1))
    waiting = _enqueue(service, "run-2")
    clock.advance(timedelta(seconds=1))
    service.retry(old)

    assert service.claim_next().id == waiting
    assert service.claim_next().id == old


def test_heartbeat_updates_running_task_only(
    service: Any, db: Database, clock: FrozenClock, events: Recorder
) -> None:
    # Claimed first: claim_next takes the oldest queued task.
    running_id = _running(service, "run-2")
    queued_id = _enqueue(service, "run-1")
    assert running_id != queued_id
    clock.advance(timedelta(seconds=30))
    events.changes.clear()

    assert service.heartbeat(running_id, progress=40) is True
    assert service.heartbeat(queued_id) is False
    assert service.heartbeat("task-9999") is False

    stored = _raw(db, running_id)
    assert stored["heartbeat_at"] == clock.now().isoformat()
    assert stored["progress"] == 40
    assert _raw(db, queued_id)["heartbeat_at"] is None
    # Heartbeats are not state changes (M2.6 note for M2.18).
    assert events.changes == []


def test_finish_succeeded_stores_result_ref_and_publishes(
    service: Any, db: Database, clock: FrozenClock, events: Recorder
) -> None:
    task_id = _running(service)
    clock.advance(timedelta(seconds=45))
    events.changes.clear()

    assert service.finish(task_id, _status("succeeded"), result_ref="draft-1") is True

    stored = _raw(db, task_id)
    assert stored["status"] == "succeeded"
    assert stored["result_ref"] == "draft-1"
    assert stored["error"] is None
    assert stored["finished_at"] == clock.now().isoformat()
    assert events.triples() == [(task_id, TASK_TYPE, "succeeded")]


@pytest.mark.parametrize("state", ["queued", "succeeded", "failed", "cancelled", "interrupted"])
@pytest.mark.parametrize("target", ["succeeded", "failed", "cancelled", "interrupted"])
def test_finish_refused_from_wrong_state(
    service: Any, db: Database, events: Recorder, state: str, target: str
) -> None:
    task_id = _enqueue(service) if state == "queued" else _ended(service, state)
    before = _snapshot(db)
    events.changes.clear()

    error = NetworkUnavailable() if target == "failed" else None  # a valid call otherwise
    assert service.finish(task_id, _status(target), error=error) is False

    assert _snapshot(db) == before
    assert events.changes == []


def test_finish_missing_task_returns_false(service: Any, events: Recorder) -> None:
    assert service.finish("task-9999", _status("failed"), error=NetworkUnavailable()) is False
    assert events.changes == []


@pytest.mark.parametrize("target", ["queued", "running"])
def test_finish_rejects_non_terminal_status(service: Any, db: Database, target: str) -> None:
    task_id = _running(service)
    before = _snapshot(db)

    with pytest.raises(ValueError):
        service.finish(task_id, _status(target))

    assert _snapshot(db) == before


@pytest.mark.parametrize(
    ("target", "with_error"),
    [("succeeded", True), ("cancelled", True), ("failed", False)],
)
def test_finish_rejects_error_that_does_not_fit_the_status(
    service: Any, db: Database, events: Recorder, target: str, with_error: bool
) -> None:
    task_id = _running(service)
    before = _snapshot(db)
    events.changes.clear()
    error = NetworkUnavailable() if with_error else None

    with pytest.raises(ValueError):
        service.finish(task_id, _status(target), error=error)

    assert _snapshot(db) == before
    assert events.changes == []


@pytest.mark.parametrize("with_error", [True, False])
def test_finish_interrupted_may_carry_an_error_or_none(
    service: Any, db: Database, with_error: bool
) -> None:
    task_id = _running(service)
    error = NetworkUnavailable() if with_error else None

    assert service.finish(task_id, _status("interrupted"), error=error) is True

    assert _raw(db, task_id)["error"] == ("NetworkUnavailable" if with_error else None)


# --- reads -------------------------------------------------------------------------


def test_get_and_recent(service: Any, clock: FrozenClock) -> None:
    a = _enqueue(service, "run-1")
    clock.advance(timedelta(seconds=1))
    b = _enqueue(service, "run-2")
    clock.advance(timedelta(seconds=1))
    c = _enqueue(service, "run-3")

    assert service.get("task-9999") is None
    assert service.get(a).id == a
    assert [row.id for row in service.recent(2)] == [c, b]
    assert [row.id for row in service.recent(10)] == [c, b, a]


# --- injected clock and ids ------------------------------------------------------------


def test_uses_injected_clock_and_ids(service: Any, db: Database, clock: FrozenClock) -> None:
    created_at = clock.now().isoformat()
    task_id = _enqueue(service, "run-1")
    assert task_id == "task-0001"
    stored = _raw(db, task_id)
    assert stored["created_at"] == created_at
    assert stored["updated_at"] == created_at

    clock.advance(timedelta(seconds=7))
    claimed_at = clock.now().isoformat()
    service.claim_next()
    stored = _raw(db, task_id)
    assert stored["updated_at"] == claimed_at
    assert stored["heartbeat_at"] == claimed_at

    clock.advance(timedelta(seconds=7))
    beat_at = clock.now().isoformat()
    assert service.heartbeat(task_id) is True
    assert _raw(db, task_id)["heartbeat_at"] == beat_at

    clock.advance(timedelta(seconds=7))
    finished_at = clock.now().isoformat()
    assert service.finish(task_id, _status("interrupted")) is True
    stored = _raw(db, task_id)
    assert stored["finished_at"] == finished_at
    assert stored["updated_at"] == finished_at

    assert _enqueue(service, "run-2") == "task-0002"


# --- the enqueue interface for other services (ADR-034) -------------------------------------


CONFORMANCE = """\
from app.domain.tasks import TaskEnqueuer, TaskRef
from app.services.tasks import TaskService


def start(enqueuer: TaskEnqueuer) -> TaskRef:
    return enqueuer.enqueue("practice", "run-1", input_ref=None)


def check(service: TaskService) -> None:
    enqueuer: TaskEnqueuer = service
    ref = start(enqueuer)
    task_id: str = ref.id
    created: bool = ref.created
"""


def test_task_service_satisfies_task_enqueuer_protocol(tmp_path: Path) -> None:
    sample = tmp_path / "conformance.py"
    sample.write_text(CONFORMANCE, encoding="utf-8")
    # Fixed argv to the venv's own interpreter; no shell, no untrusted input.
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--no-incremental",
            "--cache-dir",
            str(tmp_path / ".mypy_cache"),
            str(sample),
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "MYPYPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
