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
_SELECT_LATEST_BY_KEY = (
    "SELECT id, type, status, dedupe_key, attempt, progress, heartbeat_at, cancel_requested, "
    "input_ref, result_ref, error, created_at, updated_at, finished_at, version "
    "FROM tasks WHERE dedupe_key = ? ORDER BY updated_at DESC, rowid DESC LIMIT 1"
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
    "WHERE id = ? AND status IN (SELECT value FROM json_each(?)) "
    "AND (? <> 'succeeded' OR cancel_requested = 0) AND (? IS NULL OR attempt = ?)"
)
_REQUEUE = (
    "UPDATE tasks SET status = 'queued', attempt = attempt + 1, error = NULL, progress = 0, "
    "cancel_requested = 0, finished_at = NULL, result_ref = NULL, heartbeat_at = NULL, "
    "updated_at = ?, version = version + 1 "
    "WHERE id = ? AND status IN (SELECT value FROM json_each(?)) AND NOT EXISTS ("
    "SELECT 1 FROM tasks AS other WHERE other.dedupe_key = tasks.dedupe_key "
    "AND other.status IN ('queued', 'running'))"
)
_CLAIM_NEXT = (
    "UPDATE tasks SET status = 'running', heartbeat_at = ?, updated_at = ?, "
    "version = version + 1 "
    "WHERE status = 'queued' AND id = ("
    "SELECT id FROM tasks WHERE status = 'queued' ORDER BY updated_at, rowid LIMIT 1) "
    "RETURNING id, type, status, dedupe_key, attempt, progress, heartbeat_at, cancel_requested, "
    "input_ref, result_ref, error, created_at, updated_at, finished_at, version"
)
_HEARTBEAT = (
    "UPDATE tasks SET heartbeat_at = ?, progress = coalesce(?, progress) "
    "WHERE id = ? AND status = 'running' AND (? IS NULL OR attempt = ?) "
    "RETURNING cancel_requested"
)
_REQUEST_CANCEL = (
    "UPDATE tasks SET cancel_requested = 1, updated_at = ?, version = version + 1 "
    "WHERE id = ? AND status = 'running'"
)
_INTERRUPT_RUNNING = (
    "UPDATE tasks SET status = 'interrupted', finished_at = ?, updated_at = ?, "
    "version = version + 1 "
    "WHERE status = 'running' RETURNING id"
)
_INTERRUPT_STALE = (
    "UPDATE tasks SET status = 'interrupted', finished_at = ?, updated_at = ?, "
    "version = version + 1 "
    "WHERE status = 'running' AND type = ? AND coalesce(heartbeat_at, updated_at) < ? "
    "RETURNING id"
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
    existing = get_active(conn, row.dedupe_key)
    if existing is None:  # pragma: no cover - the conflict implies an active row
        raise sqlite3.IntegrityError("dedupe conflict without an active task")
    return existing, False


def get_active(conn: sqlite3.Connection, key: str) -> TaskRow | None:
    """The queued or running task holding dedupe ``key``, if any (at most one, by index)."""
    row = conn.execute(_SELECT_ACTIVE_BY_KEY, (key,)).fetchone()
    return None if row is None else _row(row)


def get_latest(conn: sqlite3.Connection, key: str) -> TaskRow | None:
    """The most recently changed task with dedupe ``key``, in any state, if any."""
    row = conn.execute(_SELECT_LATEST_BY_KEY, (key,)).fetchone()
    return None if row is None else _row(row)


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
    attempt: int | None = None,
) -> bool:
    """Move the task to ``target`` if its status is one of ``expected`` (and, when
    ``attempt`` is given, it is that attempt); False otherwise.

    Expected states that can't reach ``target`` (``can_transition``) are dropped; ValueError
    only when none can (re-queueing a finished task is ``requeue_for_retry``'s job).
    ``result_ref`` is accepted only when ``target`` is succeeded (ValueError otherwise).
    Entering running starts the heartbeat; entering a terminal state sets ``finished_at``.
    Entering succeeded also needs ``cancel_requested = 0``: a cancel requested before the
    success commits wins (spec 5 M2 reliability rule 6).
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
            target.value,
            attempt,
            attempt,
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


def claim_next(conn: sqlite3.Connection, now: str) -> TaskRow | None:
    """Move the oldest queued task (by ``updated_at``, then insertion order) to running and
    return it; None when nothing is queued. One conditional update, so two claimers in
    separate transactions never get the same task."""
    row = conn.execute(_CLAIM_NEXT, (now, now)).fetchone()
    return None if row is None else _row(row)


def heartbeat(
    conn: sqlite3.Connection,
    task_id: str,
    now: str,
    *,
    progress: int | None = None,
    attempt: int | None = None,
) -> bool:
    """Record a heartbeat (and progress, if given) for a running task; False otherwise.
    Not a state change, so ``version`` and ``updated_at`` stay as they are. With ``attempt``,
    only that attempt's beat counts."""
    return beat(conn, task_id, now, progress=progress, attempt=attempt) is not None


def beat(
    conn: sqlite3.Connection,
    task_id: str,
    now: str,
    *,
    progress: int | None = None,
    attempt: int | None = None,
) -> bool | None:
    """``heartbeat`` that also reports the row's cancel flag, in the same update: None when
    the task (or that ``attempt`` of it) isn't running, else whether a cancel was requested."""
    _check_progress(progress)
    row = conn.execute(_HEARTBEAT, (now, progress, task_id, attempt, attempt)).fetchone()
    return None if row is None else bool(row[0])


def request_cancel(conn: sqlite3.Connection, task_id: str, now: str) -> bool:
    """Flag a running task for cancellation; the runner ends it. False otherwise."""
    cursor = conn.execute(_REQUEST_CANCEL, (now, task_id))
    return cursor.rowcount == 1


def interrupt_running(conn: sqlite3.Connection, now: str) -> list[str]:
    """Crash recovery (ADR-076 item 10): move every running task to interrupted, with
    ``finished_at`` and ``updated_at`` set to ``now``; attempt and error stay as they are.
    Returns the ids changed, sorted (empty when nothing was running)."""
    return sorted(str(row[0]) for row in conn.execute(_INTERRUPT_RUNNING, (now, now)).fetchall())


def interrupt_stale(conn: sqlite3.Connection, task_type: str, cutoff: str, now: str) -> list[str]:
    """Watchdog (spec 5 M2 reliability rule 5): move every running task of ``task_type``
    whose heartbeat (``updated_at`` when it has none) is strictly older than ``cutoff`` to
    interrupted, with ``finished_at`` and ``updated_at`` set to ``now``. One conditional
    update, so a heartbeat or finish that commits first keeps the task out of it.
    Returns the ids changed, sorted."""
    rows = conn.execute(_INTERRUPT_STALE, (now, now, task_type, cutoff)).fetchall()
    return sorted(str(row[0]) for row in rows)


def list_recent(conn: sqlite3.Connection, limit: int) -> list[TaskRow]:
    """Up to ``limit`` tasks ordered by ``updated_at``, most recently changed first (what the
    tasks panel wants). The tasks table stays small, so no index is needed for this."""
    return [_row(row) for row in conn.execute(_SELECT_RECENT, (limit,)).fetchall()]


def list_active(conn: sqlite3.Connection) -> list[TaskRow]:
    rows = conn.execute(_SELECT_BY_STATUS, (_json_list(ACTIVE),)).fetchall()
    return [_row(row) for row in rows]
