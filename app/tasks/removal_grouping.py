"""Commit a checked grouping result with the task's succeeded state."""

from app.services.removal_grouping import RemovalGrouping
from app.tasks.context import TaskContext
from app.tasks.handlers import TaskHandler


def removal_grouping_handler(service: RemovalGrouping) -> TaskHandler:
    async def handle(ctx: TaskContext) -> str:
        return await ctx.complete(await service.run(ctx))

    return handle
