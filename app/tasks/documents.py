"""Commit a tailored CV version with task success (M6.7)."""

from app.services.documents import DocumentService
from app.tasks.context import TaskContext
from app.tasks.handlers import TaskHandler


def tailor_cv_handler(service: DocumentService) -> TaskHandler:
    async def handle(ctx: TaskContext) -> str | None:
        return await ctx.complete(await service.run_tailor_cv(ctx))

    return handle
