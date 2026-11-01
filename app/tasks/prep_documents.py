"""Commit a narrative card or an adapted answer version with task success (M8.2 parts B and C):
a failed task leaves no card and no adapted answer."""

from app.services.prep_documents import PrepDocumentService
from app.tasks.context import TaskContext
from app.tasks.handlers import TaskHandler


def draft_narrative_handler(service: PrepDocumentService) -> TaskHandler:
    async def handle(ctx: TaskContext) -> str | None:
        return await ctx.complete(await service.run(ctx))

    return handle


def adapt_answer_handler(service: PrepDocumentService) -> TaskHandler:
    async def handle(ctx: TaskContext) -> str | None:
        return await ctx.complete(await service.run_adapt(ctx))

    return handle
