"""The practice task's handler (M2.17; ADR-076 item 2): thin, the service runs the steps."""

from app.services.practice import PracticeService, practice_result_ref
from app.tasks.context import TaskContext
from app.tasks.handlers import TaskHandler


def practice_handler(practice: PracticeService) -> TaskHandler:
    """The result is only a reference, so the writer stores nothing (results tables arrive
    in M3)."""

    async def handle(ctx: TaskContext) -> str | None:
        steps = await practice.run(ctx)
        return await ctx.complete(lambda _conn: practice_result_ref(steps))

    return handle
