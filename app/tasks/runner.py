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
from app.domain.tasks import CancelFlag, TaskStatus
from app.tasks.context import TaskContext
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

    def heartbeat(self, task_id: str, *, progress: int | None = None) -> bool: ...

    def interrupt_stale(self, task_type: str, timeout: timedelta) -> list[str]: ...

    def finish(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        error: AppError | None = None,
        result_ref: str | None = None,
    ) -> bool: ...


@dataclass(frozen=True)
class _Attached:
    service: RunnerService
    slots: asyncio.Semaphore


@dataclass
class _Running:
    service: RunnerService
    task_id: str
    finishing: bool = field(default=False)  # the handler has ended; its outcome is being saved


@dataclass(frozen=True)
class _Outcome:
    status: TaskStatus
    error: AppError | None = None
    result_ref: str | None = None


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

    # --- on the event loop --------------------------------------------------------------

    async def start(self) -> None:
        if self.status == "running":
            return
        wake, stopping = asyncio.Event(), asyncio.Event()
        with self._lock:
            self._loop, self._wake, self._stopping = asyncio.get_running_loop(), wake, stopping
        self._worker = asyncio.create_task(self._run(wake, stopping), name="task-runner")

    async def stop(self) -> None:
        """Stop claiming, cancel the handlers still working and end their tasks interrupted.
        A cancellation of ``stop()`` itself propagates without cancelling the worker; calling
        ``stop()`` again finishes the job."""
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
                await self._finish(entry.service, entry.task_id, _Outcome(TaskStatus.INTERRUPTED))

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
        for task, entry in list(self._handlers.items()):
            if entry.task_id in task_ids and not entry.finishing:
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
        running = _Running(attached.service, claimed.id)
        task = asyncio.create_task(self._execute(running, claimed), name=f"task-{claimed.id}")
        self._handlers[task] = running

        def done(finished: asyncio.Task[None]) -> None:
            self._handlers.pop(finished, None)
            attached.slots.release()
            wake.set()

        task.add_done_callback(done)

    async def _execute(self, running: _Running, claimed: ClaimedTask) -> None:
        service = running.service
        entry = self._registry.get(claimed.type)
        if entry is None:
            _log.warning("task %s has no registered handler", claimed.id)
            failure = UnknownTaskType(detail="no handler")
            outcome = _Outcome(TaskStatus.FAILED, error=failure)
        else:
            outcome = await self._call(service, claimed, entry[1])
        running.finishing = True
        # Shielded: the outcome is saved even if this task is cancelled meanwhile.
        await asyncio.shield(self._finish(service, claimed.id, outcome))

    async def _call(
        self, service: RunnerService, claimed: ClaimedTask, handler: TaskHandler
    ) -> _Outcome:
        """Run the handler and turn how it ended into an outcome. A cancellation from outside
        propagates; whoever cancelled ends the task."""

        def beat(task_id: str, progress: int | None) -> object:
            return service.heartbeat(task_id, progress=progress)

        context = TaskContext(
            task_id=claimed.id,
            task_type=claimed.type,
            input_ref=claimed.input_ref,
            attempt=claimed.attempt,
            cancel_flag=CancelFlag(),
            _beat=beat,
        )
        try:
            result_ref = await handler(context)
        except AppError as error:
            return _Outcome(TaskStatus.FAILED, error=error)
        except asyncio.CancelledError:
            if cast("asyncio.Task[None]", asyncio.current_task()).cancelling():
                raise  # cancelled from outside (stop)
            return self._unexpected(claimed.id, "CancelledError")  # raised by the handler
        except BaseException as error:  # any other exception, never left running
            return self._unexpected(claimed.id, type(error).__name__)
        return _Outcome(TaskStatus.SUCCEEDED, result_ref=result_ref)

    def _unexpected(self, task_id: str, name: str) -> _Outcome:
        # Only the type: the exception's text could carry job or CV content.
        _log.error("task %s handler failed (%s)", task_id, name)
        return _Outcome(TaskStatus.FAILED, error=TaskFailed(detail=name))

    async def _finish(self, service: RunnerService, task_id: str, outcome: _Outcome) -> None:
        try:
            await asyncio.to_thread(
                service.finish,
                task_id,
                outcome.status,
                error=outcome.error,
                result_ref=outcome.result_ref,
            )
        except Exception as failure:
            _log.error("finishing task %s failed (%s)", task_id, type(failure).__name__)
