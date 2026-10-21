"""Actions on a due reminder (M5.7): ``POST /reminders/{key}/done``, ``/dismiss`` and ``/snooze``.

Launch token and origin; POST only. The body is a form whose fields each appear once: ``version``
(the reminder's, ``0`` while it was never acted on) and, for snooze, ``choice`` (``tomorrow`` or
``next_week``). An unknown or repeated field, a missing or non-numeric version and a bad choice
are a plain-text 400. A key that is no current reminder (unsafe, unknown, ended by a status
change or a cancelled event) is the fixed 404 and is never echoed. A stale version is a 409
with the reload prompt and changes nothing.

A success answers 200 for htmx, with the ``reminders:changed`` event and an ``announce`` message
in ``HX-Trigger`` (the notifications panel of M5.8 decides what the body shows), and 303 to the
home page otherwise.
"""

import json
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.errors import INVALID_REQUEST
from app.api.forms import is_htmx, posted_version, single_value_form
from app.api.notifications import panel_response
from app.api.templating import templates
from app.domain.errors import ReminderNotFound, VersionConflict, format_short_day
from app.domain.reminders import SNOOZE_CHOICES
from app.services.reminders import ReminderService, ReminderStateView

REMINDERS_CHANGED = "reminders:changed"
RELOAD_PROMPT = "fragments/shell/_reload_prompt.html"
HOME = "/"


def register_reminder_routes(app: FastAPI) -> None:
    app.add_api_route("/reminders/{key}/done", mark_done, methods=["POST"])
    app.add_api_route("/reminders/{key}/dismiss", mark_dismissed, methods=["POST"])
    app.add_api_route("/reminders/{key}/snooze", mark_snoozed, methods=["POST"])


def _service(request: Request) -> ReminderService:
    service: ReminderService = request.app.state.reminders
    return service


async def mark_done(request: Request, key: str) -> Response:
    return await _act(request, key, set(), lambda s, v, _c: s.done(key, expected_version=v), _done)


async def mark_dismissed(request: Request, key: str) -> Response:
    return await _act(
        request, key, set(), lambda s, v, _c: s.dismiss(key, expected_version=v), _dismissed
    )


async def mark_snoozed(request: Request, key: str) -> Response:
    return await _act(
        request,
        key,
        {"choice"},
        lambda s, v, c: s.snooze(key, c, expected_version=v),
        _snoozed,
    )


def _done(_state: ReminderStateView) -> str:
    return "Reminder marked done."


def _dismissed(_state: ReminderStateView) -> str:
    return "Reminder dismissed."


def _snoozed(state: ReminderStateView) -> str:
    if state.snooze_until is None:
        return "Reminder snoozed."
    return f"Reminder snoozed until {format_short_day(state.snooze_until)}."


async def _act(
    request: Request,
    key: str,
    extra: set[str],
    change: Callable[[ReminderService, int, str], ReminderStateView],
    said: Callable[[ReminderStateView], str],
) -> Response:
    form = await single_value_form(request, {"version", *extra})
    if isinstance(form, Response):
        return form
    version = posted_version(form, minimum=0)
    choice = form.get("choice", "")
    if version is None or ("choice" in extra and choice not in SNOOZE_CHOICES):
        return PlainTextResponse(INVALID_REQUEST, status_code=400)
    service = _service(request)

    def run() -> Response:
        try:
            state = change(service, version, choice)
        except ReminderNotFound:
            return PlainTextResponse(ReminderNotFound.message, status_code=404)
        except VersionConflict:
            return _conflict(request)
        if not is_htmx(request):
            return RedirectResponse(HOME, status_code=303)
        response = panel_response(request, focus=True)
        response.headers["HX-Trigger"] = json.dumps(
            {REMINDERS_CHANGED: True, "announce": said(state)}
        )
        return response

    return await run_in_threadpool(run)


def _conflict(request: Request) -> Response:
    ctx = {
        "reload_id": "reminder-conflict",
        "reload_message": VersionConflict.message,
        "reload_detail": "",
        "reload_url": HOME,
        "reload_link": "Reload",
    }
    response = templates.TemplateResponse(request, RELOAD_PROMPT, ctx, status_code=409)
    if request.headers.get("HX-Target") == "notifications-panel":
        response.headers["HX-Retarget"] = "#notifications-notice"
        response.headers["HX-Reswap"] = "innerHTML"
    return response
