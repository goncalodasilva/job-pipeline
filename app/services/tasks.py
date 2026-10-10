"""Background task rows: enqueue, cancel requests, retry and the runner's claim, heartbeat
and finish (spec 5 M2 reliability rules 1, 2, 6; ADR-034, ADR-076 items 10, 11).

Every change runs in one ``db.transaction()``; ``on_change`` hears about it only after the
commit, once per committed change, and a failing listener never undoes it.
"""

import logging
from collections.abc import Callable

from app.domain.clock import Clock
from app.domain.errors import AppError, TaskNotRetryable
from app.domain.ids import IdFactory
from app.domain.tasks import RETRY_FROM, TERMINAL, TaskChange, TaskRef, TaskStatus, dedupe_key
from app.modules.storage import Database, tasks_repo

TaskRow = tasks_repo.TaskRow

_log = logging.getLogger(__name__)
_NO_ERROR = frozenset({TaskStatus.SUCCEEDED, TaskStatus.CANCELLED})


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
        """Queue a task; an active task with the same dedupe key is returned instead."""
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
        with self._db.transaction() as conn:
            return tasks_repo.get(conn, task_id)

    def recent(self, limit: int) -> list[TaskRow]:
        """Up to ``limit`` tasks, most recently changed first."""
        with self._db.transaction() as conn:
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

    def heartbeat(self, task_id: str, *, progress: int | None = None) -> bool:
        """Record that a running task is alive; not a state change, so nothing is published."""
        now = self._now()
        with self._db.transaction() as conn:
            return tasks_repo.heartbeat(conn, task_id, now, progress=progress)

    def finish(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        error: AppError | None = None,
        result_ref: str | None = None,
    ) -> bool:
        """End a running task in a terminal ``status``; False when it isn't running.
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
            )
            row = tasks_repo.get(conn, task_id) if changed else None
        if row is not None:
            self._publish(row)
        return changed
