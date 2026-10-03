"""The background task runner (spec 2.6; ADR-034). M0 has a stub that only idles.

The runner is an asyncio worker started and stopped by the app's lifespan. Task handling,
the concurrency limit and crash recovery arrive in M2.
"""

import asyncio
from typing import Literal

RunnerStatus = Literal["running", "stopped"]


class TaskRunner:
    def __init__(self) -> None:
        self._worker: asyncio.Task[None] | None = None
        self._stopping: asyncio.Event | None = None

    @property
    def status(self) -> RunnerStatus:
        return "running" if self._worker is not None and not self._worker.done() else "stopped"

    async def start(self) -> None:
        if self.status == "running":
            return
        self._stopping = asyncio.Event()
        self._worker = asyncio.create_task(self._run(self._stopping), name="task-runner")

    async def stop(self) -> None:
        worker, stopping = self._worker, self._stopping
        self._worker = self._stopping = None
        if worker is None or stopping is None:
            return
        stopping.set()
        await worker

    async def _run(self, stopping: asyncio.Event) -> None:
        await stopping.wait()
