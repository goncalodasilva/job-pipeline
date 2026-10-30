"""Thin session handlers: output and task success share ctx.complete."""

from app.services.sessions import SessionService
from app.tasks.context import TaskContext
from app.tasks.handlers import TaskHandler


def enrichment_question_handler(service: SessionService) -> TaskHandler:
    async def handle(ctx: TaskContext) -> str | None:
        return await ctx.complete(await service.run_question(ctx))

    return handle
