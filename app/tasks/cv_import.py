"""The CV import task's handler (M3.4): thin, the service reads the CV and makes the call."""

from app.services.cv_import import CvImportService
from app.tasks.context import TaskContext
from app.tasks.handlers import TaskHandler


def cv_import_handler(cv_import: CvImportService) -> TaskHandler:
    """The drafts, the draft roles and the import's done state are written by the service's
    writer in the one transaction that ends the task succeeded."""

    async def handle(ctx: TaskContext) -> str | None:
        write = await cv_import.run(ctx)
        return await ctx.complete(write)

    return handle
