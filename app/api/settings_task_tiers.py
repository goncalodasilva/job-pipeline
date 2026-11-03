"""Settings > Providers and AI > Tier per task (M9.2 part C; ADR-089 item 9; ADR-125 item 5).

``POST /settings/tiers/tasks`` (launch token and origin) saves the per-task tiers into the
``task_tiers`` of the versioned ``ai_provider`` setting: ``version`` and one ``tier_<task>`` field
per task, each given once. A repeated or foreign field, a missing or bad ``version`` is a
plain-text 400. The version is checked first: a stale one is a 409 with the reload prompt and
changes nothing, whatever the values are. A task that is not listed or a tier that is not Fast or
Thorough is then a 422 with the Providers section (the whole page when plain), the refused tier
tied to its message and never shown back, and nothing is saved. A success answers 200 for htmx
with the Providers section, a line saying so, the list's summary as the one focus target and an
``announce`` message (303 to ``/settings`` otherwise). The model map has its own route
(``POST /settings/tiers``), which this one does not touch.
"""

import json

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.errors import INVALID_REQUEST
from app.api.forms import is_htmx, posted_version, read_form
from app.api.settings import render_task_tiers
from app.api.task_tier_forms import (
    AREA_PATH,
    FIELD_PREFIX,
    NOT_SAVED,
    SAVE_PATH,
    SAVED,
    TaskTierState,
)
from app.domain.errors import VersionConflict
from app.onboarding.steps.provider import ProviderStep


def register_task_tier_routes(app: FastAPI) -> None:
    """Add the route directly to the app; it is not a page, so it needs the token."""
    app.add_api_route(SAVE_PATH, save_task_tiers, methods=["POST"], name="settings_task_tiers")


def _answer(
    request: Request, state: TaskTierState, *, status_code: int = 200, announce: str = ""
) -> Response:
    """The Providers section for htmx, the whole page otherwise; a plain success goes back."""
    if status_code == 200 and not is_htmx(request):
        return RedirectResponse(AREA_PATH, status_code=303)
    response = render_task_tiers(request, state, status_code=status_code)
    if announce and is_htmx(request):
        response.headers["HX-Trigger"] = json.dumps({"announce": announce})
    return response


def _fields_to_change(count: int) -> str:
    return "1 task needs a change." if count == 1 else f"{count} tasks need a change."


def _parsed(form: dict[str, list[str]]) -> tuple[int, dict[str, str]] | None:
    """The version and the tier per task, or None when the request is malformed."""
    if any(len(values) != 1 for values in form.values()):
        return None
    single = {name: values[0] for name, values in form.items()}
    if not all(name == "version" or name.startswith(FIELD_PREFIX) for name in single):
        return None
    version = posted_version(single, minimum=0)
    if version is None:
        return None
    chosen = {
        name[len(FIELD_PREFIX) :]: value.replace("\x00", "")
        for name, value in single.items()
        if name != "version"
    }
    return version, chosen


async def save_task_tiers(request: Request) -> Response:
    form = await read_form(request)
    if form is None:
        return PlainTextResponse(INVALID_REQUEST, status_code=413)
    parsed = _parsed({name: list(values) for name, values in form.items()})
    if parsed is None:
        return PlainTextResponse(INVALID_REQUEST, status_code=400)
    version, chosen = parsed
    handler: ProviderStep = request.app.state.provider_settings

    def run() -> Response:
        try:
            result = handler.save_task_tiers(version, chosen)
        except VersionConflict:
            return _answer(request, TaskTierState(conflict=True), status_code=409)
        if not result.ok:
            errors = dict(result.errors)
            list_error = errors.pop("task_tiers", "") or errors.pop("form", "")
            state = TaskTierState(
                errors=errors,
                list_error=list_error,
                typed={task: tier for task, tier in chosen.items() if task not in errors},
            )
            said = f"{NOT_SAVED} {list_error or _fields_to_change(len(errors))}"
            return _answer(request, state, status_code=422, announce=said)
        return _answer(request, TaskTierState(saved=True), announce=SAVED)

    return await run_in_threadpool(run)
