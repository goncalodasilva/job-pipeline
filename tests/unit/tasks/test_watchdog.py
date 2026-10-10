"""Heartbeat and watchdog (M2.10; AC-M2.3; spec 5 M2 reliability rule 5; ADR-076).

API assumed here (proposed for the implementer):

- ``TaskContext.heartbeat(progress)`` clamps progress to 0..100 before the database write, so
  an out-of-range value never fails the task. Heartbeats come only from the handler (and,
  through it, provider activity): the runner keeps no free-running heartbeat timer, so a hung
  call stops them.
- ``HandlerRegistry(min_timeout=timedelta(...))`` (default: no minimum) refuses, with
  ``ValueError``, a ``TaskTypeSpec`` whose timeout is not longer than ``min_timeout``. Wiring
  supplies it, so ``app.tasks`` never imports ``app.modules``: ``app.wiring.PROVIDER_RUN_TIMEOUT``
  (a ``timedelta``, 5 minutes) is the overall limit on one provider run that M2.13 injects
  into the Claude provider's ``run``; ``create_app`` applies it to any registry.
- ``TaskService.interrupt_stale(task_type, timeout) -> list[str]``: one conditional update
  (``status = 'running' AND type = ? AND heartbeat_at < now - timeout``, ``now`` from the
  service's injected Clock) moving stale tasks to interrupted; returns the ids changed and
  publishes each change after the commit. A strictly-older heartbeat is stale; one exactly at
  the cutoff is not.
- ``TaskRunner(registry=..., watchdog_interval=seconds, sleep=async callable)``: while started,
  a watchdog loop awaits ``sleep(watchdog_interval)`` then runs a check, until ``stop()``.
  ``await runner.check_watchdog_once() -> list[str]`` runs one check now: for each registered
  type, ``service.interrupt_stale(type, spec.timeout)`` in a worker thread, then cancels the
  local handler of every interrupted id. A handler cancelled this way never overwrites
  interrupted (its later finish is refused by the conditional update), and its slot is freed.
  With no workspace attached the check does nothing.

Same style as test_runner.py: real migrated SQLite in a temp folder, ``FrozenClock``,
``SequentialIdFactory``, waits on conditions with deadlines, no fixed sleeps. All values are
fictional.
"""

import asyncio
import logging
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from app.domain.errors import NetworkUnavailable
from app.modules.storage import Database
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory
from tests.unit.tasks.test_runner import (
    DEADLINE_S,
    START,
    Changes,
    Gated,
    _open_migrated,
    _other_tasks,
    _settings,
    _settle,
    _status,
)

SHORT = "fictional_short"
LONG = "fictional_long"
SHORT_TIMEOUT = timedelta(minutes=2)
LONG_TIMEOUT = timedelta(minutes=10)
ONE_SECOND = timedelta(seconds=1)


# --- helpers ------------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    (tmp_path / "data").mkdir()
    database = _open_migrated(tmp_path)
    try:
        yield database
    finally:
        database.close()


def _spec(timeout: timedelta, label: str = "Fictional task") -> Any:
    from app.domain.tasks import TaskTypeSpec

    return TaskTypeSpec(timeout=timeout, label=label)


async def _wait_until(condition: Any, what: str) -> None:
    async def until() -> None:
        while not condition():
            await asyncio.to_thread(lambda: None)
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(until(), DEADLINE_S)
    except TimeoutError:
        raise AssertionError(f"never happened: {what}") from None


class Ticks:
    """An injected ``sleep``: records each requested delay and returns only when the test
    calls ``tick()``."""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self._ticks: asyncio.Queue[None] = asyncio.Queue()

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        await self._ticks.get()

    def tick(self) -> None:
        self._ticks.put_nowait(None)

    async def wait_asked(self, count: int) -> None:
        await _wait_until(lambda: len(self.delays) >= count, f"{count} watchdog sleeps")


class Stepper:
    """A handler driven by the test: each ``step(progress)`` makes it call
    ``ctx.heartbeat(progress)``; ``finish()`` makes it return. Records a cancellation."""

    def __init__(self, result: str | None = "fictional-stepper-result") -> None:
        self.result = result
        self.calls: list[Any] = []
        self.cancelled: list[str] = []
        self._commands: asyncio.Queue[int | str | None] = asyncio.Queue()
        self._acks: asyncio.Queue[None] = asyncio.Queue()

    async def __call__(self, ctx: Any) -> str | None:
        self.calls.append(ctx)
        try:
            while True:
                command = await self._commands.get()
                if command == "finish":
                    return self.result
                assert command is None or isinstance(command, int)
                await ctx.heartbeat(command)
                self._acks.put_nowait(None)
        except asyncio.CancelledError:
            self.cancelled.append(ctx.task_id)
            raise

    async def wait_started(self) -> None:
        await _wait_until(lambda: len(self.calls) >= 1, "the handler started")

    async def step(self, progress: int | None) -> None:
        self._commands.put_nowait(progress)
        await asyncio.wait_for(self._acks.get(), DEADLINE_S)

    def finish(self) -> None:
        self._commands.put_nowait("finish")


class SwallowsCancel:
    """A misbehaving handler: when cancelled it returns a result, or raises an AppError,
    instead of letting the cancellation through."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.calls: list[Any] = []
        self.cancelled: list[str] = []

    async def __call__(self, ctx: Any) -> str | None:
        self.calls.append(ctx)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.append(ctx.task_id)
            if self.mode == "returns":
                return "fictional-late-result"
            raise NetworkUnavailable(detail="fictional") from None
        return None  # pragma: no cover - never released


class WatchHarness:
    """A real ``TaskService`` on migrated SQLite whose clock the test moves, and a runner
    with a registry of the given ``{type: (timeout, handler)}``."""

    def __init__(
        self,
        db: Any,
        handlers: dict[str, tuple[timedelta, Any]],
        *,
        watchdog_interval: float = 30.0,
        sleep: Any = None,
    ) -> None:
        from app.services.tasks import TaskService
        from app.tasks import TaskRunner
        from app.tasks.handlers import HandlerRegistry

        self.loop = asyncio.get_running_loop()
        self.clock = FrozenClock(START)
        self.changes = Changes(self.loop)
        registry = HandlerRegistry()
        for task_type, (timeout, handler) in handlers.items():
            registry.register(task_type, _spec(timeout, f"Label for {task_type}"), handler)
        self.ticks = sleep if sleep is not None else Ticks()
        self.runner = TaskRunner(
            registry=registry, watchdog_interval=watchdog_interval, sleep=self.ticks
        )
        self.service = TaskService(db, self.clock, SequentialIdFactory("task"), self._on_change)

    def _on_change(self, change: Any) -> None:
        self.changes(change)
        if str(change.status) == "queued":
            self.runner.notify()

    async def enqueue(self, task_type: str, target: str = "run-1") -> str:
        ref = await asyncio.to_thread(self.service.enqueue, task_type, target)
        assert ref.created is True
        task_id: str = ref.id
        return task_id

    async def row(self, task_id: str) -> Any:
        row = await asyncio.to_thread(self.service.get, task_id)
        assert row is not None
        return row

    async def start(self, max_concurrent: int = 1) -> None:
        self.runner.attach(self.service, settings=_settings(max_concurrent))
        await self.runner.start()

    async def check(self) -> list[str]:
        ids: list[str] = await self.runner.check_watchdog_once()
        return ids


# --- AC-M2.3: a task whose heartbeat stops is interrupted after its timeout ---------------


async def test_ac_m2_3_heartbeat_stops_task_interrupted_after_timeout(db: Database) -> None:
    handler = Stepper()
    h = WatchHarness(db, {SHORT: (SHORT_TIMEOUT, handler)})
    await h.start()
    try:
        task_id = await h.enqueue(SHORT)
        await handler.wait_started()
        h.clock.advance(timedelta(seconds=30))
        await handler.step(20)  # the last sign of life; then the "provider call" hangs
        beat_at = h.clock.now().isoformat()

        h.clock.advance(SHORT_TIMEOUT)  # exactly at the cutoff: not stale yet
        assert await h.check() == []
        assert (await h.row(task_id)).status == _status("running")

        h.clock.advance(ONE_SECOND)
        assert await h.check() == [task_id]
        await h.changes.wait_status(task_id, "interrupted")
    finally:
        await h.runner.stop()

    row = await h.row(task_id)
    assert row.status == _status("interrupted")
    assert row.heartbeat_at == beat_at, "nothing beat for the hung task"
    assert row.progress == 20
    assert row.result_ref is None
    assert row.attempt == 1, "never retried automatically"
    assert h.changes.statuses(task_id) == ["queued", "running", "interrupted"]


async def test_heartbeat_within_timeout_keeps_running(db: Database) -> None:
    handler = Stepper()
    h = WatchHarness(db, {SHORT: (SHORT_TIMEOUT, handler)})
    await h.start()
    try:
        task_id = await h.enqueue(SHORT)
        await handler.wait_started()
        for progress in (10, 40, 70):
            h.clock.advance(SHORT_TIMEOUT - ONE_SECOND)
            await handler.step(progress)
            h.clock.advance(SHORT_TIMEOUT - ONE_SECOND)
            assert await h.check() == [], "a task that keeps beating is never stale"
        # Long past the timeout since the claim, but never past it since a heartbeat.
        assert (await h.row(task_id)).status == _status("running")
        handler.finish()
        await h.changes.wait_status(task_id, "succeeded")
    finally:
        await h.runner.stop()
    assert handler.cancelled == []
    row = await h.row(task_id)
    assert row.status == _status("succeeded")
    assert row.result_ref == "fictional-stepper-result"


async def test_heartbeat_comes_only_from_task_progress(db: Database) -> None:
    """No free-running timer beats for a task: while the handler is silent, heartbeat_at
    stays where the handler last put it, however the clock moves."""
    handler = Stepper()
    h = WatchHarness(db, {LONG: (LONG_TIMEOUT, handler)})
    await h.start()
    try:
        task_id = await h.enqueue(LONG)
        await handler.wait_started()
        claimed_beat = (await h.row(task_id)).heartbeat_at
        for _ in range(5):
            h.clock.advance(timedelta(minutes=1))
            await _settle()
            assert (await h.row(task_id)).heartbeat_at == claimed_beat
        await handler.step(None)
        assert (await h.row(task_id)).heartbeat_at == h.clock.now().isoformat()
    finally:
        await h.runner.stop()


async def test_watchdog_uses_task_type_timeout(db: Database) -> None:
    short, long = Gated(), Gated()
    h = WatchHarness(db, {SHORT: (SHORT_TIMEOUT, short), LONG: (LONG_TIMEOUT, long)})
    await h.start(max_concurrent=2)
    try:
        short_id = await h.enqueue(SHORT)
        long_id = await h.enqueue(LONG)
        await short.wait_started(1)
        await long.wait_started(1)

        h.clock.advance(SHORT_TIMEOUT + ONE_SECOND)
        assert await h.check() == [short_id]
        await h.changes.wait_status(short_id, "interrupted")
        assert (await h.row(long_id)).status == _status("running")

        h.clock.advance(LONG_TIMEOUT - SHORT_TIMEOUT - ONE_SECOND)  # exactly LONG_TIMEOUT
        assert await h.check() == []
        h.clock.advance(ONE_SECOND)
        assert await h.check() == [long_id]
        await h.changes.wait_status(long_id, "interrupted")
    finally:
        await h.runner.stop()
    assert (await h.row(short_id)).status == _status("interrupted")
    assert (await h.row(long_id)).status == _status("interrupted")


async def test_watchdog_cancels_local_handler(db: Database) -> None:
    hung = Gated()
    h = WatchHarness(db, {SHORT: (SHORT_TIMEOUT, hung)})
    await h.start(max_concurrent=1)
    try:
        hung_id = await h.enqueue(SHORT, "run-1")
        next_id = await h.enqueue(SHORT, "run-2")  # waits for the only slot
        await hung.wait_started(1)

        h.clock.advance(SHORT_TIMEOUT + ONE_SECOND)
        assert await h.check() == [hung_id]
        await _wait_until(lambda: hung.cancelled == [hung_id], "the handler was cancelled")

        # The slot comes back: the next queued task runs.
        await hung.wait_started(2)
        assert hung.calls[1].task_id == next_id
        hung.release_all()
        await h.changes.wait_status(next_id, "succeeded")
        await _settle()
    finally:
        await h.runner.stop()

    row = await h.row(hung_id)
    assert row.status == _status("interrupted"), "interrupted, not failed or cancelled"
    assert row.error is None
    assert row.result_ref is None
    assert h.changes.statuses(hung_id) == ["queued", "running", "interrupted"]


@pytest.mark.parametrize("mode", ["returns", "raises"])
async def test_cancelled_handler_cannot_overwrite_interrupted(db: Database, mode: str) -> None:
    handler = SwallowsCancel(mode)
    h = WatchHarness(db, {SHORT: (SHORT_TIMEOUT, handler)})
    await h.start()
    try:
        task_id = await h.enqueue(SHORT)
        await _wait_until(lambda: len(handler.calls) == 1, "the handler started")
        h.clock.advance(SHORT_TIMEOUT + ONE_SECOND)
        assert await h.check() == [task_id]
        await _wait_until(lambda: handler.cancelled == [task_id], "the handler was cancelled")
        await _settle()
    finally:
        await h.runner.stop()

    row = await h.row(task_id)
    assert row.status == _status("interrupted"), "the finish after cancellation is refused"
    assert row.result_ref is None
    assert row.error is None
    assert h.changes.statuses(task_id) == ["queued", "running", "interrupted"]


async def test_watchdog_interrupts_stale_task_not_run_here(db: Database) -> None:
    """A running row with no local handler (claimed elsewhere) is still interrupted, and
    cancelling 'its' handler is a quiet no-op."""
    hung = Gated()
    h = WatchHarness(db, {SHORT: (SHORT_TIMEOUT, hung)})
    await h.start(max_concurrent=1)
    try:
        local_id = await h.enqueue(SHORT, "run-1")
        await hung.wait_started(1)
        h.runner.notify = lambda: None  # type: ignore[method-assign]  # keep the runner off it
        foreign_id = await h.enqueue(SHORT, "run-2")
        claimed = await asyncio.to_thread(h.service.claim_next)
        assert claimed is not None and claimed.id == foreign_id

        h.clock.advance(SHORT_TIMEOUT + ONE_SECOND)
        assert sorted(await h.check()) == sorted([local_id, foreign_id])
        await _wait_until(lambda: hung.cancelled == [local_id], "the local handler was cancelled")
    finally:
        await h.runner.stop()
    assert (await h.row(local_id)).status == _status("interrupted")
    assert (await h.row(foreign_id)).status == _status("interrupted")


async def test_success_beats_watchdog_race(db: Database) -> None:
    handler = Stepper(result="fictional-draft-0042")
    h = WatchHarness(db, {SHORT: (SHORT_TIMEOUT, handler)})
    await h.start()
    try:
        task_id = await h.enqueue(SHORT)
        await handler.wait_started()
        h.clock.advance(SHORT_TIMEOUT + timedelta(minutes=5))  # stale by the clock...
        handler.finish()  # ...but the handler's success commits first
        await h.changes.wait_status(task_id, "succeeded")

        assert await h.check() == [], "the watchdog's conditional update finds no row"
    finally:
        await h.runner.stop()

    row = await h.row(task_id)
    assert row.status == _status("succeeded")
    assert row.result_ref == "fictional-draft-0042"
    assert h.changes.statuses(task_id) == ["queued", "running", "succeeded"]


async def test_watchdog_leaves_queued_tasks_alone(db: Database) -> None:
    h = WatchHarness(db, {SHORT: (SHORT_TIMEOUT, Gated())})
    queued_id = await h.enqueue(SHORT, "run-1")
    h.clock.advance(timedelta(hours=1))
    assert await h.check() == [], "no workspace attached: nothing to check"

    h.runner.attach(h.service, settings=_settings())  # attached but not started: no claim
    assert await h.check() == []
    assert (await h.row(queued_id)).status == _status("queued")


async def test_interrupt_stale_is_one_conditional_update(db: Database) -> None:
    """The service-level guard: status and heartbeat are checked in the update itself, so
    a heartbeat that lands before it keeps the task running."""
    h = WatchHarness(db, {SHORT: (SHORT_TIMEOUT, Gated())})
    task_id = await h.enqueue(SHORT)
    claimed = await asyncio.to_thread(h.service.claim_next)
    assert claimed is not None and claimed.id == task_id

    h.clock.advance(SHORT_TIMEOUT + ONE_SECOND)
    assert await asyncio.to_thread(h.service.heartbeat, task_id, progress=5) is True
    assert await asyncio.to_thread(h.service.interrupt_stale, SHORT, SHORT_TIMEOUT) == []
    assert await asyncio.to_thread(h.service.interrupt_stale, LONG, timedelta(seconds=1)) == [], (
        "only the given type"
    )
    row = await h.row(task_id)
    assert row.status == _status("running")
    version = row.version

    h.clock.advance(SHORT_TIMEOUT + ONE_SECOND)
    assert await asyncio.to_thread(h.service.interrupt_stale, SHORT, SHORT_TIMEOUT) == [task_id]
    row = await h.row(task_id)
    assert row.status == _status("interrupted")
    assert row.finished_at == h.clock.now().isoformat()
    assert row.version == version + 1
    assert await asyncio.to_thread(h.service.interrupt_stale, SHORT, SHORT_TIMEOUT) == []
    assert h.changes.statuses(task_id) == ["queued", "running", "interrupted"]


# --- the watchdog loop ----------------------------------------------------------------------


async def test_watchdog_loop_checks_on_its_interval(db: Database) -> None:
    hung = Gated()
    ticks = Ticks()
    h = WatchHarness(db, {SHORT: (SHORT_TIMEOUT, hung)}, watchdog_interval=7.5, sleep=ticks)
    await h.start()
    try:
        task_id = await h.enqueue(SHORT)
        await hung.wait_started(1)
        await ticks.wait_asked(1)
        assert ticks.delays == [7.5]

        ticks.tick()  # an interval passes, but nothing is stale yet
        await ticks.wait_asked(2)
        assert (await h.row(task_id)).status == _status("running")

        h.clock.advance(SHORT_TIMEOUT + ONE_SECOND)
        ticks.tick()
        await h.changes.wait_status(task_id, "interrupted")
        await _wait_until(lambda: hung.cancelled == [task_id], "the handler was cancelled")
        await ticks.wait_asked(3)
        assert set(ticks.delays) == {7.5}
    finally:
        await h.runner.stop()


async def test_watchdog_loop_stops_with_runner_stop(db: Database) -> None:
    ticks = Ticks()
    h = WatchHarness(db, {SHORT: (SHORT_TIMEOUT, Gated())}, sleep=ticks)
    await h.start()
    await ticks.wait_asked(1)

    await h.runner.stop()

    assert h.runner.status == "stopped"
    assert _other_tasks() == set(), "the watchdog loop ends with stop()"
    asked = len(ticks.delays)
    ticks.tick()
    await _settle()
    assert len(ticks.delays) == asked, "no watchdog check after stop()"

    await h.runner.start()  # and a restart brings it back
    await ticks.wait_asked(asked + 1)
    await h.runner.stop()
    assert _other_tasks() == set()


async def test_watchdog_loop_survives_a_failed_check(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    hung = Gated()
    ticks = Ticks()
    h = WatchHarness(db, {SHORT: (SHORT_TIMEOUT, hung)}, sleep=ticks)
    real = h.service.interrupt_stale
    failures: list[int] = []

    def flaky(task_type: str, timeout: timedelta) -> list[str]:
        if not failures:
            failures.append(1)
            raise RuntimeError("fictional database is busy for Ada Example")
        result: list[str] = real(task_type, timeout)
        return result

    h.service.interrupt_stale = flaky  # type: ignore[method-assign]
    await h.start()
    try:
        task_id = await h.enqueue(SHORT)
        await hung.wait_started(1)
        h.clock.advance(SHORT_TIMEOUT + ONE_SECOND)
        await ticks.wait_asked(1)
        ticks.tick()  # this check fails
        await ticks.wait_asked(2)
        assert h.runner.status == "running"
        ticks.tick()  # the next one works
        await h.changes.wait_status(task_id, "interrupted")
    finally:
        await h.runner.stop()
    assert failures == [1]
    assert "RuntimeError" in caplog.text
    assert "Ada Example" not in caplog.text


@pytest.mark.parametrize("interval", [0, -1.0])
def test_watchdog_interval_must_be_positive(interval: float) -> None:
    from app.tasks import TaskRunner
    from app.tasks.handlers import HandlerRegistry

    with pytest.raises(ValueError):
        TaskRunner(registry=HandlerRegistry(), watchdog_interval=interval)


# --- progress clamping ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "stored"), [(-5, 0), (0, 0), (55, 55), (100, 100), (150, 100), (None, None)]
)
async def test_progress_clamped_0_to_100(given: int | None, stored: int | None) -> None:
    from app.domain.tasks import CancelFlag
    from app.tasks import TaskContext

    beats: list[tuple[str, int | None]] = []

    async def no_complete(write: object) -> str:  # this test never saves a result
        raise AssertionError("complete is not used here")

    ctx = TaskContext(
        task_id="task-0001",
        task_type=SHORT,
        input_ref=None,
        attempt=1,
        cancel_flag=CancelFlag(),
        _beat=lambda task_id, progress: beats.append((task_id, progress)),
        _complete=no_complete,
    )
    await ctx.heartbeat(given)
    assert beats == [("task-0001", stored)]


async def test_out_of_range_progress_never_fails_the_task(db: Database) -> None:
    handler = Stepper()
    h = WatchHarness(db, {SHORT: (SHORT_TIMEOUT, handler)})
    await h.start()
    try:
        task_id = await h.enqueue(SHORT)
        await handler.wait_started()
        await handler.step(250)
        assert (await h.row(task_id)).progress == 100
        await handler.step(-3)
        assert (await h.row(task_id)).progress == 0
        handler.finish()
        await h.changes.wait_status(task_id, "succeeded")
    finally:
        await h.runner.stop()


# --- per-type timeouts longer than the provider's call timeout ------------------------------


def test_registry_refuses_timeout_not_longer_than_provider_call_timeout() -> None:
    from app.tasks.handlers import HandlerRegistry

    registry = HandlerRegistry(min_timeout=timedelta(seconds=60))
    for timeout in (timedelta(seconds=30), timedelta(seconds=60)):
        with pytest.raises(ValueError):
            registry.register(SHORT, _spec(timeout), Gated())
        assert registry.get(SHORT) is None
    spec = _spec(timedelta(seconds=61))
    registry.register(SHORT, spec, Gated())
    entry = registry.get(SHORT)
    assert entry is not None and entry[0] is spec


def test_wiring_provider_run_timeout_is_the_five_minute_run_limit() -> None:
    """One source of truth: the overall limit on one provider run (M2.13 injects it into the
    Claude provider's ``run``) is also the floor for every task type's timeout."""
    from app.modules.ai.providers.claude_sdk import CONNECTION_TIMEOUT_SECONDS
    from app.wiring import PROVIDER_RUN_TIMEOUT

    assert isinstance(PROVIDER_RUN_TIMEOUT, timedelta)
    assert timedelta(seconds=CONNECTION_TIMEOUT_SECONDS) < PROVIDER_RUN_TIMEOUT
    assert timedelta(minutes=5) == PROVIDER_RUN_TIMEOUT


# --- review follow-ups ----------------------------------------------------------------------


async def test_failed_type_check_does_not_stop_other_types(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """Each type is interrupted and its handlers cancelled on its own: a failing check for
    one type neither blocks another type's cancellation nor leaves its slot taken."""
    caplog.set_level(logging.DEBUG)
    hung = Gated()
    h = WatchHarness(db, {SHORT: (SHORT_TIMEOUT, hung), LONG: (LONG_TIMEOUT, Gated())})
    real = h.service.interrupt_stale

    def failing_long(task_type: str, timeout: timedelta) -> list[str]:
        if task_type == LONG:
            raise RuntimeError("fictional database is busy for Ada Example")
        result: list[str] = real(task_type, timeout)
        return result

    h.service.interrupt_stale = failing_long  # type: ignore[method-assign]
    await h.start(max_concurrent=1)
    try:
        hung_id = await h.enqueue(SHORT, "run-1")
        next_id = await h.enqueue(SHORT, "run-2")
        await hung.wait_started(1)
        h.clock.advance(SHORT_TIMEOUT + ONE_SECOND)

        assert await h.check() == [hung_id]
        await _wait_until(lambda: hung.cancelled == [hung_id], "the handler was cancelled")
        await hung.wait_started(2)
        assert hung.calls[1].task_id == next_id
        hung.release_all()
        await h.changes.wait_status(next_id, "succeeded")
    finally:
        await h.runner.stop()
    assert (await h.row(hung_id)).status == _status("interrupted")
    assert "RuntimeError" in caplog.text
    assert "Ada Example" not in caplog.text


async def test_watchdog_loop_restarts_after_it_died(db: Database) -> None:
    class DiesOnce(Ticks):
        async def __call__(self, delay: float) -> None:
            if not self.delays:
                self.delays.append(delay)
                raise RuntimeError("fictional sleep failure")
            await super().__call__(delay)

    ticks = DiesOnce()
    h = WatchHarness(db, {SHORT: (SHORT_TIMEOUT, Gated())}, sleep=ticks)
    await h.start()
    try:
        await ticks.wait_asked(1)
        await _settle()
        h.runner.notify()  # the worker's next look brings the watchdog back
        await ticks.wait_asked(2)
    finally:
        await h.runner.stop()
    assert _other_tasks() == set()


async def test_finish_in_flight_loses_to_watchdog_interrupt(db: Database) -> None:
    """The documented window: a handler returned, but its finish is still in a worker thread
    when the watchdog's interrupt commits. The task stays interrupted, the late finish is
    refused without a crash, and the slot comes back."""
    import threading

    handler = Stepper(result="fictional-late-draft")
    h = WatchHarness(db, {SHORT: (SHORT_TIMEOUT, handler)})
    real_finish = h.service.finish
    entered, release = threading.Event(), threading.Event()
    results: list[bool] = []

    def gated_finish(task_id: str, status: Any, **kwargs: Any) -> bool:
        if not entered.is_set():
            entered.set()
            assert release.wait(DEADLINE_S)
        changed: bool = real_finish(task_id, status, **kwargs)
        results.append(changed)
        return changed

    h.service.finish = gated_finish  # type: ignore[method-assign]
    await h.start(max_concurrent=1)
    try:
        task_id = await h.enqueue(SHORT, "run-1")
        next_id = await h.enqueue(SHORT, "run-2")
        await handler.wait_started()
        h.clock.advance(SHORT_TIMEOUT + ONE_SECOND)
        handler.finish()
        await _wait_until(entered.is_set, "the finish reached its worker thread")

        assert await h.check() == [task_id]
        release.set()
        await _wait_until(lambda: len(handler.calls) >= 2, "the next task started")
        assert handler.calls[1].task_id == next_id
        handler.finish()
        await h.changes.wait_status(next_id, "succeeded")
    finally:
        release.set()
        await h.runner.stop()

    assert results[0] is False, "the late finish is refused"
    assert handler.cancelled == []
    row = await h.row(task_id)
    assert row.status == _status("interrupted")
    assert row.result_ref is None
    assert h.changes.statuses(task_id) == ["queued", "running", "interrupted"]
