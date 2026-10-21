"""The Board screen (M5.4): the columns with their status menu, the sortable table and the two
follow-up questions asked after some moves.

``GET /board`` is a page route (``PAGE_TAG``): it loads without the launch token and, with
``HX-Request: true`` (token needed), answers the ``#board`` block with ``HX-Push-Url`` set to the
page address for the validated values. Query: ``view`` (``board`` or ``table``), ``sort``
(``company``, ``role``, ``status``, ``applied`` or ``activity``), ``dir`` (``asc`` or ``desc``),
``page`` (50 rows a page) and ``closed`` (``1`` shows the Closed cards). A bad value is dropped
for its default. ``focus`` (``view``, ``sort``, ``page`` or ``closed``) asks for focus on the
control that was used and an announcement. ``GET /board/columns`` (token) answers the
``#board-content`` block for the same values.

``POST /applications/{id}/status`` (token and origin) is the only move endpoint, for the menu
and for a drop: exactly ``version`` and ``status`` and optionally ``source`` (``menu`` or
``board``). A stale version answers 409 with the reload prompt in ``#board-notice``. The answers
(and those of the two follow-ups) are the ``#board-content`` block; the Closed column is shown
open when the page address (``HX-Current-URL``) says so. ``POST /applications/{id}/interview-date``
and ``/feedback`` save the follow-up answers; a refused one answers 422 with the error tied to
its field. An unknown or unsafe id is always the fixed plain-text 404, never echoed.
"""

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.errors import INVALID_REQUEST
from app.api.forms import posted_version, single_value_form
from app.api.pages import AREAS, area
from app.api.security import PAGE_TAG
from app.api.templating import render, renders_fragment, templates
from app.domain.board import (
    NATURAL,
    SORT_LABELS,
    address,
    other_direction,
    page_number,
    sort_choice,
    sort_message,
    view_choice,
)
from app.domain.errors import ApplicationNotFound, StatusNotAllowed, VersionConflict
from app.domain.ids import is_safe_id
from app.services.board_views import BoardViews, FollowUpInvalid

BOARD_FRAGMENT = "fragments/board/_board.html"
CONTENT_FRAGMENT = "fragments/board/_content.html"
INTERVIEW_FORM = "fragments/board/_interview_form.html"
FEEDBACK_FORM = "fragments/board/_feedback_form.html"
SOURCES = ("menu", "board")
INTERVIEW_FIELDS = {"date", "time", "time_zone", "duration_minutes"}
FOCUS = ("view", "sort", "page", "closed")


def register_board_routes(app: FastAPI) -> None:
    app.add_api_route(
        "/board",
        board_page,
        methods=["GET", "HEAD"],
        tags=[PAGE_TAG],
        response_class=HTMLResponse,
        name="page_board",
    )
    app.add_api_route("/board/columns", show_columns, methods=["GET"])
    app.add_api_route("/applications/{application_id}/status", move_application, methods=["POST"])
    app.add_api_route(
        "/applications/{application_id}/interview-date", save_interview_date, methods=["POST"]
    )
    app.add_api_route("/applications/{application_id}/feedback", save_feedback, methods=["POST"])


def _views(request: Request) -> BoardViews:
    views: BoardViews = request.app.state.board_views
    return views


def _not_found() -> Response:
    return PlainTextResponse(ApplicationNotFound.message, status_code=404)


def _invalid() -> Response:
    return PlainTextResponse(INVALID_REQUEST, status_code=400)


def _with_query(path: str, **values: str) -> str:
    joined = "&".join(f"{name}={value}" for name, value in values.items())
    return f"{path}{'&' if '?' in path else '?'}{joined}"


def _context(
    request: Request,
    *,
    view: str,
    sort: str,
    direction: str,
    page: int,
    closed: bool,
    focus: str = "",
    focus_id: str = "",
    follow_up: Any = None,
) -> dict[str, Any]:
    """Everything the ``#board`` block and the ``#board-content`` block draw, for validated
    values. The table's page is the one that exists, never the one asked for."""
    views = _views(request)
    board = table = None
    if view == "table":
        table = views.table(sort, direction, page)
        page = table.page
        empty = table.empty
    else:
        board = views.board(
            closed=closed,
            focus_id=focus_id,
            focus_closed=focus == "closed",
            follow_up=follow_up,
        )
        empty = board.empty
    here = address(view, sort, direction, page, closed)
    pages = table.pages if table else 1
    return {
        "area": area("board"),
        "areas": AREAS,
        "view": view,
        "empty": empty,
        "board": board,
        "table": table,
        "focus": focus,
        "address": here,
        "sort": sort,
        "dir": direction,
        "page": page,
        "pages": pages,
        "sort_text": sort_message(sort, direction),
        "view_links": _view_links(view, sort, direction),
        "columns": _sort_columns(sort, direction),
        "previous_href": address("table", sort, direction, page - 1) if page > 1 else "",
        "next_href": address("table", sort, direction, page + 1) if page < pages else "",
        "closed_open": address("board", closed=True),
        "closed_get": "/board/columns?closed=1&focus=closed",
        "closed_shut_get": "/board/columns?focus=closed",
    }


def _view_links(view: str, sort: str, direction: str) -> tuple[dict[str, Any], ...]:
    links = []
    for key, label in (("board", "Board"), ("table", "Table")):
        href = address(key, sort, direction)
        get = _with_query(href, focus="view")
        links.append({"label": label, "href": href, "request": get, "current": key == view})
    return tuple(links)


def _sort_columns(sort: str, direction: str) -> tuple[dict[str, Any], ...]:
    """The table's header cells: each sorts by its column, the sorted one the other way."""
    columns = []
    for key, label in SORT_LABELS.items():
        href = address("table", key, other_direction(direction) if key == sort else NATURAL[key])
        aria_sort = "none"
        if key == sort:
            aria_sort = "ascending" if direction == "asc" else "descending"
        columns.append(
            {
                "key": key,
                "label": label,
                "active": key == sort,
                "aria_sort": aria_sort,
                "href": href,
                "request": _with_query(href, focus="sort"),
            }
        )
    return tuple(columns)


def _query_context(request: Request) -> dict[str, Any]:
    query = request.query_params
    sort, direction = sort_choice(query.get("sort", ""), query.get("dir", ""))
    focus = query.get("focus", "")
    return _context(
        request,
        view=view_choice(query.get("view", "")),
        sort=sort,
        direction=direction,
        page=page_number(query.get("page", "")),
        closed=query.get("closed") == "1",
        focus=focus if focus in FOCUS else "",
    )


def _said(ctx: dict[str, Any]) -> str:
    """The words for what a focus request just did."""
    focus = ctx["focus"]
    if focus == "view":
        return "Table view." if ctx["view"] == "table" else "Board view."
    if focus == "sort":
        return str(ctx["sort_text"])
    if focus == "page":
        return f"Page {ctx['page']} of {ctx['pages']}."
    if focus == "closed" and ctx["board"] is not None:
        return (
            "Closed applications shown."
            if ctx["board"].closed.expanded
            else ("Closed applications hidden.")
        )
    return ""


def _announce(response: Response, message: str) -> Response:
    if message:
        response.headers["HX-Trigger-After-Settle"] = json.dumps({"announce": message})
    return response


def board_page(request: Request) -> Response:
    ctx = _query_context(request)
    response = render(request, page="pages/board.html", fragment=BOARD_FRAGMENT, ctx=ctx)
    if renders_fragment(request):
        # The address bar holds the page (never a fragment route), with validated values.
        response.headers["HX-Push-Url"] = ctx["address"]
        _announce(response, _said(ctx))
    return response


def show_columns(request: Request) -> Response:
    ctx = _query_context(request)
    return _announce(templates.TemplateResponse(request, CONTENT_FRAGMENT, ctx), _said(ctx))


def _closed_in_address(request: Request) -> bool:
    """Whether the page the request came from shows the Closed cards (``HX-Current-URL``)."""
    try:
        parts = urlsplit(request.headers.get("HX-Current-URL", ""))
    except ValueError:
        return False
    return parts.path == "/board" and parse_qs(parts.query).get("closed", [""])[0] == "1"


def _content(
    request: Request,
    *,
    focus_id: str,
    follow_up: Any = None,
    status_code: int = 200,
) -> Response:
    """The board after a move or a follow-up: the card keeps focus (or, when it is in the
    collapsed Closed column, the Closed button)."""
    ctx = _context(
        request,
        view="board",
        sort="activity",
        direction=NATURAL["activity"],
        page=1,
        closed=_closed_in_address(request),
        focus_id=focus_id,
        follow_up=follow_up,
    )
    return templates.TemplateResponse(request, CONTENT_FRAGMENT, ctx, status_code=status_code)


def _reload_prompt(request: Request) -> Response:
    ctx = {
        "reload_id": "board-reload",
        "reload_message": VersionConflict.message,
        "reload_detail": "The card was not moved.",
        "reload_url": "/board",
    }
    response = templates.TemplateResponse(
        request, "fragments/shell/_reload_prompt.html", ctx, status_code=409
    )
    # The rest of the board stays as it is; only the notice above it changes.
    response.headers["HX-Retarget"] = "#board-notice"
    response.headers["HX-Reswap"] = "innerHTML"
    return response


def _system_zone(request: Request) -> str | None:
    zone = getattr(request.app.state, "system_time_zone", None)
    return zone if isinstance(zone, str) else None


async def move_application(request: Request, application_id: str) -> Response:
    form = await single_value_form(request, {"version", "status", "source"})
    if isinstance(form, Response):
        return form
    version = posted_version(form, minimum=1)
    status = form.get("status", "")
    if version is None or not is_safe_id(status) or form.get("source", "menu") not in SOURCES:
        return _invalid()
    return await run_in_threadpool(
        _move_answer, request, application_id, version, status, form.get("source", "menu")
    )


def _move_answer(
    request: Request, application_id: str, version: int, status: str, source: str
) -> Response:
    views = _views(request)
    try:
        outcome = views.move(application_id, status, version=version, source=source)
    except ApplicationNotFound:
        return _not_found()
    except StatusNotAllowed:
        return _invalid()  # a status that doesn't exist: nothing was written
    except VersionConflict:
        return _reload_prompt(request)
    follow_up = (
        views.follow_up(
            outcome.follow_up,
            application_id,
            time_zone=_system_zone(request),
            moved_to=outcome.label,
        )
        if outcome.follow_up
        else None
    )
    response = _content(request, focus_id=application_id, follow_up=follow_up)
    return _announce(response, f"Moved to {outcome.label}.")


def _refused(
    request: Request,
    template: str,
    application_id: str,
    kind: str,
    form: dict[str, str],
    error: FollowUpInvalid,
) -> Response:
    """The follow-up form again, answering 422: errors tied to their fields, what the user
    typed kept (autoescaped; NUL can't be in HTML), and only the form replaced."""
    typed = {name: value.replace("\x00", "") for name, value in form.items()}
    follow_up = _views(request).follow_up(
        kind,
        application_id,
        time_zone=_system_zone(request),
        submitted=typed,
        errors=error.errors,
    )
    response = templates.TemplateResponse(
        request, template, {"follow_up": follow_up}, status_code=422
    )
    response.headers["HX-Retarget"] = (
        "#interview-form" if kind == "interview_date" else "#feedback-form"
    )
    response.headers["HX-Reswap"] = "outerHTML"
    return response


async def save_interview_date(request: Request, application_id: str) -> Response:
    form = await single_value_form(request, INTERVIEW_FIELDS)
    if isinstance(form, Response):
        return form
    if set(form) != INTERVIEW_FIELDS:
        return _invalid()
    return await run_in_threadpool(_interview_answer, request, application_id, form)


def _interview_answer(request: Request, application_id: str, form: dict[str, str]) -> Response:
    try:
        _views(request).add_interview_date(application_id, form)
    except ApplicationNotFound:
        return _not_found()
    except FollowUpInvalid as error:
        return _refused(request, INTERVIEW_FORM, application_id, "interview_date", form, error)
    return _announce(_content(request, focus_id=application_id), "Interview date saved.")


async def save_feedback(request: Request, application_id: str) -> Response:
    form = await single_value_form(request, {"text"})
    if isinstance(form, Response):
        return form
    if set(form) != {"text"}:
        return _invalid()
    return await run_in_threadpool(_feedback_answer, request, application_id, form)


def _feedback_answer(request: Request, application_id: str, form: dict[str, str]) -> Response:
    try:
        _views(request).add_feedback(application_id, form["text"])
    except ApplicationNotFound:
        return _not_found()
    except FollowUpInvalid as error:
        return _refused(request, FEEDBACK_FORM, application_id, "feedback", form, error)
    return _announce(_content(request, focus_id=application_id), "Feedback saved.")
