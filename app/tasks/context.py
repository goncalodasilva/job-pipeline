"""What a task handler receives from the runner (spec 5 M2 reliability rules 5, 6, 9)."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from app.domain.tasks import CancelFlag

Beat = Callable[[str, int | None], object]
"""Blocking: records a heartbeat for a task id with an optional progress value."""


@dataclass(frozen=True)
class TaskContext:
    task_id: str
    task_type: str
    input_ref: str | None
    attempt: int
    cancel_flag: CancelFlag
    _beat: Beat = field(repr=False)

    async def heartbeat(self, progress: int | None = None) -> None:
        """Record that the task is alive (and its progress, 0 to 100). The database write
        runs in a worker thread, never on the event loop (reliability rule 9)."""
        await asyncio.to_thread(self._beat, self.task_id, progress)
