"""Failure injection with a real process kill (M2.21; AC-M2.2, AC-M2.6; spec 4.1 and 5 M2
reliability rules 4 and 7; ADR-076 items 3, 10 and 11).

The real app runs in a subprocess (``tests/running_app.py``) on temporary folders with a
completed setup and the fake provider, so the practice task (M2.17) is the vehicle: a ``slow``
one is 5 steps of one fake call each, 3 s per call, and a usage record is reserved before each
call and completed after it, so the task is in flight for about 15 s and a call is in flight
for about 3 s of it. The test waits (with a deadline, never a fixed sleep) until the task is
running with a call reserved but not finished, then SIGKILLs the process, as a crash would.

What the crash left behind is read from the data folder in the test process, through a
read-only SQLite connection and through the real ``UsageService`` (its counts), with no app
running. The app is then restarted on the same folders (``start_app(..., restart=True)``) and
the same reads and the app's own routes show what recovery did.

Every value is fictional, the folders are under ``tmp_path`` and nothing leaves loopback.
Every app a test starts is stopped in a ``finally``.
"""

import signal
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import NamedTuple, Self

import httpx
import pytest

from app.config import DATABASE_FILE
from app.modules.storage import Database
from app.services.practice import PRACTICE, PRACTICE_STEPS
from app.services.usage import UsageService
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory
from tests.integration.test_task_routes import MSG_INTERRUPTED, Item
from tests.page_token import page_token
from tests.running_app import RunningApp, start_app
from tests.waiting import wait_until

pytestmark = pytest.mark.integration

FAKE = "fake"  # the usage service the practice task is counted on
TIER = "fast"
DEADLINE_S = 20.0  # the longest any wait for the app takes (a call is 3 s, a start about 1 s)
RETRY_DEADLINE_S = 40.0  # a retried slow task runs its 5 steps of 3 s again
SENTINEL_DEADLINE_S = 30.0  # long enough to let an automatic retry of a slow task show first
REQUEST_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 5.0
TOKEN_HEADER = "X-JP-Token"  # noqa: S105 - a header name, not a secret


# --- reading the data folder as another process would see it -----------------------------------


class TaskRow(NamedTuple):
    id: str
    status: str
    attempt: int
    error: str | None
    result_ref: str | None
    finished_at: str | None
    updated_at: str
    version: int


class UsageRow(NamedTuple):
    id: str
    task_id: str | None
    service: str | None
    tier: str | None
    task_type: str
    success: int | None
    requested_at: str
    updated_at: str
    version: int


def _read(folder: Path, sql: str) -> list[tuple[object, ...]]:
    """One read through a read-only connection: it can't change the folder it inspects."""
    uri = f"{(folder / DATABASE_FILE).as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=READ_TIMEOUT_S)) as conn:
        return list(conn.execute(sql).fetchall())


def task_rows(folder: Path) -> list[TaskRow]:
    rows = _read(
        folder,
        "SELECT id, status, attempt, error, result_ref, finished_at, updated_at, version "
        "FROM tasks ORDER BY rowid",
    )
    return [TaskRow(*row) for row in rows]  # type: ignore[arg-type]


def task_row(folder: Path, task_id: str) -> TaskRow:
    found = [row for row in task_rows(folder) if row.id == task_id]
    assert len(found) == 1, f"task {task_id} is in the database exactly once"
    return found[0]


def usage_rows(folder: Path) -> list[UsageRow]:
    rows = _read(
        folder,
        "SELECT id, task_id, service, tier, task_type, success, requested_at, updated_at, "
        "version FROM usage_records ORDER BY rowid",
    )
    return [UsageRow(*row) for row in rows]  # type: ignore[arg-type]


def usage_of(folder: Path, task_id: str) -> list[UsageRow]:
    return [row for row in usage_rows(folder) if row.task_id == task_id]


def wait_in_flight(folder: Path, task_id: str, *, finished_calls: int = 0) -> list[UsageRow]:
    """Wait until ``task_id`` is running with exactly one call reserved and not finished (the
    provider call is in flight) after at least ``finished_calls`` finished ones, and return
    the task's usage records at that moment."""

    def in_flight() -> list[UsageRow] | None:
        rows = usage_of(folder, task_id)
        unfinished = [row for row in rows if row.success is None]
        finished = [row for row in rows if row.success is not None]
        running = task_row(folder, task_id).status == "running"
        return (
            rows if running and len(unfinished) == 1 and len(finished) >= finished_calls else None
        )

    return wait_until(
        in_flight,
        timeout=DEADLINE_S,
        message=f"task {task_id} running with a call reserved after {finished_calls} finished",
    )


def counted_by_the_usage_service(folder: Path, at: str) -> tuple[int, int, int]:
    """What the real ``UsageService`` says about the fake service: requests counted, requests
    unfinished and the requests the breakdown by tier and task lists. ``at`` is a record's own
    ``requested_at``, used as the (frozen) time, so the cycle in view is the one the record
    belongs to."""
    clock = FrozenClock(datetime.fromisoformat(at))
    with Database.open(folder / DATABASE_FILE) as db:
        usage = UsageService(db, clock, SequentialIdFactory("usage"), UTC)
        status = usage.status(FAKE)
        listed = sum(row.requests for row in usage.breakdown(FAKE))
    return status.local_count, status.unfinished_count, listed


# --- the running app ---------------------------------------------------------------------------


class Session:
    """One started app: its token, its routes and the folder it works on."""

    def __init__(self, app: RunningApp) -> None:
        self.app = app
        self.folder = app.data_dir
        self.client = app.client()
        self.client.timeout = httpx.Timeout(REQUEST_TIMEOUT_S)
        self._token: str | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.client.close()

    @property
    def token(self) -> str:
        """The launch token this process embeds in its pages (a new one at every start)."""
        if self._token is None:
            page = self.client.get("/settings")
            assert page.status_code == 200, (page.status_code, self.app.output_tail())
            self._token = page_token(page.text)
        return self._token

    def read_headers(self) -> dict[str, str]:
        return {TOKEN_HEADER: self.token}

    def action_headers(self) -> dict[str, str]:
        """Token and matching origin, as htmx sends for a state-changing request."""
        return {TOKEN_HEADER: self.token, "origin": self.app.origin}

    def start_practice(self, *, duration: str, outcome: str = "ok", tier: str = TIER) -> str:
        """Start a practice task through its route and return the new task's id."""
        known = {row.id for row in task_rows(self.folder)}
        response = self.client.post(
            "/tasks/practice",
            data={"duration": duration, "outcome": outcome, "tier": tier},
            headers=self.action_headers(),
        )
        assert response.status_code == 303, (response.status_code, self.app.output_tail())
        assert "practice=started" in response.headers["location"], "a new task, not a duplicate"
        new = [row.id for row in task_rows(self.folder) if row.id not in known]
        assert len(new) == 1, f"one task was enqueued, got {new}"
        return new[0]

    def fragment(self, task_id: str) -> Item:
        response = self.client.get(f"/tasks/{task_id}", headers=self.read_headers())
        assert response.status_code == 200, response.status_code
        return Item(response.text, task_id)

    def retry(self, task_id: str) -> httpx.Response:
        return self.client.post(f"/tasks/{task_id}/retry", headers=self.action_headers())

    def wait_succeeded(self, task_id: str, *, timeout: float) -> TaskRow:
        def succeeded() -> TaskRow | None:
            row = task_row(self.folder, task_id)
            return row if row.status == "succeeded" else None

        return wait_until(succeeded, timeout=timeout, message=f"task {task_id} succeeded")


@contextmanager
def crashing_app(tmp_path: Path) -> Iterator[Session]:
    """A started app that the test kills with SIGKILL before the block ends. Whatever happens,
    no process is left running."""
    app = start_app(tmp_path, output_name="before-kill")
    try:
        with Session(app) as session:
            yield session
    finally:
        result = app.stop()  # after kill() this only returns the recorded result
    assert result.killed, "the test must kill the app before the block ends"
    assert result.returncode == -signal.SIGKILL, app.output_tail()


@contextmanager
def restarted_app(tmp_path: Path) -> Iterator[Session]:
    """The app started again on the folders a killed one left, then stopped gracefully."""
    app = start_app(tmp_path, restart=True, output_name="after-kill")
    try:
        with Session(app) as session:
            yield session
    finally:
        result = app.stop()
    assert not result.killed, app.output_tail()
    assert result.returncode == 0, app.output_tail()


def crash_during_slow_task(
    tmp_path: Path, *, finished_calls: int = 0
) -> tuple[str, list[UsageRow]]:
    """Start a slow practice task, kill the app while it runs with a call in flight, and
    return the task's id and its usage records as they were just before the kill."""
    with crashing_app(tmp_path) as first:
        task_id = first.start_practice(duration="slow")
        in_flight = wait_in_flight(first.folder, task_id, finished_calls=finished_calls)
        first.app.kill()
    return task_id, in_flight


# --- AC-M2.2 -----------------------------------------------------------------------------------


def test_ac_m2_2_kill_during_task_leaves_interrupted_after_restart(tmp_path: Path) -> None:
    task_id, _in_flight = crash_during_slow_task(tmp_path)
    folder = tmp_path / "data"

    # What the kill left: the process died before it could write an ending, so the row still
    # says running. Recovery has to be what changes it.
    left = task_row(folder, task_id)
    assert left.status == "running", "the kill found the task mid-flight"
    assert left.finished_at is None and left.result_ref is None
    assert left.attempt == 1

    with restarted_app(tmp_path) as second:
        after = task_row(folder, task_id)
        assert after.status == "interrupted", "recovery ends a task the crash left running"
        assert after.attempt == 1, "interrupting is not a retry"
        assert after.finished_at is not None, "an interrupted task has an end time"
        assert after.result_ref is None, "no result from a run that never finished"
        assert after.error is None, "interrupted is shown by status; no error kind stored"
        assert after.version == left.version + 1

        # The answer the user sees: status in words, the reason and a Retry button.
        item = second.fragment(task_id)
        assert item.text("status") == "Interrupted"
        assert item.text("reason") == MSG_INTERRUPTED
        assert item.actions == {"retry"}
        assert item.part("progress") is None, "nothing is shown as still running"
        assert [row.status for row in task_rows(folder)] == ["interrupted"]


def test_ac_m2_2_no_automatic_retry_after_restart(tmp_path: Path) -> None:
    killed_id, before = crash_during_slow_task(tmp_path)
    folder = tmp_path / "data"
    assert before and all(row.task_id == killed_id for row in before)

    with restarted_app(tmp_path) as second:
        recovered_row = task_row(folder, killed_id)
        assert recovered_row.status == "interrupted"
        assert recovered_row.attempt == 1

        # There is no timed wait here (rule: no fixed sleeps). A sentinel stands in for it: it
        # proves the runner is alive and has had its turn, and since the runner claims queued
        # tasks oldest first, a task re-queued by the restart would have run (or be running)
        # before the sentinel finished. It does not see a retry fired later than that, such as
        # a timer; the checks on the row right after recovery above cover a re-queue at once.
        sentinel_id = second.start_practice(duration="quick")
        assert sentinel_id != killed_id
        sentinel = second.wait_succeeded(sentinel_id, timeout=SENTINEL_DEADLINE_S)
        assert sentinel.result_ref == f"{PRACTICE}:{PRACTICE_STEPS}"

        killed = task_row(folder, killed_id)
        assert killed == recovered_row, "the killed task is exactly as recovery left it"
        assert killed.status == "interrupted"
        assert killed.attempt == 1, "never retried by itself"

        # No provider call for the killed task: every call goes through the usage meter, so a
        # call would be a new record. Its records are the ones from before the kill, unchanged;
        # the others are the sentinel's own calls.
        assert usage_of(folder, killed_id) == before, "no call and no change for the killed task"
        sentinel_usage = usage_of(folder, sentinel_id)
        assert len(sentinel_usage) == PRACTICE_STEPS
        assert all(row.success == 1 for row in sentinel_usage)
        assert len(usage_rows(folder)) == len(before) + PRACTICE_STEPS, "the only new calls"
        assert [row.status for row in task_rows(folder)] == ["interrupted", "succeeded"]


def test_ac_m2_2_retry_after_restart_succeeds(tmp_path: Path) -> None:
    task_id, before = crash_during_slow_task(tmp_path)
    folder = tmp_path / "data"

    with restarted_app(tmp_path) as second:
        assert task_row(folder, task_id).status == "interrupted"
        assert second.fragment(task_id).actions == {"retry"}, "the user is offered Retry"

        answer = second.retry(task_id)  # through the real route, with token and origin
        assert answer.status_code == 200, (answer.status_code, answer.text[:300])
        retried = Item(answer.text, task_id)
        assert retried.text("status") in ("Queued", "Running"), "back in the queue"
        assert retried.text("reason") is None

        done = second.wait_succeeded(task_id, timeout=RETRY_DEADLINE_S)
        assert done.attempt == 2, "the same task, its second attempt"
        assert done.result_ref == f"{PRACTICE}:{PRACTICE_STEPS}"
        assert done.error is None
        assert done.finished_at is not None

        item = second.fragment(task_id)
        assert item.text("status") == "Done"
        assert item.actions == set()
        assert item.part("reason") is None

        # The second attempt really ran its calls, all finished; the crashed attempt's
        # reserved call is still there, never completed by the retry.
        records = usage_of(folder, task_id)
        assert len(records) == len(before) + PRACTICE_STEPS
        assert records[: len(before)] == before
        assert all(row.success == 1 for row in records[len(before) :])


# --- AC-M2.6 -----------------------------------------------------------------------------------


def test_ac_m2_6_kill_during_provider_call_keeps_reserved_usage_counted(tmp_path: Path) -> None:
    # One call finished and the next one reserved and in flight when the process is killed.
    task_id, in_flight = crash_during_slow_task(tmp_path, finished_calls=1)
    folder = tmp_path / "data"
    reserved = next(row for row in in_flight if row.success is None)
    finished = [row for row in in_flight if row.success is not None]
    assert len(in_flight) == len(finished) + 1

    # After the kill, with no app running: the reserved record is still there, never
    # completed, and the usage service counts it with the finished ones.
    after_kill = usage_rows(folder)
    assert after_kill == in_flight, "the kill lost nothing and completed nothing"
    assert (reserved.task_id, reserved.service, reserved.task_type, reserved.tier) == (
        task_id,
        FAKE,
        PRACTICE,
        TIER,
    )
    expected = (len(in_flight), 1, len(in_flight))
    assert counted_by_the_usage_service(folder, reserved.requested_at) == expected

    # After the restart: the task is interrupted, and the reserved record is untouched, column
    # for column, and still counted (as an unfinished call).
    with restarted_app(tmp_path):
        assert task_row(folder, task_id).status == "interrupted"
        assert usage_rows(folder) == after_kill, "recovery leaves every usage record alone"
        assert counted_by_the_usage_service(folder, reserved.requested_at) == expected
