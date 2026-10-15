"""The Fact bank page: Library and Drafts to review (M3.5; AC-M3.1, AC-M3.2, AC-M3.6).

``GET /facts`` is a page route (``PAGE_TAG``, like ``register_pages``): with ``HX-Request:
true`` it returns the main content only. ``?tab=library|drafts|capture`` picks the tab (any
other value is the Library and is never shown back); the Library also reads ``q``, ``role``,
``skill``, ``status`` and ``page``. GET and HEAD change nothing (spec 2.9 rule 12).

Fragments (launch token; the same with or without ``HX-Request``; they change nothing):
``GET /facts/library`` answers ``#library-results`` for the same query fields, with what was
found in an "announce" trigger and, for htmx, the page's own address in ``HX-Push-Url``;
``GET /facts/drafts`` answers ``#fact-drafts`` and the Drafts tab's link with its count. The
kept uploads' list (``GET /facts/imports``) and the import routes are in
``app/api/facts_import.py``.

Draft actions (launch token and origin; URL-encoded; field ``version``, the version the page
showed): ``POST /facts/{id}/confirm``, ``/discard``, ``/undo-discard`` and
``/confirm-edited`` (with the wording: ``text``, or a story's ``situation``, ``action`` and
``result``), and ``POST /facts/import/{id}/confirm-all`` with one ``version:<fact id>`` field
per draft. An htmx answer is the fact's own block (``#fact-<id>``) with one focus target, the
words for the live region in an "announce" trigger and, swapped out of band, the Drafts tab's
link, the "Skip to drafts" link, and its group's heading and batch form. The batch answers its
own wrapper only (``#draft-group-<import id>-batch``: the result, which takes focus, and the
form as it is now) and removes, out of band, the blocks of the drafts it confirmed: every other
block on the page is left as it is, so a discarded draft keeps its Undo and an open inline form
its typing. A plain request answers 303 to the Drafts tab. "Restore draft" in the Library's
Discarded filter and on a discarded draft's own page posts to ``/undo-discard`` too (M3.6),
with the hidden field ``from`` (``library`` or ``editor``): its htmx answer is then the card
that says where the draft is now, its 409 links back to where it was pressed, and so does
the plain 303.

Errors (spec 2.11): ``FactNotFound`` is a fixed 404 text that repeats nothing from the
request, and so is a fact id that isn't of the safe shape, before any lookup;
``VersionConflict`` and ``FactTransitionRefused`` answer 409, for htmx as the block holding
the reload prompt (the batch: its wrapper, with no form beside the prompt);
``FactInputInvalid`` answers 422 with the message tied to its field (the Library's status
filter, a draft's wording) and nothing saved. A version that is missing or not a number is a
stale one. A rewording takes only the fields that draft's form shows; any other is ignored.
Rendering reads the database, so the async routes do their work in a worker thread.
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.facts_import import DRAFTS_TAB_URL, IMPORT_PATH, import_context
from app.api.forms import is_htmx, read_form
from app.api.pages import AREAS, area
from app.api.security import PAGE_TAG
from app.api.templating import render, templates
from app.domain.errors import (
    AppError,
    FactInputInvalid,
    FactNotFound,
    FactTransitionRefused,
    VersionConflict,
)
from app.domain.ids import is_safe_id
from app.onboarding.registry import Form
from app.services.fact_views import (
    CAPTURE,
    DRAFTS,
    LIBRARY,
    MSG_CONFIRMED,
    MSG_DISCARDED,
    MSG_EDITED,
    MSG_RESTORED,
    FactItem,
    FactViews,
    LibraryView,
    batch_message,
    library_params,
)
from app.services.facts import FactService

FACTS_PATH: Final = "/facts"
LIBRARY_PATH: Final = f"{FACTS_PATH}/library"
DRAFTS_PATH: Final = f"{FACTS_PATH}/drafts"
FACTS_PAGE: Final = "pages/facts.html"
FACTS_FRAGMENT: Final = "fragments/facts/_facts.html"
LIBRARY_FRAGMENT: Final = "fragments/facts/_library_results.html"
DRAFTS_FRAGMENT: Final = "fragments/facts/_drafts_answer.html"
DRAFT_FRAGMENT: Final = "fragments/facts/_draft_answer.html"
GROUP_FRAGMENT: Final = "fragments/facts/_group_answer.html"
CONFLICT_FRAGMENT: Final = "fragments/facts/_draft_conflict.html"
RESTORED_FRAGMENT: Final = "fragments/facts/_library_restored.html"
# "Restore draft" outside the Drafts tab says where it was pressed in this hidden field: the
# answer is then that place's card, and a refusal or a plain request leads back there.
FROM_FIELD: Final = "from"
FROM_LIBRARY: Final = "library"
FROM_EDITOR: Final = "editor"
DISCARDED_URL: Final = f"{FACTS_PATH}?tab={LIBRARY}&status=discarded"
VERSION_FIELD: Final = "version"
BATCH_PREFIX: Final = "version:"
# The paging links: an answer to one of them moves focus to the count (the link is gone).
PAGING_LINKS: Final = ("library-previous", "library-next")
_NO_VERSION: Final = 0  # versions start at 1, so this one is always stale

_CONFLICTS: Final = (VersionConflict, FactTransitionRefused)


@dataclass(frozen=True)
class _Action:
    """A draft action: what it does, how the fact's block reads afterwards and what the
    live region says."""

    run: Callable[[Request, str, int, Form], object]
    mode: str
    said: str


def register_fact_pages(app: FastAPI) -> None:
    """Add the Fact bank's routes directly to the app (not through an included router)."""
    app.add_api_route(
        FACTS_PATH,
        facts_page,
        methods=["GET", "HEAD"],
        tags=[PAGE_TAG],
        response_class=HTMLResponse,
        name="page_facts",
    )
    # Not pages: each needs the token, and the POSTs the origin as well.
    for path, fragment, name in (
        (LIBRARY_PATH, library_results, "facts_library"),
        (DRAFTS_PATH, drafts_region, "facts_drafts"),
    ):
        app.add_api_route(path, fragment, methods=["GET"], response_class=HTMLResponse, name=name)
    posts = (
        (IMPORT_PATH + "/{import_id:path}/confirm-all", confirm_all, "facts_confirm_all"),
        (FACTS_PATH + "/{fact_id:path}/confirm", confirm_fact, "facts_confirm"),
        (FACTS_PATH + "/{fact_id:path}/confirm-edited", confirm_edited, "facts_confirm_edited"),
        (FACTS_PATH + "/{fact_id:path}/discard", discard_fact, "facts_discard"),
        (FACTS_PATH + "/{fact_id:path}/undo-discard", undo_discard, "facts_undo_discard"),
    )
    for path, endpoint, name in posts:
        app.add_api_route(path, endpoint, methods=["POST"], response_class=HTMLResponse, name=name)


# --- the page and its fragments (plain functions: FastAPI runs them in its thread pool) ---------


def facts_page(request: Request) -> HTMLResponse:
    tab = request.query_params.get("tab", LIBRARY)
    return _page(request, tab if tab in (DRAFTS, CAPTURE) else LIBRARY)


def library_results(request: Request) -> HTMLResponse:
    """``#library-results`` alone. Focus stays in the filter being used; only an answer to
    a paging link, which the swap removes, moves it to the count."""
    library = _views(request).library(library_params(request.query_params))
    ctx = {
        "area": area("facts"),
        "library": library,
        "library_alone": True,
        "count_focus": request.headers.get("HX-Trigger") in PAGING_LINKS,
    }
    response = templates.TemplateResponse(request, LIBRARY_FRAGMENT, ctx)
    if library.status_error is not None:
        response.status_code = 422
        return response
    response.headers["HX-Trigger"] = json.dumps({"announce": library.announce})
    if is_htmx(request):  # the address a reload or a bookmark needs: the page, never this route
        response.headers["HX-Push-Url"] = f"{FACTS_PATH}?{library.query}"
    return response


def drafts_region(request: Request) -> HTMLResponse:
    """``#fact-drafts`` alone, then the Drafts tab's link (out of band) with its count."""
    views = _views(request)
    ctx = {"drafts": views.drafts(), "edits": {}, "tab": views.drafts_tab()}
    return templates.TemplateResponse(request, DRAFTS_FRAGMENT, ctx)


def _page(
    request: Request, tab: str, *, edits: dict[str, Any] | None = None, status_code: int = 200
) -> HTMLResponse:
    """The full page on ``tab``, or for htmx its main content. ``edits`` holds a refused
    inline form per fact id, shown back open with its error. Blocking."""
    views = _views(request)
    library: LibraryView | None = None
    if tab == LIBRARY:
        library = views.library(library_params(request.query_params))
        if library.status_error is not None:
            status_code = 422
    ctx = {
        "area": area("facts"),
        "areas": AREAS,
        "tabs": views.tabs(tab),
        "current_tab": tab,
        "refused": status_code == 422,
        "library": library,
        "library_alone": False,
        "count_focus": False,
        "drafts": views.drafts() if tab == DRAFTS else None,
        "edits": edits or {},
        "cv_import": import_context(request) if tab == DRAFTS else None,
    }
    response = render(request, page=FACTS_PAGE, fragment=FACTS_FRAGMENT, ctx=ctx)
    response.status_code = status_code
    return response


# --- draft actions ------------------------------------------------------------------------------


def facts_service(request: Request) -> FactService:
    facts: FactService = request.app.state.facts
    return facts


def _reword(request: Request, fact_id: str, version: int, form: Form) -> object:
    """Only the wording fields this draft's own form shows are read (``text`` for a fact,
    a story's parts): a forged extra field changes nothing."""
    shown = [field.name for field in _views(request).draft(fact_id).fields]
    wording = {name: form[name][0] for name in shown if form.get(name)}
    return facts_service(request).reword_and_confirm(fact_id, wording, expected_version=version)


_CONFIRM: Final = _Action(
    lambda request, fact_id, version, _form: facts_service(request).confirm(
        fact_id, expected_version=version
    ),
    "confirmed",
    MSG_CONFIRMED,
)
_CONFIRM_EDITED: Final = _Action(_reword, "edited", MSG_EDITED)
_DISCARD: Final = _Action(
    lambda request, fact_id, version, _form: facts_service(request).discard(
        fact_id, expected_version=version
    ),
    "discarded",
    MSG_DISCARDED,
)
_UNDO_DISCARD: Final = _Action(
    lambda request, fact_id, version, _form: facts_service(request).undo_discard(
        fact_id, expected_version=version
    ),
    "restored",
    MSG_RESTORED,
)


async def confirm_fact(request: Request, fact_id: str) -> Response:
    return await _draft_action(request, fact_id, _CONFIRM)


async def confirm_edited(request: Request, fact_id: str) -> Response:
    return await _draft_action(request, fact_id, _CONFIRM_EDITED)


async def discard_fact(request: Request, fact_id: str) -> Response:
    return await _draft_action(request, fact_id, _DISCARD)


async def undo_discard(request: Request, fact_id: str) -> Response:
    return await _draft_action(request, fact_id, _UNDO_DISCARD)


async def confirm_all(request: Request, import_id: str) -> Response:
    form = await read_form(request)
    if form is None:
        return HTMLResponse("", status_code=413)
    return await run_in_threadpool(_confirm_all, request, import_id, form)


async def _draft_action(request: Request, fact_id: str, action: _Action) -> Response:
    if (unknown := no_such_id(fact_id)) is not None:
        return unknown
    form = await read_form(request)
    if form is None:
        return HTMLResponse("", status_code=413)
    # Blocking (the database): never on the event loop.
    return await run_in_threadpool(_run_action, request, fact_id, form, action)


def _pressed_in(form: Form, fact_id: str, action: _Action) -> tuple[str, str]:
    """Where a restore was pressed (``FROM_LIBRARY``, ``FROM_EDITOR`` or "" for the Drafts
    tab) and the address that leads back there. Only "Restore draft" has another place."""
    place = (form.get(FROM_FIELD) or ("",))[0] if action is _UNDO_DISCARD else ""
    if place == FROM_LIBRARY:
        return place, DISCARDED_URL
    if place == FROM_EDITOR:
        return place, f"{FACTS_PATH}/{fact_id}/edit"
    return "", DRAFTS_TAB_URL


def _run_action(request: Request, fact_id: str, form: Form, action: _Action) -> Response:
    place, back_url = _pressed_in(form, fact_id, action)
    try:
        action.run(request, fact_id, form_version(form.get(VERSION_FIELD, ())), form)
    except FactNotFound as error:
        return not_found(error)
    except _CONFLICTS as error:
        return _conflict(request, error, fact_id=fact_id, reload_url=back_url)
    except FactInputInvalid as error:
        return _invalid_wording(request, fact_id, form, error)
    if not is_htmx(request):
        return RedirectResponse(back_url, status_code=303)
    views = _views(request)
    item = views.draft(fact_id)
    if place:
        return _restored_elsewhere(request, item, action.said, listed=place == FROM_LIBRARY)
    ctx = {
        "item": item,
        "mode": action.mode,
        "edit": None,
        "tab": views.drafts_tab(),
        # The group's other drafts are as they were, but its heading counts this one and
        # its batch form lists it.
        "group": views.group_of(item),
    }
    response = templates.TemplateResponse(request, DRAFT_FRAGMENT, ctx)
    response.headers["HX-Trigger"] = json.dumps({"announce": action.said})
    return response


def _restored_elsewhere(request: Request, item: FactItem, said: str, *, listed: bool) -> Response:
    """A discarded draft restored outside the Drafts tab: its card says where it is now.
    ``listed`` is the Library's Discarded filter, where the card is a list item and the
    Drafts tab's link gets its new count; the draft's own page has neither."""
    ctx = {"item": item, "tab": _views(request).drafts_tab() if listed else None}
    response = templates.TemplateResponse(request, RESTORED_FRAGMENT, ctx)
    response.headers["HX-Trigger"] = json.dumps({"announce": said})
    return response


def _confirm_all(request: Request, import_id: str, form: Form) -> Response:
    """Confirm the listed drafts of one import together, or none of them; the drafts that
    need a check are left, and the answer says how many. Blocking."""
    facts = facts_service(request)
    views = _views(request)
    if views.group(import_id) is None:  # also keeps an id of another shape out of the page
        return not_found(FactNotFound())
    listed = {
        name[len(BATCH_PREFIX) :]: form_version(values)
        for name, values in form.items()
        if name.startswith(BATCH_PREFIX)
    }
    try:
        confirmed = facts.confirm_import(import_id, expected_versions=listed)
    except FactNotFound as error:
        return not_found(error)
    except _CONFLICTS as error:
        return _conflict(request, error, import_id=import_id)
    if not is_htmx(request):
        return RedirectResponse(DRAFTS_TAB_URL, status_code=303)
    said = batch_message(len(confirmed), len(listed) - len(confirmed))
    ctx = _group_context(request, import_id, said=said, confirmed=[fact.id for fact in confirmed])
    response = templates.TemplateResponse(request, GROUP_FRAGMENT, ctx)
    response.headers["HX-Trigger"] = json.dumps({"announce": said})
    return response


# --- helpers ------------------------------------------------------------------------------------


def _views(request: Request) -> FactViews:
    views: FactViews = request.app.state.fact_views
    return views


def form_version(values: Sequence[str]) -> int:
    """The version a form sent, when it sent one whole number; anything else is stale."""
    given = tuple(values)
    usable = len(given) == 1 and given[0].isascii() and given[0].isdigit() and len(given[0]) < 10
    return int(given[0]) if usable else _NO_VERSION


def not_found(error: AppError) -> PlainTextResponse:
    """A fixed message: nothing of the request is repeated."""
    return PlainTextResponse(error.user_message, status_code=404)


def no_such_id(value: str) -> PlainTextResponse | None:
    """The answer to an id that isn't of the safe shape (``is_safe_id``): the 404 an unknown
    fact gets, before anything is looked up. ``None`` for an id that may name a fact."""
    return None if is_safe_id(value) else not_found(FactNotFound())


def _group_context(
    request: Request,
    import_id: str,
    *,
    said: str | None = None,
    conflict: str | None = None,
    confirmed: Sequence[str] = (),
) -> dict[str, Any]:
    """What the batch's answer shows: the result or the reason, the group as it is now and
    the ids of the facts just confirmed, whose blocks leave the page."""
    views = _views(request)
    return {
        "group": views.group(import_id),
        "said": said,
        "conflict": conflict,
        "confirmed": confirmed,
        "tab": views.drafts_tab(),
        "reload_url": DRAFTS_TAB_URL,
    }


def _conflict(
    request: Request,
    error: AppError,
    *,
    fact_id: str = "",
    import_id: str = "",
    reload_url: str = DRAFTS_TAB_URL,
) -> Response:
    """409: the draft, or one the batch lists, changed since the page showed it, or isn't a
    draft any more. For htmx the reload prompt takes the place of the fact's block (its
    forms hold a version that no longer works); for the batch, of its form (the versions it
    lists no longer work either). ``reload_url`` is the page the action was pressed on.
    Nothing is announced: nothing happened. A plain request gets the message."""
    if not is_htmx(request):
        return PlainTextResponse(error.user_message, status_code=409)
    if import_id:
        ctx = _group_context(request, import_id, conflict=error.user_message)
        return templates.TemplateResponse(request, GROUP_FRAGMENT, ctx, status_code=409)
    ctx = {"fact_id": fact_id, "conflict": error.user_message, "reload_url": reload_url}
    return templates.TemplateResponse(request, CONFLICT_FRAGMENT, ctx, status_code=409)


def _invalid_wording(
    request: Request, fact_id: str, form: Form, error: FactInputInvalid
) -> Response:
    """422: the wording can't be saved. The draft's form is shown back open, with what was
    typed, the message tied to its field and that field focused; for a plain request, on
    the full Drafts tab. Nothing is saved."""
    views = _views(request)
    item = views.draft(fact_id)
    names = [field.name for field in item.fields]
    edit = {
        "typed": {name: form[name][0].replace("\x00", "") for name in names if form.get(name)},
        "field": error.field if error.field in names else names[0],
        "message": error.user_message,
    }
    if not is_htmx(request):
        return _page(request, DRAFTS, edits={fact_id: edit}, status_code=422)
    ctx = {"item": item, "mode": "draft", "edit": edit, "tab": None, "group": None}
    return templates.TemplateResponse(request, DRAFT_FRAGMENT, ctx, status_code=422)
