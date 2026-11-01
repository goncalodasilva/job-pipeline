"""Feedback and task success commit together; a failed task leaves the attempt without any."""

from app.services.answer_practice import AnswerPracticeService
from app.tasks.context import TaskContext
from app.tasks.handlers import TaskHandler


def answer_feedback_handler(service: AnswerPracticeService) -> TaskHandler:
    async def handle(ctx: TaskContext) -> str | None:
        return await ctx.complete(await service.run(ctx))

    return handle
