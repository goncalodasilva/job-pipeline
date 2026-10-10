"""What the task status fragments show (M2.19): plain data built from task rows, so routes
and templates hold no rules. Status is in words, a type shows the registry's label (never its
stored name), and a failure reads as the spec 2.11 message for its stored kind: the stored
text itself is never shown. Only an id of the safe shape (``is_task_id``) is looked up or
shown: any other id, asked for or stored, is treated as no task.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.domain.clock import Clock
from app.domain.errors import TaskNotRetryable, message_for_kind
from app.domain.tasks import RETRY_FROM, TaskStatus, is_task_id
from app.services.tasks import TaskRow, TaskService

PANEL_LIMIT = 20
"""How many recent tasks the panel lists."""

NEUTRAL_LABEL = "Task"
"""Shown for a type no handler is registered for (an older or newer version queued it)."""

CANCELLING = "Cancelling…"
MSG_INTERRUPTED = (
    "This task stopped before it finished, for example because the app closed. "
    "Retry to run it again."
)
MSG_CANCELLING = "Cancelling the task."
MSG_REQUEUED = "Task queued again."
MSG_ALREADY_ACTIVE = "This task is already queued or running."

NONE_RUNNING = "No tasks running"

# How a finished task is announced: "<label> <outcome>" (M2.20). The reason for a failure
# is in the panel, so the sentence says where to look.
_OUTCOMES = {
    TaskStatus.SUCCEEDED: "finished.",
    TaskStatus.FAILED: "failed. Open Tasks to see why.",
    TaskStatus.CANCELLED: "cancelled.",
    TaskStatus.INTERRUPTED: "was interrupted.",
}

_WORDS = {
    TaskStatus.QUEUED: "Queued",
    TaskStatus.RUNNING: "Running",
    TaskStatus.SUCCEEDED: "Done",
    TaskStatus.FAILED: "Failed",
    TaskStatus.CANCELLED: "Cancelled",
    TaskStatus.INTERRUPTED: "Interrupted",
}


@dataclass(frozen=True)
class TaskView:
    id: str
    label: str
    status: str
    progress: int | None
    """0 to 100 while the task is queued or running, else None."""
    reason: str | None
    has_result: bool
    can_cancel: bool
    can_retry: bool


@dataclass(frozen=True)
class ActionView:
    """The answer to a cancel or retry."""

    task: TaskView
    announce: str | None = None
    """What to tell the user happened; None when nothing did."""
    refused: bool = False
    """True when the task can't be retried; ``task`` then carries the reason."""
    resync: bool = False
    """True when whatever lists tasks should refetch: the page showed this task in a state
    it isn't in, or ``task`` is another task that may already be listed."""


@dataclass(frozen=True)
class FinishedView:
    """A finished task for the page to announce once."""

    key: str
    """The task's id and attempt: a retry that ends is news again."""
    message: str
    age: int | None
    """Whole seconds since it finished, by the app's clock; None when that isn't known."""


@dataclass(frozen=True)
class IndicatorView:
    """What the top-bar indicator shows (M2.20)."""

    count: str
    """The running and the queued tasks counted, in words."""
    finished: tuple[FinishedView, ...]
    """The recently finished tasks, newest change first."""


def _count_words(running: int, queued: int) -> str:
    """ "1 running", "2 queued", "1 running, 1 queued"; "No tasks running" when idle."""
    parts = [
        f"{count} {word}" for count, word in ((running, "running"), (queued, "queued")) if count
    ]
    return ", ".join(parts) or NONE_RUNNING


def _age(finished_at: str | None, now: datetime) -> int | None:
    if finished_at is None:
        return None
    try:
        return max(0, int((now - datetime.fromisoformat(finished_at)).total_seconds()))
    except (ValueError, TypeError):  # not a time, or one without a zone
        return None


def _reason(row: TaskRow) -> str | None:
    if row.status == TaskStatus.FAILED:
        return message_for_kind(row.error)
    if row.status == TaskStatus.INTERRUPTED:
        return MSG_INTERRUPTED
    return None


def task_view(row: TaskRow, label: str | None, *, reason: str | None = None) -> TaskView | None:
    """``reason`` replaces the row's own (a refused retry explains itself there). None for a
    stored id that hasn't the safe shape: it would become an element id and a URL."""
    if not is_task_id(row.id):
        return None
    running = row.status == TaskStatus.RUNNING
    cancelling = running and row.cancel_requested
    active = running or row.status == TaskStatus.QUEUED
    return TaskView(
        id=row.id,
        label=label or NEUTRAL_LABEL,
        status=CANCELLING if cancelling else _WORDS[row.status],
        progress=max(0, min(100, row.progress)) if active else None,
        reason=reason if reason is not None else _reason(row),
        has_result=row.status == TaskStatus.SUCCEEDED and row.result_ref is not None,
        can_cancel=active and not cancelling,
        can_retry=row.status in RETRY_FROM,
    )


class TaskViews:
    """``tasks`` answers the open workspace's task service (None while none is open);
    ``label_for`` answers a registered type's label (None for any other type)."""

    def __init__(
        self,
        tasks: Callable[[], TaskService | None],
        label_for: Callable[[str], str | None],
        clock: Clock,
    ) -> None:
        self._tasks = tasks
        self._label_for = label_for
        self._clock = clock

    def _view(self, row: TaskRow, *, reason: str | None = None) -> TaskView | None:
        return task_view(row, self._label_for(row.type), reason=reason)

    def _service(self, task_id: str) -> TaskService | None:
        """The task service, for an id of the safe shape only."""
        return self._tasks() if is_task_id(task_id) else None

    def panel(self) -> list[TaskView]:
        """The most recently changed tasks, newest change first. Reads only."""
        service = self._tasks()
        if service is None:
            return []
        views = (self._view(row) for row in service.recent(PANEL_LIMIT))
        return [view for view in views if view is not None]

    def indicator(self) -> IndicatorView:
        """The running count and the finished tasks among those the panel lists. Reads only.
        A sentence holds a registered label and a fixed word, never anything stored."""
        service = self._tasks()
        if service is None:
            return IndicatorView(_count_words(0, 0), ())
        now = self._clock.now()
        finished = tuple(
            FinishedView(
                key=f"{row.id}:{row.attempt}",
                message=f"{self._label_for(row.type) or NEUTRAL_LABEL} {_OUTCOMES[row.status]}",
                age=_age(row.finished_at, now),
            )
            for row in service.recent(PANEL_LIMIT)
            if row.status in _OUTCOMES and is_task_id(row.id)
        )
        return IndicatorView(_count_words(*service.active_counts()), finished)

    def count(self) -> str:
        """The indicator's count alone, for a page being rendered. Reads only."""
        service = self._tasks()
        return _count_words(*service.active_counts()) if service is not None else NONE_RUNNING

    def get(self, task_id: str) -> TaskView | None:
        """One task, or None when there is none with that id. Reads only."""
        service = self._service(task_id)
        row = service.get(task_id) if service is not None else None
        return None if row is None else self._view(row)

    def cancel(self, task_id: str) -> ActionView | None:
        """Ask for a cancel and show the task as it is afterwards (unchanged when it had
        already finished). None when there is no such task. Only "cancelling" is announced
        from here: a task that has ended is announced once, by the indicator."""
        service = self._service(task_id)
        row = service.request_cancel(task_id) if service is not None else None
        view = None if row is None else self._view(row)
        if row is None or view is None:
            return None
        if row.status == TaskStatus.RUNNING and row.cancel_requested:
            return ActionView(view, MSG_CANCELLING)
        return ActionView(view)

    def retry(self, task_id: str) -> ActionView | None:
        """Retry and show the task that is now queued or running: the same one, or the
        active task already holding its place. A task that can't be retried is shown as it
        is with ``TaskNotRetryable``'s message. None when there is no such task."""
        service = self._service(task_id)
        if service is None:
            return None
        try:
            row = service.retry(task_id)
        except TaskNotRetryable as refused:
            message = refused.user_message
            current = service.get(task_id)
            view = None if current is None else self._view(current, reason=message)
            return None if view is None else ActionView(view, message, refused=True, resync=True)
        view = None if row is None else self._view(row)
        if row is None or view is None:
            return None
        if row.id != task_id:
            return ActionView(view, MSG_ALREADY_ACTIVE, resync=True)
        return ActionView(view, MSG_REQUEUED)
