"""Background task rows: enqueue, cancel requests, retry and the runner's claim, heartbeat
and finish (spec 5 M2 reliability rules 1, 2, 6; ADR-034, ADR-076 items 10, 11).

Every change runs in one ``db.transaction()``; ``on_change`` hears about it only after the
commit, once per committed change, and a failing listener never undoes it.
"""

import logging
import sqlite3
from collections.abc import Callable
from datetime import timedelta

from app.domain.clock import Clock
from app.domain.errors import AppError, TaskNotRetryable
from app.domain.ids import IdFactory
from app.domain.tasks import (
    RETRY_FROM,
    TERMINAL,
    CancelFlag,
    Completion,
    CompletionOutcome,
    TaskChange,
    TaskRef,
    TaskStatus,
    dedupe_key,
)
from app.domain.usage import is_task_type
from app.modules.storage import Database, tasks_repo

TaskRow = tasks_repo.TaskRow

_log = logging.getLogger(__name__)
_NO_ERROR = frozenset({TaskStatus.SUCCEEDED, TaskStatus.CANCELLED})


class _Refused(Exception):
    """Rolls back ``complete``'s transaction when its success update was refused."""

    def __init__(self, outcome: CompletionOutcome) -> None:
        super().__init__(outcome.value)
        self.outcome = outcome


def _refusal(row: TaskRow | None, attempt: int) -> CompletionOutcome:
    """Why that attempt's success was refused: a cancel requested while it still runs, or
    it is no longer running (finished, interrupted, or a later attempt runs)."""
    if (
        row is not None
        and row.status == TaskStatus.RUNNING
        and row.attempt == attempt
        and row.cancel_requested
    ):
        return CompletionOutcome.CANCEL_REQUESTED
    return CompletionOutcome.SUPERSEDED


class TaskService:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        ids: IdFactory,
        on_change: Callable[[TaskChange], None],
    ) -> None:
        self._db = db
        self._clock = clock
        self._ids = ids
        self._on_change = on_change

    def _now(self) -> str:
        return self._clock.now().isoformat()

    def _publish(self, row: TaskRow) -> None:
        try:
            self._on_change(TaskChange(row.id, row.type, row.status))
        except Exception as error:
            # Only the type: the listener's message could carry anything.
            _log.warning("task change listener failed (%s)", type(error).__name__)

    # --- for routes and other services --------------------------------------------------

    def enqueue(self, task_type: str, target: str, *, input_ref: str | None = None) -> TaskRef:
        """Queue a task; an active task with the same dedupe key is returned instead.
        ``ValueError`` for a task type that is not an id (the rule usage records follow): it is
        stored, shown and sent on the event stream, so it is never prose."""
        if not is_task_type(task_type):
            raise ValueError("a task type is 1 to 64 characters: a-z first, then a-z, 0-9 or _")
        now = self._now()
        new = TaskRow(
            id=self._ids.new(),
            type=task_type,
            status=TaskStatus.QUEUED,
            dedupe_key=dedupe_key(task_type, target),
            attempt=1,
            progress=0,
            heartbeat_at=None,
            cancel_requested=False,
            input_ref=input_ref,
            result_ref=None,
            error=None,
            created_at=now,
            updated_at=now,
            finished_at=None,
            version=1,
        )
        with self._db.transaction() as conn:
            row, created = tasks_repo.insert_or_get_active(conn, new)
        if created:
            self._publish(row)
        return TaskRef(row.id, created)

    def request_cancel(self, task_id: str) -> TaskRow | None:
        """A queued task ends cancelled at once; a running one gets the cancel flag and the
        runner ends it. Finished tasks stay as they are. None when the task doesn't exist."""
        now = self._now()
        with self._db.transaction() as conn:
            row = tasks_repo.get(conn, task_id)
            if row is None:
                return None
            if row.status == TaskStatus.QUEUED:
                changed = tasks_repo.transition(
                    conn, task_id, [TaskStatus.QUEUED], TaskStatus.CANCELLED, now=now
                )
            elif row.status == TaskStatus.RUNNING:
                changed = tasks_repo.request_cancel(conn, task_id, now)
            else:
                changed = False
            if changed:
                row = tasks_repo.get(conn, task_id)
        if row is None:  # pragma: no cover - read in the same transaction as the update
            return None
        if changed:
            self._publish(row)
        return row

    def retry(self, task_id: str) -> TaskRow | None:
        """Re-queue a failed, cancelled or interrupted task as its next attempt (same row).
        When another active task holds its dedupe key, that task is returned unchanged.
        None when the task doesn't exist; ``TaskNotRetryable`` when it is queued, running or
        succeeded."""
        now = self._now()
        with self._db.transaction() as conn:
            row = tasks_repo.get(conn, task_id)
            if row is None:
                return None
            if row.status not in RETRY_FROM:
                raise TaskNotRetryable(detail=f"task is {row.status.value}")
            changed = tasks_repo.requeue_for_retry(conn, task_id, now)
            if changed:
                result = tasks_repo.get(conn, task_id)
            else:
                result = tasks_repo.get_active(conn, row.dedupe_key)
        if result is not None and changed:
            self._publish(result)
        return result

    def get(self, task_id: str) -> TaskRow | None:
        with self._db.read() as conn:
            return tasks_repo.get(conn, task_id)

    def recent(self, limit: int) -> list[TaskRow]:
        """Up to ``limit`` tasks, most recently changed first."""
        with self._db.read() as conn:
            return tasks_repo.list_recent(conn, limit)

    # --- when a workspace opens -----------------------------------------------------------

    def recover_interrupted(self) -> int:
        """Crash recovery (ADR-076 item 10): every task left running by an earlier process
        becomes interrupted, in one transaction, before the runner claims anything. Queued
        tasks stay queued; nothing is retried. Returns how many tasks were interrupted."""
        now = self._now()
        with self._db.transaction() as conn:
            changed = tasks_repo.interrupt_running(conn, now)
            rows = [row for task_id in changed if (row := tasks_repo.get(conn, task_id))]
        if rows:
            _log.info("Interrupted %d tasks left running", len(rows))
        for row in rows:
            self._publish(row)
        return len(rows)

    # --- for the task runner ------------------------------------------------------------

    def claim_next(self) -> TaskRow | None:
        """Start the oldest queued task; None when nothing is queued."""
        now = self._now()
        with self._db.transaction() as conn:
            row = tasks_repo.claim_next(conn, now)
        if row is not None:
            self._publish(row)
        return row

    def heartbeat(
        self,
        task_id: str,
        *,
        progress: int | None = None,
        attempt: int | None = None,
        cancel_flag: CancelFlag | None = None,
    ) -> bool:
        """Record that a running task (that ``attempt`` of it, when given) is alive; not a
        state change, so nothing is published. False when it isn't running. When the row's
        cancel flag is set (perhaps by another process), ``cancel_flag`` is set too, so the
        handler sees the request."""
        now = self._now()
        with self._db.transaction() as conn:
            requested = tasks_repo.beat(conn, task_id, now, progress=progress, attempt=attempt)
        if requested and cancel_flag is not None:
            cancel_flag.cancel()
        return requested is not None

    def cancel_requested(self, task_id: str, *, attempt: int | None = None) -> bool:
        """Whether a cancel was requested for this running task (that ``attempt`` of it,
        when given)."""
        row = self.get(task_id)
        return (
            row is not None
            and row.status == TaskStatus.RUNNING
            and row.cancel_requested
            and (attempt is None or row.attempt == attempt)
        )

    def interrupt_stale(self, task_type: str, timeout: timedelta) -> list[str]:
        """Watchdog: every running ``task_type`` task whose last heartbeat is more than
        ``timeout`` ago becomes interrupted, in one conditional update. Returns the ids
        changed; each change is published after the commit. Times compare as ISO text, so
        this relies on every stored timestamp coming from the injected (UTC) Clock."""
        now = self._clock.now()
        cutoff = (now - timeout).isoformat()
        with self._db.transaction() as conn:
            changed = tasks_repo.interrupt_stale(conn, task_type, cutoff, now.isoformat())
            rows = [row for task_id in changed if (row := tasks_repo.get(conn, task_id))]
        for row in rows:
            self._publish(row)
        return changed

    def complete(
        self, task_id: str, write: Callable[[sqlite3.Connection], str], *, attempt: int
    ) -> Completion:
        """Save that attempt's result and its success atomically (spec 5 M2 rule 3): in ONE
        transaction ``write(conn)`` stores the result and returns its ref, then the
        conditional running -> succeeded update stores that ref. A refused update (a cancel
        was requested, or that attempt is no longer running) rolls the writer's rows back
        too. An exception from ``write`` or the update rolls back and propagates unchanged.
        The success is published after the commit."""
        now = self._now()
        try:
            with self._db.transaction() as conn:
                result_ref = write(conn)
                if not conn.in_transaction:
                    # Its rows are already committed apart from the success: refuse it.
                    raise sqlite3.ProgrammingError("the result writer ended the transaction")
                changed = tasks_repo.transition(
                    conn,
                    task_id,
                    [TaskStatus.RUNNING],
                    TaskStatus.SUCCEEDED,
                    now=now,
                    result_ref=result_ref,
                    attempt=attempt,
                )
                row = tasks_repo.get(conn, task_id)
                if not changed:
                    raise _Refused(_refusal(row, attempt))
        except _Refused as refused:
            return Completion(refused.outcome)
        if row is not None:  # pragma: no branch - read in the transaction that updated it
            self._publish(row)
        return Completion(CompletionOutcome.SUCCEEDED, result_ref)

    def finish(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        error: AppError | None = None,
        result_ref: str | None = None,
        attempt: int | None = None,
    ) -> bool:
        """End a running task (that ``attempt`` of it, when given) in a terminal ``status``;
        False when it isn't running, or when it would succeed while a cancel is requested
        (the runner then ends it cancelled).
        Failed needs an ``error``, succeeded and cancelled take none, interrupted may have
        one. ``error`` is stored as its stable ``kind`` only, never its text."""
        if status not in TERMINAL:
            raise ValueError(f"a task can't finish as {status.value!r}")
        if status == TaskStatus.FAILED and error is None:
            raise ValueError("a failed task needs the error that ended it")
        if status in _NO_ERROR and error is not None:
            raise ValueError(f"a task finishing as {status.value!r} carries no error")
        kind = None if error is None else error.kind
        now = self._now()
        with self._db.transaction() as conn:
            changed = tasks_repo.transition(
                conn,
                task_id,
                [TaskStatus.RUNNING],
                status,
                now=now,
                error=kind,
                result_ref=result_ref,
                attempt=attempt,
            )
            row = tasks_repo.get(conn, task_id) if changed else None
        if row is not None:
            self._publish(row)
        return changed
