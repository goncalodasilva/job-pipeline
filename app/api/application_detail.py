"""Token-free application pages, protected detail and versioned editor routes."""

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.errors import INVALID_REQUEST
from app.api.forms import posted_version, single_value_form
from app.api.pages import AREAS, area
from app.api.security import PAGE_TAG
from app.api.templating import render, templates
from app.domain.application_detail import EVENT_FIELDS, DetailInvalid
from app.domain.errors import ApplicationNotFound, VersionConflict
from app.services.application_detail import ApplicationDetail

DETAIL = "fragments/applications/_detail.html"


def register_application_detail_routes(app: FastAPI) -> None:
    app.add_api_route(
        "/applications/{application_id}",
        page,
        methods=["GET", "HEAD"],
        tags=[PAGE_TAG],
        response_class=HTMLResponse,
        name="page_application",
    )
    app.add_api_route("/applications/{application_id}/detail", drawer, methods=["GET"])
    app.add_api_route("/applications/{application_id}/notes", save_note, methods=["POST"])
    app.add_api_route("/applications/{application_id}/notes/{note_id}", save_note, methods=["POST"])
    app.add_api_route("/applications/{application_id}/events", save_event, methods=["POST"])
    app.add_api_route(
        "/applications/{application_id}/events/{event_id}", save_event, methods=["POST"]
    )
    app.add_api_route(
        "/applications/{application_id}/events/{event_id}/cancel", cancel_event, methods=["POST"]
    )
    app.add_api_route(
        "/applications/{application_id}/events/{event_id}/ics", download, methods=["GET"]
    )


def _service(request: Request) -> ApplicationDetail:
    service: ApplicationDetail = request.app.state.application_detail
    return service


def context(request: Request, aid: str) -> dict[str, Any]:
    return {
        **_service(request).view(aid, getattr(request.app.state, "system_time_zone", None)),
        "areas": AREAS,
        "area": area("board"),
    }


def _not_found() -> Response:
    return PlainTextResponse(ApplicationNotFound.message, status_code=404)


def page(request: Request, application_id: str) -> Response:
    try:
        return render(
            request,
            page="pages/application_detail.html",
            fragment=DETAIL,
            ctx=context(request, application_id),
        )
    except ApplicationNotFound:
        return _not_found()


def drawer(request: Request, application_id: str) -> Response:
    try:
        return templates.TemplateResponse(
            request, "fragments/applications/_drawer.html", context(request, application_id)
        )
    except ApplicationNotFound:
        return _not_found()


def answer(request: Request, aid: str, *, message: str = "", follow_up: Any = None) -> Response:
    ctx = context(request, aid)
    ctx.update(message=message, follow_up=follow_up)
    response = templates.TemplateResponse(request, DETAIL, ctx)
    response.headers["HX-Trigger-After-Settle"] = json.dumps(
        {"announce": message, "board:changed": {}, "application:saved": {"id": aid}}
    )
    return response


def _refused(
    request: Request,
    aid: str,
    form: dict[str, str],
    *,
    kind: str,
    item_id: str = "",
    errors: dict[str, str] | None = None,
    conflict: bool = False,
) -> Response:
    # Replace only the submitting form, preserving other unsaved editors and their versions.
    ctx = context(request, aid)
    item: dict[str, Any] = next(
        (item for item in ctx["events" if kind == "event" else "notes"] if item["id"] == item_id),
        {},
    )
    ctx.update(
        submitted={k: v.replace("\x00", "") for k, v in form.items()},
        errors=errors or {},
        conflict=conflict,
        editor=item,
        editor_id=item_id,
    )
    if kind == "event":
        for checkbox in ("all_day", "thank_you_reminder"):
            ctx["submitted"].setdefault(checkbox, "")
    template = f"fragments/applications/_{kind}_form.html"
    response = templates.TemplateResponse(
        request, template, ctx, status_code=409 if conflict else 422
    )
    response.headers["HX-Retarget"] = f"#{kind}-form" + (f"-{item_id}" if item_id else "")
    response.headers["HX-Reswap"] = "outerHTML"
    return response


async def save_note(request: Request, application_id: str, note_id: str | None = None) -> Response:
    form = await single_value_form(request, {"text", "version"} if note_id else {"text"})
    if isinstance(form, Response):
        return form
    version = posted_version(form, minimum=1) if note_id else None
    if "text" not in form or (note_id and version is None):
        return PlainTextResponse(INVALID_REQUEST, status_code=400)

    def save() -> Response:
        try:
            _service(request).note(application_id, form["text"], note_id=note_id, version=version)
            return answer(request, application_id, message="Note saved.")
        except ApplicationNotFound:
            return _not_found()
        except DetailInvalid as error:
            return _refused(
                request,
                application_id,
                form,
                kind="note",
                item_id=note_id or "",
                errors=error.errors,
            )
        except VersionConflict:
            return _refused(
                request, application_id, form, kind="note", item_id=note_id or "", conflict=True
            )

    return await run_in_threadpool(save)


async def save_event(
    request: Request, application_id: str, event_id: str | None = None
) -> Response:
    form = await single_value_form(
        request, set(EVENT_FIELDS) | ({"version"} if event_id else set())
    )
    if isinstance(form, Response):
        return form
    version = posted_version(form, minimum=1) if event_id else None
    if event_id and version is None:
        return PlainTextResponse(INVALID_REQUEST, status_code=400)

    def save() -> Response:
        try:
            notice = _service(request).save_event(
                application_id, form, event_id=event_id, version=version
            )
            return answer(
                request, application_id, message="Event saved." + (" " + notice if notice else "")
            )
        except ApplicationNotFound:
            return _not_found()
        except DetailInvalid as error:
            return _refused(
                request,
                application_id,
                form,
                kind="event",
                item_id=event_id or "",
                errors=error.errors,
            )
        except VersionConflict:
            return _refused(
                request, application_id, form, kind="event", item_id=event_id or "", conflict=True
            )

    return await run_in_threadpool(save)


async def cancel_event(request: Request, application_id: str, event_id: str) -> Response:
    form = await single_value_form(request, {"version"})
    if isinstance(form, Response):
        return form
    version = posted_version(form, minimum=1)
    if version is None:
        return PlainTextResponse(INVALID_REQUEST, status_code=400)

    def cancel() -> Response:
        try:
            _service(request).cancel(application_id, event_id, version)
            return answer(request, application_id, message="Event cancelled.")
        except ApplicationNotFound:
            return _not_found()
        except VersionConflict:
            response = templates.TemplateResponse(
                request,
                "fragments/shell/_reload_prompt.html",
                {
                    "reload_id": "application-reload",
                    "reload_message": VersionConflict.message,
                    "reload_detail": "The event was not cancelled.",
                    "reload_url": f"/applications/{application_id}",
                },
                status_code=409,
            )
            response.headers["HX-Retarget"] = "#application-notice"
            response.headers["HX-Reswap"] = "innerHTML"
            return response

    return await run_in_threadpool(cancel)


def download(request: Request, application_id: str, event_id: str) -> Response:
    try:
        exported = _service(request).export(application_id, event_id)
    except ApplicationNotFound:
        return _not_found()
    return Response(
        exported.content,
        media_type="text/calendar",
        headers={
            "Content-Disposition": 'attachment; filename="jobpipeline-event.ics"',
            "Cache-Control": "no-store",
        },
    )
