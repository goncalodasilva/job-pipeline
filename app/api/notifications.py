"""Read-only notification fragments and an explicit POST marking shown keys seen."""

import json

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.errors import INVALID_REQUEST
from app.api.forms import single_value_form
from app.api.templating import templates
from app.services.reminders import ReminderService
from app.services.task_views import TaskViews


def register_notification_routes(app: FastAPI) -> None:
    app.add_api_route("/notifications/panel", panel, methods=["GET"])
    app.add_api_route("/notifications/count", count, methods=["GET"])
    app.add_api_route("/notifications/seen", seen, methods=["POST"])


def panel_response(request: Request, *, focus: bool = False) -> Response:
    reminders: ReminderService = request.app.state.reminders
    views: TaskViews | None = getattr(request.app.state, "task_views", None)
    attention = len(views.top_bar().attention) if views else 0
    return templates.TemplateResponse(
        request,
        "fragments/notifications/_panel.html",
        {
            "notifications": reminders.notifications(),
            "attention": attention,
            "focus": focus,
        },
    )


def panel(request: Request) -> Response:
    return panel_response(request)


def count(request: Request) -> Response:
    reminders: ReminderService = request.app.state.reminders
    return templates.TemplateResponse(
        request,
        "fragments/notifications/_count.html",
        {
            "notifications": reminders.notifications(),
            "oob": True,
        },
    )


async def seen(request: Request) -> Response:
    form = await single_value_form(request, {"keys"})
    if isinstance(form, Response):
        return form
    try:
        keys = json.loads(form.get("keys", ""))
        if (
            not isinstance(keys, list)
            or len(keys) > 10000
            or any(not isinstance(k, str) for k in keys)
        ):
            raise ValueError
    except (ValueError, TypeError):
        return PlainTextResponse(INVALID_REQUEST, status_code=400)

    reminders: ReminderService = request.app.state.reminders
    try:
        await run_in_threadpool(reminders.seen, tuple(keys))
    except ValueError:
        return PlainTextResponse(INVALID_REQUEST, status_code=400)
    return Response(status_code=204, headers={"HX-Trigger": "reminders:changed"})
