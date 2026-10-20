"""Thin handler: summary and successful task state commit together (M4.4)."""

import asyncio
from collections.abc import Callable

from app.services.job_summary import JobSummaryService
from app.tasks.context import TaskContext
from app.tasks.handlers import TaskHandler


def job_summary_handler(
    service: JobSummaryService, after_summary: Callable[[str], None] | None = None
) -> TaskHandler:
    async def handle(ctx: TaskContext) -> str | None:
        write = await service.run(ctx)
        result = await ctx.complete(write)
        if after_summary is not None:
            await asyncio.to_thread(after_summary, (ctx.input_ref or "").partition("/")[0])
        return result

    return handle
