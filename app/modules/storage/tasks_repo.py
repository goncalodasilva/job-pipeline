"""Rows of the ``tasks`` table (spec 5 M2 reliability rules 1, 2, 5, 6; ADR-038, ADR-076 items
10, 11). Callers own the transaction; every status change is a conditional update that bumps
``version`` and ``updated_at``."""

import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.domain.tasks import ACTIVE, RETRY_FROM, TERMINAL, TaskStatus, can_transition

# Literal SQL only (no string building); variable status sets go in as a JSON array.
_SELECT_BY_ID = (
    "SELECT id, type, status, dedupe_key, attempt, progress, heartbeat_at, cancel_requested, "
    "input_ref, result_ref, error, created_at, updated_at, finished_at, version "
    "FROM tasks WHERE id = ?"
)
_SELECT_ACTIVE_BY_KEY = (
    "SELECT id, type, status, dedupe_key, attempt, progress, heartbeat_at, cancel_requested, "
    "input_ref, result_ref, error, created_at, updated_at, finished_at, version "
    "FROM tasks WHERE dedupe_key = ? AND status IN ('queued', 'running')"
)
_SELECT_RECENT = (
    "SELECT id, type, status, dedupe_key, attempt, progress, heartbeat_at, cancel_requested, "
    "input_ref, result_ref, error, created_at, updated_at, finished_at, version "
    "FROM tasks ORDER BY updated_at DESC, rowid DESC LIMIT ?"
)
_SELECT_BY_STATUS = (
    "SELECT id, type, status, dedupe_key, attempt, progress, heartbeat_at, cancel_requested, "
    "input_ref, result_ref, error, created_at, updated_at, finished_at, version "
    "FROM tasks WHERE status IN (SELECT value FROM json_each(?)) ORDER BY updated_at, rowid"
)
_INSERT = (
    "INSERT INTO tasks (id, type, status, dedupe_key, attempt, progress, heartbeat_at, "
    "cancel_requested, input_ref, result_ref, error, created_at, updated_at, finished_at, "
    "version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT (dedupe_key) WHERE status IN ('queued', 'running') DO NOTHING"
)
_TRANSITION = (
    "UPDATE tasks SET status = ?, updated_at = ?, version = version + 1, "
    "heartbeat_at = coalesce(?, heartbeat_at), finished_at = coalesce(?, finished_at), "
    "error = coalesce(?, error), result_ref = coalesce(?, result_ref), "
    "progress = coalesce(?, progress) "
    "WHERE id = ? AND status IN (SELECT value FROM json_each(?))"
)
_REQUEUE = (
    "UPDATE tasks SET status = 'queued', attempt = attempt + 1, error = NULL, progress = 0, "
    "cancel_requested = 0, finished_at = NULL, result_ref = NULL, heartbeat_at = NULL, "
    "updated_at = ?, version = version + 1 "
    "WHERE id = ? AND status IN (SELECT value FROM json_each(?)) AND NOT EXISTS ("
    "SELECT 1 FROM tasks AS other WHERE other.dedupe_key = tasks.dedupe_key "
    "AND other.status IN ('queued', 'running'))"
)
_HEARTBEAT = (
    "UPDATE tasks SET heartbeat_at = ?, progress = coalesce(?, progress) "
    "WHERE id = ? AND status = 'running'"
)
_REQUEST_CANCEL = (
    "UPDATE tasks SET cancel_requested = 1, updated_at = ?, version = version + 1 "
    "WHERE id = ? AND status = 'running'"
)


@dataclass(frozen=True)
class TaskRow:
    id: str
    type: str
    status: TaskStatus
    dedupe_key: str
    attempt: int
    progress: int
    heartbeat_at: str | None
    cancel_requested: bool
    input_ref: str | None
    result_ref: str | None
    error: str | None
    created_at: str
    updated_at: str
    finished_at: str | None
    version: int


def _row(values: Sequence[object]) -> TaskRow:
    (
        task_id,
        task_type,
        status,
        key,
        attempt,
        progress,
        heartbeat_at,
        cancel_requested,
        input_ref,
        result_ref,
        error,
        created_at,
        updated_at,
        finished_at,
        version,
    ) = values
    return TaskRow(
        id=str(task_id),
        type=str(task_type),
        status=TaskStatus(str(status)),
        dedupe_key=str(key),
        attempt=int(str(attempt)),
        progress=int(str(progress)),
        heartbeat_at=None if heartbeat_at is None else str(heartbeat_at),
        cancel_requested=bool(cancel_requested),
        input_ref=None if input_ref is None else str(input_ref),
        result_ref=None if result_ref is None else str(result_ref),
        error=None if error is None else str(error),
        created_at=str(created_at),
        updated_at=str(updated_at),
        finished_at=None if finished_at is None else str(finished_at),
        version=int(str(version)),
    )


def _check_progress(progress: int | None) -> None:
    if progress is not None and not 0 <= progress <= 100:
        raise ValueError("progress must be between 0 and 100")


def _json_list(statuses: Iterable[TaskStatus]) -> str:
    return json.dumps(sorted(status.value for status in statuses))


def get(conn: sqlite3.Connection, task_id: str) -> TaskRow | None:
    row = conn.execute(_SELECT_BY_ID, (task_id,)).fetchone()
    return None if row is None else _row(row)


def insert_or_get_active(conn: sqlite3.Connection, row: TaskRow) -> tuple[TaskRow, bool]:
    """Insert ``row``; when an active task already holds its dedupe key, return that task and
    False instead. Any other constraint failure raises. ValueError unless ``row`` is a fresh
    task: queued, attempt 1, no cancel request, not finished."""
    if (
        row.status != TaskStatus.QUEUED
        or row.attempt != 1
        or row.cancel_requested
        or row.finished_at is not None
    ):
        raise ValueError("a new task must be queued, attempt 1, not cancelled or finished")
    cursor = conn.execute(
        _INSERT,
        (
            row.id,
            row.type,
            row.status.value,
            row.dedupe_key,
            row.attempt,
            row.progress,
            row.heartbeat_at,
            int(row.cancel_requested),
            row.input_ref,
            row.result_ref,
            row.error,
            row.created_at,
            row.updated_at,
            row.finished_at,
            row.version,
        ),
    )
    if cursor.rowcount == 1:
        return row, True
    existing = conn.execute(_SELECT_ACTIVE_BY_KEY, (row.dedupe_key,)).fetchone()
    if existing is None:  # pragma: no cover - the conflict implies an active row
        raise sqlite3.IntegrityError("dedupe conflict without an active task")
    return _row(existing), False


def transition(
    conn: sqlite3.Connection,
    task_id: str,
    expected: Iterable[TaskStatus],
    target: TaskStatus,
    *,
    now: str,
    error: str | None = None,
    result_ref: str | None = None,
    progress: int | None = None,
) -> bool:
    """Move the task to ``target`` if its status is one of ``expected``; False otherwise.

    Expected states that can't reach ``target`` (``can_transition``) are dropped; ValueError
    only when none can (re-queueing a finished task is ``requeue_for_retry``'s job).
    ``result_ref`` is accepted only when ``target`` is succeeded (ValueError otherwise).
    Entering running starts the heartbeat; entering a terminal state sets ``finished_at``.
    Options left as ``None`` keep their stored values.
    """
    sources = [TaskStatus(s) for s in expected]
    allowed = [s for s in sources if can_transition(s, target)]
    if not allowed:
        raise ValueError(
            f"no allowed transition to {target.value!r} from {[s.value for s in sources]}"
        )
    if result_ref is not None and target != TaskStatus.SUCCEEDED:
        raise ValueError("result_ref is only stored when a task succeeds")
    _check_progress(progress)
    heartbeat_at = now if target == TaskStatus.RUNNING else None
    finished_at = now if target in TERMINAL else None
    cursor = conn.execute(
        _TRANSITION,
        (
            target.value,
            now,
            heartbeat_at,
            finished_at,
            error,
            result_ref,
            progress,
            task_id,
            _json_list(allowed),
        ),
    )
    return cursor.rowcount == 1


def requeue_for_retry(conn: sqlite3.Connection, task_id: str, now: str) -> bool:
    """Re-queue a failed, cancelled or interrupted task as its next attempt (ADR-076 item 11):
    attempt + 1, progress 0, and error, result_ref, heartbeat, cancel flag and
    ``finished_at`` cleared.

    False when the task isn't in a retryable state, or when another active task holds its
    dedupe key (the caller then returns that task instead)."""
    cursor = conn.execute(_REQUEUE, (now, task_id, _json_list(RETRY_FROM)))
    return cursor.rowcount == 1


def heartbeat(
    conn: sqlite3.Connection, task_id: str, now: str, *, progress: int | None = None
) -> bool:
    """Record a heartbeat (and progress, if given) for a running task; False otherwise.
    Not a state change, so ``version`` and ``updated_at`` stay as they are."""
    _check_progress(progress)
    cursor = conn.execute(_HEARTBEAT, (now, progress, task_id))
    return cursor.rowcount == 1


def request_cancel(conn: sqlite3.Connection, task_id: str, now: str) -> bool:
    """Flag a running task for cancellation; the runner ends it. False otherwise."""
    cursor = conn.execute(_REQUEST_CANCEL, (now, task_id))
    return cursor.rowcount == 1


def list_recent(conn: sqlite3.Connection, limit: int) -> list[TaskRow]:
    """Up to ``limit`` tasks ordered by ``updated_at``, most recently changed first (what the
    tasks panel wants). The tasks table stays small, so no index is needed for this."""
    return [_row(row) for row in conn.execute(_SELECT_RECENT, (limit,)).fetchall()]


def list_active(conn: sqlite3.Connection) -> list[TaskRow]:
    rows = conn.execute(_SELECT_BY_STATUS, (_json_list(ACTIVE),)).fetchall()
    return [_row(row) for row in rows]
