"""The Refine with AI task's handler (M3.8): thin, the service makes the call."""

from app.services.fact_refine import FactRefineService
from app.tasks.context import TaskContext
from app.tasks.handlers import TaskHandler


def fact_refine_handler(refine: FactRefineService) -> TaskHandler:
    """The turn's questions, suggestion and flagged claims are written to the conversation
    by the service's writer, in the one transaction that ends the task succeeded. The fact
    itself is never written."""

    async def handle(ctx: TaskContext) -> str | None:
        write = await refine.run(ctx)
        return await ctx.complete(write)

    return handle
