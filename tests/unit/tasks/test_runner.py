"""Task runner core (M2.8; AC-M2.2 no automatic retry; spec 5 M2 reliability rules 1, 4, 9;
ADR-034, ADR-038, ADR-076 items 10, 13).

API assumed here (``app.tasks``; ``app.tasks.handlers``; ``app.domain.settings``):

- ``TaskRunner(registry=HandlerRegistry())`` runs on the event loop. ``start()``/``stop()`` are
  idempotent and ``status`` is "running" or "stopped". It idles until
  ``attach(service, settings=TaskRunnerSettings(...))`` hands it a ``TaskService``; ``attach``
  may be called before ``start`` or from any thread (step 1 opens the workspace in a worker
  thread). ``notify()`` wakes it after an enqueue and is safe from any thread; the app wires the
  service's ``on_change`` to call it for queued tasks.
- A semaphore of ``settings.max_concurrent`` limits running handlers, taken before a claim, so
  no task is claimed (made running) while it would wait for a slot.
- ``HandlerRegistry().register(task_type, TaskTypeSpec, handler)``; ``get(task_type)`` gives
  ``(spec, handler)`` or None; empty by default.
- A handler is ``async def handler(ctx: TaskContext) -> str | None`` (the result_ref). The
  context has ``task_id``, ``task_type``, ``input_ref``, ``attempt``, ``cancel_flag``
  (a ``CancelFlag``) and ``async heartbeat(progress=None)``.
- Return -> finish succeeded with the result_ref; an ``AppError`` -> failed with its kind; any
  other exception -> failed with a generic AppError kind (never the exception's text); an
  unregistered type -> failed plainly. Never retried automatically. ``stop()`` cancels running
  handlers and finishes them interrupted before returning.
- Every service call goes through ``asyncio.to_thread``: no ``transaction()`` on the loop thread.

The start/stop checks of the M0.13 stub tests are kept (ADR-076 item 13). Real migrated SQLite
in a temp folder, ``FrozenClock`` and ``SequentialIdFactory``; handlers are gated by
``asyncio.Event``; every wait has a deadline (``asyncio.wait_for``), never a fixed sleep. Imports
of the new names are inside helpers so each test fails on its own while they are missing. All
values are fictional.
"""

import asyncio
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.domain.errors import AppError, NetworkUnavailable
from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory

START = datetime(2026, 3, 14, 9, 15, 0, tzinfo=UTC)
DEADLINE_S = 10.0
ECHO = "fictional_echo"
OTHER = "fictional_other"
FICTIONAL_FAILURE_TEXT = "Fictional handler failure for Ada Example at Nowhere Ltd"


# --- helpers ------------------------------------------------------------------------------


def _other_tasks() -> set[asyncio.Task[object]]:
    return {task for task in asyncio.all_tasks() if task is not asyncio.current_task()}


def _status(value: str) -> Any:
    from app.domain.tasks import TaskStatus

    return TaskStatus(value)


def _spec(label: str = "Fictional echo") -> Any:
    from app.domain.tasks import TaskTypeSpec

    return TaskTypeSpec(timeout=timedelta(minutes=5), label=label)


def _settings(max_concurrent: int = 1) -> Any:
    from app.domain.settings import TaskRunnerSettings

    return TaskRunnerSettings(max_concurrent=max_concurrent)


def _registry(handlers: dict[str, Any]) -> Any:
    from app.tasks.handlers import HandlerRegistry

    registry = HandlerRegistry()
    for task_type, handler in handlers.items():
        registry.register(task_type, _spec(f"Label for {task_type}"), handler)
    return registry


def _runner(handlers: dict[str, Any] | None = None) -> Any:
    from app.tasks import TaskRunner

    return TaskRunner(registry=_registry(handlers or {}))


def _known_error_kinds() -> set[str]:
    kinds: set[str] = set()
    stack: list[type[AppError]] = [AppError]
    while stack:
        cls = stack.pop()
        kinds.add(cls.kind)
        stack.extend(cls.__subclasses__())
    return kinds


async def _settle(rounds: int = 25) -> None:
    """Give the runner many chances to act (worker-thread round trips and loop turns); no
    fixed-time sleep."""
    for _ in range(rounds):
        await asyncio.to_thread(lambda: None)
        await asyncio.sleep(0)


class Changes:
    """Records ``TaskChange`` events from any thread and lets tests await a status."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._lock = threading.Lock()
        self._items: list[Any] = []
        self._event = asyncio.Event()

    def __call__(self, change: Any) -> None:
        with self._lock:
            self._items.append(change)
        self._loop.call_soon_threadsafe(self._event.set)

    def statuses(self, task_id: str) -> list[str]:
        with self._lock:
            return [str(c.status) for c in self._items if c.id == task_id]

    async def wait_status(self, task_id: str, status: str) -> None:
        async def until() -> None:
            while True:
                self._event.clear()
                if status in self.statuses(task_id):
                    return
                await self._event.wait()

        try:
            await asyncio.wait_for(until(), DEADLINE_S)
        except TimeoutError:
            raise AssertionError(
                f"task {task_id} never reached {status}; saw {self.statuses(task_id)}"
            ) from None


class Gated:
    """An async handler that blocks each call until the test releases it."""

    def __init__(self, result: str | None = "fictional-result-ref") -> None:
        self.result = result
        self.calls: list[Any] = []
        self.threads: list[int] = []
        self.gates: list[asyncio.Event] = []
        self.cancelled: list[str] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.open = False
        self._started = asyncio.Event()

    async def __call__(self, ctx: Any) -> str | None:
        gate = asyncio.Event()
        if self.open:
            gate.set()
        self.calls.append(ctx)
        self.threads.append(threading.get_ident())
        self.gates.append(gate)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self._started.set()
        try:
            await gate.wait()
        except asyncio.CancelledError:
            self.cancelled.append(ctx.task_id)
            raise
        finally:
            self.in_flight -= 1
        return self.result

    def release_all(self) -> None:
        self.open = True
        for gate in self.gates:
            gate.set()

    async def wait_started(self, count: int) -> None:
        async def until() -> None:
            while True:
                self._started.clear()
                if len(self.calls) >= count:
                    return
                await self._started.wait()

        try:
            await asyncio.wait_for(until(), DEADLINE_S)
        except TimeoutError:
            raise AssertionError(f"{count} handler calls expected, saw {len(self.calls)}") from None


class Raising:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls: list[Any] = []

    async def __call__(self, ctx: Any) -> str | None:
        self.calls.append(ctx)
        raise self.error


class ThreadSpy:
    """Wraps a service and records which thread each method call runs on."""

    def __init__(self, service: Any) -> None:
        self._service = service
        self.calls: list[tuple[str, int]] = []

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._service, name)
        if not callable(attribute):
            return attribute

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, threading.get_ident()))
            return attribute(*args, **kwargs)

        return wrapper


class LoopGuardDatabase:
    """A ``Database`` stand-in whose ``transaction()`` refuses the event-loop thread."""

    def __init__(self, db: Database, loop_thread: int) -> None:
        self._db = db
        self._loop_thread = loop_thread
        self.entries: list[bool] = []  # True when entered on the loop thread

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        on_loop = threading.get_ident() == self._loop_thread
        self.entries.append(on_loop)
        if on_loop:
            raise AssertionError("transaction() entered on the event-loop thread")
        with self._db.transaction() as conn:
            yield conn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)


class Harness:
    """A real ``TaskService`` on migrated SQLite; its ``on_change`` records the change and
    wakes the runner for queued tasks, as the app wires it."""

    def __init__(self, db: Any, handlers: dict[str, Any]) -> None:
        from app.services.tasks import TaskService

        self.loop = asyncio.get_running_loop()
        self.changes = Changes(self.loop)
        self.runner = _runner(handlers)
        self.wake_on_enqueue = True
        self.service = TaskService(
            db, FrozenClock(START), SequentialIdFactory("task"), self._on_change
        )

    def _on_change(self, change: Any) -> None:
        self.changes(change)
        if self.wake_on_enqueue and str(change.status) == "queued":
            self.runner.notify()

    async def enqueue(
        self, task_type: str = ECHO, target: str = "run-1", input_ref: str | None = None
    ) -> str:
        ref = await asyncio.to_thread(self.service.enqueue, task_type, target, input_ref=input_ref)
        assert ref.created is True
        task_id: str = ref.id
        return task_id

    async def row(self, task_id: str) -> Any:
        row = await asyncio.to_thread(self.service.get, task_id)
        assert row is not None
        return row


def _open_migrated(tmp_path: Path) -> Database:
    database = Database.open(tmp_path / "data" / "app.db")
    apply_migrations(database, MIGRATIONS_DIR, tmp_path / "backups", FrozenClock(START))
    return database


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    (tmp_path / "data").mkdir()
    database = _open_migrated(tmp_path)
    try:
        yield database
    finally:
        database.close()


async def _count(db: Database, status: str) -> int:
    def count() -> int:
        with db.transaction() as conn:
            value: int = conn.execute(
                "SELECT count(*) FROM tasks WHERE status = ?", (status,)
            ).fetchone()[0]
            return value

    return await asyncio.to_thread(count)


# --- start / stop (kept from the M0.13 stub tests; ADR-076 item 13) ----------------------


async def test_start_stop_idempotent() -> None:
    runner = _runner()
    assert runner.status == "stopped"

    await runner.start()
    await runner.start()
    assert runner.status == "running"
    assert len(_other_tasks()) == 1, "starting twice must create exactly one worker"

    await runner.stop()
    await runner.stop()
    assert runner.status == "stopped"
    assert _other_tasks() == set(), "stop must signal and await the worker"


async def test_runner_can_restart_after_stop() -> None:
    runner = _runner()
    await runner.start()
    await runner.stop()

    await runner.start()
    assert runner.status == "running"
    assert len(_other_tasks()) == 1

    await runner.stop()
    assert runner.status == "stopped"
    assert _other_tasks() == set()


async def test_runner_stop_before_start_is_fine() -> None:
    runner = _runner()
    await runner.stop()
    assert runner.status == "stopped"
    assert _other_tasks() == set()


async def test_runner_worker_stays_idle_until_stopped() -> None:
    runner = _runner()
    await runner.start()
    for _ in range(5):
        await asyncio.sleep(0)  # yield to the worker; it must not finish on its own
    assert runner.status == "running"
    (worker,) = _other_tasks()
    assert not worker.done()
    await runner.stop()
    assert worker.done()


async def test_cancelling_stop_is_not_swallowed() -> None:
    runner = _runner()
    await runner.start()
    stopper = asyncio.create_task(runner.stop())
    await asyncio.sleep(0)  # let stop() reach its await on the worker
    stopper.cancel()

    with pytest.raises(asyncio.CancelledError):
        await stopper


# --- handlers registry and settings -------------------------------------------------------


def test_handler_registry_starts_empty_and_refuses_duplicates() -> None:
    from app.tasks.handlers import HandlerRegistry

    registry = HandlerRegistry()
    assert registry.get("practice") is None

    handler = Gated()
    spec = _spec()
    registry.register(ECHO, spec, handler)
    got_spec, got_handler = registry.get(ECHO)
    assert got_spec is spec
    assert got_handler is handler
    with pytest.raises(ValueError):
        registry.register(ECHO, _spec("Another"), Gated())


def test_handler_registry_refuses_type_with_separator() -> None:
    from app.tasks.handlers import HandlerRegistry

    with pytest.raises(ValueError):
        HandlerRegistry().register("fictional:echo", _spec(), Gated())


# --- running tasks ------------------------------------------------------------------------


async def test_runner_runs_queued_task_to_succeeded(db: Database) -> None:
    from app.domain.tasks import CancelFlag

    handler = Gated(result="fictional-draft-0001")
    handler.release_all()
    h = Harness(db, {ECHO: handler})
    task_id = await h.enqueue(input_ref="fictional-input-7")

    h.runner.attach(h.service, settings=_settings())  # before start, as the lifespan does
    await h.runner.start()
    try:
        await h.changes.wait_status(task_id, "succeeded")
    finally:
        await h.runner.stop()

    row = await h.row(task_id)
    assert row.status == _status("succeeded")
    assert row.result_ref == "fictional-draft-0001"
    assert row.error is None
    assert row.attempt == 1
    assert h.changes.statuses(task_id) == ["queued", "running", "succeeded"]

    (ctx,) = handler.calls
    assert ctx.task_id == task_id
    assert ctx.task_type == ECHO
    assert ctx.input_ref == "fictional-input-7"
    assert ctx.attempt == 1
    assert isinstance(ctx.cancel_flag, CancelFlag)
    assert ctx.cancel_flag.cancelled is False
    assert handler.threads == [threading.get_ident()], "handlers run on the event loop"


async def test_runner_idles_without_workspace_then_runs_after_open(db: Database) -> None:
    handler = Gated()
    handler.release_all()
    h = Harness(db, {ECHO: handler})
    await h.runner.start()
    try:
        task_id = await h.enqueue()  # wakes the runner, which has no service yet
        await _settle()
        assert h.runner.status == "running"
        assert handler.calls == []
        assert (await h.row(task_id)).status == _status("queued")

        # Step 1 opens the workspace in a worker thread and hands the service over from there.
        await asyncio.to_thread(h.runner.attach, h.service, settings=_settings())
        await h.changes.wait_status(task_id, "succeeded")
    finally:
        await h.runner.stop()
    assert len(handler.calls) == 1


async def test_runner_detach_leaves_the_worker_idle_until_the_next_attach(db: Database) -> None:
    """Delete all data closes the workspace: the runner lets go of its service, keeps running
    and takes the next workspace's service."""
    handler = Gated()
    handler.release_all()
    h = Harness(db, {ECHO: handler})
    h.runner.attach(h.service, settings=_settings())
    await h.runner.start()
    try:
        await asyncio.to_thread(h.runner.detach)
        task_id = await h.enqueue()  # wakes the runner, which has no service any more
        await _settle()
        assert h.runner.status == "running"
        assert handler.calls == []
        assert (await h.row(task_id)).status == _status("queued")

        await asyncio.to_thread(h.runner.attach, h.service, settings=_settings())
        await h.changes.wait_status(task_id, "succeeded")
    finally:
        await h.runner.stop()
    assert len(handler.calls) == 1


async def test_runner_wakes_on_enqueue(db: Database) -> None:
    handler = Gated()
    handler.release_all()
    h = Harness(db, {ECHO: handler})
    h.runner.attach(h.service, settings=_settings())
    await h.runner.start()
    try:
        await _settle()  # the runner found nothing and is waiting
        first = await h.enqueue(target="run-1")
        await h.changes.wait_status(first, "succeeded")

        # An explicit notify() from another thread wakes it too.
        h.wake_on_enqueue = False
        await _settle()
        second = await h.enqueue(target="run-2")
        await asyncio.to_thread(h.runner.notify)
        await h.changes.wait_status(second, "succeeded")
    finally:
        await h.runner.stop()
    assert [ctx.task_id for ctx in handler.calls] == [first, second]


@pytest.mark.parametrize("limit", [1, 2, 3])
async def test_concurrency_limit_respected(db: Database, limit: int) -> None:
    handler = Gated()
    h = Harness(db, {ECHO: handler})
    task_ids = [await h.enqueue(target=f"run-{n}") for n in range(4)]
    h.runner.attach(h.service, settings=_settings(limit))
    await h.runner.start()
    try:
        await handler.wait_started(limit)
        await _settle()
        assert len(handler.calls) == limit, "no more handlers than the limit"
        assert await _count(db, "running") == limit, "no task claimed while waiting for a slot"
        assert await _count(db, "queued") == 4 - limit

        while len(handler.calls) < len(task_ids):
            started = len(handler.calls)
            next(gate for gate in handler.gates if not gate.is_set()).set()
            await handler.wait_started(started + 1)
            await _settle()
            assert handler.in_flight <= limit
        handler.release_all()
        for task_id in task_ids:
            await h.changes.wait_status(task_id, "succeeded")
    finally:
        await h.runner.stop()
    assert handler.max_in_flight == limit


async def test_failed_handler_records_error_kind_and_never_retries(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    known = Raising(NetworkUnavailable(detail=FICTIONAL_FAILURE_TEXT))
    unexpected = Raising(RuntimeError(FICTIONAL_FAILURE_TEXT))
    fine = Gated()
    fine.release_all()
    h = Harness(db, {"fictional_known": known, "fictional_unexpected": unexpected, ECHO: fine})
    h.runner.attach(h.service, settings=_settings())
    await h.runner.start()
    try:
        known_id = await h.enqueue("fictional_known", "run-1")
        await h.changes.wait_status(known_id, "failed")
        unexpected_id = await h.enqueue("fictional_unexpected", "run-1")
        await h.changes.wait_status(unexpected_id, "failed")
        later_id = await h.enqueue(ECHO, "run-1")
        await h.changes.wait_status(later_id, "succeeded")
        await _settle()
    finally:
        await h.runner.stop()

    known_row = await h.row(known_id)
    assert known_row.status == _status("failed")
    assert known_row.error == NetworkUnavailable.kind
    assert known_row.result_ref is None

    unexpected_row = await h.row(unexpected_id)
    assert unexpected_row.status == _status("failed")
    assert unexpected_row.error in _known_error_kinds(), "a generic AppError kind"
    assert "RuntimeError" not in unexpected_row.error
    assert FICTIONAL_FAILURE_TEXT not in unexpected_row.error

    for task_id, handler in ((known_id, known), (unexpected_id, unexpected)):
        assert len(handler.calls) == 1, "never retried automatically"
        assert (await h.row(task_id)).attempt == 1
        assert h.changes.statuses(task_id) == ["queued", "running", "failed"]
    assert FICTIONAL_FAILURE_TEXT not in caplog.text


async def test_unknown_task_type_fails_plainly(db: Database) -> None:
    handler = Gated()
    handler.release_all()
    h = Harness(db, {ECHO: handler})
    h.runner.attach(h.service, settings=_settings())
    await h.runner.start()
    try:
        unknown_id = await h.enqueue("fictional_unregistered", "run-1")
        await h.changes.wait_status(unknown_id, "failed")
        known_id = await h.enqueue(ECHO, "run-1")
        await h.changes.wait_status(known_id, "succeeded")  # the runner keeps going
    finally:
        await h.runner.stop()

    row = await h.row(unknown_id)
    assert row.status == _status("failed")
    assert row.error in _known_error_kinds()
    assert "fictional_unregistered" not in row.error
    assert row.attempt == 1
    assert [ctx.task_id for ctx in handler.calls] == [known_id]


async def test_db_calls_run_off_the_event_loop_thread(db: Database) -> None:
    progress_seen: list[bool] = []

    async def beating(ctx: Any) -> str:
        await ctx.heartbeat(progress=40)
        await ctx.heartbeat()
        progress_seen.append(True)
        return "fictional-result-ref"

    h = Harness(db, {ECHO: beating, OTHER: Raising(NetworkUnavailable())})
    spy = ThreadSpy(h.service)
    h.runner.attach(spy, settings=_settings())
    await h.runner.start()
    try:
        ok_id = await h.enqueue(ECHO, "run-1")
        await h.changes.wait_status(ok_id, "succeeded")
        failed_id = await h.enqueue(OTHER, "run-1")
        await h.changes.wait_status(failed_id, "failed")
    finally:
        await h.runner.stop()

    loop_thread = threading.get_ident()
    names = {name for name, _ in spy.calls}
    assert {"claim_next", "heartbeat", "finish"} <= names
    on_loop = [name for name, thread in spy.calls if thread == loop_thread]
    assert on_loop == [], f"service calls made on the event-loop thread: {on_loop}"
    assert progress_seen == [True]
    assert (await h.row(ok_id)).progress == 40


async def test_transaction_never_entered_on_loop_thread(db: Database) -> None:
    async def beating(ctx: Any) -> str:
        await ctx.heartbeat(progress=10)
        return "fictional-result-ref"

    blocked = Gated()
    guard = LoopGuardDatabase(db, threading.get_ident())
    h = Harness(guard, {ECHO: beating, OTHER: Raising(RuntimeError("fictional")), "slow": blocked})
    h.runner.attach(h.service, settings=_settings(2))
    await h.runner.start()
    try:
        ok_id = await h.enqueue(ECHO, "run-1")
        await h.changes.wait_status(ok_id, "succeeded")
        failed_id = await h.enqueue(OTHER, "run-1")
        await h.changes.wait_status(failed_id, "failed")
        unknown_id = await h.enqueue("fictional_unregistered", "run-1")
        await h.changes.wait_status(unknown_id, "failed")
        slow_id = await h.enqueue("slow", "run-1")
        await blocked.wait_started(1)
    finally:
        await h.runner.stop()  # finishing as interrupted is a database call too

    assert (await h.row(slow_id)).status == _status("interrupted")
    assert guard.entries, "the service used the database"
    assert not any(guard.entries), "transaction() was entered on the event-loop thread"


async def test_stop_cancels_running_and_marks_interrupted(db: Database) -> None:
    handler = Gated()
    h = Harness(db, {ECHO: handler})
    h.runner.attach(h.service, settings=_settings(2))
    await h.runner.start()
    running = [await h.enqueue(target=f"run-{n}") for n in range(2)]
    await handler.wait_started(2)

    await h.runner.stop()

    assert h.runner.status == "stopped"
    assert sorted(handler.cancelled) == sorted(running), "running handlers were cancelled"
    for task_id in running:
        row = await h.row(task_id)
        assert row.status == _status("interrupted")
        assert row.attempt == 1
        assert row.result_ref is None
    assert _other_tasks() == set(), "no handler task outlives stop()"


async def test_stop_leaves_queued_tasks_queued(db: Database) -> None:
    handler = Gated()
    h = Harness(db, {ECHO: handler})
    h.runner.attach(h.service, settings=_settings(1))
    await h.runner.start()
    first = await h.enqueue(target="run-1")
    second = await h.enqueue(target="run-2")
    await handler.wait_started(1)

    await h.runner.stop()

    assert (await h.row(first)).status == _status("interrupted")
    assert (await h.row(second)).status == _status("queued"), "queued tasks resume next time"
    assert len(handler.calls) == 1


async def test_attach_from_another_thread_is_thread_safe(db: Database) -> None:
    """The hand-off from step 1 happens off the loop; it must reach the runner on the loop."""
    handler = Gated()
    handler.release_all()
    h = Harness(db, {ECHO: handler})
    task_id = await h.enqueue()
    await h.runner.start()
    try:
        done = threading.Event()

        def from_thread() -> None:
            h.runner.attach(h.service, settings=_settings())
            done.set()

        thread = threading.Thread(target=from_thread)
        thread.start()
        await asyncio.wait_for(asyncio.to_thread(done.wait, DEADLINE_S), DEADLINE_S)
        thread.join(DEADLINE_S)
        await h.changes.wait_status(task_id, "succeeded")
    finally:
        await h.runner.stop()
    assert handler.threads == [threading.get_ident()]


# --- code review of M2.8: finishing races, handler cancellation, claim errors --------------


class BlockingSucceed:
    """Wraps a service; ``finish(succeeded)`` blocks in its worker thread until released."""

    def __init__(self, service: Any) -> None:
        self._service = service
        self.entered = threading.Event()
        self.release = threading.Event()

    def finish(self, task_id: str, status: Any, **kwargs: Any) -> bool:
        if str(status) == "succeeded":
            self.entered.set()
            assert self.release.wait(DEADLINE_S), "the test never released finish()"
        result: bool = self._service.finish(task_id, status, **kwargs)
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._service, name)


async def test_stop_keeps_result_of_handler_that_already_returned(db: Database) -> None:
    handler = Gated(result="fictional-paid-for-result")
    handler.release_all()
    h = Harness(db, {ECHO: handler})
    blocking = BlockingSucceed(h.service)
    h.runner.attach(blocking, settings=_settings())
    await h.runner.start()
    task_id = await h.enqueue()
    await asyncio.wait_for(asyncio.to_thread(blocking.entered.wait, DEADLINE_S), DEADLINE_S)

    stopper = asyncio.create_task(h.runner.stop())
    await _settle()
    blocking.release.set()
    await asyncio.wait_for(stopper, DEADLINE_S)

    row = await h.row(task_id)
    assert row.status == _status("succeeded"), "a returned result is never thrown away"
    assert row.result_ref == "fictional-paid-for-result"
    assert _other_tasks() == set()


class FictionalAbort(BaseException):
    """A ``BaseException`` that is not an ``Exception`` (fictional)."""


@pytest.mark.parametrize(
    "raised", [asyncio.CancelledError(), FictionalAbort()], ids=["cancelled_error", "base"]
)
async def test_handler_raising_base_exception_never_leaves_task_running(
    db: Database, raised: BaseException
) -> None:
    from app.domain.errors import TaskFailed

    fine = Gated()
    fine.release_all()
    h = Harness(db, {OTHER: Raising(raised), ECHO: fine})
    h.runner.attach(h.service, settings=_settings())
    await h.runner.start()
    try:
        bad_id = await h.enqueue(OTHER, "run-1")
        await h.changes.wait_status(bad_id, "failed")
        later_id = await h.enqueue(ECHO, "run-1")
        await h.changes.wait_status(later_id, "succeeded")  # the runner keeps going
    finally:
        await h.runner.stop()

    row = await h.row(bad_id)
    assert row.status == _status("failed")
    assert row.error == TaskFailed.kind
    assert row.attempt == 1


class ClaimFailsOnce:
    """Wraps a service; the first ``claim_next`` raises (a fictional database error)."""

    def __init__(self, service: Any) -> None:
        self._service = service
        self.claims = 0

    def claim_next(self) -> Any:
        self.claims += 1
        if self.claims == 1:
            raise RuntimeError("fictional database is busy")
        return self._service.claim_next()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._service, name)


async def test_claim_error_retries_once_after_a_backoff(db: Database) -> None:
    from app.tasks import TaskRunner

    handler = Gated()
    handler.release_all()
    h = Harness(db, {ECHO: handler})
    h.runner = TaskRunner(registry=_registry({ECHO: handler}), claim_retry_seconds=0.01)
    task_id = await h.enqueue()  # queued before the runner starts: no wake after the error
    failing = ClaimFailsOnce(h.service)
    h.runner.attach(failing, settings=_settings())
    await h.runner.start()
    try:
        await h.changes.wait_status(task_id, "succeeded")
    finally:
        await h.runner.stop()
    assert failing.claims >= 2
    assert len(handler.calls) == 1


@pytest.mark.parametrize("seconds", [0, -1.0])
def test_claim_retry_seconds_must_be_positive(seconds: float) -> None:
    from app.tasks import TaskRunner

    with pytest.raises(ValueError):
        TaskRunner(registry=_registry({}), claim_retry_seconds=seconds)


class FinishFails:
    """Wraps a service; every ``finish`` raises (a fictional database error)."""

    def __init__(self, service: Any) -> None:
        self._service = service
        self.finishes: list[str] = []

    def finish(self, task_id: str, status: Any, **kwargs: Any) -> bool:
        self.finishes.append(task_id)
        raise RuntimeError(FICTIONAL_FAILURE_TEXT)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._service, name)


async def test_failed_finish_is_logged_by_type_and_runner_keeps_going(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    handler = Gated()
    handler.release_all()
    h = Harness(db, {ECHO: handler})
    failing = FinishFails(h.service)
    h.runner.attach(failing, settings=_settings())
    await h.runner.start()
    try:
        first = await h.enqueue(target="run-1")
        second = await h.enqueue(target="run-2")
        await handler.wait_started(2)  # the slot came back after the failed finish
        await _settle()
    finally:
        await h.runner.stop()
    assert failing.finishes[:2] == [first, second]
    assert "RuntimeError" in caplog.text
    assert FICTIONAL_FAILURE_TEXT not in caplog.text


async def test_stop_again_after_a_cancelled_stop_interrupts_running_tasks(db: Database) -> None:
    handler = Gated()
    h = Harness(db, {ECHO: handler})
    h.runner.attach(h.service, settings=_settings())
    await h.runner.start()
    task_id = await h.enqueue()
    await handler.wait_started(1)

    stopper = asyncio.create_task(h.runner.stop())
    await asyncio.sleep(0)
    stopper.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopper

    await h.runner.stop()
    assert h.runner.status == "stopped"
    assert handler.cancelled == [task_id]
    assert (await h.row(task_id)).status == _status("interrupted")
    assert _other_tasks() == set()
