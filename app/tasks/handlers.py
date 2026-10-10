"""Task types the runner knows: each maps to its spec and a thin async handler that calls a
service (spec 2.3 rule 6; ADR-034). The registry starts empty; wiring fills it."""

from collections.abc import Awaitable, Callable

from app.domain.tasks import TaskTypeSpec, dedupe_key
from app.tasks.context import TaskContext

TaskHandler = Callable[[TaskContext], Awaitable[str | None]]
"""Runs one task and returns its ``result_ref`` (or None)."""


class HandlerRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, tuple[TaskTypeSpec, TaskHandler]] = {}

    def register(self, task_type: str, spec: TaskTypeSpec, handler: TaskHandler) -> None:
        """Add a task type. ``ValueError`` for a type already registered or one that can't
        form a dedupe key (blank, or containing ':')."""
        dedupe_key(task_type, "check")  # raises ValueError for an unusable type name
        if task_type in self._entries:
            raise ValueError(f"task type {task_type!r} is already registered")
        self._entries[task_type] = (spec, handler)

    def get(self, task_type: str) -> tuple[TaskTypeSpec, TaskHandler] | None:
        return self._entries.get(task_type)
