"""Cancellation in the runner (M2.11; AC-M2.4; spec 5 M2 reliability rule 6; ADR-054).

API assumed here (proposed for the implementer):

- ``TaskRunner.notify_cancel(task_id)``: safe from any thread (it hops onto the loop with
  ``call_soon_threadsafe``). The app wires the task service's ``on_change`` to call it for
  every change whose status is ``running``; the runner then mirrors the database flag: when the
  row's ``cancel_requested`` is set and the task is a local running handler, it sets that
  task's ``CancelFlag`` and cancels the handler's asyncio task (which aborts an in-flight
  provider call). A ``running`` change from a claim (flag not set) cancels nothing.
- Heartbeats report the flag too (``TaskService.heartbeat`` returns whether a cancel was
  requested, or equivalent): after ``await ctx.heartbeat()`` sees a flag set by another process
  (no ``on_change`` here), ``ctx.cancel_flag.cancelled`` is True, or the heartbeat raises the
  cancel signal itself.
- ``ctx.check_cancelled()``: does nothing while the flag is clear and raises a cancel signal
  once it is set. Handlers call it between steps and let it propagate.
- A handler whose task's cancel flag is set ends ``cancelled`` (never failed or interrupted)
  however it ends: a cancel signal, ``CallCancelled`` from the provider, an outside
  cancellation, or even a returned result. Its partial output is discarded: no ``result_ref``,
  no error kind, and writes it would have made after the cancel never happen.
- ``TaskService.finish(id, SUCCEEDED, ...)`` is conditional on ``cancel_requested = 0``, so a
  cancel requested just before the success commits ends the task cancelled.
- A cancel request after success is ignored; a queued task cancelled before its claim never
  reaches the handler or the provider; ``stop()`` ends running tasks interrupted, never
  cancelled.

Real migrated SQLite in a temp folder, ``FrozenClock``, ``SequentialIdFactory`` and the
``FakeAIProvider`` whose injected sleep waits on an ``asyncio.Event`` that is never set, so a
call only ends by cancellation. Every wait has a deadline; no fixed sleeps. All values are
fictional.
"""

import asyncio
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from app.modules.ai import CallCancelled
from app.modules.ai.providers.fake import FakeAIProvider, ScriptedResponse
from app.modules.storage import Database, tasks_repo
from tests.fakes.ai import make_request
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory
from tests.unit.tasks.test_runner import (
    DEADLINE_S,
    START,
    Changes,
    _open_migrated,
    _registry,
    _settings,
    _settle,
    _status,
)

DRAFTING = "fictional_drafting"
AI_TASK = "fictional_cover_letter"
LONG_CALL_SECONDS = 600.0
FICTIONAL_ANSWER = {"answer": "A fictional cover letter for Example Ltd."}


# --- helpers ------------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    (tmp_path / "data").mkdir()
    database = _open_migrated(tmp_path)
    with database.transaction() as conn:
        conn.execute(
            "CREATE TABLE fictional_drafts ("
            "id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, body TEXT NOT NULL)"
        )
    try:
        yield database
    finally:
        database.close()


def _drafts(db: Database) -> list[tuple[str, str]]:
    with db.transaction() as conn:
        rows = conn.execute("SELECT task_id, body FROM fictional_drafts ORDER BY id").fetchall()
    return [(str(task_id), str(body)) for task_id, body in rows]


def _write_draft(db: Database, task_id: str, body: str) -> None:
    with db.transaction() as conn:
        conn.execute("INSERT INTO fictional_drafts (task_id, body) VALUES (?, ?)", (task_id, body))


def _flag_in_database(db: Database, task_id: str) -> None:
    """Another process asks for the cancel: the row's flag is set, no ``on_change`` fires."""
    with db.transaction() as conn:
        assert tasks_repo.request_cancel(conn, task_id, START.isoformat()) is True


async def _wait_until(condition: Callable[[], object], what: str) -> None:
    async def until() -> None:
        while not condition():
            await asyncio.to_thread(lambda: None)
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(until(), DEADLINE_S)
    except TimeoutError:
        raise AssertionError(f"never happened: {what}") from None


class Hang:
    """The fake provider's injected sleep: never returns on its own, records a cancellation
    and whether the call's cancel token was already set when it came."""

    def __init__(self, spy: "TokenSpy") -> None:
        self._spy = spy
        self._never = asyncio.Event()
        self.started = 0
        self.cancelled = 0
        self.token_set_at_cancel: list[bool] = []

    async def __call__(self, delay: float) -> None:
        self.started += 1
        try:
            await self._never.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            self.token_set_at_cancel.append(self._spy.tokens[-1].cancelled)
            raise


class TokenSpy:
    """Wraps the fake provider; records each call's cancel token and how the call ended."""

    def __init__(self, sleep: Callable[[float], Any] | None = None) -> None:
        self.hang = Hang(self)
        self.inner = FakeAIProvider(sleep=sleep or self.hang)
        self.tokens: list[Any] = []
        self.raised: list[str] = []
        self.returned = 0

    def script(self, *responses: ScriptedResponse) -> None:
        self.inner.script(AI_TASK, *responses)

    async def run(self, request: Any, cancel: Any) -> Any:
        self.tokens.append(cancel)
        try:
            response = await self.inner.run(request, cancel)
        except BaseException as error:
            self.raised.append(type(error).__name__)
            raise
        self.returned += 1
        return response


class AIHandler:
    """Beats, calls the provider with the task's cancel flag, then saves a draft."""

    def __init__(self, db: Database, provider: TokenSpy) -> None:
        self._db = db
        self._provider = provider
        self.contexts: list[Any] = []
        self.wrote: list[str] = []

    async def __call__(self, ctx: Any) -> str | None:
        self.contexts.append(ctx)
        await ctx.heartbeat(10)
        response = await self._provider.run(make_request(task=AI_TASK), ctx.cancel_flag)
        await asyncio.to_thread(_write_draft, self._db, ctx.task_id, response.raw_text)
        self.wrote.append(ctx.task_id)
        return f"fictional-draft:{ctx.task_id}"


class TwoSteps:
    """Step 1 keeps its output in memory and beats; it then waits for the test; between the
    steps it checks for a cancel; step 2 saves both parts as a draft."""

    def __init__(self, db: Database, *, beat_before_check: bool = False) -> None:
        self._db = db
        self._beat_before_check = beat_before_check
        self.contexts: list[Any] = []
        self.step1_done = asyncio.Event()
        self.gate = asyncio.Event()
        self.partial: list[str] = []
        self.step2_started: list[str] = []

    async def __call__(self, ctx: Any) -> str | None:
        self.contexts.append(ctx)
        self.partial.append("Fictional opening paragraph for Example Ltd.")
        await ctx.heartbeat(50)
        self.step1_done.set()
        await self.gate.wait()
        if self._beat_before_check:
            await ctx.heartbeat(60)
        ctx.check_cancelled()
        self.step2_started.append(ctx.task_id)
        body = " ".join([*self.partial, "Fictional closing paragraph."])
        await asyncio.to_thread(_write_draft, self._db, ctx.task_id, body)
        return f"fictional-draft:{ctx.task_id}"


class Checked:
    """Checks for a cancel between two beats and returns: a task nobody cancels."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def __call__(self, ctx: Any) -> str | None:
        self.calls.append(ctx)
        ctx.check_cancelled()
        await ctx.heartbeat(50)
        ctx.check_cancelled()
        return "fictional-draft-checked"


class CancelOnSuccess:
    """Wraps the service: the first succeeded ``finish`` is preceded by a cancel request,
    so the cancel lands just before the success commits."""

    def __init__(self, service: Any) -> None:
        self._service = service
        self.armed = True
        self.finishes: list[tuple[str, str, bool]] = []

    def finish(self, task_id: str, status: Any, **kwargs: Any) -> bool:
        if self.armed and str(status) == "succeeded":
            self.armed = False
            self._service.request_cancel(task_id)
        changed: bool = self._service.finish(task_id, status, **kwargs)
        self.finishes.append((task_id, str(status), changed))
        return changed

    def __getattr__(self, name: str) -> Any:
        return getattr(self._service, name)


class Harness:
    """A real ``TaskService`` whose ``on_change`` records changes, wakes the runner for queued
    tasks and tells it about running ones (where a cancel request shows), as the app wires it."""

    def __init__(
        self,
        db: Database,
        handlers: dict[str, Any],
        *,
        wrap: Callable[[Any], Any] | None = None,
    ) -> None:
        from app.services.tasks import TaskService
        from app.tasks import TaskRunner

        self.changes = Changes(asyncio.get_running_loop())
        self.runner = TaskRunner(registry=_registry(handlers))
        self.service = TaskService(
            db, FrozenClock(START), SequentialIdFactory("task"), self._on_change
        )
        self.runner_service = self.service if wrap is None else wrap(self.service)

    def _on_change(self, change: Any) -> None:
        self.changes(change)
        status = str(change.status)
        if status == "queued":
            self.runner.notify()
        elif status == "running":
            self.runner.notify_cancel(change.id)

    async def start(self) -> None:
        self.runner.attach(self.runner_service, settings=_settings())
        await self.runner.start()

    async def enqueue(self, task_type: str = DRAFTING, target: str = "application-1") -> str:
        ref = await asyncio.to_thread(self.service.enqueue, task_type, target)
        assert ref.created is True
        task_id: str = ref.id
        return task_id

    async def cancel(self, task_id: str) -> Any:
        """``request_cancel`` from another thread, as a route's worker thread calls it."""
        return await asyncio.to_thread(self.service.request_cancel, task_id)

    async def row(self, task_id: str) -> Any:
        row = await asyncio.to_thread(self.service.get, task_id)
        assert row is not None
        return row


def _assert_cancelled_cleanly(row: Any, changes: Changes) -> None:
    assert row.status == _status("cancelled")
    assert row.result_ref is None, "partial output must not be referenced"
    assert row.error is None, "a cancel is not a failure"
    assert row.finished_at is not None
    seen = changes.statuses(row.id)
    assert "failed" not in seen and "interrupted" not in seen and "succeeded" not in seen


# --- AC-M2.4 ------------------------------------------------------------------------------


async def test_ac_m2_4_cancel_during_provider_call_ends_cancelled_and_discards_output(
    db: Database,
) -> None:
    provider = TokenSpy()
    provider.script(ScriptedResponse(data=FICTIONAL_ANSWER, delay_seconds=LONG_CALL_SECONDS))
    handler = AIHandler(db, provider)
    h = Harness(db, {DRAFTING: handler})
    task_id = await h.enqueue()
    await h.start()
    try:
        await _wait_until(lambda: provider.hang.started == 1, "the provider call started")

        row = await h.cancel(task_id)  # from a worker thread, as the cancel route does
        assert row is not None and row.status == _status("running")
        await h.changes.wait_status(task_id, "cancelled")
    finally:
        await h.runner.stop()

    assert provider.hang.cancelled == 1, "the in-flight provider call must be aborted"
    assert provider.raised and provider.raised[0] in {"CancelledError", "CallCancelled"}
    assert provider.returned == 0
    assert handler.wrote == [], "the handler's later writes must never run"
    assert await asyncio.to_thread(_drafts, db) == []
    _assert_cancelled_cleanly(await h.row(task_id), h.changes)


async def test_ac_m2_4_cancel_passes_the_tasks_flag_to_the_provider(db: Database) -> None:
    provider = TokenSpy()
    provider.script(ScriptedResponse(data=FICTIONAL_ANSWER, delay_seconds=LONG_CALL_SECONDS))
    handler = AIHandler(db, provider)
    h = Harness(db, {DRAFTING: handler})
    task_id = await h.enqueue()
    await h.start()
    try:
        await _wait_until(lambda: provider.hang.started == 1, "the provider call started")
        (ctx,) = handler.contexts
        assert provider.tokens == [ctx.cancel_flag]
        assert provider.tokens[0] is ctx.cancel_flag, "the provider gets the task's own flag"
        assert ctx.cancel_flag.cancelled is False

        await h.cancel(task_id)
        await h.changes.wait_status(task_id, "cancelled")
    finally:
        await h.runner.stop()

    assert provider.tokens[0].cancelled is True, "the database flag is mirrored to the token"
    assert provider.hang.token_set_at_cancel == [True], (
        "the flag must be set before the call is cancelled, so a provider sees it"
    )


async def test_ac_m2_4_cancel_between_steps_discards_partial_output(db: Database) -> None:
    handler = TwoSteps(db)
    h = Harness(db, {DRAFTING: handler})
    task_id = await h.enqueue()
    await h.start()
    try:
        await asyncio.wait_for(handler.step1_done.wait(), DEADLINE_S)
        (ctx,) = handler.contexts

        await h.cancel(task_id)
        # Mirrored from the request itself, not from a later heartbeat.
        await _wait_until(
            lambda: ctx.cancel_flag.cancelled or h.changes.statuses(task_id)[-1] == "cancelled",
            "the cancel request reached the running task",
        )
        handler.gate.set()
        await h.changes.wait_status(task_id, "cancelled")
    finally:
        await h.runner.stop()

    assert handler.partial, "step 1 ran and kept its output in memory"
    assert handler.step2_started == [], "step 2 must not start after a cancel"
    assert await asyncio.to_thread(_drafts, db) == []
    _assert_cancelled_cleanly(await h.row(task_id), h.changes)


async def test_ac_m2_4_cancel_from_another_process_seen_on_heartbeat(db: Database) -> None:
    handler = TwoSteps(db, beat_before_check=True)
    h = Harness(db, {DRAFTING: handler})
    task_id = await h.enqueue()
    await h.start()
    try:
        await asyncio.wait_for(handler.step1_done.wait(), DEADLINE_S)
        (ctx,) = handler.contexts

        await asyncio.to_thread(_flag_in_database, db, task_id)  # no on_change for this one
        handler.gate.set()
        await h.changes.wait_status(task_id, "cancelled")
    finally:
        await h.runner.stop()

    assert ctx.cancel_flag.cancelled is True, "the heartbeat mirrors the database flag"
    assert handler.step2_started == []
    assert await asyncio.to_thread(_drafts, db) == []
    _assert_cancelled_cleanly(await h.row(task_id), h.changes)


async def test_ac_m2_4_call_cancelled_from_provider_ends_cancelled_not_failed(
    db: Database,
) -> None:
    """The flag reaches the task on a heartbeat (another process); the handler doesn't check
    it and calls the provider, which refuses with ``CallCancelled``."""
    provider = TokenSpy()
    provider.script(ScriptedResponse(data=FICTIONAL_ANSWER))
    gate = asyncio.Event()
    contexts: list[Any] = []

    async def handler(ctx: Any) -> str | None:
        contexts.append(ctx)
        await gate.wait()
        await ctx.heartbeat(20)  # sees the flag set by the other process
        response = await provider.run(make_request(task=AI_TASK), ctx.cancel_flag)
        await asyncio.to_thread(_write_draft, db, ctx.task_id, response.raw_text)
        return "fictional-draft-unreachable"

    h = Harness(db, {DRAFTING: handler})
    task_id = await h.enqueue()
    await h.start()
    try:
        await _wait_until(lambda: contexts, "the handler started")
        await asyncio.to_thread(_flag_in_database, db, task_id)
        gate.set()
        await h.changes.wait_status(task_id, "cancelled")
    finally:
        await h.runner.stop()

    # Either the heartbeat raised the cancel signal (no call made) or the provider refused.
    assert provider.raised in ([], [CallCancelled.__name__])
    assert provider.returned == 0
    assert await asyncio.to_thread(_drafts, db) == []
    _assert_cancelled_cleanly(await h.row(task_id), h.changes)


async def test_ac_m2_4_cancel_just_before_success_commits_ends_cancelled(db: Database) -> None:
    async def handler(ctx: Any) -> str | None:
        return "fictional-draft-too-late"

    wrapper: list[CancelOnSuccess] = []

    def wrap(service: Any) -> Any:
        wrapper.append(CancelOnSuccess(service))
        return wrapper[0]

    h = Harness(db, {DRAFTING: handler}, wrap=wrap)
    task_id = await h.enqueue()
    await h.start()
    try:
        await h.changes.wait_status(task_id, "cancelled")
    finally:
        await h.runner.stop()

    (spy,) = wrapper
    assert (task_id, "succeeded", False) in spy.finishes, (
        "the success update must be refused once a cancel was requested"
    )
    _assert_cancelled_cleanly(await h.row(task_id), h.changes)


async def test_finish_succeeded_refused_while_cancel_requested(db: Database) -> None:
    from app.domain.tasks import TaskStatus

    h = Harness(db, {})
    task_id = await h.enqueue()
    claimed = await asyncio.to_thread(h.service.claim_next)
    assert claimed is not None and claimed.id == task_id
    await h.cancel(task_id)

    refused = await asyncio.to_thread(
        h.service.finish, task_id, TaskStatus.SUCCEEDED, result_ref="fictional-draft-late"
    )
    assert refused is False
    row = await h.row(task_id)
    assert row.status == _status("running") and row.result_ref is None

    assert await asyncio.to_thread(h.service.finish, task_id, TaskStatus.CANCELLED) is True
    assert (await h.row(task_id)).status == _status("cancelled")


async def test_cancel_after_success_is_ignored(db: Database) -> None:
    handler = Checked()
    h = Harness(db, {DRAFTING: handler})
    task_id = await h.enqueue()
    await h.start()
    try:
        await h.changes.wait_status(task_id, "succeeded")
        events_before = len(h.changes.statuses(task_id))

        row = await h.cancel(task_id)
        await _settle()
    finally:
        await h.runner.stop()

    assert row is not None and row.status == _status("succeeded")
    after = await h.row(task_id)
    assert after.status == _status("succeeded")
    assert after.result_ref == "fictional-draft-checked"
    assert after.cancel_requested is False
    assert len(h.changes.statuses(task_id)) == events_before, "an ignored cancel publishes nothing"


async def test_uncancelled_task_passes_its_checks_and_succeeds(db: Database) -> None:
    """A claim's ``running`` change reaches ``notify_cancel`` too; with no flag set it must
    cancel nothing, and ``check_cancelled`` lets the handler through."""
    handler = Checked()
    h = Harness(db, {DRAFTING: handler})
    task_id = await h.enqueue()
    await h.start()
    try:
        await h.changes.wait_status(task_id, "succeeded")
    finally:
        await h.runner.stop()

    (ctx,) = handler.calls
    assert ctx.cancel_flag.cancelled is False
    row = await h.row(task_id)
    assert row.status == _status("succeeded")
    assert row.result_ref == "fictional-draft-checked"


async def test_cancel_queued_never_calls_provider(db: Database) -> None:
    provider = TokenSpy()
    provider.script(ScriptedResponse(data=FICTIONAL_ANSWER))
    handler = AIHandler(db, provider)
    h = Harness(db, {DRAFTING: handler})
    task_id = await h.enqueue()

    row = await h.cancel(task_id)  # before any workspace is attached: still queued
    assert row is not None and row.status == _status("cancelled")
    await h.start()
    try:
        await _settle()
    finally:
        await h.runner.stop()

    assert handler.contexts == []
    assert provider.tokens == [] and provider.inner.requests == []
    assert await asyncio.to_thread(_drafts, db) == []
    assert h.changes.statuses(task_id) == ["queued", "cancelled"]
    _assert_cancelled_cleanly(await h.row(task_id), h.changes)


async def test_shutdown_is_interrupted_not_cancelled(db: Database) -> None:
    provider = TokenSpy()
    provider.script(ScriptedResponse(data=FICTIONAL_ANSWER, delay_seconds=LONG_CALL_SECONDS))
    handler = AIHandler(db, provider)
    h = Harness(db, {DRAFTING: handler})
    task_id = await h.enqueue()
    await h.start()
    try:
        await _wait_until(lambda: provider.hang.started == 1, "the provider call started")
    finally:
        await h.runner.stop()

    assert provider.hang.cancelled == 1, "stop aborts the in-flight call"
    row = await h.row(task_id)
    assert row.status == _status("interrupted")
    assert row.result_ref is None
    assert row.cancel_requested is False
    assert "cancelled" not in h.changes.statuses(task_id)
    assert await asyncio.to_thread(_drafts, db) == []


# --- code review of M2.11 -------------------------------------------------------------------


class CancelRightAfterClaim:
    """Wraps the service; the cancel is requested in the claiming thread, right after the
    claim commits and before the runner has registered the handler."""

    def __init__(self, service: Any) -> None:
        self._service = service

    def claim_next(self) -> Any:
        row = self._service.claim_next()
        if row is not None:
            assert self._service.request_cancel(row.id) is not None
        return row

    def __getattr__(self, name: str) -> Any:
        return getattr(self._service, name)


async def test_cancel_between_claim_and_handler_start_is_not_lost(db: Database) -> None:
    """The handler calls the provider without a heartbeat or check first, so only the
    runner can deliver this cancel."""
    provider = TokenSpy()
    provider.script(ScriptedResponse(data=FICTIONAL_ANSWER, delay_seconds=LONG_CALL_SECONDS))

    async def handler(ctx: Any) -> str | None:
        response = await provider.run(make_request(task=AI_TASK), ctx.cancel_flag)
        await asyncio.to_thread(_write_draft, db, ctx.task_id, response.raw_text)
        return "fictional-draft-unreachable"

    h = Harness(db, {DRAFTING: handler}, wrap=CancelRightAfterClaim)
    task_id = await h.enqueue()
    await h.start()
    try:
        await h.changes.wait_status(task_id, "cancelled")
    finally:
        await h.runner.stop()

    assert provider.returned == 0
    assert provider.hang.cancelled == provider.hang.started, "a started call is aborted"
    assert await asyncio.to_thread(_drafts, db) == []
    _assert_cancelled_cleanly(await h.row(task_id), h.changes)


async def test_shutdown_with_unmirrored_cancel_request_is_interrupted(db: Database) -> None:
    """The flag is set in the database by another process (no notice, no heartbeat since):
    a shutdown still ends the task interrupted."""
    provider = TokenSpy()
    provider.script(ScriptedResponse(data=FICTIONAL_ANSWER, delay_seconds=LONG_CALL_SECONDS))
    handler = AIHandler(db, provider)
    h = Harness(db, {DRAFTING: handler})
    task_id = await h.enqueue()
    await h.start()
    try:
        await _wait_until(lambda: provider.hang.started == 1, "the provider call started")
        await asyncio.to_thread(_flag_in_database, db, task_id)
    finally:
        await h.runner.stop()

    row = await h.row(task_id)
    assert row.status == _status("interrupted")
    assert row.result_ref is None
    assert "cancelled" not in h.changes.statuses(task_id)
    assert await asyncio.to_thread(_drafts, db) == []


class Swallowing:
    """Swallows the runner's cancellation and returns a result anyway (a badly behaved
    handler); with ``beat_after`` it beats once more after swallowing it."""

    def __init__(self, *, beat_after: bool = False) -> None:
        self._beat_after = beat_after
        self.started = asyncio.Event()
        self.swallowed: list[str] = []
        self.after_beat: list[str] = []

    async def __call__(self, ctx: Any) -> str | None:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.swallowed.append(ctx.task_id)
        if self._beat_after:
            await ctx.heartbeat(90)  # the cancel is delivered again here
            self.after_beat.append(ctx.task_id)
        return "fictional-draft-after-cancel"


@pytest.mark.parametrize("beat_after", [False, True], ids=["returns", "beats_then_returns"])
async def test_handler_that_swallows_the_cancel_still_ends_cancelled(
    db: Database, beat_after: bool
) -> None:
    handler = Swallowing(beat_after=beat_after)
    h = Harness(db, {DRAFTING: handler})
    task_id = await h.enqueue()
    await h.start()
    try:
        await asyncio.wait_for(handler.started.wait(), DEADLINE_S)
        await h.cancel(task_id)
        await h.changes.wait_status(task_id, "cancelled")
    finally:
        await h.runner.stop()

    assert handler.swallowed == [task_id]
    assert handler.after_beat == [], "a heartbeat after the cancel raises the cancel signal"
    _assert_cancelled_cleanly(await h.row(task_id), h.changes)
