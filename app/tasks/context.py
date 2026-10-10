"""What a task handler receives from the runner (spec 5 M2 reliability rules 5, 6, 9)."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from app.domain.tasks import CancelFlag


class TaskCancelled(Exception):
    """The cancel signal: raised by ``TaskContext.check_cancelled`` once the task's cancel
    flag is set. Handlers let it propagate; the runner ends the task cancelled."""


Beat = Callable[[str, int | None], object]
"""Blocking: records a heartbeat for a task id with an optional progress value (and, in the
runner, sets the task's cancel flag when the database says a cancel was requested)."""


@dataclass(frozen=True)
class TaskContext:
    task_id: str
    task_type: str
    input_ref: str | None
    attempt: int
    cancel_flag: CancelFlag
    _beat: Beat = field(repr=False)

    async def heartbeat(self, progress: int | None = None) -> None:
        """Record that the task is alive (and its progress, clamped to 0..100). The database
        write runs in a worker thread, never on the event loop (reliability rule 9). Only
        handlers beat: a hung call stops the heartbeats and the watchdog ends the task.
        Raises ``TaskCancelled`` once a cancel was requested (seen by this beat or before)."""
        clamped = None if progress is None else max(0, min(100, progress))
        await asyncio.to_thread(self._beat, self.task_id, clamped)
        self.check_cancelled()

    def check_cancelled(self) -> None:
        """Raise ``TaskCancelled`` once a cancel was requested; call it between steps."""
        if self.cancel_flag.cancelled:
            raise TaskCancelled
