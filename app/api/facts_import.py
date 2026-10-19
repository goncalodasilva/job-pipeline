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

On the Fact bank's Drafts tab (M3.5) the form, the kept uploads' Start and the budget dialog
all swap the import section (``IMPORT_TARGET``), so every htmx answer to an upload or a start
is that section: with a line saying the import started (it takes focus, and the same words go
to the live region), with the budget dialog inside (200), or with the reason nothing started
(409, and 400 or 413 for a refused upload, which app.js lets the section show when the answer
is HTML with something in it; a form too large to be a start answers 413 as plain text). A
file sent together with a pasted text is refused in this page's own words. A removal
answers the kept uploads' list (``#cv-import-list``) with its result. A plain request gets
303 back to the section, or the reason as text. An unknown id stays a fixed 404 text.

``GET /facts/imports`` (launch token; changes nothing) answers the kept uploads' list, each
with its task's status. The list fetches it on the body's task events. The request carries the
stamp of the drafts the page shows (``drafts_seen``); when an import finished or was removed
since, the answer fires ``facts:drafts-changed`` on the body and the drafts fetch themselves
again. So the drafts are replaced only when there is something new, never on every task event.
"""

import json
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.budget_gate import dialog_context, reason_context, refusal_message
from app.api.errors import DOCUMENT_ERRORS, INVALID_REQUEST
from app.api.forms import is_htmx, read_form
from app.api.templating import templates
from app.api.uploads import BOTH_GIVEN, read_upload
from app.domain.errors import (
    AppError,
    CvImportAlreadyDone,
    CvImportBusy,
    CvImportNotFound,
    ProviderNotConfigured,
    UploadNotAccepted,
    UploadTooLarge,
)
from app.onboarding.registry import Form
from app.services.budget_gate import BudgetGate, Refusal, Start, confirmation_from_form
from app.services.cv_import import CvImportService, tier_from_form
from app.services.fact_views import MSG_CHOOSE_ONE, MSG_IMPORT_FAILED, FactViews

IMPORT_PATH: Final = "/facts/import"
IMPORTS_PATH: Final = "/facts/imports"
DRAFTS_TAB_URL: Final = "/facts?tab=drafts"
SECTION_URL: Final = f"{DRAFTS_TAB_URL}#cv-import"  # where a plain request goes back to
# The element a start's answer replaces, and the control that gets focus back when the
# budget dialog is closed without a start.
IMPORT_TARGET: Final = "#cv-import"
IMPORT_FOCUS: Final = "#cv-import-start"
DIALOG_ID: Final = "cv-import-budget"
REASON_ID: Final = "cv-import-budget-reason"
SECTION_FRAGMENT: Final = "fragments/facts/_import.html"
LIST_FRAGMENT: Final = "fragments/facts/_import_list.html"
MSG_STARTED: Final = "CV import started."
# The status line goes on to say where the result will be: the announcement stays short.
_STARTED_NEXT: Final = (
    "When it finishes, its drafts are listed under Drafts to review. "
    "If it can't be read, the reason appears under Uploaded CVs."
)
MSG_REMOVED: Final = "The uploaded CV was removed."
# Fired on the body when the drafts the page shows are out of date; the drafts refetch on it.
DRAFTS_CHANGED_EVENT: Final = "facts:drafts-changed"
DRAFTS_SEEN_FIELD: Final = "drafts_seen"
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
    app.add_api_route(  # not a page: it needs the token
        IMPORTS_PATH,
        list_imports,
        methods=["GET"],
        response_class=HTMLResponse,
        name="facts_imports",
    )


def import_context(
    request: Request,
    *,
    status: str | None = None,
    error: str | None = None,
    refusal: Refusal | None = None,
    refused_import: str = "",
    list_status: str | None = None,
    list_error: str | None = None,
) -> dict[str, Any]:
    """The ``cv_import`` template value: everything the import section shows. ``status``
    (an import started) or ``error`` (nothing started, and why) takes focus; ``refusal`` is
    the budget gate's answer about ``refused_import``, shown as its dialog or as the reason
    with a link to the budgets; ``list_status`` and ``list_error`` are a removal's result.
    Blocking (reads the kept uploads and their tasks)."""
    views: FactViews = request.app.state.fact_views
    dialog = reason = None
    if refusal is not None:
        dialog = dialog_context(
            refusal,
            dialog_id=DIALOG_ID,
            action=start_path(refused_import),
            target=IMPORT_TARGET,
            fields={},
            return_focus=IMPORT_FOCUS,
        )
        if dialog is None:
            reason = reason_context(refusal_message(refusal), reason_id=REASON_ID, focus=True)
    return {
        "uploads": views.uploads(),
        "draft_count": views.draft_count(),
        "practice": views.practice_provider(),
        # app.js says the second instead of the first: on this page the reason is in the row.
        "failed_said": views.import_failed_said,
        "failed_instead": MSG_IMPORT_FAILED,
        "status": status,
        "error": error,
        "dialog": dialog,
        "budget_reason": reason,
        "list_status": list_status,
        "list_error": list_error,
    }


def _section(request: Request, status_code: int = 200, **shown: Any) -> HTMLResponse:
    ctx = {"cv_import": import_context(request, **shown)}
    return templates.TemplateResponse(request, SECTION_FRAGMENT, ctx, status_code=status_code)


def list_imports(request: Request) -> HTMLResponse:
    """The kept uploads' list alone. A plain function: FastAPI runs it in its thread pool.
    Nothing in the answer takes focus or is announced."""
    views: FactViews = request.app.state.fact_views
    ctx = {"cv_import": import_context(request)}
    response = templates.TemplateResponse(request, LIST_FRAGMENT, ctx)
    seen = request.query_params.get(DRAFTS_SEEN_FIELD)
    if seen is not None and seen != views.drafts_stamp():
        response.headers["HX-Trigger"] = json.dumps({DRAFTS_CHANGED_EVENT: ""})
    return response


async def upload_cv(request: Request) -> Response:
    """Takes only ``Request``: ``read_upload`` caps the body as it reads it."""
    service: CvImportService = request.app.state.cv_import
    try:
        upload = await read_upload(request)
        # Blocking (a parse in a child process, a file, the database): never on the loop.
        import_id = await run_in_threadpool(service.store, upload.data)
    except _UPLOAD_ERRORS as error:
        status = 413 if isinstance(error, UploadTooLarge) else 400
        if not is_htmx(request):
            return PlainTextResponse(error.user_message, status_code=status)
        return await run_in_threadpool(_section, request, status, error=_page_words(error))
    return await run_in_threadpool(_start, request, import_id, {})


async def start_import(request: Request, import_id: str) -> Response:
    form = await read_form(request)
    if form is None:  # no form of this page is that large: text, so the section isn't replaced
        return PlainTextResponse(INVALID_REQUEST, status_code=413)
    return await run_in_threadpool(_start, request, import_id, form)


def _page_words(error: AppError) -> str:
    """The refusal as the import section words it: the error's own message, except for a
    file sent together with a pasted text, where the page can say what to do."""
    if isinstance(error, UploadNotAccepted) and str(error) == BOTH_GIVEN:
        return MSG_CHOOSE_ONE
    return error.user_message


async def remove_import(request: Request, import_id: str) -> Response:
    return await run_in_threadpool(_remove, request, import_id)


def _remove(request: Request, import_id: str) -> Response:
    """Remove the upload and answer the list without it. Its drafts stay, no longer tied to
    an import, so the drafts shown are out of date. Blocking."""
    service: CvImportService = request.app.state.cv_import
    try:
        service.remove(import_id)
    except CvImportNotFound as error:
        return PlainTextResponse(error.user_message, status_code=404)
    except CvImportBusy as error:
        if not is_htmx(request):
            return PlainTextResponse(error.user_message, status_code=409)
        ctx = {"cv_import": import_context(request, list_error=error.user_message)}
        return templates.TemplateResponse(request, LIST_FRAGMENT, ctx, status_code=409)
    if not is_htmx(request):
        return RedirectResponse(SECTION_URL, status_code=303)
    ctx = {"cv_import": import_context(request, list_status=MSG_REMOVED)}
    response = templates.TemplateResponse(request, LIST_FRAGMENT, ctx)
    response.headers["HX-Trigger"] = json.dumps({"announce": MSG_REMOVED, DRAFTS_CHANGED_EVENT: ""})
    return response


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
        service.start(import_id, tier=tier)
    except _START_ERRORS as error:
        return _refused(request, error)
    if not is_htmx(request):
        return RedirectResponse(SECTION_URL, status_code=303)
    response = _section(request, status=f"{MSG_STARTED} {_STARTED_NEXT}")
    response.headers["HX-Trigger"] = json.dumps({"announce": MSG_STARTED})
    return response


def _not_started(request: Request, import_id: str, answer: Refusal) -> Response:
    """The budget gate's answer to a start it didn't let through. For htmx the section: 200
    with the dialog inside when there is something to confirm (as Settings > Testing tools
    does: the dialog alone would replace the section), else 409 with the reason. A plain
    request gets the reason as text. The upload is kept and listed."""
    if not is_htmx(request):
        return PlainTextResponse(refusal_message(answer), status_code=409)
    ctx = {"cv_import": import_context(request, refusal=answer, refused_import=import_id)}
    status = 200 if ctx["cv_import"]["dialog"] is not None else 409
    return templates.TemplateResponse(request, SECTION_FRAGMENT, ctx, status_code=status)


def _refused(request: Request, error: AppError) -> Response:
    """A fixed message: nothing of the request is repeated. A 409 for htmx is the section
    with the message (a 409 is swapped in, and text alone would replace the section)."""
    if isinstance(error, CvImportNotFound):
        return PlainTextResponse(error.user_message, status_code=404)
    if not is_htmx(request):
        return PlainTextResponse(error.user_message, status_code=409)
    return _section(request, 409, error=error.user_message)
