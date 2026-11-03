"""Settings > Appearance (M9.2 part B; ADR-131 item 6; ADR-042 follow-up; ADR-125 item 5).

``POST /settings/appearance`` (launch token and origin) saves the versioned ``appearance``
setting: ``version`` (``0`` while nothing is stored), ``theme`` and ``density``, each given once
and nothing else (anything else, or a bad ``version``, is a plain-text 400). The version is
checked first: a stale one is a 409 with the reload prompt and changes nothing, whatever the
values are. A value outside the lists is then a 422 with the section (the whole page when plain),
each refused select tied to its fixed message and never shown back. A success answers 200 for
htmx with the section, a line saying so, the heading as the one focus target, an ``announce``
message and the ``appearance:changed`` event, which ``app.js`` applies to ``<html>`` at once (303
to ``/settings/appearance`` otherwise).
"""

import json

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.appearance_forms import (
    AREA_PATH,
    CHANGED_EVENT,
    NOT_SAVED,
    SAVE_PATH,
    SAVED,
    AppearanceState,
)
from app.api.errors import INVALID_REQUEST
from app.api.forms import is_htmx, posted_version, single_value_form
from app.api.settings_areas import render_appearance_settings
from app.domain.errors import VersionConflict
from app.services.appearance import CHOICES, AppearancePreferences

FIELDS = {"version", *CHOICES}


def register_appearance_routes(app: FastAPI) -> None:
    """Add the route directly to the app; it is not a page, so it needs the token."""
    app.add_api_route(SAVE_PATH, save_appearance, methods=["POST"])


def _answer(
    request: Request,
    state: AppearanceState,
    *,
    status_code: int = 200,
    triggers: dict[str, object] | None = None,
) -> Response:
    """The section for htmx, the full page otherwise; a plain success goes back to the page."""
    if status_code == 200 and not is_htmx(request):
        return RedirectResponse(AREA_PATH, status_code=303)
    response = render_appearance_settings(request, state)
    response.status_code = status_code
    if triggers and is_htmx(request):
        response.headers["HX-Trigger"] = json.dumps(triggers)
    return response


def _fields_to_change(count: int) -> str:
    return "1 field needs a change." if count == 1 else f"{count} fields need a change."


async def save_appearance(request: Request) -> Response:
    form = await single_value_form(request, FIELDS)
    if isinstance(form, Response):
        return form
    version = posted_version(form, minimum=0)
    if version is None or not set(CHOICES) <= set(form):
        return PlainTextResponse(INVALID_REQUEST, status_code=400)
    preferences: AppearancePreferences = request.app.state.appearance

    def run() -> Response:
        try:
            result = preferences.save(form, expected_version=version)
        except VersionConflict:
            return _answer(request, AppearanceState(conflict=True), status_code=409)
        if result.saved is None:
            errors = result.errors or {}
            state = AppearanceState(
                errors=errors, typed={n: form[n] for n in CHOICES if n not in errors}
            )
            said = f"{NOT_SAVED} {_fields_to_change(len(errors))}"
            return _answer(request, state, status_code=422, triggers={"announce": said})
        saved = result.saved.value
        return _answer(
            request,
            AppearanceState(saved=True),
            triggers={
                "announce": SAVED,
                CHANGED_EVENT: {"theme": saved.theme, "density": saved.density},
            },
        )

    return await run_in_threadpool(run)
