"""Thin handler: summary and successful task state commit together (M4.4)."""

from app.services.job_summary import JobSummaryService
from app.tasks.context import TaskContext
from app.tasks.handlers import TaskHandler


def job_summary_handler(service: JobSummaryService) -> TaskHandler:
    async def handle(ctx: TaskContext) -> str | None:
        write = await service.run(ctx)
        return await ctx.complete(write)

    return handle
