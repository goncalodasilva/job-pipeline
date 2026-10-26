"""Commit a country CV template draft with task success (M6.6)."""

from app.services.cv_templates import CvTemplateService
from app.tasks.context import TaskContext
from app.tasks.handlers import TaskHandler


def draft_cv_template_handler(service: CvTemplateService) -> TaskHandler:
    async def handle(ctx: TaskContext) -> str | None:
        return await ctx.complete(await service.run_draft(ctx))

    return handle
