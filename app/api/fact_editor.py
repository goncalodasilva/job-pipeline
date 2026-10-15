"""The fact editor, a fact's history and the merge screen (M3.6; AC-M3.2, AC-M3.3).

Pages (``PAGE_TAG``; with ``HX-Request: true`` the main content only; GET and HEAD change
nothing, whatever the query says: spec 2.9 rule 12):

- ``GET /facts/new`` (``?type=fact|story``; anything else is a fact): the editor, empty.
- ``GET /facts/{id}/edit``: the editor for one fact with its history below (``#fact-history``).
  A discarded fact is shown without a form.
- ``GET /facts/{draft id}/merge`` (``?q=`` searches, ``?target=`` compares): the merge screen;
  for a draft a merge closed, the result and, while it can be taken back, "Undo merge"; for
  any other fact that isn't a draft, 409: the page, saying so, with the way back to the
  Drafts tab. An htmx search (``q`` in the query) moves focus to the candidates' heading
  and announces what it found.

The id in a page's address is one segment of the safe shape (``is_safe_id``): anything else is
the fixed 404 before any lookup, and an address with more segments is not a page at all. The
other routes answer an id of another shape the same way, and so does the merge POST for its
``target``; on the merge page such a ``target`` is an unknown one (the search, with its note).

Everything else needs the launch token, and each POST the app's own origin (URL-encoded):

- ``GET /facts/{id}/history``: the ``#fact-history`` fragment.
- ``POST /facts/new`` and ``POST /facts/{id}/edit`` (with ``version``): the editor's fields.
  The answer is the page's content with the saved form, a line that says so (it takes focus)
  and the words for the live region; a new fact also sends its own address in ``HX-Push-Url``.
  An htmx answer starts with the ``<title>`` its heading needs, which htmx applies.
- ``POST /facts/{id}/archive`` and ``/restore`` (``version``): the answer is the block with
  those buttons and, out of band, the editor's version field and the history.
- ``POST /facts/{draft id}/merge`` (``target``, ``draft_version``, ``target_version``,
  ``wording`` and, for ``edited``, the wording fields) and ``POST /facts/{draft id}/undo-merge``
  (``draft_version``, ``target_version``): the answer is the merge screen's body.

A plain POST answers 303. Errors (spec 2.11): ``FactNotFound`` is a fixed 404 text that repeats
nothing of the request; ``VersionConflict`` and ``FactTransitionRefused`` answer 409 with the
reload prompt and nothing announced, what was typed in the editor still there to copy (read-only
when the fact was discarded meanwhile and has no form any more); on the merge screen a
``FactTransitionRefused`` leads back to the Drafts tab instead, since reloading would refuse
again. ``FactInputInvalid`` answers 422 with the message tied to its field, that field focused
and what was typed kept. A version that is missing or not a number is a stale one; a form too
large to be one of these answers 413 as plain text. Everything here reads the database, so the
async routes do their work in a worker thread.
"""

import json
from collections.abc import Callable
from typing import Final
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.errors import INVALID_REQUEST
from app.api.facts import FACTS_PATH, facts_service, form_version, no_such_id, not_found
from app.api.facts_import import DRAFTS_TAB_URL
from app.api.forms import is_htmx, read_form
from app.api.pages import AREAS, area
from app.api.security import PAGE_TAG
from app.api.templating import render, renders_fragment, templates
from app.domain.errors import (
    AppError,
    FactInputInvalid,
    FactNotFound,
    FactTransitionRefused,
    VersionConflict,
)
from app.onboarding.registry import Form
from app.services.fact_editor import (
    MSG_ARCHIVED,
    MSG_UNARCHIVED,
    EditorView,
    FactEditor,
    added_message,
    one,
    saved_message,
    stale,
)
from app.services.fact_history import FactHistory, HistoryView
from app.services.fact_merge import (
    MSG_MERGE_UNDONE,
    MSG_MERGED,
    MSG_NOT_A_DRAFT,
    SEARCH,
    FactMerge,
    MergeView,
)

NEW_PATH: Final = f"{FACTS_PATH}/new"
EDITOR_PAGE: Final = "pages/fact_editor.html"
EDITOR_FRAGMENT: Final = "fragments/facts/_editor.html"
STATE_FRAGMENT: Final = "fragments/facts/_editor_state_answer.html"
HISTORY_FRAGMENT: Final = "fragments/facts/_history.html"
MERGE_PAGE: Final = "pages/fact_merge.html"
MERGE_FRAGMENT: Final = "fragments/facts/_merge.html"
MERGE_BODY_FRAGMENT: Final = "fragments/facts/_merge_body.html"
_MAX_QUERY_CHARS: Final = 200
_MAX_ID_CHARS: Final = 64

_CONFLICTS: Final = (VersionConflict, FactTransitionRefused)


def edit_path(fact_id: str) -> str:
    return f"{FACTS_PATH}/{fact_id}/edit"


def merge_path(draft_id: str) -> str:
    return f"{FACTS_PATH}/{draft_id}/merge"


def register_fact_editor_routes(app: FastAPI) -> None:
    """Add the editor's, the history's and the merge screen's routes directly to the app."""
    pages = (
        (NEW_PATH, new_fact_page, "page_fact_new"),
        # One safe segment each: a page loads without the token, so its pattern must not
        # reach any other address (``/facts/a/b/edit`` is not a page).
        (FACTS_PATH + "/{fact_id}/edit", edit_fact_page, "page_fact_edit"),
        (FACTS_PATH + "/{draft_id}/merge", merge_page, "page_fact_merge"),
    )
    for path, page, name in pages:
        app.add_api_route(
            path,
            page,
            methods=["GET", "HEAD"],
            tags=[PAGE_TAG],
            response_class=HTMLResponse,
            name=name,
        )
    # Not pages: each needs the token, and the POSTs the origin as well.
    app.add_api_route(
        FACTS_PATH + "/{fact_id:path}/history",
        fact_history,
        methods=["GET"],
        response_class=HTMLResponse,
        name="facts_history",
    )
    posts = (
        (NEW_PATH, create_fact, "facts_create"),
        (FACTS_PATH + "/{fact_id:path}/edit", save_fact, "facts_save"),
        (FACTS_PATH + "/{fact_id:path}/archive", archive_fact, "facts_archive"),
        (FACTS_PATH + "/{fact_id:path}/restore", restore_fact, "facts_restore"),
        (FACTS_PATH + "/{fact_id:path}/merge", merge_fact, "facts_merge"),
        (FACTS_PATH + "/{fact_id:path}/undo-merge", undo_merge, "facts_undo_merge"),
    )
    for path, endpoint, name in posts:
        app.add_api_route(path, endpoint, methods=["POST"], response_class=HTMLResponse, name=name)


def _editor(request: Request) -> FactEditor:
    editor: FactEditor = request.app.state.fact_editor
    return editor


def _history(request: Request) -> FactHistory:
    history: FactHistory = request.app.state.fact_history
    return history


def _merges(request: Request) -> FactMerge:
    merges: FactMerge = request.app.state.fact_merge
    return merges


def _announcing(response: Response, said: str) -> Response:
    response.headers["HX-Trigger"] = json.dumps({"announce": said})
    return response


def _too_large() -> Response:
    """413: no form of these routes is that large. Plain text, so nothing is swapped in."""
    return PlainTextResponse(INVALID_REQUEST, status_code=413)


# --- the editor ---------------------------------------------------------------------------------


def new_fact_page(request: Request) -> Response:
    return _editor_page(request, _editor(request).new(request.query_params.get("type", "")))


def edit_fact_page(request: Request, fact_id: str) -> Response:
    if (unknown := no_such_id(fact_id)) is not None:  # before anything is looked up
        return unknown
    try:
        return _editor_page(request, _editor(request).edit(fact_id))
    except FactNotFound as error:
        return not_found(error)


def fact_history(request: Request, fact_id: str) -> Response:
    """``#fact-history`` alone."""
    if (unknown := no_such_id(fact_id)) is not None:
        return unknown
    try:
        ctx = {"history": _history(request).of(fact_id), "oob": False}
    except FactNotFound as error:
        return not_found(error)
    return templates.TemplateResponse(request, HISTORY_FRAGMENT, ctx)


def _history_of(request: Request, view: EditorView) -> HistoryView | None:
    return None if view.fact_id is None else _history(request).of(view.fact_id)


def _editor_page(
    request: Request,
    view: EditorView,
    *,
    said: str | None = None,
    conflict: str | None = None,
    status_code: int = 200,
    titled: bool = False,
) -> Response:
    """The editor's page, or for htmx its content: the heading, what the last save did, the
    form, the archive block and the history. ``titled`` is for the answer to a save: as a
    fragment it starts with the page's ``<title>``, since its heading may be a new one (a
    full page has its own). Blocking."""
    ctx = {
        "area": area("facts"),
        "areas": AREAS,
        "editor": view,
        "history": _history_of(request, view),
        "said": said,
        "state_said": None,
        "conflict": conflict,
        "reload_url": edit_path(view.fact_id) if view.fact_id else NEW_PATH,
        "refused": status_code != 200,
        "swap_title": titled and renders_fragment(request),
    }
    response = render(request, page=EDITOR_PAGE, fragment=EDITOR_FRAGMENT, ctx=ctx)
    response.status_code = status_code
    return response


async def create_fact(request: Request) -> Response:
    form = await read_form(request)
    if form is None:
        return _too_large()
    return await run_in_threadpool(_create, request, form)


async def _posted(
    request: Request, fact_id: str, work: Callable[..., Response], *more: object
) -> Response:
    """A POST that names a fact in its address: ``work(request, fact_id, form, *more)`` in a
    worker thread. An id of another shape is the fixed 404 before anything is read."""
    if (unknown := no_such_id(fact_id)) is not None:
        return unknown
    form = await read_form(request)
    if form is None:
        return _too_large()
    return await run_in_threadpool(work, request, fact_id, form, *more)


async def save_fact(request: Request, fact_id: str) -> Response:
    return await _posted(request, fact_id, _save)


def _create(request: Request, form: Form) -> Response:
    editor = _editor(request)
    try:
        made = editor.create(form)
    except FactInputInvalid as error:
        view = editor.new(one(form, "type"), typed=form, error=error)
        return _editor_page(request, view, status_code=422, titled=True)
    if not is_htmx(request):
        return RedirectResponse(edit_path(made.id), status_code=303)
    said = added_message(made)
    response = _editor_page(request, editor.edit(made.id), said=said, titled=True)
    # The address a reload needs: the fact's own page, never the empty form.
    response.headers["HX-Push-Url"] = edit_path(made.id)
    return _announcing(response, said)


def _save(request: Request, fact_id: str, form: Form) -> Response:
    editor = _editor(request)
    version = form_version(form.get("version", ()))
    try:
        saved = editor.save(fact_id, form, expected_version=version)
    except FactNotFound as error:
        return not_found(error)
    except _CONFLICTS as error:
        # Still there to copy: in the form, or read-only for a fact discarded meanwhile.
        view = stale(editor.edit(fact_id, typed=form))
        return _editor_page(
            request, view, conflict=error.user_message, status_code=409, titled=True
        )
    except FactInputInvalid as error:
        view = editor.edit(fact_id, typed=form, error=error, version=version)
        return _editor_page(request, view, status_code=422, titled=True)
    if not is_htmx(request):
        return RedirectResponse(edit_path(fact_id), status_code=303)
    said = saved_message(saved)
    answer = _editor_page(request, editor.edit(fact_id), said=said, titled=True)
    return _announcing(answer, said)


# --- archive and restore ------------------------------------------------------------------------


async def archive_fact(request: Request, fact_id: str) -> Response:
    return await _posted(request, fact_id, _change_state, _archive, MSG_ARCHIVED)


async def restore_fact(request: Request, fact_id: str) -> Response:
    return await _posted(request, fact_id, _change_state, _restore, MSG_UNARCHIVED)


def _archive(request: Request, fact_id: str, version: int) -> object:
    return facts_service(request).archive(fact_id, expected_version=version)


def _restore(request: Request, fact_id: str, version: int) -> object:
    return facts_service(request).restore(fact_id, expected_version=version)


def _change_state(
    request: Request,
    fact_id: str,
    form: Form,
    change: Callable[[Request, str, int], object],
    said: str,
) -> Response:
    """Archive or restore, then the block with those buttons as it is now. The editor's
    form stays as it is on the page, typing included: only its version field is replaced."""
    try:
        change(request, fact_id, form_version(form.get("version", ())))
    except FactNotFound as error:
        return not_found(error)
    except _CONFLICTS as error:
        return _state_conflict(request, fact_id, error)
    if not is_htmx(request):
        return RedirectResponse(edit_path(fact_id), status_code=303)
    view = _editor(request).edit(fact_id)
    ctx = {
        "editor": view,
        "history": _history_of(request, view),
        "state_said": said,
        "conflict": None,
    }
    return _announcing(templates.TemplateResponse(request, STATE_FRAGMENT, ctx), said)


def _state_conflict(request: Request, fact_id: str, error: AppError) -> Response:
    """409: the reload prompt takes the place of the buttons, whose version no longer works.
    A plain request gets the message."""
    if not is_htmx(request):
        return PlainTextResponse(error.user_message, status_code=409)
    ctx = {
        "editor": None,
        "history": None,
        "state_said": None,
        "conflict": error.user_message,
        "reload_url": edit_path(fact_id),
    }
    return templates.TemplateResponse(request, STATE_FRAGMENT, ctx, status_code=409)


# --- the merge screen ---------------------------------------------------------------------------


def merge_page(request: Request, draft_id: str) -> Response:
    if (unknown := no_such_id(draft_id)) is not None:  # before anything is looked up
        return unknown
    query = request.query_params
    try:
        view = _merges(request).page(
            draft_id,
            q=query.get("q", "")[:_MAX_QUERY_CHARS],
            # One more than an id has, so that a longer value stays of another shape: the
            # service looks no such target up (the search, with the note an unknown one gets).
            target_id=query.get("target", "")[: _MAX_ID_CHARS + 1],
        )
    except FactNotFound as error:
        return not_found(error)
    except FactTransitionRefused:
        # Not a draft (any more), as a link kept from before says: the page, with the way back.
        return _merge_answer(
            request, draft_id, None, status_code=409, conflict=MSG_NOT_A_DRAFT, back=True
        )
    # A search sent by the page's own form (htmx): focus goes to what it found, in words too.
    searched = renders_fragment(request) and "q" in query and view.state == SEARCH
    answer = _merge_answer(request, draft_id, view, searched=searched)
    return _announcing(answer, view.found) if searched else answer


def _merge_answer(
    request: Request,
    draft_id: str,
    view: MergeView | None,
    *,
    body_only: bool = False,
    status_code: int = 200,
    said: str | None = None,
    conflict: str | None = None,
    reload_url: str | None = None,
    back: bool = False,
    searched: bool = False,
) -> Response:
    """The merge screen: the page (for htmx, its content), or its body alone as the answer
    to one of its forms. ``said`` is what the last action did; ``conflict`` why it was
    refused, with ``reload_url`` (the screen itself unless given) or, with ``back``, the link
    to the Drafts tab in place of a reload that would be refused again. ``searched`` marks
    the candidates' heading as the focus target. Blocking."""
    ctx = {
        "area": area("facts"),
        "areas": AREAS,
        "merge": view,
        "draft_id": draft_id,
        "said": said,
        "conflict": conflict,
        "reload_url": reload_url or merge_path(draft_id),
        "back_url": DRAFTS_TAB_URL if back else None,
        "searched": searched,
        "refused": status_code != 200,
    }
    if body_only:
        return templates.TemplateResponse(
            request, MERGE_BODY_FRAGMENT, ctx, status_code=status_code
        )
    response = render(request, page=MERGE_PAGE, fragment=MERGE_FRAGMENT, ctx=ctx)
    response.status_code = status_code
    return response


async def merge_fact(request: Request, fact_id: str) -> Response:
    return await _posted(request, fact_id, _merge)


async def undo_merge(request: Request, fact_id: str) -> Response:
    return await _posted(request, fact_id, _undo_merge)


def _merge(request: Request, draft_id: str, form: Form) -> Response:
    target_id = one(form, "target")
    if (unknown := no_such_id(target_id)) is not None:  # names nothing: never looked up
        return unknown
    merges = _merges(request)
    htmx = is_htmx(request)
    target_version = form_version(form.get("target_version", ()))
    try:
        facts_service(request).merge(
            draft_id,
            target_id,
            merges.settle(draft_id, target_id, form, target_version=target_version),
            expected_draft_version=form_version(form.get("draft_version", ())),
            expected_target_version=target_version,
        )
    except FactNotFound as error:
        return not_found(error)
    except _CONFLICTS as error:
        # A stale comparison is shown again by the reload; a refused one has no comparison.
        target = target_id if isinstance(error, VersionConflict) else ""
        return _merge_conflict(request, draft_id, error, target_id=target)
    except FactInputInvalid as error:
        return _merge_refused(request, draft_id, target_id, form, error)
    if not htmx:
        return RedirectResponse(merge_path(draft_id), status_code=303)
    answer = _merge_answer(request, draft_id, merges.page(draft_id), body_only=True)
    return _announcing(answer, MSG_MERGED)


def _merge_refused(
    request: Request, draft_id: str, target_id: str, form: Form, error: FactInputInvalid
) -> Response:
    """422: the comparison again, with the choice and what was typed, the message tied to
    its field and that field focused. Nothing is merged."""
    try:
        view = _merges(request).page(draft_id, target_id=target_id, typed=form, error=error)
    except FactTransitionRefused as refused:
        return _merge_conflict(request, draft_id, refused)
    return _merge_answer(request, draft_id, view, body_only=is_htmx(request), status_code=422)


def _undo_merge(request: Request, draft_id: str, form: Form) -> Response:
    try:
        facts_service(request).undo_merge(
            draft_id,
            expected_draft_version=form_version(form.get("draft_version", ())),
            expected_target_version=form_version(form.get("target_version", ())),
        )
    except FactNotFound as error:
        return not_found(error)
    except _CONFLICTS as error:
        return _merge_conflict(request, draft_id, error)
    if not is_htmx(request):
        return RedirectResponse(DRAFTS_TAB_URL, status_code=303)
    view = _merges(request).page(draft_id)
    answer = _merge_answer(request, draft_id, view, body_only=True, said=MSG_MERGE_UNDONE)
    return _announcing(answer, MSG_MERGE_UNDONE)


def _merge_conflict(
    request: Request, draft_id: str, error: AppError, *, target_id: str = ""
) -> Response:
    """409: one of the two changed since the comparison was shown, or this can't be merged
    (or taken back) any more. The prompt takes the place of the screen's body, whose forms
    hold versions that no longer work: a stale version offers the reload, a refusal the way
    back to the Drafts tab (the same page would refuse again). Nothing is announced:
    nothing happened."""
    reload_url = merge_path(draft_id)
    if target_id:
        reload_url = f"{reload_url}?{urlencode({'target': target_id})}"
    return _merge_answer(
        request,
        draft_id,
        None,
        body_only=is_htmx(request),
        status_code=409,
        conflict=error.user_message,
        reload_url=reload_url,
        back=isinstance(error, FactTransitionRefused),
    )
