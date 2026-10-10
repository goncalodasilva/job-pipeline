"""The background task runner (spec 2.6; spec 5 M2 reliability rules 1, 4, 9; ADR-034).

An asyncio worker on the app's event loop, started and stopped by the lifespan. It idles until
``attach`` hands it a task service (when a workspace opens, possibly from another thread) and
then sleeps on an ``asyncio.Event`` that ``notify()``, ``attach``, a finished handler and
``stop()`` set; it never polls. Before each claim it takes a slot of a semaphore sized by
``TaskRunnerSettings.max_concurrent``, so no task is made running while it would wait for a
slot. Each claimed task runs as its own asyncio task on the loop. Every service call goes
through ``asyncio.to_thread``: the database's transaction lock is per thread (ADR-038), so
nothing is awaited inside a transaction and the loop never blocks on SQLite.

Outcomes: a returned result_ref ends the task succeeded; an ``AppError`` ends it failed with
that error's kind; anything else (including a ``CancelledError`` the handler raises itself, or
another ``BaseException``) fails it with ``TaskFailed``, logged by exception type only; an
unregistered type fails with ``UnknownTaskType``. Nothing is retried automatically. Once a
handler has returned or raised, its finishing write always completes. ``stop()`` cancels the
handlers still working and ends their tasks interrupted; queued tasks stay queued and resume
next time (ADR-076 item 10). A failed claim (a database error) schedules one wake-up after a
backoff (``claim_retry_seconds`` doubling up to 30 s, reset by a successful claim): only on
that error path, so the runner still never polls.

Watchdog (reliability rule 5): once a workspace is attached, the worker starts one watchdog
loop that awaits the injected ``sleep(watchdog_interval)`` and then runs
``check_watchdog_once``, until ``stop()``. A check asks the service to interrupt, per
registered type, every running task whose heartbeat is older than the type's timeout (one
conditional update each), then cancels the local handlers of those tasks. A cancelled
handler's own finish is refused by the conditional update, and its slot is freed. Each type
is checked on its own: a failed check is logged by task type and exception type only, and
the other types (and the loop) carry on. A task that went past its timeout without a
heartbeat is interrupted even if its handler returns at that moment: whichever conditional
update commits first wins, and a finish still in its worker thread is then refused. A
watchdog loop that died is started again on the worker's next look.

Cancellation (reliability rule 6): ``notify_cancel(task_id)`` (safe from any thread; the app
calls it for every ``running`` change) makes the runner read the row's cancel flag when the
task is a local handler still working; the runner also reads it once as it starts each
handler, so a cancel requested between the claim and that start is not lost. When the flag is
set the runner sets that task's ``CancelFlag`` and cancels the handler's asyncio task, which
aborts an in-flight provider call. A heartbeat mirrors the flag too (a cancel from another
process) and then raises ``TaskCancelled``. Once the flag is set the task ends cancelled
however the handler ends (``TaskCancelled``, a provider's cancel error or an outside
cancellation): no result, no error kind. A result returned anyway is refused by the database
(a success needs ``cancel_requested = 0``) and the task ends cancelled. ``stop()`` and the
watchdog cancel without setting the flag, and a flag read that comes back once ``stop()`` has
begun is ignored, so their tasks end interrupted.

Attempts never mix: a handler's heartbeats, flag reads and finish name its attempt, so a
handler of an earlier attempt that outlives it (it swallowed a cancellation) never beats for,
reads the flag of or finishes the next attempt of the same task.
"""

import asyncio
import contextlib
import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal, Protocol, cast

from app.domain.errors import AppError, TaskFailed, UnknownTaskType
from app.domain.settings import TaskRunnerSettings
from app.domain.tasks import CancelFlag, Completion, CompletionOutcome, TaskStatus
from app.tasks.context import ResultWriter, TaskCancelled, TaskContext, TaskSuperseded
from app.tasks.handlers import HandlerRegistry, TaskHandler

RunnerStatus = Literal["running", "stopped"]

DEFAULT_CLAIM_RETRY_SECONDS = 1.0
DEFAULT_WATCHDOG_INTERVAL_SECONDS = 30.0
MAX_CLAIM_RETRY_SECONDS = 30.0

_log = logging.getLogger(__name__)


class ClaimedTask(Protocol):
    @property
    def id(self) -> str: ...
    @property
    def type(self) -> str: ...
    @property
    def input_ref(self) -> str | None: ...
    @property
    def attempt(self) -> int: ...


class RunnerService(Protocol):
    """The part of ``TaskService`` the runner uses. All of it blocks."""

    def claim_next(self) -> ClaimedTask | None: ...

    def heartbeat(
        self,
        task_id: str,
        *,
        progress: int | None = None,
        attempt: int | None = None,
        cancel_flag: CancelFlag | None = None,
    ) -> bool: ...

    def cancel_requested(self, task_id: str, *, attempt: int | None = None) -> bool: ...

    def interrupt_stale(self, task_type: str, timeout: timedelta) -> list[str]: ...

    def complete(self, task_id: str, write: ResultWriter, *, attempt: int) -> Completion: ...

    def finish(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        error: AppError | None = None,
        result_ref: str | None = None,
        attempt: int | None = None,
    ) -> bool: ...


@dataclass(frozen=True)
class _Attached:
    service: RunnerService
    slots: asyncio.Semaphore


@dataclass
class _Running:
    service: RunnerService
    task_id: str
    attempt: int
    flag: CancelFlag = field(default_factory=CancelFlag)
    finishing: bool = field(default=False)  # the handler has ended; its outcome is being saved
    settled: bool = field(default=False)  # ctx.complete committed, or found the row moved on


@dataclass(frozen=True)
class _Outcome:
    status: TaskStatus
    error: AppError | None = None
    result_ref: str | None = None


_CANCELLED = _Outcome(TaskStatus.CANCELLED)


class TaskRunner:
    def __init__(
        self,
        *,
        registry: HandlerRegistry,
        claim_retry_seconds: float = DEFAULT_CLAIM_RETRY_SECONDS,
        watchdog_interval: float = DEFAULT_WATCHDOG_INTERVAL_SECONDS,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        if not claim_retry_seconds > 0:
            raise ValueError("claim_retry_seconds must be positive")
        if not watchdog_interval > 0:
            raise ValueError("watchdog_interval must be positive")
        self._registry = registry
        self._claim_retry_seconds = claim_retry_seconds
        self._watchdog_interval = watchdog_interval
        self._sleep = sleep
        self._watchdog: asyncio.Task[None] | None = None
        self._retry: asyncio.TimerHandle | None = None
        self._lock = threading.Lock()  # guards what attach/notify read from other threads
        self._attached: _Attached | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._stopping: asyncio.Event | None = None
        self._worker: asyncio.Task[None] | None = None
        self._handlers: dict[asyncio.Task[None], _Running] = {}
        self._probes: set[asyncio.Task[None]] = set()  # cancel-flag reads in flight
        self._shutting_down = False  # stop() has begun: a flag read coming back is ignored

    @property
    def status(self) -> RunnerStatus:
        return "running" if self._worker is not None and not self._worker.done() else "stopped"

    # --- from any thread ----------------------------------------------------------------

    def attach(self, service: RunnerService, *, settings: TaskRunnerSettings) -> None:
        """Hand over the open workspace's task service; safe from any thread, before or
        after ``start``."""
        attached = _Attached(service, asyncio.Semaphore(settings.max_concurrent))
        with self._lock:
            self._attached = attached
        self.notify()

    def notify(self) -> None:
        """Wake the worker (after an enqueue); safe from any thread, a no-op when stopped."""
        with self._lock:
            loop, wake = self._loop, self._wake
        if loop is None or wake is None:
            return
        with contextlib.suppress(RuntimeError):  # the loop has closed: nothing to wake
            loop.call_soon_threadsafe(wake.set)

    def notify_cancel(self, task_id: str) -> None:
        """A task's row changed while running (a claim or a cancel request); safe from any
        thread, a no-op when stopped. When it is a local handler still working and its cancel
        flag is set in the database, the handler is cancelled."""
        with self._lock:
            loop = self._loop
        if loop is None:
            return
        with contextlib.suppress(RuntimeError):  # the loop has closed: nothing to cancel
            loop.call_soon_threadsafe(self._probe_cancel, task_id)

    # --- on the event loop --------------------------------------------------------------

    def _probe_cancel(self, task_id: str) -> None:
        for task, entry in list(self._handlers.items()):
            if entry.task_id == task_id:
                self._start_probe(task, entry)

    def _start_probe(self, task: "asyncio.Task[None]", entry: _Running) -> None:
        if entry.finishing or entry.flag.cancelled or self._shutting_down:
            return
        probe = asyncio.create_task(self._read_cancel(task, entry), name="task-cancel")
        self._probes.add(probe)
        probe.add_done_callback(self._probes.discard)

    async def _read_cancel(self, task: "asyncio.Task[None]", entry: _Running) -> None:
        try:
            requested = await asyncio.to_thread(
                entry.service.cancel_requested, entry.task_id, attempt=entry.attempt
            )
        except Exception as error:
            # Only the type: a database message could quote stored values.
            _log.error("reading cancel of task %s failed (%s)", entry.task_id, type(error).__name__)
            return
        if not requested or entry.finishing or self._shutting_down:
            return  # stop() ends a task interrupted even when a cancel was requested
        # Both happen in this one step: the handler only resumes (with CancelledError) on a
        # later turn of the loop, and by then its flag is already set.
        entry.flag.cancel()
        task.cancel()

    async def start(self) -> None:
        if self.status == "running":
            return
        self._shutting_down = False
        wake, stopping = asyncio.Event(), asyncio.Event()
        with self._lock:
            self._loop, self._wake, self._stopping = asyncio.get_running_loop(), wake, stopping
        self._worker = asyncio.create_task(self._run(wake, stopping), name="task-runner")

    async def stop(self) -> None:
        """Stop claiming, cancel the handlers still working and end their tasks interrupted.
        A cancellation of ``stop()`` itself propagates without cancelling the worker; calling
        ``stop()`` again finishes the job."""
        self._shutting_down = True
        worker, wake, stopping = self._worker, self._wake, self._stopping
        if worker is not None and wake is not None and stopping is not None:
            stopping.set()
            wake.set()
            # Shielded: a cancelled stop() never cuts the worker off in the middle of a claim.
            await asyncio.shield(worker)
        self._worker = None  # only once the worker has ended (a cancelled stop keeps it)
        watchdog, self._watchdog = self._watchdog, None
        if watchdog is not None:
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
        with self._lock:
            self._loop = self._wake = self._stopping = None
        if self._retry is not None:
            self._retry.cancel()
            self._retry = None
        probes = list(self._probes)
        for probe in probes:
            probe.cancel()
        await asyncio.gather(*probes, return_exceptions=True)
        await self._interrupt_handlers()

    async def _interrupt_handlers(self) -> None:
        running = dict(self._handlers)
        for task, entry in running.items():
            if not entry.finishing:  # a handler that has ended keeps its outcome
                task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        for entry in running.values():
            if not entry.finishing:
                # Conditional: a task that already finished stays as it ended.
                interrupted = _Outcome(TaskStatus.INTERRUPTED)
                await self._finish(entry.service, entry.task_id, entry.attempt, interrupted)

    async def _run(self, wake: asyncio.Event, stopping: asyncio.Event) -> None:
        delay = self._claim_retry_seconds
        while not stopping.is_set():
            wake.clear()  # before looking, so a notify() from now on is never lost
            with self._lock:
                attached = self._attached
            if attached is not None:
                self._ensure_watchdog()
            if attached is None or attached.slots.locked():
                await wake.wait()
                continue
            await attached.slots.acquire()  # a free slot: returns at once
            try:
                claimed = await asyncio.to_thread(attached.service.claim_next)
            except Exception as error:
                attached.slots.release()
                # Only the type: a database message could quote stored values.
                _log.error("claiming a task failed (%s); trying again later", type(error).__name__)
                self._retry = asyncio.get_running_loop().call_later(delay, wake.set)
                delay = min(delay * 2, max(MAX_CLAIM_RETRY_SECONDS, self._claim_retry_seconds))
                await wake.wait()
                continue
            delay = self._claim_retry_seconds
            if claimed is None:
                attached.slots.release()
                await wake.wait()
                continue
            self._spawn(attached, claimed, wake)

    async def check_watchdog_once(self) -> list[str]:
        """Interrupt the running tasks whose heartbeat is older than their type's timeout
        and cancel their local handlers, type by type; returns the ids interrupted. A type
        whose check fails is logged and skipped. Does nothing (and returns ``[]``) with no
        workspace attached."""
        with self._lock:
            attached = self._attached
        if attached is None:
            return []
        interrupted: list[str] = []
        for task_type, spec in self._registry.specs():
            try:
                stale = await asyncio.to_thread(
                    attached.service.interrupt_stale, task_type, spec.timeout
                )
            except Exception as error:
                # Only the names: a database message could quote stored values.
                _log.error("watchdog check for %s failed (%s)", task_type, type(error).__name__)
                continue
            self._cancel_local(set(stale))
            interrupted += stale
        return interrupted

    def _cancel_local(self, task_ids: set[str]) -> None:
        """Cancel the local handlers of the tasks just interrupted. The row that was running
        holds the task's newest attempt, so only the newest local attempt of each task is
        cancelled; an older one (a handler that outlived its attempt) is left alone."""
        newest: dict[str, tuple[asyncio.Task[None], _Running]] = {}
        for task, entry in self._handlers.items():
            current = newest.get(entry.task_id)
            if entry.task_id in task_ids and (
                current is None or entry.attempt > current[1].attempt
            ):
                newest[entry.task_id] = (task, entry)
        for task, entry in newest.values():
            if not entry.finishing:
                task.cancel()  # its slot comes back when the task ends

    def _ensure_watchdog(self) -> None:
        current = self._watchdog
        if current is not None and not current.done():
            return
        if current is not None and not current.cancelled() and current.exception() is not None:
            _log.error(
                "watchdog loop ended (%s); starting it again", type(current.exception()).__name__
            )
        self._watchdog = asyncio.create_task(self._watch(), name="task-watchdog")

    async def _watch(self) -> None:
        while True:
            await self._sleep(self._watchdog_interval)
            try:
                await self.check_watchdog_once()
            except Exception as error:
                # Only the type: a database message could quote stored values.
                _log.error("watchdog check failed (%s)", type(error).__name__)

    def _spawn(self, attached: _Attached, claimed: ClaimedTask, wake: asyncio.Event) -> None:
        running = _Running(attached.service, claimed.id, claimed.attempt)
        task = asyncio.create_task(self._execute(running, claimed), name=f"task-{claimed.id}")
        self._handlers[task] = running
        # A cancel requested between the claim and now had no handler to reach: read it once.
        self._start_probe(task, running)

        def done(finished: asyncio.Task[None]) -> None:
            self._handlers.pop(finished, None)
            attached.slots.release()
            wake.set()

        task.add_done_callback(done)

    async def _execute(self, running: _Running, claimed: ClaimedTask) -> None:
        entry = self._registry.get(claimed.type)
        if entry is None:
            _log.warning("task %s has no registered handler", claimed.id)
            failure = UnknownTaskType(detail="no handler")
            outcome = _Outcome(TaskStatus.FAILED, error=failure)
        else:
            outcome = await self._call(running, claimed, entry[1])
        running.finishing = True
        if running.settled:
            return  # the row already ended as complete left it; the handler's outcome is moot
        # Shielded: the outcome is saved even if this task is cancelled meanwhile.
        await asyncio.shield(self._save(running, outcome))

    async def _save(self, running: _Running, outcome: _Outcome) -> None:
        service, task_id, attempt = running.service, running.task_id, running.attempt
        saved = await self._finish(service, task_id, attempt, outcome)
        if outcome.status == TaskStatus.SUCCEEDED and saved is False:
            # Refused: a cancel was requested before the success committed (also how a
            # result returned after a cancel is discarded), or this attempt is no longer
            # running (then this is refused too).
            await self._finish(service, task_id, attempt, _CANCELLED)

    async def _call(
        self, running: _Running, claimed: ClaimedTask, handler: TaskHandler
    ) -> _Outcome:
        """Run the handler and turn how it ended into an outcome. Once its cancel flag is set
        it ends cancelled, however it ended. Otherwise a cancellation from outside propagates;
        whoever cancelled ends the task."""
        service, flag, attempt = running.service, running.flag, running.attempt

        def beat(task_id: str, progress: int | None) -> object:
            return service.heartbeat(task_id, progress=progress, attempt=attempt, cancel_flag=flag)

        def settle(completion: Completion) -> None:
            if completion.outcome == CompletionOutcome.CANCEL_REQUESTED:
                flag.cancel()  # the task ends cancelled however the handler ends
            else:
                running.settled = True  # committed, or the row moved on: never finish it

        async def complete(write: ResultWriter) -> str:
            # The writer and the success share one transaction in a worker thread.
            saving = asyncio.ensure_future(
                asyncio.to_thread(service.complete, claimed.id, write, attempt=attempt)
            )
            try:
                completion = await asyncio.shield(saving)
            except asyncio.CancelledError:
                # The thread runs on regardless and its outcome decides the task: wait for
                # it, so a committed success is never finished again by this handler.
                await asyncio.wait({saving})
                if saving.exception() is None:
                    settle(saving.result())
                raise
            settle(completion)
            if completion.outcome == CompletionOutcome.CANCEL_REQUESTED:
                raise TaskCancelled
            if completion.outcome == CompletionOutcome.SUPERSEDED:
                raise TaskSuperseded
            return cast(str, completion.result_ref)

        context = TaskContext(
            task_id=claimed.id,
            task_type=claimed.type,
            input_ref=claimed.input_ref,
            attempt=claimed.attempt,
            cancel_flag=flag,
            _beat=beat,
            _complete=complete,
        )
        try:
            result_ref = await handler(context)
        except asyncio.CancelledError:
            if flag.cancelled:
                return _CANCELLED
            if cast("asyncio.Task[None]", asyncio.current_task()).cancelling():
                raise  # cancelled from outside (stop, watchdog)
            return self._unexpected(claimed.id, "CancelledError")  # raised by the handler
        except BaseException as error:  # any other exception, never left running
            if flag.cancelled:
                return _CANCELLED  # the cancel signal or a provider's cancel error
            if isinstance(error, AppError):
                return _Outcome(TaskStatus.FAILED, error=error)
            return self._unexpected(claimed.id, type(error).__name__)
        return _Outcome(TaskStatus.SUCCEEDED, result_ref=result_ref)

    def _unexpected(self, task_id: str, name: str) -> _Outcome:
        # Only the type: the exception's text could carry job or CV content.
        _log.error("task %s handler failed (%s)", task_id, name)
        return _Outcome(TaskStatus.FAILED, error=TaskFailed(detail=name))

    async def _finish(
        self, service: RunnerService, task_id: str, attempt: int, outcome: _Outcome
    ) -> bool | None:
        """Save the outcome of that attempt; False when it was refused, None when saving
        failed (logged)."""
        try:
            return await asyncio.to_thread(
                service.finish,
                task_id,
                outcome.status,
                error=outcome.error,
                result_ref=outcome.result_ref,
                attempt=attempt,
            )
        except Exception as failure:
            _log.error("finishing task %s failed (%s)", task_id, type(failure).__name__)
            return None
