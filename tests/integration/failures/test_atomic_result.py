"""Atomic results and failure injection (M2.12; AC-M2.5; spec 5 M2 reliability rule 3).

API assumed here (proposed for the implementer):

- ``await ctx.complete(write) -> str``: ``write`` is a sync callable ``write(conn) -> result_ref``
  supplied by the handler's service. In one worker thread and ONE ``db.transaction()`` the
  runner's service runs ``write(conn)`` and then the conditional update running -> succeeded
  with that ``result_ref`` (``WHERE id = ? AND attempt = ? AND status = 'running' AND
  cancel_requested = 0``). ``complete`` returns the ``result_ref``; the task is already
  succeeded when it returns. The runner service gets a blocking method
  ``complete(task_id, write, *, attempt) -> ...`` (the name ``complete`` is assumed by the
  ``CancelBeforeComplete`` wrapper below; its return shape is the implementer's choice).
- A refused update rolls everything back, the writer's rows included, and ``complete`` raises
  a signal: ``TaskCancelled`` (``app.tasks.context``) when a cancel was requested, so the task
  ends cancelled; ``TaskSuperseded`` (new, ``app.tasks.context``) when the task (or that
  attempt of it) is no longer running (interrupted by the watchdog, or a later attempt is
  running), so the row stays as it is. A second ``complete`` in the same handler is refused
  the same way and leaves no second result.
- After a successful ``complete`` the runner never finishes the task again: whatever the
  handler then returns (None, another value) or raises is ignored; the task stays succeeded
  with the writer's ``result_ref``.
- An exception in the writer, or between the write and the status update, rolls the writer's
  rows back and propagates out of ``complete``; the runner marks the task failed with the
  error's ``kind`` (an ``AppError``) or ``TaskFailed`` (anything else), never its text.
- Results are new rows: a retry or a later task of the same type and target inserts a new
  draft and never touches an earlier one.

Failure injection stays in the test: a writer that raises after inserting, and a test-created
SQLite trigger that aborts the running -> succeeded update (so the failure lands after the
writer ran, inside the same transaction, without any production flag).

Real migrated SQLite in a temp folder with a test-created ``fictional_drafts`` table,
``FrozenClock``, ``SequentialIdFactory``; every wait has a deadline; no fixed sleeps. All values
are fictional.
"""

import asyncio
import sqlite3
import threading
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from app.domain.errors import AppError
from app.modules.storage import Database
from app.tasks.context import TaskCancelled
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
PAST_TIMEOUT = timedelta(minutes=6)  # the test registry's timeout is 5 minutes
TYPE_TIMEOUT = timedelta(minutes=5)
FICTIONAL_ERROR_TEXT = "Fictional disk trouble while saving a letter for Ada Example"


class FictionalStorageFull(AppError):
    """A fictional domain error a writer might raise; stored by its kind only."""

    kind = "FictionalStorageFull"
    message = "Fictional: the drafts folder is full."


# --- helpers ------------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    (tmp_path / "data").mkdir()
    database = _open_migrated(tmp_path)
    with database.transaction() as conn:
        conn.execute(
            "CREATE TABLE fictional_drafts (id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, "
            "attempt INTEGER NOT NULL, body TEXT NOT NULL)"
        )
    try:
        yield database
    finally:
        database.close()


def _drafts(db: Database) -> list[tuple[int, str, int, str]]:
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT id, task_id, attempt, body FROM fictional_drafts ORDER BY id"
        ).fetchall()
    return [(int(i), str(t), int(a), str(b)) for i, t, a, b in rows]


def _fail_success_updates(db: Database) -> None:
    """Failure injection between the write and the success: the running -> succeeded update
    aborts inside the same transaction, after the writer ran."""
    with db.transaction() as conn:
        conn.execute(
            "CREATE TRIGGER fictional_fail_success BEFORE UPDATE OF status ON tasks "
            "WHEN NEW.status = 'succeeded' "
            "BEGIN SELECT RAISE(ABORT, 'fictional injected failure'); END"
        )


def _flag_in_database(db: Database, task_id: str) -> None:
    """Another process asks for the cancel: the row's flag is set, no ``on_change`` fires."""
    from app.modules.storage import tasks_repo

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


class DraftWriter:
    """Builds the sync writer a service passes to ``ctx.complete``: it inserts one draft and
    returns its ref, or raises ``fail_with`` after inserting. Records how it was called."""

    def __init__(self, body: str = "Fictional cover letter for Example Ltd.") -> None:
        self.body = body
        self.fail_with: dict[int, BaseException] = {}  # by attempt
        self.calls: list[str] = []
        self.threads: list[int] = []
        self.in_transaction: list[bool] = []
        self.status_seen: list[str] = []

    def for_task(self, ctx: Any) -> Callable[[sqlite3.Connection], str]:
        def write(conn: sqlite3.Connection) -> str:
            self.calls.append(ctx.task_id)
            self.threads.append(threading.get_ident())
            self.in_transaction.append(conn.in_transaction)
            (status,) = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (ctx.task_id,)
            ).fetchone()
            self.status_seen.append(str(status))
            cursor = conn.execute(
                "INSERT INTO fictional_drafts (task_id, attempt, body) VALUES (?, ?, ?)",
                (ctx.task_id, ctx.attempt, self.body),
            )
            failure = self.fail_with.get(ctx.attempt)
            if failure is not None:
                raise failure
            return f"fictional-draft:{cursor.lastrowid}"

        return write


class Completing:
    """A handler that (optionally) waits at a gate, then saves its result through
    ``ctx.complete``. It records what each ``complete`` returned or raised. With
    ``swallow_cancel`` it swallows a cancellation at the gate and completes anyway (a badly
    behaved handler racing the watchdog)."""

    def __init__(
        self,
        writer: DraftWriter,
        *,
        gated: bool = False,
        swallow_cancel: bool = False,
        then: Callable[[Any, str], Any] | None = None,
    ) -> None:
        self.writer = writer
        self.gated = gated
        self.swallow_cancel = swallow_cancel
        self.then = then
        self.contexts: list[Any] = []
        self.outcomes: list[object] = []  # a result_ref or the exception complete raised
        self.swallowed: list[str] = []
        self.at_gate = asyncio.Event()
        self.gate = asyncio.Event()
        self.loop_thread = threading.get_ident()

    async def complete(self, ctx: Any) -> str:
        try:
            ref: str = await ctx.complete(self.writer.for_task(ctx))
        except BaseException as error:
            self.outcomes.append(error)
            raise
        self.outcomes.append(ref)
        return ref

    async def __call__(self, ctx: Any) -> str | None:
        self.contexts.append(ctx)
        if self.gated:
            self.at_gate.set()
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                if not self.swallow_cancel:
                    raise
                self.swallowed.append(ctx.task_id)
        ref = await self.complete(ctx)
        if self.then is not None:
            result: str | None = await self.then(ctx, ref)
            return result
        return ref


class CancelBeforeComplete:
    """Wraps the service: the first ``complete`` is preceded by a cancel request (published,
    so the runner hears it), so the cancel lands just before the result's transaction."""

    def __init__(self, service: Any) -> None:
        self._service = service
        self.armed = True

    def complete(self, task_id: str, *args: Any, **kwargs: Any) -> Any:
        if self.armed:
            self.armed = False
            assert self._service.request_cancel(task_id) is not None
        return self._service.complete(task_id, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._service, name)


class Harness:
    """A real ``TaskService`` with a ``FrozenClock`` the test can move; ``on_change`` records
    changes, wakes the runner for queued tasks and tells it about running ones."""

    def __init__(
        self,
        db: Database,
        handlers: dict[str, Any],
        *,
        wrap: Callable[[Any], Any] | None = None,
        max_concurrent: int = 1,
    ) -> None:
        from app.services.tasks import TaskService
        from app.tasks import TaskRunner

        self.db = db
        self.clock = FrozenClock(START)
        self.max_concurrent = max_concurrent
        self.changes = Changes(asyncio.get_running_loop())
        self.runner = TaskRunner(registry=_registry(handlers))
        self.service = TaskService(db, self.clock, SequentialIdFactory("task"), self._on_change)
        self.runner_service = self.service if wrap is None else wrap(self.service)

    def _on_change(self, change: Any) -> None:
        self.changes(change)
        status = str(change.status)
        if status == "queued":
            self.runner.notify()
        elif status == "running":
            self.runner.notify_cancel(change.id)

    async def start(self) -> None:
        self.runner.attach(self.runner_service, settings=_settings(self.max_concurrent))
        await self.runner.start()

    async def enqueue(self, target: str = "application-1") -> str:
        ref = await asyncio.to_thread(self.service.enqueue, DRAFTING, target)
        assert ref.created is True
        task_id: str = ref.id
        return task_id

    async def row(self, task_id: str) -> Any:
        row = await asyncio.to_thread(self.service.get, task_id)
        assert row is not None
        return row

    async def drafts(self) -> list[tuple[int, str, int, str]]:
        return await asyncio.to_thread(_drafts, self.db)


def _assert_no_success(row: Any, changes: Changes) -> None:
    assert row.result_ref is None, "a rolled-back result must not be referenced"
    assert "succeeded" not in changes.statuses(row.id)


# --- AC-M2.5 ------------------------------------------------------------------------------


async def test_ac_m2_5_complete_saves_result_and_success_in_one_transaction(
    db: Database,
) -> None:
    writer = DraftWriter()
    states: list[Any] = []
    h: Harness

    async def then(ctx: Any, ref: str) -> str:
        states.append(await h.row(ctx.task_id))  # before the handler returns
        return ref

    handler = Completing(writer, then=then)
    h = Harness(db, {DRAFTING: handler})
    task_id = await h.enqueue()
    await h.start()
    try:
        await h.changes.wait_status(task_id, "succeeded")
        # The change is published from the saving thread: the handler may not have resumed
        # yet, and stopping the runner now would cancel it before ``then`` has read the row.
        await _wait_until(lambda: states, "the handler read the row after complete")
    finally:
        await h.runner.stop()

    (ref,) = handler.outcomes
    ((draft_id, draft_task, draft_attempt, body),) = await h.drafts()
    assert ref == f"fictional-draft:{draft_id}"
    assert (draft_task, draft_attempt, body) == (task_id, 1, writer.body)
    assert writer.calls == [task_id]
    assert writer.threads[0] != handler.loop_thread, "the writer runs in a worker thread"
    assert writer.in_transaction == [True], "the writer runs inside the transaction"
    assert writer.status_seen == ["running"], "the write comes before the status update"
    (seen,) = states
    assert seen.status == _status("succeeded") and seen.result_ref == ref, (
        "complete returns only once the success is committed"
    )
    row = await h.row(task_id)
    assert row.status == _status("succeeded")
    assert row.result_ref == ref and row.error is None
    assert h.changes.statuses(task_id).count("succeeded") == 1


@pytest.mark.parametrize("where", ["in_writer", "before_status_update"])
async def test_ac_m2_5_failure_between_result_and_success_leaves_no_orphaned_draft(
    db: Database, where: str
) -> None:
    writer = DraftWriter()
    if where == "in_writer":
        writer.fail_with[1] = FictionalStorageFull(detail=FICTIONAL_ERROR_TEXT)
        expected_kind = "FictionalStorageFull"
    else:
        await asyncio.to_thread(_fail_success_updates, db)
        expected_kind = "TaskFailed"
    handler = Completing(writer)
    h = Harness(db, {DRAFTING: handler})
    task_id = await h.enqueue()
    await h.start()
    try:
        await h.changes.wait_status(task_id, "failed")
    finally:
        await h.runner.stop()

    assert writer.calls == [task_id], "the writer ran and inserted its draft"
    assert await h.drafts() == [], "the draft must be rolled back with the failed success"
    row = await h.row(task_id)
    assert row.status == _status("failed")
    assert row.error == expected_kind
    _assert_no_success(row, h.changes)


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (FictionalStorageFull(detail=FICTIONAL_ERROR_TEXT), "FictionalStorageFull"),
        (RuntimeError(FICTIONAL_ERROR_TEXT), "TaskFailed"),
        (sqlite3.IntegrityError(FICTIONAL_ERROR_TEXT), "TaskFailed"),
    ],
    ids=["app_error", "runtime_error", "database_error"],
)
async def test_injected_failure_marks_task_failed_with_kind(
    db: Database, error: BaseException, kind: str
) -> None:
    writer = DraftWriter()
    writer.fail_with[1] = error
    handler = Completing(writer)
    h = Harness(db, {DRAFTING: handler})
    task_id = await h.enqueue()
    await h.start()
    try:
        await h.changes.wait_status(task_id, "failed")
    finally:
        await h.runner.stop()

    assert writer.calls == [task_id]
    (raised,) = handler.outcomes
    assert raised is error, "complete propagates the writer's own exception"
    row = await h.row(task_id)
    assert row.status == _status("failed")
    assert row.error == kind
    assert FICTIONAL_ERROR_TEXT not in (row.error or ""), "only the kind is stored"
    assert await h.drafts() == []
    _assert_no_success(row, h.changes)


@pytest.mark.parametrize("watchdog", ["other_process", "local_swallowed"])
async def test_result_rolled_back_when_task_interrupted_meanwhile(
    db: Database, watchdog: str
) -> None:
    """The row is interrupted while the handler is before ``complete``: by a watchdog that
    doesn't cancel this handler (another process), or by the runner's watchdog whose
    cancellation the handler swallows."""
    writer = DraftWriter()
    handler = Completing(writer, gated=True, swallow_cancel=True)
    h = Harness(db, {DRAFTING: handler})
    task_id = await h.enqueue()
    await h.start()
    try:
        await asyncio.wait_for(handler.at_gate.wait(), DEADLINE_S)
        h.clock.advance(PAST_TIMEOUT)
        if watchdog == "other_process":
            stale = await asyncio.to_thread(h.service.interrupt_stale, DRAFTING, TYPE_TIMEOUT)
            assert stale == [task_id]
            handler.gate.set()
        else:
            assert await h.runner.check_watchdog_once() == [task_id]
        await _wait_until(lambda: handler.outcomes, "the handler called complete")
        await _settle()
    finally:
        await h.runner.stop()

    (raised,) = handler.outcomes
    assert type(raised).__name__ == "TaskSuperseded", (
        f"complete on an interrupted task must raise TaskSuperseded, got {raised!r}"
    )
    assert await h.drafts() == [], "the writer's rows are rolled back"
    row = await h.row(task_id)
    assert row.status == _status("interrupted"), "interrupted stays interrupted"
    assert row.error is None
    _assert_no_success(row, h.changes)


@pytest.mark.parametrize("how", ["flag_from_another_process", "cancel_just_before_complete"])
async def test_result_rolled_back_when_cancelled_meanwhile(db: Database, how: str) -> None:
    """``cancel_just_before_complete`` publishes the request, so the runner's cancel probe may
    cancel the handler while ``complete`` is in flight: it may see ``CancelledError`` rather
    than ``TaskCancelled``. The refused-for-cancel path that raises ``TaskCancelled`` is
    pinned by ``flag_from_another_process`` (no event, only the database flag)."""
    writer = DraftWriter()
    if how == "flag_from_another_process":
        handler = Completing(writer, gated=True)
        h = Harness(db, {DRAFTING: handler})
    else:
        handler = Completing(writer)
        h = Harness(db, {DRAFTING: handler}, wrap=CancelBeforeComplete)
    task_id = await h.enqueue()
    await h.start()
    try:
        if how == "flag_from_another_process":
            await asyncio.wait_for(handler.at_gate.wait(), DEADLINE_S)
            await asyncio.to_thread(_flag_in_database, db, task_id)  # no on_change
            handler.gate.set()
        await h.changes.wait_status(task_id, "cancelled")
        # As above: let the handler see what ``complete`` raised before the runner stops,
        # or the stop's own cancellation is what it records.
        await _wait_until(lambda: handler.outcomes, "the handler called complete")
    finally:
        await h.runner.stop()

    if how == "flag_from_another_process":
        (raised,) = handler.outcomes
        assert isinstance(raised, TaskCancelled), f"expected TaskCancelled, got {raised!r}"
    else:
        (raised,) = handler.outcomes
        assert isinstance(raised, (TaskCancelled, asyncio.CancelledError)), raised
    assert await h.drafts() == [], "the writer's rows are rolled back"
    row = await h.row(task_id)
    assert row.status == _status("cancelled")
    assert row.error is None
    _assert_no_success(row, h.changes)
    assert "failed" not in h.changes.statuses(task_id)


async def test_retry_result_is_new_row(db: Database) -> None:
    writer = DraftWriter()
    writer.fail_with[1] = FictionalStorageFull(detail=FICTIONAL_ERROR_TEXT)
    handler = Completing(writer)
    h = Harness(db, {DRAFTING: handler})
    task_id = await h.enqueue()
    await h.start()
    try:
        # Attempt 1 fails after its write; the write is rolled back.
        await h.changes.wait_status(task_id, "failed")
        assert await h.drafts() == []

        # The retry (attempt 2) completes: one new draft.
        retried = await asyncio.to_thread(h.service.retry, task_id)
        assert retried is not None and retried.attempt == 2
        await h.changes.wait_status(task_id, "succeeded")
        first_drafts = await h.drafts()
        ((first_id, first_task, first_attempt, first_body),) = first_drafts
        assert (first_task, first_attempt) == (task_id, 2)
        first_row = await h.row(task_id)
        assert first_row.result_ref == f"fictional-draft:{first_id}"

        # A later task for the same type and target writes a second draft.
        writer.fail_with.clear()  # the injected failure was for the first task only
        writer.body = "Fictional second cover letter for Example Ltd."
        second_id_task = await h.enqueue()
        assert second_id_task != task_id
        await h.changes.wait_status(second_id_task, "succeeded")
    finally:
        await h.runner.stop()

    drafts = await h.drafts()
    assert len(drafts) == 2, "a new result is a new row"
    assert drafts[0] == (first_id, first_task, first_attempt, first_body), (
        "the earlier draft is never overwritten"
    )
    second_draft_id, second_task, _attempt, second_body = drafts[1]
    assert second_draft_id != first_id
    assert second_task == second_id_task and second_body == writer.body
    assert (await h.row(task_id)).result_ref == first_row.result_ref
    assert (await h.row(second_id_task)).result_ref == f"fictional-draft:{second_draft_id}"


async def test_complete_called_twice_is_refused_and_keeps_first_result(db: Database) -> None:
    writer = DraftWriter()
    second: list[object] = []

    async def then(ctx: Any, ref: str) -> str:
        try:
            await ctx.complete(writer.for_task(ctx))
        except Exception as error:
            second.append(error)
        else:
            second.append("accepted")
        return ref

    handler = Completing(writer, then=then)
    h = Harness(db, {DRAFTING: handler})
    task_id = await h.enqueue()
    await h.start()
    try:
        await h.changes.wait_status(task_id, "succeeded")
        await _wait_until(lambda: second, "the second complete returned")
        await _settle()
    finally:
        await h.runner.stop()

    (first_ref,) = handler.outcomes
    (outcome,) = second
    assert isinstance(outcome, Exception), "a second complete must be refused"
    assert not isinstance(outcome, TaskCancelled), "nobody cancelled this task"
    drafts = await h.drafts()
    assert len(drafts) == 1, "the refused second result leaves no row"
    assert first_ref == f"fictional-draft:{drafts[0][0]}"
    row = await h.row(task_id)
    assert row.status == _status("succeeded") and row.result_ref == first_ref
    assert h.changes.statuses(task_id).count("succeeded") == 1


async def test_complete_from_stale_attempt_is_refused(db: Database) -> None:
    """Attempt 1 outlives its interruption (it swallows the watchdog's cancel) and completes
    while attempt 2 is running: refused by the attempt match; only attempt 2's draft lands."""
    writer = DraftWriter()
    gates = {1: asyncio.Event(), 2: asyncio.Event()}
    at_gate = {1: asyncio.Event(), 2: asyncio.Event()}
    outcomes: dict[int, list[object]] = {1: [], 2: []}

    async def handler(ctx: Any) -> str | None:
        at_gate[ctx.attempt].set()
        try:
            await gates[ctx.attempt].wait()
        except asyncio.CancelledError:
            await gates[ctx.attempt].wait()  # swallowed: carries on once released
        try:
            ref: str = await ctx.complete(writer.for_task(ctx))
        except BaseException as error:
            outcomes[ctx.attempt].append(error)
            raise
        outcomes[ctx.attempt].append(ref)
        return ref

    h = Harness(db, {DRAFTING: handler}, max_concurrent=2)
    task_id = await h.enqueue()
    await h.start()
    try:
        await asyncio.wait_for(at_gate[1].wait(), DEADLINE_S)
        h.clock.advance(PAST_TIMEOUT)
        assert await h.runner.check_watchdog_once() == [task_id]

        retried = await asyncio.to_thread(h.service.retry, task_id)
        assert retried is not None and retried.attempt == 2
        await asyncio.wait_for(at_gate[2].wait(), DEADLINE_S)

        gates[1].set()  # the stale attempt tries to complete while attempt 2 runs
        await _wait_until(lambda: outcomes[1], "attempt 1 called complete")
        assert await h.drafts() == [], "the stale attempt's write is rolled back"
        mid = await h.row(task_id)
        assert mid.status == _status("running") and mid.attempt == 2
        assert mid.result_ref is None

        gates[2].set()
        await h.changes.wait_status(task_id, "succeeded")
    finally:
        await h.runner.stop()

    (stale,) = outcomes[1]
    assert type(stale).__name__ == "TaskSuperseded", (
        f"a stale attempt's complete must raise TaskSuperseded, got {stale!r}"
    )
    (ref,) = outcomes[2]
    ((draft_id, draft_task, draft_attempt, _body),) = await h.drafts()
    assert (draft_task, draft_attempt) == (task_id, 2)
    row = await h.row(task_id)
    assert row.status == _status("succeeded") and row.attempt == 2
    assert row.result_ref == ref == f"fictional-draft:{draft_id}"


async def _return_none(ctx: Any, ref: str) -> None:
    return None


async def _return_other(ctx: Any, ref: str) -> str:
    return "fictional-other-ref"


async def _raise_after(ctx: Any, ref: str) -> str:
    raise FictionalStorageFull(detail=FICTIONAL_ERROR_TEXT)


@pytest.mark.parametrize(
    "then", [_return_none, _return_other, _raise_after], ids=["none", "other_ref", "raises"]
)
async def test_handler_outcome_after_complete_is_ignored(
    db: Database, then: Callable[[Any, str], Any]
) -> None:
    """Once ``complete`` committed, the runner never finishes the task again."""
    writer = DraftWriter()
    handler = Completing(writer, then=then)
    h = Harness(db, {DRAFTING: handler})
    task_id = await h.enqueue()
    await h.start()
    try:
        await h.changes.wait_status(task_id, "succeeded")
        await _wait_until(lambda: handler.outcomes, "complete returned")
        await _settle()
    finally:
        await h.runner.stop()

    (ref,) = handler.outcomes
    row = await h.row(task_id)
    assert row.status == _status("succeeded")
    assert row.result_ref == ref, "the writer's ref stays the task's result"
    assert row.error is None
    assert h.changes.statuses(task_id).count("succeeded") == 1
    assert len(await h.drafts()) == 1


class HoldBeforeTransaction:
    """Wraps the service: ``complete``'s worker thread waits at ``gate`` before it reaches
    the result's transaction (a thread scheduled late)."""

    def __init__(self, service: Any, entered: threading.Event, gate: threading.Event) -> None:
        self._service = service
        self._entered = entered
        self._gate = gate

    def complete(self, task_id: str, *args: Any, **kwargs: Any) -> Any:
        self._entered.set()
        assert self._gate.wait(DEADLINE_S), "the test never released complete"
        return self._service.complete(task_id, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._service, name)


@pytest.mark.parametrize("held", ["in_writer", "before_transaction"])
async def test_stop_during_complete_keeps_result_with_success(db: Database, held: str) -> None:
    """``stop()`` cancels a handler whose ``complete`` is still in its worker thread; the
    handler swallows the cancellation and returns None. The thread's outcome decides the
    task: the runner never finishes it on its own, so a success always carries its draft."""
    writer = DraftWriter()
    entered, gate = threading.Event(), threading.Event()
    swallowed: list[str] = []

    async def handler(ctx: Any) -> str | None:
        inner = writer.for_task(ctx)

        def write(conn: sqlite3.Connection) -> str:
            if held == "in_writer":
                entered.set()
                assert gate.wait(DEADLINE_S), "the test never released the writer"
            return inner(conn)

        try:
            ref: str = await ctx.complete(write)
        except asyncio.CancelledError:
            swallowed.append(ctx.task_id)
            return None
        return ref

    wrap = None
    if held == "before_transaction":

        def wrap(service: Any) -> Any:
            return HoldBeforeTransaction(service, entered, gate)

    h = Harness(db, {DRAFTING: handler}, wrap=wrap)
    task_id = await h.enqueue()
    await h.start()
    stopping: asyncio.Task[None] | None = None
    try:
        await _wait_until(entered.is_set, "complete reached its worker thread")
        stopping = asyncio.create_task(h.runner.stop())
        await _settle()  # the cancellation lands while the thread is held
        gate.set()
        await asyncio.wait_for(stopping, DEADLINE_S)
    finally:
        gate.set()
        if stopping is None:
            await h.runner.stop()

    assert swallowed == [task_id], "stop() cancelled the handler during complete"
    ((draft_id, draft_task, _attempt, _body),) = await h.drafts()
    assert draft_task == task_id
    row = await h.row(task_id)
    assert row.status == _status("succeeded")
    assert row.result_ref == f"fictional-draft:{draft_id}", "a success carries its draft"
    assert row.error is None
    assert h.changes.statuses(task_id).count("succeeded") == 1
