"""The job inbox screen (M4.7): the page, its protected card fragments and triage answers.

``GET /inbox`` is a page route (``PAGE_TAG``): it loads without the launch token and, with
``HX-Request: true``, answers the main content (token needed). Query: ``view`` (``queue``,
``snoozed``, ``removed`` or ``to_apply``), ``page`` and ``selected`` (the focused job).
``GET /inbox/queue`` (token) answers the ``#inbox`` block alone, with the same query and
``focus=1`` for a view change: focus moves to the card and the view is announced.

``GET /inbox/jobs/{id}/card`` and ``/detail`` are fragments for the focused card and its details
preview. Like every GET they change nothing: a card is marked seen only by ``POST
/inbox/jobs/{id}/seen`` (``version``), which the shown card sends once displayed (spec 2.9 rule
12). A prefetched card is never shown, so it is never marked.

The screen's actions are htmx requests from the page's scripts (as everywhere in the app,
the launch token comes with them); none has a plain-form fallback. ``POST
/inbox/jobs/{id}/triage`` (token and origin) carries the job's ``version`` and an
``action``: ``remove`` (optional ``reason`` and ``text``), ``snooze`` (``choice``, and ``picked``
for ``date``), ``to_apply``, ``undo`` or ``restore``. A stale version, or a move the job's
state does not allow, answers 409 with the reload prompt in ``#inbox-notice``; a value that
needs correcting answers 422 with the error tied to its field (only ``text`` and ``picked``
are shown back; an unknown job is always the fixed 404, whatever else was wrong). The other
fields (``view``, ``page``, ``next_id``) only say which card is shown next; a page with nothing
left on it gives way to the last page that has jobs. A field given twice, or one not named
here, is a 400.

``GET`` and ``POST /settings/shortcuts`` show and save the versioned "Use single-key shortcuts"
switch.

Every unknown or unsafe job id answers the fixed 404 text, which never repeats the id.
"""

import json
from datetime import date
from typing import Any
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.errors import INVALID_REQUEST
from app.api.forms import posted_version, single_value_form
from app.api.job_summary import summary_context
from app.api.pages import AREAS, area
from app.api.security import PAGE_TAG
from app.api.templating import render, templates
from app.domain.errors import (
    JobNotFound,
    JobTriageInputInvalid,
    StatusNotAllowed,
    TriageNotAllowed,
    VersionConflict,
    format_short_day,
)
from app.domain.ids import is_safe_id
from app.domain.inbox import InboxFilters
from app.domain.inbox_options import InboxSelection, selection
from app.services.inbox import MAX_PAGE
from app.services.inbox_views import InboxViews
from app.services.job_triage import JobTriageService

VIEWS = {
    "queue": "Queue",
    "snoozed": "Snoozed",
    "to_apply": "To apply",
    "removed": "Removed",
}
SCREEN_FRAGMENT = "fragments/inbox/_screen.html"
CARD_FRAGMENT = "fragments/inbox/_card.html"
INBOX_FRAGMENT = "fragments/inbox/_inbox.html"
ACTIONS = ("remove", "snooze", "to_apply", "undo", "restore")
TRIAGE_FIELDS = {
    "version",
    "action",
    "reason",
    "text",
    "choice",
    "picked",
    "view",
    "page",
    "next_id",
    "filters",
}


def register_inbox_routes(app: FastAPI) -> None:
    app.add_api_route(
        "/inbox",
        inbox_page,
        methods=["GET", "HEAD"],
        tags=[PAGE_TAG],
        response_class=HTMLResponse,
        name="page_inbox",
    )
    app.add_api_route("/inbox/queue", show_queue, methods=["GET"])
    app.add_api_route("/inbox/jobs/{job_id}/card", show_card, methods=["GET"])
    app.add_api_route("/inbox/jobs/{job_id}/seen", mark_seen, methods=["POST"])
    app.add_api_route("/inbox/jobs/{job_id}/triage", triage, methods=["POST"])
    app.add_api_route("/settings/shortcuts", show_shortcuts, methods=["GET"])
    app.add_api_route("/settings/shortcuts", save_shortcuts, methods=["POST"])


def _views(request: Request) -> InboxViews:
    views: InboxViews = request.app.state.inbox_views
    return views


def _page_number(raw: str) -> int:
    try:
        number = int(raw)
    except ValueError:
        return 1
    return number if 1 <= number <= MAX_PAGE else 1


def _view(raw: str) -> str:
    return raw if raw in VIEWS else "queue"


def _safe(raw: str) -> str:
    return raw if is_safe_id(raw) else ""


def _not_found() -> Response:
    return PlainTextResponse(JobNotFound.message, status_code=404)


def _card_context(
    request: Request,
    job_id: str,
    *,
    focus: bool,
    announce: bool = False,
    shortcuts_on: bool,
    view: str,
    page: int,
    position: int,
    total: int,
    previous_id: str,
    following_id: str,
    error: JobTriageInputInvalid | None = None,
    submitted: dict[str, str] | None = None,
    filter_query: str = "",
) -> dict[str, Any]:
    shown = _views(request).card(job_id)
    # A job with no summary and none being made offers the summary panel, which holds the
    # budget check, the reason it may be blocked and the way to start again.
    panel = (
        summary_context(request, job_id)
        if shown["summary"] is None and not shown["processing"]
        else None
    )
    return {
        "card": shown,
        "panel": panel,
        "focus": focus,
        "announce": announce,
        "shortcuts_on": shortcuts_on,
        "view": view,
        "page": page,
        "position": position,
        "total": total,
        "previous_id": previous_id,
        "following_id": following_id,
        "error": error,
        "submitted": submitted or {},
        "filter_query": filter_query,
    }


def _inbox_context(
    request: Request,
    *,
    selected: str = "",
    view: str = "queue",
    page: int = 1,
    focus: bool = False,
    after_action: bool = False,
    message: str = "",
    undo: dict[str, Any] | None = None,
    error: JobTriageInputInvalid | None = None,
    submitted: dict[str, str] | None = None,
    filters: InboxFilters | None = None,
    group_by: str = "",
) -> dict[str, Any]:
    filters = filters or InboxFilters(view=view)
    selected_options = InboxSelection(filters, group_by)
    shown = _views(request).page(filters, page=page, selected=selected, group_by=group_by)
    organisation = request.app.state.inbox_organisation
    ctx = {
        **shown,
        "area": area("inbox"),
        "areas": AREAS,
        "views": VIEWS,
        "message": message,
        "undo": undo,
        "after_action": after_action,
        "focus": focus,
        "shortcuts_on": shown["shortcuts"].value.enabled,
        "card": None,
        "panel": None,
        "filters": filters,
        "filter_query": selected_options.query(),
        "selection_query": selected_options.query(include_view=True),
        "group_by": group_by,
        "saved_views": organisation.saved_views(),
        "organisation_error": "",
        "organisation_conflict": False,
        "organisation_message": "",
        "view_name": "",
        "error_view_id": "",
        "undo_view": None,
        **organisation.options(),
    }
    if shown["selected"]:
        ctx.update(
            _card_context(
                request,
                shown["selected"],
                focus=focus,
                shortcuts_on=shown["shortcuts"].value.enabled,
                view=view,
                page=shown["page"],
                position=shown["position"],
                total=shown["total"],
                previous_id=shown["previous_id"],
                following_id=shown["following_id"],
                error=error,
                submitted=submitted,
                filter_query=selected_options.query(),
            )
        )
    return ctx


def _inbox_response(request: Request, **kwargs: Any) -> Response:
    ctx = _inbox_context(request, **kwargs)
    response = templates.TemplateResponse(request, INBOX_FRAGMENT, ctx)
    if ctx["message"]:
        response.headers["HX-Trigger-After-Settle"] = json.dumps({"announce": ctx["message"]})
    return response


def _view_message(view: str, total: int) -> str:
    return f"{VIEWS[view]}: {total} {'job' if total == 1 else 'jobs'}."


def _queue_context(request: Request, *, focus: bool) -> dict[str, Any]:
    query = request.query_params
    picked = selection(dict(query))
    return _inbox_context(
        request,
        selected=query.get("selected", ""),
        view=picked.filters.view,
        filters=picked.filters,
        group_by=picked.group_by,
        page=_page_number(query.get("page", "1")),
        focus=focus,
    )


def inbox_page(request: Request) -> Response:
    ctx = _queue_context(request, focus=False)
    return render(request, page="pages/inbox.html", fragment=SCREEN_FRAGMENT, ctx=ctx)


def show_queue(request: Request) -> Response:
    """The ``#inbox`` block alone: a view or page change (``focus=1``: focus moves to the
    card and the view is announced), or a refresh after a job was added."""
    focus = request.query_params.get("focus") == "1"
    ctx = _queue_context(request, focus=focus)
    response = templates.TemplateResponse(request, INBOX_FRAGMENT, ctx)
    if request.query_params.get("push") == "1":
        # The filter form is a plain GET to /inbox that htmx sends to this fragment route; the
        # address bar must hold the page, which a reload, Back or a bookmark can open.
        query = ctx["selection_query"]
        response.headers["HX-Push-Url"] = "/inbox?" + query if query else "/inbox"
    if focus:
        message = _view_message(ctx["view"], ctx["total"])
        if ctx["filter_query"]:
            message += " Filtered" + (
                f", grouped by {ctx['group_by']}." if ctx["group_by"] else "."
            )
        response.headers["HX-Trigger-After-Settle"] = json.dumps({"announce": message})
    return response


def show_card(request: Request, job_id: str) -> Response:
    query = request.query_params
    try:
        ctx = _card_context(
            request,
            job_id,
            focus=query.get("focus") == "1",
            announce=query.get("focus") == "1",
            shortcuts_on=_views(request).shortcuts().value.enabled,
            view=_view(query.get("view", "queue")),
            page=_page_number(query.get("page", "1")),
            position=_page_number(query.get("position", "1")),
            total=_page_number(query.get("total", "1")),
            previous_id=_safe(query.get("previous", "")),
            following_id=_safe(query.get("next", "")),
            filter_query=selection(dict(query)).query(),
        )
    except JobNotFound:
        return _not_found()
    return templates.TemplateResponse(request, CARD_FRAGMENT, ctx)


def _reload_prompt(
    request: Request,
    *,
    detail: str,
    url: str,
    target: str = "",
    message: str = VersionConflict.message,
    link: str = "Reload the page",
) -> Response:
    ctx = {
        "reload_link": link,
        "reload_id": "inbox-reload",
        "reload_message": message,
        "reload_detail": detail,
        "reload_url": url,
    }
    response = templates.TemplateResponse(
        request, "fragments/shell/_reload_prompt.html", ctx, status_code=409
    )
    if target:
        response.headers["HX-Retarget"] = target
        response.headers["HX-Reswap"] = "innerHTML"
    return response


async def mark_seen(request: Request, job_id: str) -> Response:
    form = await single_value_form(request, {"version"})
    if isinstance(form, Response):
        return form
    version = posted_version(form, minimum=1)
    if version is None:
        return PlainTextResponse(INVALID_REQUEST, status_code=400)
    service: JobTriageService = request.app.state.job_triage
    try:
        job = await run_in_threadpool(service.mark_seen, job_id, expected_version=version)
    except JobNotFound:
        return _not_found()
    except (VersionConflict, TriageNotAllowed):
        # The shown card is out of date; its actions say so with the reload prompt. Nothing
        # is swapped here: the card's request is ``hx-swap="none"``.
        return PlainTextResponse(VersionConflict.message, status_code=409)
    return JSONResponse({"version": job.version, "state": job.triage_state})


def _perform(state: Any, job_id: str, form: dict[str, str], version: int) -> Any:
    """Run the posted triage action on the app's services, looked up now rather than at
    start-up. To apply and Undo go through the application service, so the application is made
    or removed with the job's move; every other action is the triage service's."""
    service: JobTriageService = state.job_triage
    action = form["action"]
    if action == "to_apply":
        return state.applications.apply_to_job(job_id, expected_job_version=version).job
    if action == "undo":
        return state.applications.undo_apply(job_id, expected_job_version=version)
    if action == "remove":
        return service.remove(
            job_id,
            expected_version=version,
            reason=form.get("reason") or None,
            text=form.get("text") or None,
        )
    if action == "snooze":
        choice = form.get("choice", "")
        # The typed date is read by the service once the job is found; a quick choice ignores it.
        picked = form.get("picked", "") if choice == "date" else None
        return service.snooze(job_id, expected_version=version, choice=choice, picked=picked)
    return service.restore(job_id, expected_version=version)


def _triage_message(action: str, snooze_until: str | None) -> str:
    if action == "snooze" and snooze_until:
        return f"Snoozed until {format_short_day(date.fromisoformat(snooze_until))}."
    return {
        "remove": "Removed. Undo available.",
        "to_apply": "Marked to apply.",
        "undo": "Back in the queue.",
        "restore": "Restored. The job is back in the queue.",
    }[action]


def _triage_answer(request: Request, job_id: str, form: dict[str, str], version: int) -> Response:
    view = _view(form.get("view", "queue"))
    picked = selection({**dict(parse_qsl(form.get("filters", ""))), "view": view})
    try:
        job = _perform(request.app.state, job_id, form, version)
    except JobNotFound:
        return _not_found()
    except StatusNotAllowed as refused:
        if form["action"] == "undo":  # a worked-on application: the Board is where it goes on
            return _reload_prompt(
                request,
                detail="The job stays in To apply.",
                url="/board",
                target="#inbox-notice",
                message=refused.user_message,
                link="Open the Board",
            )
        return _reload_prompt(
            request,
            detail="Nothing was changed.",
            url="/inbox",
            target="#inbox-notice",
            message=refused.user_message,
        )
    except JobTriageInputInvalid as error:
        # The values typed so far come back (autoescaped; NUL cannot be in HTML).
        typed = {name: form.get(name, "").replace("\x00", "") for name in ("text", "picked")}
        response = _inbox_response(
            request,
            selected=job_id,
            view=view,
            page=_page_number(form.get("page", "1")),
            focus=True,
            message=error.user_message,
            error=error,
            submitted=typed,
            filters=picked.filters,
            group_by=picked.group_by,
        )
        response.status_code = 422
        return response
    except (VersionConflict, TriageNotAllowed):
        return _reload_prompt(
            request, detail="Your last change was not saved.", url="/inbox", target="#inbox-notice"
        )
    action = form["action"]
    page = _page_number(form.get("page", "1"))
    undo = (
        {
            "id": job.id,
            "version": job.version,
            "view": view,
            "page": page,
            "filters": picked.query(),
        }
        if action in ("remove", "to_apply", "snooze")
        else None
    )
    return _inbox_response(
        request,
        selected=job_id if action == "undo" else form.get("next_id", ""),
        view=view,
        page=page,
        focus=True,
        after_action=True,
        message=_triage_message(action, job.snooze_until),
        undo=undo,
        filters=picked.filters,
        group_by=picked.group_by,
    )


async def triage(request: Request, job_id: str) -> Response:
    form = await single_value_form(request, TRIAGE_FIELDS)
    if isinstance(form, Response):
        return form
    version = posted_version(form, minimum=1)
    if version is None or form.get("action") not in ACTIONS:
        return PlainTextResponse(INVALID_REQUEST, status_code=400)
    return await run_in_threadpool(_triage_answer, request, job_id, form, version)


def _shortcuts_answer(request: Request, *, saved: bool = False, conflict: bool = False) -> Response:
    ctx = {
        "shortcuts": _views(request).shortcuts(),
        "shortcuts_saved": saved,
        "shortcuts_conflict": conflict,
        "conflict_message": VersionConflict.message,
    }
    return templates.TemplateResponse(
        request, "fragments/settings/_shortcuts.html", ctx, status_code=409 if conflict else 200
    )


def show_shortcuts(request: Request) -> Response:
    return _shortcuts_answer(request)


async def save_shortcuts(request: Request) -> Response:
    form = await single_value_form(request, {"version", "enabled"})
    if isinstance(form, Response):
        return form
    version = posted_version(form, minimum=0)
    if version is None or form.get("enabled", "") not in ("", "1"):
        return PlainTextResponse(INVALID_REQUEST, status_code=400)
    views = _views(request)
    try:
        await run_in_threadpool(views.save_shortcuts, form.get("enabled") == "1", version)
    except VersionConflict:
        return await run_in_threadpool(_shortcuts_answer, request, conflict=True)
    response = await run_in_threadpool(_shortcuts_answer, request, saved=True)
    response.headers["HX-Trigger"] = json.dumps({"announce": "Saved."})
    return response
