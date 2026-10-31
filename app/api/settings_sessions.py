"""Settings > Sessions (M7.8; AC-M7.6; ADR-117 item 8, ADR-119).

``POST /settings/sessions`` (launch token and origin) saves the versioned ``sessions`` setting:
``version`` (``0`` while nothing is stored), ``frequency``, ``length`` and ``ask_later_default``,
all required, and ``paused`` (``1`` when on, absent when off). A field appears once; an unknown
or repeated field, a missing required field, a bad ``version`` and a ``paused`` other than ``1``
are a plain-text 400. A success answers 200 for htmx with the whole section, a line saying so,
one focus target (the heading) and an ``announce`` message (303 to ``/settings`` otherwise). A
stale version is a 409 with the reload prompt and changes nothing, whatever the values are (it
is checked first); a refused value is then a 422 with the section (the full page when plain),
each refused field tied to its fixed message.
"""

import json

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.errors import INVALID_REQUEST
from app.api.forms import is_htmx, posted_version, single_value_form
from app.api.session_settings_forms import (
    NOT_SAVED,
    SAVE_PATH,
    SAVED,
    SETTINGS_PATH,
    SessionSettingsState,
)
from app.api.settings import render_session_settings
from app.domain.errors import VersionConflict
from app.services.session_preferences import CHOICES, SessionPreferences

FIELDS = {"version", *CHOICES, "paused"}


def register_session_settings_routes(app: FastAPI) -> None:
    """Add the route directly to the app; it is not a page, so it needs the token."""
    app.add_api_route(SAVE_PATH, save_sessions, methods=["POST"])


def _answer(
    request: Request, state: SessionSettingsState, *, status_code: int = 200, announce: str = ""
) -> Response:
    """The section for htmx, the full page otherwise; a plain success goes back to the page."""
    if status_code == 200 and not is_htmx(request):
        return RedirectResponse(SETTINGS_PATH, status_code=303)
    response = render_session_settings(request, state)
    response.status_code = status_code
    if announce and is_htmx(request):
        response.headers["HX-Trigger"] = json.dumps({"announce": announce})
    return response


def _fields_to_change(count: int) -> str:
    return "1 field needs a change." if count == 1 else f"{count} fields need a change."


async def save_sessions(request: Request) -> Response:
    form = await single_value_form(request, FIELDS)
    if isinstance(form, Response):
        return form
    version = posted_version(form, minimum=0)
    if version is None or not set(CHOICES) <= set(form) or form.get("paused", "1") != "1":
        return PlainTextResponse(INVALID_REQUEST, status_code=400)
    paused = "paused" in form
    preferences: SessionPreferences = request.app.state.session_preferences

    def run() -> Response:
        try:
            result = preferences.save(form, paused=paused, expected_version=version)
        except VersionConflict:
            return _answer(request, SessionSettingsState(conflict=True), status_code=409)
        if result.errors:
            state = SessionSettingsState(
                errors=result.errors,
                typed={n: form[n] for n in CHOICES if n not in result.errors},
                paused=paused,
            )
            said = f"{NOT_SAVED} {_fields_to_change(len(result.errors))}"
            return _answer(request, state, status_code=422, announce=said)
        return _answer(request, SessionSettingsState(saved=True), announce=SAVED)

    return await run_in_threadpool(run)
