"""Commit a tailored CV or cover letter version with task success (M6.7, M6.9)."""

from app.services.documents import DocumentService
from app.services.letters import LetterService
from app.tasks.context import TaskContext
from app.tasks.handlers import TaskHandler


def tailor_cv_handler(service: DocumentService) -> TaskHandler:
    async def handle(ctx: TaskContext) -> str | None:
        return await ctx.complete(await service.run_tailor_cv(ctx))

    return handle


def draft_cover_letter_handler(service: LetterService) -> TaskHandler:
    async def handle(ctx: TaskContext) -> str | None:
        return await ctx.complete(await service.run_draft_cover_letter(ctx))

    return handle
