"""Commit a gap check or angle proposal with task success (M6.5)."""

from app.services.tailoring import TailoringService
from app.tasks.context import TaskContext
from app.tasks.handlers import TaskHandler


def gap_check_handler(service: TailoringService) -> TaskHandler:
    async def handle(ctx: TaskContext) -> str | None:
        return await ctx.complete(await service.run_gap_check(ctx))

    return handle


def propose_angles_handler(service: TailoringService) -> TaskHandler:
    async def handle(ctx: TaskContext) -> str | None:
        return await ctx.complete(await service.run_angles(ctx))

    return handle
