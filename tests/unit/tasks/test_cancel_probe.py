"""Edge paths of the runner's cancel probe (M2.11; spec 5 M2 reliability rule 6).

``notify_cancel(task_id)`` makes the runner read the row's cancel flag for a local handler.
These pin what happens when that read fails, finds no request, finishes after the handler
has already ended, or is still in flight when the runner stops. The harness here does not
wire ``on_change`` to ``notify_cancel``, so each test calls it itself. Real migrated SQLite
in a temp folder, ``FrozenClock`` and ``SequentialIdFactory``; every wait has a deadline.
All values are fictional.
"""

import asyncio
import logging
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.modules.storage import Database
from tests.unit.tasks.test_runner import (
    DEADLINE_S,
    FICTIONAL_FAILURE_TEXT,
    Gated,
    Harness,
    _open_migrated,
    _settings,
    _settle,
    _status,
)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    (tmp_path / "data").mkdir()
    database = _open_migrated(tmp_path)
    try:
        yield database
    finally:
        database.close()


class ProbeFails:
    """Wraps a service; reading the cancel flag raises (a fictional database error)."""

    def __init__(self, service: Any) -> None:
        self._service = service
        self.reads = 0

    def cancel_requested(self, task_id: str, **kwargs: Any) -> bool:
        self.reads += 1
        raise RuntimeError(FICTIONAL_FAILURE_TEXT)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._service, name)


class ProbeBlocks:
    """Wraps a service; once ``armed``, reading the cancel flag blocks in its worker thread
    until released (the read the runner makes when it starts a handler comes before)."""

    def __init__(self, service: Any) -> None:
        self._service = service
        self.armed = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def cancel_requested(self, task_id: str, **kwargs: Any) -> bool:
        if not self.armed:
            unarmed: bool = self._service.cancel_requested(task_id, **kwargs)
            return unarmed
        self.entered.set()
        assert self.release.wait(DEADLINE_S), "the test never released the read"
        result: bool = self._service.cancel_requested(task_id, **kwargs)
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._service, name)


async def _start(h: Harness, service: Any) -> str:
    h.runner.attach(service, settings=_settings())
    await h.runner.start()
    return await h.enqueue()


async def test_failed_cancel_read_is_logged_by_type_and_task_runs_on(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    handler = Gated()
    h = Harness(db, {"fictional_echo": handler})
    failing = ProbeFails(h.service)
    task_id = await _start(h, failing)
    try:
        await handler.wait_started(1)
        h.runner.notify_cancel(task_id)
        await _settle()
        assert failing.reads == 2, "the read when the handler started, then the notice"
        handler.release_all()
        await h.changes.wait_status(task_id, "succeeded")
    finally:
        await h.runner.stop()

    assert handler.cancelled == []
    assert "RuntimeError" in caplog.text
    assert FICTIONAL_FAILURE_TEXT not in caplog.text


async def test_running_notice_without_cancel_request_cancels_nothing(db: Database) -> None:
    handler = Gated()
    h = Harness(db, {"fictional_echo": handler})
    task_id = await _start(h, h.service)
    try:
        await handler.wait_started(1)
        h.runner.notify_cancel(task_id)
        await _settle()
        (ctx,) = handler.calls
        assert ctx.cancel_flag.cancelled is False
        handler.release_all()
        await h.changes.wait_status(task_id, "succeeded")
    finally:
        await h.runner.stop()

    assert handler.cancelled == []
    assert (await h.row(task_id)).status == _status("succeeded")


async def test_cancel_read_after_handler_returned_ends_cancelled_without_cancelling_it(
    db: Database,
) -> None:
    """The handler returns while the read is in flight: the success is refused by the
    cancel request in the database and the task ends cancelled; the finishing write is
    never cancelled."""
    handler = Gated()
    h = Harness(db, {"fictional_echo": handler})
    blocking = ProbeBlocks(h.service)
    task_id = await _start(h, blocking)
    try:
        await handler.wait_started(1)
        await _settle()  # the read when the handler started is done
        blocking.armed = True
        await asyncio.to_thread(h.service.request_cancel, task_id)  # no notice wired here
        h.runner.notify_cancel(task_id)
        await asyncio.wait_for(asyncio.to_thread(blocking.entered.wait, DEADLINE_S), DEADLINE_S)
        handler.release_all()
        await h.changes.wait_status(task_id, "cancelled")
        blocking.release.set()
        await _settle()
    finally:
        blocking.release.set()
        await h.runner.stop()

    assert handler.cancelled == []
    row = await h.row(task_id)
    assert row.status == _status("cancelled")
    assert row.result_ref is None and row.error is None


async def test_stop_with_cancel_read_in_flight_ends_interrupted(db: Database) -> None:
    """The cancel is requested in the database and its read is in flight when the runner
    stops; the read then comes back while ``stop()`` runs: the task still ends interrupted."""
    handler = Gated()
    h = Harness(db, {"fictional_echo": handler})
    blocking = ProbeBlocks(h.service)
    task_id = await _start(h, blocking)
    try:
        await handler.wait_started(1)
        await _settle()  # the read when the handler started is done
        blocking.armed = True
        await asyncio.to_thread(h.service.request_cancel, task_id)  # no notice wired here
        h.runner.notify_cancel(task_id)
        await asyncio.wait_for(asyncio.to_thread(blocking.entered.wait, DEADLINE_S), DEADLINE_S)
        stopper = asyncio.create_task(h.runner.stop())
        await asyncio.sleep(0)  # stop() has begun
        blocking.release.set()
        await asyncio.wait_for(stopper, DEADLINE_S)
    finally:
        blocking.release.set()
        await h.runner.stop()

    assert handler.cancelled == [task_id]
    assert (await h.row(task_id)).status == _status("interrupted")


# --- attempts never mix -------------------------------------------------------------------


class Zombie:
    """Attempt 1 swallows the watchdog's cancellation, waits for the test, beats and returns
    a result; later attempts wait for the test and return their own result."""

    def __init__(self) -> None:
        self.started = {1: asyncio.Event(), 2: asyncio.Event()}
        self.gates = {1: asyncio.Event(), 2: asyncio.Event()}
        self.swallowed = asyncio.Event()
        self.returned: list[int] = []

    async def __call__(self, ctx: Any) -> str | None:
        self.started[ctx.attempt].set()
        if ctx.attempt == 1:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.swallowed.set()
            await self.gates[1].wait()
            await ctx.heartbeat(77)
        else:
            await self.gates[ctx.attempt].wait()
        self.returned.append(ctx.attempt)
        return f"fictional-result-attempt-{ctx.attempt}"


async def test_an_old_attempt_never_finishes_or_beats_for_the_next_one(db: Database) -> None:
    from datetime import timedelta

    from tests.unit.tasks.test_watchdog import SHORT, SHORT_TIMEOUT, WatchHarness

    handler = Zombie()
    h = WatchHarness(db, {SHORT: (SHORT_TIMEOUT, handler)})
    await h.start(max_concurrent=2)
    try:
        task_id = await h.enqueue(SHORT)
        await asyncio.wait_for(handler.started[1].wait(), DEADLINE_S)
        h.clock.advance(SHORT_TIMEOUT + timedelta(seconds=1))
        assert await h.check() == [task_id]
        await asyncio.wait_for(handler.swallowed.wait(), DEADLINE_S)

        retried = await asyncio.to_thread(h.service.retry, task_id)
        assert retried is not None and retried.attempt == 2
        await asyncio.wait_for(handler.started[2].wait(), DEADLINE_S)
        claimed = await h.row(task_id)
        assert claimed.status == _status("running") and claimed.attempt == 2

        h.clock.advance(timedelta(minutes=1))
        handler.gates[1].set()  # attempt 1 beats and returns its result now
        await _wait_for(lambda: handler.returned == [1], "attempt 1 returned")
        await _settle()

        row = await h.row(task_id)
        assert row.status == _status("running"), "attempt 1 must not end attempt 2"
        assert row.result_ref is None
        assert row.heartbeat_at == claimed.heartbeat_at, "attempt 1's beat must not count"
        assert row.progress == 0

        handler.gates[2].set()
        await h.changes.wait_status(task_id, "succeeded")
    finally:
        for gate in handler.gates.values():
            gate.set()
        await h.runner.stop()

    row = await h.row(task_id)
    assert row.attempt == 2
    assert row.result_ref == "fictional-result-attempt-2"


async def _wait_for(condition: Any, what: str) -> None:
    async def until() -> None:
        while not condition():
            await asyncio.to_thread(lambda: None)
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(until(), DEADLINE_S)
    except TimeoutError:
        raise AssertionError(f"never happened: {what}") from None
