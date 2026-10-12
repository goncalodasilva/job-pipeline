"""Routes of the CV import (M3.4; AC-M3.1, AC-M3.7; spec 2.9 rules 5 and 12). None is a page:
each needs the launch token and origin.

``POST /facts/import`` (multipart, read with ``read_upload``: one ``file`` part or a ``text``
field) stores the upload and then tries to start its task. A refused upload answers 413
(``UploadTooLarge``) or 400 (every other document error) with the error's message as text,
and nothing is stored. ``POST /facts/import/{id}/start`` (URL-encoded) starts a stored upload
and ``POST /facts/import/{id}/remove`` deletes its file and row.

A start asks the budget gate first, as the practice route does (M2.24), about the saved
provider's usage service and the import's tier. A warning not yet confirmed answers 200 (htmx)
with the budget dialog, whose "Continue" posts the confirmation to the start route, so the CV
is never sent again; a Thorough cap with Fast free offers one run on Fast (``tier=fast``). A
block, a cap without that offer and unreadable budgets answer 409 with the reason; a plain
request always gets the reason as 409 text. None of these queues anything, and the upload is
kept either way: it can be started later or removed.

An id that is no import (or not of the safe shape) answers 404 with a fixed message that
repeats nothing from the request; the id is a ``path`` parameter so that an id holding slashes
gets the same answer. 409: a start of an import that is already done, a start without a saved
provider, a removal while the import's task is queued or running.

The Fact bank page is M3.5: until then a start answers the task's fragment (htmx) or 303 to
``/facts``, a removal a line of text or the same 303, and the dialog and the blocked reason
stand alone. ``IMPORT_TARGET`` and ``IMPORT_FOCUS`` name the elements M3.5's form provides.
"""

from typing import Final

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.budget_gate import dialog_context, reason_context, refusal_message
from app.api.errors import DOCUMENT_ERRORS
from app.api.forms import is_htmx, read_form
from app.api.templating import templates
from app.api.uploads import read_upload
from app.domain.errors import (
    AppError,
    CvImportAlreadyDone,
    CvImportBusy,
    CvImportNotFound,
    ProviderNotConfigured,
    UploadTooLarge,
)
from app.onboarding.registry import Form
from app.services.budget_gate import BudgetGate, Refusal, Start, confirmation_from_form
from app.services.cv_import import CvImportService, tier_from_form
from app.services.task_views import TaskViews

IMPORT_PATH: Final = "/facts/import"
FACTS_URL: Final = "/facts"
# What M3.5's import form provides: the element a start's answer replaces, and the control
# that gets focus back when the budget dialog is closed without a start.
IMPORT_TARGET: Final = "#cv-import"
IMPORT_FOCUS: Final = "#cv-import-start"
DIALOG_ID: Final = "cv-import-budget"
REASON_ID: Final = "cv-import-budget-reason"
MSG_REMOVED: Final = "The uploaded CV was removed."
_UPLOAD_ERRORS: Final[tuple[type[AppError], ...]] = (UploadTooLarge, *DOCUMENT_ERRORS)
_START_ERRORS: Final[tuple[type[AppError], ...]] = (
    CvImportNotFound,
    CvImportAlreadyDone,
    ProviderNotConfigured,
)


def start_path(import_id: str) -> str:
    return f"{IMPORT_PATH}/{import_id}/start"


def register_fact_import_routes(app: FastAPI) -> None:
    routes = (
        (IMPORT_PATH, upload_cv, "facts_import"),
        (IMPORT_PATH + "/{import_id:path}/start", start_import, "facts_import_start"),
        (IMPORT_PATH + "/{import_id:path}/remove", remove_import, "facts_import_remove"),
    )
    for path, endpoint, name in routes:
        app.add_api_route(path, endpoint, methods=["POST"], response_class=HTMLResponse, name=name)


async def upload_cv(request: Request) -> Response:
    """Takes only ``Request``: ``read_upload`` caps the body as it reads it."""
    service: CvImportService = request.app.state.cv_import
    try:
        upload = await read_upload(request)
        # Blocking (a parse in a child process, a file, the database): never on the loop.
        import_id = await run_in_threadpool(service.store, upload.data)
    except _UPLOAD_ERRORS as error:
        status = 413 if isinstance(error, UploadTooLarge) else 400
        return PlainTextResponse(error.user_message, status_code=status)
    return await run_in_threadpool(_start, request, import_id, {})


async def start_import(request: Request, import_id: str) -> Response:
    form = await read_form(request)
    if form is None:
        return HTMLResponse("", status_code=413)
    return await run_in_threadpool(_start, request, import_id, form)


async def remove_import(request: Request, import_id: str) -> Response:
    service: CvImportService = request.app.state.cv_import
    try:
        await run_in_threadpool(service.remove, import_id)
    except (CvImportNotFound, CvImportBusy) as error:
        return _refused(error)
    if not is_htmx(request):
        return RedirectResponse(FACTS_URL, status_code=303)
    return PlainTextResponse(MSG_REMOVED)


def _start(request: Request, import_id: str, form: Form) -> Response:
    """Ask the budget gate, then queue the import's task. Only ``Start`` queues it; a
    confirmation that names the warning due right now is stored by the check. Blocking."""
    service: CvImportService = request.app.state.cv_import
    gate: BudgetGate = request.app.state.budget_gate
    tier = tier_from_form(form)
    try:
        plan = service.plan_start(import_id, tier=tier)
        answer = gate.check(plan.service, tier=plan.tier, confirmation=confirmation_from_form(form))
        if not isinstance(answer, Start):
            return _not_started(request, import_id, answer)
        ref = service.start(import_id, tier=tier)
    except _START_ERRORS as error:
        return _refused(error)
    if not is_htmx(request):
        return RedirectResponse(FACTS_URL, status_code=303)
    views: TaskViews = request.app.state.task_views
    task = views.get(ref.id)
    if task is None:  # pragma: no cover - the task was queued a moment ago
        return PlainTextResponse("", status_code=200)
    ctx = {"task": task, "refresh": True, "focus": True}
    return templates.TemplateResponse(request, "fragments/tasks/_task.html", ctx)


def _not_started(request: Request, import_id: str, answer: Refusal) -> Response:
    """The budget gate's answer to a start it didn't let through: for htmx the dialog (200)
    when there is something to confirm, else the reason (409); a plain request gets the
    reason as text. The upload is kept."""
    if not is_htmx(request):
        return PlainTextResponse(refusal_message(answer), status_code=409)
    dialog = dialog_context(
        answer,
        dialog_id=DIALOG_ID,
        action=start_path(import_id),
        target=IMPORT_TARGET,
        fields={},
        return_focus=IMPORT_FOCUS,
    )
    if dialog is not None:
        return templates.TemplateResponse(
            request, "fragments/budget/_dialog.html", {"budget_dialog": dialog}
        )
    reason = reason_context(refusal_message(answer), reason_id=REASON_ID, focus=True)
    return templates.TemplateResponse(
        request, "fragments/budget/_blocked.html", {"budget_blocked": reason}, status_code=409
    )


def _refused(error: AppError) -> PlainTextResponse:
    """A fixed message: nothing of the request is repeated."""
    status = 404 if isinstance(error, CvImportNotFound) else 409
    return PlainTextResponse(error.user_message, status_code=status)
