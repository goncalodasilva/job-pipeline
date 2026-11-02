"""A company brief and its task success commit together; an unsavable result stays process-local."""

from app.services.company_brief import BriefService
from app.tasks.context import TaskContext
from app.tasks.handlers import TaskHandler


def company_brief_handler(service: BriefService) -> TaskHandler:
    async def handle(ctx: TaskContext) -> str | None:
        write, held, company_id = await service.run(ctx)
        result = await ctx.complete(write)
        service.remember(company_id, held)
        return result

    return handle
