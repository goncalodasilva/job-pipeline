"""Commit an assessment with task success, then follow a newer summary if needed (M4.5)."""

import asyncio

from app.services.job_fit import JobFitService
from app.tasks.context import TaskContext
from app.tasks.handlers import TaskHandler


def job_fit_handler(service: JobFitService) -> TaskHandler:
    async def handle(ctx: TaskContext) -> str | None:
        write, job_id, summary_id = await service.run(ctx)
        result = await ctx.complete(write)
        await asyncio.to_thread(service.after_summary, job_id, unless_summary_id=summary_id)
        return result

    return handle
