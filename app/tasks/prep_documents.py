"""Commit a narrative card version with task success (M8.2 part B): a failed task leaves no card."""

from app.services.prep_documents import PrepDocumentService
from app.tasks.context import TaskContext
from app.tasks.handlers import TaskHandler


def draft_narrative_handler(service: PrepDocumentService) -> TaskHandler:
    async def handle(ctx: TaskContext) -> str | None:
        return await ctx.complete(await service.run(ctx))

    return handle
