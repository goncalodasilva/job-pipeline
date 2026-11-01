"""Question output and task success commit together; unsavable results remain process-local."""

from app.services.prep import PrepService
from app.tasks.context import TaskContext
from app.tasks.handlers import TaskHandler


def prep_handler(service: PrepService, *, web: bool = False) -> TaskHandler:
    async def handle(ctx: TaskContext) -> str | None:
        write, transient, aid = await service.run(ctx, web=web)
        result = await ctx.complete(write)
        service.remember(aid, transient)
        return result

    return handle
