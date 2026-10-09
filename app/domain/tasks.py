"""Background task states, dedupe keys, task-type specs and the cancel flag (spec 5 M2
reliability rules 1, 2, 6; ADR-076 item 11)."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol


class TaskStatus(StrEnum):
    """The values 0001's CHECK on ``tasks.status`` accepts."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


ACTIVE: Final = frozenset({TaskStatus.QUEUED, TaskStatus.RUNNING})
TERMINAL: Final = frozenset(
    {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.INTERRUPTED}
)
# Retry (ADR-076 item 11) re-queues a finished row from these; never from succeeded.
RETRY_FROM: Final = frozenset({TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.INTERRUPTED})

# The only ordinary transitions; terminal states leave only through retry.
ALLOWED_TRANSITIONS: Final[Mapping[TaskStatus, frozenset[TaskStatus]]] = MappingProxyType(
    {
        TaskStatus.QUEUED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
        TaskStatus.RUNNING: frozenset(
            {
                TaskStatus.SUCCEEDED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
                TaskStatus.INTERRUPTED,
            }
        ),
    }
)

_SEPARATOR: Final = ":"


def can_transition(source: TaskStatus, target: TaskStatus) -> bool:
    return target in ALLOWED_TRANSITIONS.get(source, frozenset())


def dedupe_key(task_type: str, target: str) -> str:
    """``type:target``; the type can't hold ':' so the first ':' always separates the two."""
    if not task_type.strip() or not target.strip():
        raise ValueError("a dedupe key needs a task type and a target")
    if _SEPARATOR in task_type:
        raise ValueError("a task type can't contain ':'")
    return f"{task_type}{_SEPARATOR}{target}"


@dataclass(frozen=True)
class TaskTypeSpec:
    """Per-type settings the runner needs: how long a task may go without a heartbeat, and
    the label shown in the tasks panel."""

    timeout: timedelta
    label: str

    def __post_init__(self) -> None:
        if not isinstance(self.timeout, timedelta):
            raise TypeError("timeout must be a timedelta")
        if self.timeout <= timedelta(0):
            raise ValueError("timeout must be positive")
        if not isinstance(self.label, str):
            raise TypeError("label must be a str")
        if not self.label.strip():
            raise ValueError("label must not be blank")


@dataclass(frozen=True)
class TaskRef:
    """What ``enqueue`` returns: the task's id, and False in ``created`` when an active task
    with the same dedupe key already existed and its id is returned instead."""

    id: str
    created: bool


@dataclass(frozen=True)
class TaskChange:
    """A committed task state change, published after the commit (for the tasks panel)."""

    id: str
    type: str
    status: TaskStatus


class TaskEnqueuer(Protocol):
    """How other services start background work (ADR-034); wiring injects the task service."""

    def enqueue(self, task_type: str, target: str, *, input_ref: str | None = None) -> TaskRef:
        """Queue a task, or return the active task already queued for ``type:target``."""
        ...


class CancelFlag:
    """A per-task cancel signal; satisfies the AI module's ``CancelToken`` by shape. Only
    ``cancel()`` sets it and nothing clears it."""

    __slots__ = ("_cancelled",)

    def __init__(self) -> None:
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True
