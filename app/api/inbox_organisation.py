"""Protected saved-view decisions and the read-only values-to-map page."""

import json
from typing import Any
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.forms import posted_version, single_value_form
from app.api.pages import AREAS, area
from app.api.security import PAGE_TAG
from app.api.templating import render, templates
from app.domain.errors import VersionConflict
from app.services.inbox_organisation import InboxOrganisation


def register_inbox_organisation_routes(app: FastAPI) -> None:
    app.add_api_route(
        "/inbox/values",
        values_page,
        methods=["GET", "HEAD"],
        tags=[PAGE_TAG],
        response_class=HTMLResponse,
        name="page_inbox_values",
    )
    app.add_api_route("/inbox/values/{value_id}", decide_value, methods=["POST"])
    app.add_api_route("/inbox/saved-views", save_view, methods=["POST"])
    app.add_api_route("/inbox/saved-views/{view_id}", change_view, methods=["POST"])


def _service(request: Request) -> InboxOrganisation:
    service: InboxOrganisation = request.app.state.inbox_organisation
    return service


def _saved(
    request: Request,
    *,
    query: str = "",
    error: str = "",
    conflict: bool = False,
    message: str = "",
    name: str = "",
    error_view_id: str = "",
    undo: dict[str, str] | None = None,
    status: int = 200,
) -> Response:
    ctx = {
        "error_view_id": error_view_id,
        "undo_view": undo,
        "saved_views": _service(request).saved_views(),
        "selection_query": query,
        "organisation_error": error,
        "organisation_conflict": conflict,
        "organisation_message": message,
        "view_name": name,
    }
    response = templates.TemplateResponse(
        request, "fragments/inbox/_saved_views.html", ctx, status_code=status
    )
    if message:
        response.headers["HX-Trigger"] = json.dumps({"announce": message})
    return response


async def save_view(request: Request) -> Response:
    form = await single_value_form(request, {"name", "filters"})
    if isinstance(form, Response):
        return form

    def save() -> Response:
        try:
            _service(request).create_view(
                form.get("name", ""), dict(parse_qsl(form.get("filters", "")))
            )
        except ValueError as error:
            return _saved(
                request,
                query=form.get("filters", ""),
                name=form.get("name", "").replace("\x00", ""),
                error=str(error),
                status=422,
            )
        return _saved(request, query=form.get("filters", ""), message="View saved.")

    return await run_in_threadpool(save)


async def change_view(request: Request, view_id: str) -> Response:
    form = await single_value_form(request, {"name", "action", "version", "filters"})
    if isinstance(form, Response):
        return form
    version = posted_version(form, minimum=1)
    if version is None or form.get("action") not in ("rename", "delete"):
        return PlainTextResponse("Invalid request.", status_code=400)

    def change() -> Response:
        query = form.get("filters", "")
        shown = next((v for v in _service(request).saved_views() if v["id"] == view_id), None)
        try:
            if form["action"] == "delete":
                _service(request).delete_view(view_id, expected_version=version)
            else:
                _service(request).rename_view(
                    view_id, form.get("name", ""), expected_version=version
                )
        except (LookupError, VersionConflict):
            # Changed or deleted elsewhere: a reload prompt, which htmx swaps (a 404 it would not).
            return _saved(request, query=query, conflict=True, status=409)
        except ValueError as error:
            return _saved(
                request,
                query=query,
                error=str(error),
                name=form.get("name", "").replace("\x00", ""),
                error_view_id=view_id,
                status=422,
            )
        if form["action"] == "delete":
            undo = {"name": shown["name"], "filters": shown["query"]} if shown else None
            return _saved(request, query=query, message="View deleted.", undo=undo)
        return _saved(request, query=query, message="View renamed.")

    return await run_in_threadpool(change)


def _values_context(request: Request, **extra: Any) -> dict[str, Any]:
    return {
        **_service(request).values(),
        "area": area("inbox"),
        "areas": AREAS,
        "value_error": "",
        "value_conflict": False,
        "value_message": "",
        "error_id": "",
        **extra,
    }


def values_page(request: Request) -> Response:
    return render(
        request,
        page="pages/inbox_values.html",
        fragment="fragments/inbox/_values.html",
        ctx=_values_context(request),
    )


async def decide_value(request: Request, value_id: str) -> Response:
    form = await single_value_form(request, {"action", "version", "target"})
    if isinstance(form, Response):
        return form
    version = posted_version(form, minimum=1)
    if version is None or form.get("action") not in ("map", "new", "ignore"):
        return PlainTextResponse("Invalid request.", status_code=400)

    def decide() -> Response:
        ctx: dict[str, Any] = {}
        status = 200
        try:
            _service(request).decide(
                value_id, form["action"], expected_version=version, target=form.get("target", "")
            )
            ctx["value_message"] = {
                "map": "Mapped.",
                "new": "Added as a new value.",
                "ignore": "Ignored. The text is kept as written.",
            }[form["action"]]
        except LookupError:
            return PlainTextResponse("This value wasn't found.", status_code=404)
        except VersionConflict:
            ctx["value_conflict"], status = True, 409
        except ValueError as error:
            ctx.update(value_error=str(error), error_id=value_id)
            status = 422
        response = templates.TemplateResponse(
            request,
            "fragments/inbox/_values.html",
            _values_context(request, **ctx),
            status_code=status,
        )
        if ctx.get("value_message"):
            response.headers["HX-Trigger"] = json.dumps({"announce": ctx["value_message"]})
        return response

    return await run_in_threadpool(decide)
