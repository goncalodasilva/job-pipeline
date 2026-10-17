"""The capture inbox and possible duplicates on the Fact bank page (M3.7). None is a page: each
needs the launch token, and each POST the app's own origin (URL-encoded).

Capture inbox (``?tab=capture``; ADR-090 item 10):

- ``POST /facts/capture`` (``text``): adds a note. htmx: the ``#capture-inbox`` section with the
  note listed and "Note added." (focused, and announced). A blank note answers 422 with the
  section, the field marked and described by its error, and nothing stored; a plain request
  then gets the whole page on the tab. A plain success answers 303 to the tab.
- ``POST /facts/capture/{id}/delete``: a soft delete. htmx: the section without the note,
  "Note deleted." and an Undo posting to ``/facts/capture/{id}/undo-delete``, which brings it
  back ("Note restored."). A note no longer in the inbox (deleted or turned into a fact
  meanwhile) is left as it is and the section says so, with no Undo. Plain: 303 to the tab.

"Turn into a fact" is a link to the editor (``/facts/new?note=<id>``, ``app/api/fact_editor.py``).

Possible duplicates (Drafts tab; ADR-089 item 7): ``GET /facts/duplicates`` answers the
``#fact-duplicates`` region (changes nothing, no AI call, no task; with ``?changed=1`` only the
line saying the drafts changed), ``POST /facts/duplicates/dismiss`` (``draft_id``, ``fact_id``)
remembers a pair and answers the region without it ("Suggestion dismissed.", with Undo), and
``POST /facts/duplicates/undo-dismiss`` forgets it again ("Suggestion restored."). Plain: 303
to the Drafts tab.

An id that names no note or no fact is a fixed 404 text that repeats nothing. A dismissal of
anything but a draft and a confirmed fact answers 409: for htmx the region as it is now,
saying the pair changed. Everything here reads the database, so
the async routes do their work in a worker thread.
"""

import json
from typing import Final

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.errors import INVALID_REQUEST
from app.api.facts import FACTS_PATH, capture_context, capture_page, not_found
from app.api.facts_import import DRAFTS_TAB_URL
from app.api.forms import is_htmx, read_form
from app.api.templating import templates
from app.domain.errors import FactInputInvalid, FactNotFound, FactTransitionRefused
from app.onboarding.registry import Form
from app.services.capture import (
    MSG_ALREADY_IN_INBOX,
    MSG_NOT_IN_INBOX,
    MSG_NOTE_ADDED,
    MSG_NOTE_DELETED,
    MSG_NOTE_RESTORED,
    TEXT_FIELD,
    CaptureService,
)
from app.services.fact_duplicates import (
    MSG_DISMISSED,
    MSG_PAIR_CHANGED,
    MSG_UNDISMISSED,
    DuplicatesView,
    FactDuplicates,
)

CAPTURE_PATH: Final = f"{FACTS_PATH}/capture"
CAPTURE_TAB_URL: Final = f"{FACTS_PATH}?tab=capture"
DUPLICATES_PATH: Final = f"{FACTS_PATH}/duplicates"
DISMISS_PATH: Final = f"{DUPLICATES_PATH}/dismiss"
UNDO_DISMISS_PATH: Final = f"{DUPLICATES_PATH}/undo-dismiss"
CAPTURE_FRAGMENT: Final = "fragments/facts/_capture.html"
DUPLICATES_FRAGMENT: Final = "fragments/facts/_duplicates.html"


def register_fact_capture_routes(app: FastAPI) -> None:
    posts = (
        (CAPTURE_PATH, add_note, "facts_capture_add"),
        (CAPTURE_PATH + "/{note_id:path}/delete", delete_note, "facts_capture_delete"),
        (CAPTURE_PATH + "/{note_id:path}/undo-delete", undo_delete_note, "facts_capture_undo"),
        (DISMISS_PATH, dismiss_pair, "facts_duplicates_dismiss"),
        (UNDO_DISMISS_PATH, undo_dismiss_pair, "facts_duplicates_undo_dismiss"),
    )
    for path, endpoint, name in posts:
        app.add_api_route(path, endpoint, methods=["POST"], response_class=HTMLResponse, name=name)
    app.add_api_route(  # not a page: it needs the token
        DUPLICATES_PATH,
        duplicates_region,
        methods=["GET"],
        response_class=HTMLResponse,
        name="facts_duplicates",
    )


def _duplicates(request: Request) -> FactDuplicates:
    duplicates: FactDuplicates = request.app.state.fact_duplicates
    return duplicates


def _section(request: Request, said: str, *, undo_id: str | None = None) -> Response:
    ctx = {"capture": capture_context(request, status=said, undo_id=undo_id)}
    response = templates.TemplateResponse(request, CAPTURE_FRAGMENT, ctx)
    response.headers["HX-Trigger"] = json.dumps({"announce": said})
    return response


async def _form(request: Request) -> Form | None:
    return await read_form(request)


def _too_large() -> Response:
    return PlainTextResponse(INVALID_REQUEST, status_code=413)


# --- the capture inbox --------------------------------------------------------------------------


async def add_note(request: Request) -> Response:
    form = await _form(request)
    if form is None:
        return _too_large()
    return await run_in_threadpool(_add, request, form)


def _add(request: Request, form: Form) -> Response:
    typed = (form.get(TEXT_FIELD) or ("",))[0]
    try:
        capture: CaptureService = request.app.state.capture
        capture.add(typed)
    except FactInputInvalid as error:
        return _refused(request, typed, error)
    if not is_htmx(request):
        return RedirectResponse(CAPTURE_TAB_URL, status_code=303)
    return _section(request, MSG_NOTE_ADDED)


def _refused(request: Request, typed: str, error: FactInputInvalid) -> Response:
    """422: the section (or, for a plain request, the page on the tab) with what was typed
    and the message tied to the field. Nothing is stored."""
    shown = {"error": error.user_message, "typed": typed.replace("\x00", "")}
    if not is_htmx(request):
        return capture_page(request, capture_context(request, **shown), status_code=422)
    ctx = {"capture": capture_context(request, **shown)}
    return templates.TemplateResponse(request, CAPTURE_FRAGMENT, ctx, status_code=422)


async def delete_note(request: Request, note_id: str) -> Response:
    return await run_in_threadpool(_change, request, note_id, undo=False)


async def undo_delete_note(request: Request, note_id: str) -> Response:
    return await run_in_threadpool(_change, request, note_id, undo=True)


def _change(request: Request, note_id: str, *, undo: bool) -> Response:
    capture: CaptureService = request.app.state.capture
    try:
        changed = capture.undo_delete(note_id) if undo else capture.delete(note_id)
    except FactNotFound as error:
        return not_found(error)
    if not is_htmx(request):
        return RedirectResponse(CAPTURE_TAB_URL, status_code=303)
    if undo:
        return _section(request, MSG_NOTE_RESTORED if changed else MSG_ALREADY_IN_INBOX)
    if not changed:  # deleted or turned into a fact meanwhile: nothing to undo
        return _section(request, MSG_NOT_IN_INBOX)
    return _section(request, MSG_NOTE_DELETED, undo_id=note_id)


# --- possible duplicates ------------------------------------------------------------------------


def duplicates_region(request: Request) -> HTMLResponse:
    """``#fact-duplicates`` with the pairs found now, announced; with ``changed`` in the query
    (the region asking for itself after the drafts changed on the page) only the line saying
    so, which reads nothing and is not announced. A plain function: FastAPI runs it in its
    thread pool. Changes nothing."""
    if request.query_params.get("changed"):
        ctx = {"duplicates": FactDuplicates.stale()}
        return templates.TemplateResponse(request, DUPLICATES_FRAGMENT, ctx)
    return _region(request, _duplicates(request).view(), announce=True)


def _region(
    request: Request, view: DuplicatesView, *, announce: bool, status_code: int = 200
) -> HTMLResponse:
    response = templates.TemplateResponse(
        request, DUPLICATES_FRAGMENT, {"duplicates": view}, status_code=status_code
    )
    if announce:
        response.headers["HX-Trigger"] = json.dumps({"announce": view.said})
    return response


async def dismiss_pair(request: Request) -> Response:
    form = await _form(request)
    if form is None:
        return _too_large()
    return await run_in_threadpool(_pair_action, request, form, undo=False)


async def undo_dismiss_pair(request: Request) -> Response:
    form = await _form(request)
    if form is None:
        return _too_large()
    return await run_in_threadpool(_pair_action, request, form, undo=True)


def _pair_action(request: Request, form: Form, *, undo: bool) -> Response:
    """Dismiss a pair, or take a dismissal back, and answer the region as it is now. A pair
    that is no longer a draft and a confirmed fact answers 409: for htmx the region (which
    a 409 replaces) without that pair, saying so; a plain request gets the message."""
    draft_id = (form.get("draft_id") or ("",))[0]
    fact_id = (form.get("fact_id") or ("",))[0]
    duplicates = _duplicates(request)
    try:
        if undo:
            duplicates.undismiss(draft_id, fact_id)
        else:
            duplicates.dismiss(draft_id, fact_id)
    except FactNotFound as error:
        return not_found(error)
    except FactTransitionRefused as error:
        if not is_htmx(request):
            return PlainTextResponse(error.user_message, status_code=409)
        view = duplicates.view(status=MSG_PAIR_CHANGED)
        return _region(request, view, announce=True, status_code=409)
    if not is_htmx(request):
        return RedirectResponse(DRAFTS_TAB_URL, status_code=303)
    if undo:
        return _region(request, duplicates.view(status=MSG_UNDISMISSED), announce=True)
    view = duplicates.view(status=MSG_DISMISSED, undo=(draft_id, fact_id))
    return _region(request, view, announce=True)
