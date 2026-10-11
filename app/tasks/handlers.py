"""Task types the runner knows: each maps to its spec and a thin async handler that calls a
service (spec 2.3 rule 6; ADR-034). The registry starts empty; wiring fills it."""

from collections.abc import Awaitable, Callable, Iterator
from datetime import timedelta

from app.domain.tasks import TaskTypeSpec, dedupe_key
from app.tasks.context import TaskContext

TaskHandler = Callable[[TaskContext], Awaitable[str | None]]
"""Runs one task and returns its ``result_ref`` (or None)."""


class HandlerRegistry:
    def __init__(self, *, min_timeout: timedelta | None = None) -> None:
        """``min_timeout`` (wiring passes the provider's call timeout): every registered
        type's timeout must be longer, so a slow but live provider call is never taken for
        a hung task."""
        self._min_timeout = min_timeout
        self._entries: dict[str, tuple[TaskTypeSpec, TaskHandler]] = {}

    def register(self, task_type: str, spec: TaskTypeSpec, handler: TaskHandler) -> None:
        """Add a task type. ``ValueError`` for a type already registered, one that can't
        form a dedupe key (blank, or containing ':'), or a timeout not longer than
        ``min_timeout``."""
        dedupe_key(task_type, "check")  # raises ValueError for an unusable type name
        if self._min_timeout is not None and spec.timeout <= self._min_timeout:
            raise ValueError(
                f"task type {task_type!r} needs a timeout longer than {self._min_timeout}"
            )
        if task_type in self._entries:
            raise ValueError(f"task type {task_type!r} is already registered")
        self._entries[task_type] = (spec, handler)

    def require_min_timeout(self, min_timeout: timedelta) -> None:
        """Apply a floor (the longer of it and any floor already set) to the types already
        registered and to every later one. ``ValueError`` when a registered type's timeout
        is not longer than it."""
        for task_type, (spec, _handler) in self._entries.items():
            if spec.timeout <= min_timeout:
                raise ValueError(
                    f"task type {task_type!r} needs a timeout longer than {min_timeout}"
                )
        if self._min_timeout is None or min_timeout > self._min_timeout:
            self._min_timeout = min_timeout

    def get(self, task_type: str) -> tuple[TaskTypeSpec, TaskHandler] | None:
        return self._entries.get(task_type)

    def specs(self) -> Iterator[tuple[str, TaskTypeSpec]]:
        """Each registered type with its spec, in registration order."""
        for task_type, (spec, _handler) in list(self._entries.items()):
            yield task_type, spec
