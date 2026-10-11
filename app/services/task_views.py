"""What the task status fragments show (M2.19): plain data built from task rows, so routes
and templates hold no rules. Status is in words, a type shows the registry's label (never its
stored name), and a failure reads as the spec 2.11 message for its stored kind: the stored
text itself is never shown. Only an id of the safe shape (``is_task_id``) is looked up or
shown: any other id, asked for or stored, is treated as no task.

Each task also carries one time (M2.26): when it ended, for a task that has; else when it was
last queued, started or asked to cancel (``updated_at``, which a heartbeat or saved progress
never moves). It is shown as words counted from the app's clock ("5 minutes ago"), with the
exact local time beside it. A stored time is read only when it is an ISO 8601 time with a
zone; anything else shows no time. The top bar's "needs attention" count is built by the page
from ``attention``: the ``<id>:<attempt>`` keys of the listed tasks that ended failed or
interrupted.

A page being rendered asks ``top_bar()`` and the indicator route ``indicator()``: each reads
the task service once (``overview``), so the count and the keys come from one snapshot.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, tzinfo

from app.domain.clock import Clock
from app.domain.errors import TaskNotRetryable, format_day_time, message_for_kind
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

NEEDS_ATTENTION = frozenset({TaskStatus.FAILED, TaskStatus.INTERRUPTED})
"""How a task ends when the user should look at it: both offer Retry."""

JUST_NOW = "just now"
_AGO_UNITS = ((86400, "day"), (3600, "hour"), (60, "minute"))  # largest first

_WORDS = {
    TaskStatus.QUEUED: "Queued",
    TaskStatus.RUNNING: "Running",
    TaskStatus.SUCCEEDED: "Done",
    TaskStatus.FAILED: "Failed",
    TaskStatus.CANCELLED: "Cancelled",
    TaskStatus.INTERRUPTED: "Interrupted",
}


@dataclass(frozen=True)
class TimeView:
    """When a task last changed state, for a ``<time>`` element."""

    iso: str
    """The exact time in the local zone, ISO 8601 with its offset."""
    exact: str
    """The exact local time in words, e.g. "10 October 2026, 14:05"."""
    ago: str
    """ "just now", "5 minutes ago", "1 hour ago", "3 days ago"."""
    started: bool = False
    """True when this is the time a running task started (and no cancel was asked for)."""


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
    time: TimeView | None = None
    """None when the stored time can't be read."""
    attention: str | None = None
    """The task's ``<id>:<attempt>`` key when it ended failed or interrupted, else None."""


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
class TopBarView:
    """What the Tasks button of a rendered page shows (M2.20, M2.26)."""

    count: str
    """The running and the queued tasks counted, in words."""
    attention: tuple[str, ...] = ()
    """The ``<id>:<attempt>`` keys of the recent tasks that ended failed or interrupted."""


@dataclass(frozen=True)
class IndicatorView:
    """What the top-bar indicator shows (M2.20)."""

    count: str
    """The running and the queued tasks counted, in words."""
    finished: tuple[FinishedView, ...]
    """The recently finished tasks, newest change first."""
    attention: tuple[str, ...] = ()
    """The ``<id>:<attempt>`` keys of those that ended failed or interrupted."""


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


def _ago(seconds: float) -> str:
    """The age in the largest whole unit, rounded down; "just now" under a minute and for a
    time that is still to come (the clock was set back)."""
    for size, unit in _AGO_UNITS:
        count = int(seconds // size)
        if count >= 1:
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    return JUST_NOW


def _time(row: TaskRow, now: datetime, tz: tzinfo) -> TimeView | None:
    stored = row.finished_at if row.finished_at is not None else row.updated_at
    try:
        moment = datetime.fromisoformat(stored)
        if moment.utcoffset() is None:  # no zone: it can't be placed, so it isn't shown
            return None
        local = moment.astimezone(tz)
        age = (now - local).total_seconds()
    except (ValueError, TypeError, OverflowError):  # not a time
        return None
    started = row.status == TaskStatus.RUNNING and not row.cancel_requested
    return TimeView(
        iso=local.isoformat(), exact=format_day_time(local), ago=_ago(age), started=started
    )


def _key(row: TaskRow) -> str:
    return f"{row.id}:{row.attempt}"


def _attention(ended: list[TaskRow]) -> tuple[str, ...]:
    return tuple(_key(row) for row in ended if row.status in NEEDS_ATTENTION)


def _reason(row: TaskRow) -> str | None:
    if row.status == TaskStatus.FAILED:
        return message_for_kind(row.error)
    if row.status == TaskStatus.INTERRUPTED:
        return MSG_INTERRUPTED
    return None


def task_view(
    row: TaskRow,
    label: str | None,
    *,
    reason: str | None = None,
    time: TimeView | None = None,
) -> TaskView | None:
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
        time=time,
        attention=_key(row) if row.status in NEEDS_ATTENTION else None,
    )


class TaskViews:
    """``tasks`` answers the open workspace's task service (None while none is open);
    ``label_for`` answers a registered type's label (None for any other type)."""

    def __init__(
        self,
        tasks: Callable[[], TaskService | None],
        label_for: Callable[[str], str | None],
        clock: Clock,
        tz: tzinfo,
    ) -> None:
        self._tasks = tasks
        self._label_for = label_for
        self._clock = clock
        self._tz = tz

    def _view(self, row: TaskRow, *, reason: str | None = None) -> TaskView | None:
        time = _time(row, self._clock.now(), self._tz)
        return task_view(row, self._label_for(row.type), reason=reason, time=time)

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

    def _overview(self) -> tuple[str, list[TaskRow]]:
        """The count in words and the ended tasks among those the panel lists, from one
        read of the task service."""
        service = self._tasks()
        if service is None:
            return _count_words(0, 0), []
        recent, counts = service.overview(PANEL_LIMIT)
        ended = [row for row in recent if row.status in _OUTCOMES and is_task_id(row.id)]
        return _count_words(*counts), ended

    def top_bar(self) -> TopBarView:
        """The Tasks button of a page being rendered: the count and the attention keys,
        nothing to announce. Reads only."""
        count, ended = self._overview()
        return TopBarView(count, _attention(ended))

    def indicator(self) -> IndicatorView:
        """The running count and the finished tasks among those the panel lists. Reads only.
        A sentence holds a registered label and a fixed word, never anything stored."""
        count, ended = self._overview()
        now = self._clock.now()
        finished = tuple(
            FinishedView(
                key=_key(row),
                message=f"{self._label_for(row.type) or NEUTRAL_LABEL} {_OUTCOMES[row.status]}",
                age=_age(row.finished_at, now),
            )
            for row in ended
        )
        return IndicatorView(count, finished, _attention(ended))

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
