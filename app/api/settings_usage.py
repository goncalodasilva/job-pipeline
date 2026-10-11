"""Settings > Usage budgets: the two save routes (M2.22; AC-M2.7, AC-M2.10; spec 3.5, 3.7).

``POST /settings/usage/budgets`` (launch token and origin; POST only) saves or, with
``action=remove``, removes one service's request budget: hidden ``service`` and ``version``,
then ``allowance``, ``first_warning``, ``second_warning``, ``block_at``, ``reset_day`` and the
optional ``fast_cap`` and ``thorough_cap``. Remove ignores the fields.

``POST /settings/usage/concurrency`` saves ``max_concurrent`` (1 to 3) with the task-runner
setting's ``version``.

Both answer 303 to ``/settings``, or for htmx 200 with the form's block, a line in it saying
what was done, focus on its heading and an "announce" trigger (never ``HX-Push-Url``). A
refused form answers 422 with the errors next to their fields and what was typed kept (htmx:
the block, and an announcement of how many fields need a change; plain: the full page). A
stale version answers 409 with a reload prompt and changes nothing. A service that isn't
offered answers 404 and is never shown back.

A saved budget changes the one version every budget form carries (ADR-079), so the htmx
answer also refreshes the other forms' hidden ``version`` out of band; what is typed in them
stays. A refused or stale form refreshes nothing and keeps the version it was sent with, so
a tab that is behind is told to reload until it does.

Every message is fixed copy with at most a service's display name and a count.

``GET /settings/usage`` (M2.23; launch token; GET only; changes nothing) answers the usage
panel alone, the same with or without ``HX-Request``. The panel fetches it again after a
budget was saved or removed: that htmx answer fires ``usage:changed`` once it has settled
(``HX-Trigger-After-Settle``), so the panel shows the new budget without a reload. It also
fetches it again, debounced and silently, when a task changes (``tasks:updated``).
"""

import json
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.forms import first_value, is_htmx, read_form, whole_number
from app.api.settings import SETTINGS_PATH, render_settings
from app.api.templating import templates
from app.api.usage_forms import (
    BUDGETS_PATH,
    CONCURRENCY,
    CONCURRENCY_PATH,
    PANEL_PATH,
    USAGE_CHANGED_EVENT,
    UsageFormState,
    entered_budget,
    expected_version,
    panel_context,
    typed_budget,
    usage_settings,
)
from app.domain.errors import VersionConflict
from app.onboarding.registry import Form
from app.services.usage_settings import (
    CONCURRENCY_FIELD,
    UsageSettings,
    UsageSettingUnavailable,
)

BUDGET_FRAGMENT = "fragments/settings/_usage_budget_answer.html"
CONCURRENCY_FRAGMENT = "fragments/settings/_usage_concurrency.html"
PANEL_FRAGMENT = "fragments/settings/_usage_panel.html"
NOT_AVAILABLE = "This setting isn't available."
REMOVE = "remove"
CONCURRENCY_NAME = "Background tasks at once"


@dataclass(frozen=True)
class _Copy:
    """What one form says about its outcome."""

    announced: str  # saved or removed: the live region
    confirmed: str  # saved or removed: the line in the block
    refused: str  # what wasn't saved, before the count of fields
    conflict: str  # a stale version: what it means for this form


def _budget_copy(name: str, *, removing: bool) -> _Copy:
    if removing:
        return _Copy(
            announced=f"Budget for {name} removed. It has no limit now.",
            confirmed="Removed. This service has no limit now.",
            refused=f"Budget for {name} not saved.",
            conflict="The budget wasn't removed.",
        )
    return _Copy(
        announced=f"Budget for {name} saved.",
        confirmed="Saved.",
        refused=f"Budget for {name} not saved.",
        conflict="What you typed here isn't saved.",
    )


def _concurrency_copy(value: object) -> _Copy:
    # ``announced`` is read only after a save, when ``value`` is one of the numbers offered;
    # anything else that was sent never reaches a message.
    count = value if isinstance(value, int) else 0
    tasks = "1 background task" if count == 1 else f"{count} background tasks"
    return _Copy(
        announced=f"Saved. Up to {tasks} from the next start.",
        confirmed="Saved. It applies the next time you start the app.",
        refused=f"{CONCURRENCY_NAME} not saved.",
        conflict="What you chose here isn't saved.",
    )


def _fields_to_change(count: int) -> str:
    return "1 field needs a change." if count == 1 else f"{count} fields need a change."


def register_usage_settings(app: FastAPI) -> None:
    """Add the routes directly to the app; none is a page, so all need the token."""
    app.add_api_route(
        PANEL_PATH,
        usage_panel,
        methods=["GET"],
        response_class=HTMLResponse,
        name="settings_usage_panel",
    )
    app.add_api_route(
        BUDGETS_PATH,
        save_budget,
        methods=["POST"],
        response_class=HTMLResponse,
        name="settings_usage_budgets",
    )
    app.add_api_route(
        CONCURRENCY_PATH,
        save_concurrency,
        methods=["POST"],
        response_class=HTMLResponse,
        name="settings_usage_concurrency",
    )


def usage_panel(request: Request) -> HTMLResponse:
    """The usage panel alone. A plain function: FastAPI runs it in its thread pool, so the
    database reads never block the event loop."""
    ctx = {"usage_panel": panel_context(request)}
    return templates.TemplateResponse(request, PANEL_FRAGMENT, ctx)


async def save_budget(request: Request) -> Response:
    form = await read_form(request)
    if form is None:
        return HTMLResponse("", status_code=413)
    try:
        # Blocking (database): never on the event loop.
        return await run_in_threadpool(_budget_answer, request, form)
    except UsageSettingUnavailable:
        return PlainTextResponse(NOT_AVAILABLE, status_code=404)


async def save_concurrency(request: Request) -> Response:
    form = await read_form(request)
    if form is None:
        return HTMLResponse("", status_code=413)
    try:
        return await run_in_threadpool(_concurrency_answer, request, form)
    except UsageSettingUnavailable:
        return PlainTextResponse(NOT_AVAILABLE, status_code=404)


# --- helpers ---------------------------------------------------------------------------------


def _budget_answer(request: Request, form: Form) -> Response:
    settings = usage_settings(request)
    service = first_value(form, "service")
    name = settings.name(service)  # refuses a service that isn't offered, before anything else
    removing = first_value(form, "action") == REMOVE
    typed = {} if removing else typed_budget(form)

    def change(version: int) -> dict[str, str]:
        if removing:
            settings.remove_budget(service, expected_version=version)
            return {}
        return dict(settings.save_budget(service, entered_budget(typed), expected_version=version))

    return _answer(
        request,
        change,
        version=expected_version(form),
        state=UsageFormState(target=service, typed=typed),
        fragment=BUDGET_FRAGMENT,
        copy=_budget_copy(name, removing=removing),
        then=USAGE_CHANGED_EVENT,  # the usage panel shows the budget: it fetches itself again
    )


def _concurrency_answer(request: Request, form: Form) -> Response:
    settings: UsageSettings = usage_settings(request)
    value = whole_number(first_value(form, CONCURRENCY_FIELD))

    def change(version: int) -> dict[str, str]:
        error = settings.save_concurrency(value, expected_version=version)
        return {CONCURRENCY_FIELD: error} if error else {}

    return _answer(
        request,
        change,
        version=expected_version(form),
        state=UsageFormState(target=CONCURRENCY),  # a refused value is never shown back
        fragment=CONCURRENCY_FRAGMENT,
        copy=_concurrency_copy(value),
    )


def _answer(
    request: Request,
    change: Callable[[int], dict[str, str]],
    *,
    version: int | None,
    state: UsageFormState,
    fragment: str,
    copy: _Copy,
    then: str = "",
) -> Response:
    """Run ``change`` with the form's version and answer for its outcome: the errors it
    returns (422), a stale version (409), or done; ``then`` is an event the page fires once
    a done answer has settled. Blocking."""
    kept = "" if version is None else str(version)
    try:
        if version is None:  # not a number: no page of this app sent it
            raise VersionConflict(detail="the form's version isn't a number")
        errors = change(version)
    except VersionConflict:
        stale = UsageFormState(
            target=state.target,
            typed=state.typed,
            conflict=True,
            conflict_detail=copy.conflict,
            kept_version=kept,
        )
        return _shown(request, stale, fragment, status_code=409)
    if errors:
        refused = UsageFormState(
            target=state.target, typed=state.typed, errors=errors, kept_version=kept
        )
        said = f"{copy.refused} {_fields_to_change(len(errors))}"
        return _shown(request, refused, fragment, status_code=422, announce=said)
    if not is_htmx(request):
        return RedirectResponse(SETTINGS_PATH, status_code=303)
    done = UsageFormState(target=state.target, changed=True, confirmed=copy.confirmed)
    response = _shown(request, done, fragment, announce=copy.announced)
    if then:
        response.headers["HX-Trigger-After-Settle"] = then
    return response


def _shown(
    request: Request,
    state: UsageFormState,
    fragment: str,
    *,
    status_code: int = 200,
    announce: str = "",
) -> HTMLResponse:
    """The form's answer; ``announce`` reaches the live region, so only htmx gets it."""
    response = render_settings(request, fragment=fragment, usage=state)
    response.status_code = status_code
    if announce and is_htmx(request):
        response.headers["HX-Trigger"] = json.dumps({"announce": announce})
    return response
