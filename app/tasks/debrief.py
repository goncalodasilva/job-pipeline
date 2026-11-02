"""Draft facts and task success commit together; a failed task leaves the debrief without any."""

from app.services.debrief import DebriefService
from app.tasks.context import TaskContext
from app.tasks.handlers import TaskHandler


def debrief_facts_handler(service: DebriefService) -> TaskHandler:
    async def handle(ctx: TaskContext) -> str | None:
        return await ctx.complete(await service.run(ctx))

    return handle
