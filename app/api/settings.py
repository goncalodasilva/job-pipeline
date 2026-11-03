"""Settings > Providers and AI (M1.21, M9.2; AC-M1.3, AC-M1.4; detailed design "Settings").

``GET /settings`` is a page route (``PAGE_TAG``, like ``register_pages``): the Providers and AI
area, the first of the Settings area pages (``app/api/settings_nav.py``; the others are in
``app/api/settings_areas.py`` and ``app/api/settings_data.py``). With
``HX-Request: true`` it returns the main content only. Its Providers section reuses the
provider step's handler (``ProviderStep``, over ``ProviderSetupService``; ADR-062) and its
fragment, posting to the Settings routes instead of the guide's. GET and HEAD have no side
effects (spec 2.9 rule 12): the credential status comes from the settings row and cached facts,
never the keychain. A link re-runs the setup guide from its first step (detailed design; ADR-066:
the finished guide offers "Back to the app" and resets nothing). While the practice task is
offered (fakes allowed and the fake saved; M2.17), a "Testing tools" section follows Providers;
its form posts to ``/tasks/practice`` (``app/api/tasks.py``).

``GET /settings/testing-tools`` (M2.24; launch token; GET only; changes nothing) answers the
Testing tools section alone, the same with or without ``HX-Request``. The section fetches it
on the body's ``usage:changed`` event (a budget was saved or removed on this page), so its
Start button is blocked or freed without a reload. The request carries the form's values:
they stay selected when each is a listed value, and are never shown back otherwise. Nothing
in the answer takes focus or is announced.

``POST /settings/providers`` (launch token and origin) saves like the provider step: ``kind``,
``credential``, ``action=save`` (default) or ``remove=<kind>`` (deletes only with
``confirm=<kind>``; without it, or with ``keep=<kind>``, it answers 200 with the section asking
or no longer asking, and changes nothing). A valid save answers 303 to
``/settings``, or for htmx 200 with the Providers section and an "announce" trigger (never
``HX-Push-Url``); a refused one answers 422 with the field error, showing back only the kind.

``POST /settings/providers/test`` (launch token and origin; POST only) saves, then tests, with
the provider step's messages; it never marks a guide step done. A result answers 200: htmx
gets the section with the result focused and announced, a plain request the full page.

``POST /settings/tiers`` (launch token and origin; POST only; M1.22) maps the Fast and
Thorough tiers to models the active provider offers: 303 to ``/settings``, or for htmx 200
with the Providers section, focus on the tier status and an "announce" trigger. A refused map
answers 422 with the field errors and never shows the submitted values back.

Usage budgets, Lists and statuses, Sessions, Your details, CV templates and Keyboard shortcuts
moved to their own area pages (M9.2); this page holds only what Providers and AI needs.

Rendering reads the database, so the async routes render in a worker thread.
"""

import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.budget_gate import dialog_context, reason_context, refusal_message
from app.api.forms import echo_submitted, is_htmx, read_form
from app.api.pages import AREAS, area
from app.api.security import PAGE_TAG
from app.api.settings_nav import AREA_FRAGMENT, AREA_PAGE, area_ctx
from app.api.setup_redirect import setup_step_path
from app.api.templating import render, templates
from app.api.usage_forms import USAGE_CHANGED_EVENT
from app.onboarding.registry import ConnectionCheck, Form, StepRegistry, StepResult
from app.onboarding.steps.provider import ProviderStep
from app.services.budget_gate import BlockedAction, BudgetGate, Refusal
from app.services.practice import (
    PRACTICE_FIELDS,
    PRACTICE_SERVICE,
    PracticeService,
    choice_from_form,
)

SETTINGS_PATH = "/settings"
PROVIDERS_PATH = f"{SETTINGS_PATH}/providers"
PROVIDERS_TEST_PATH = f"{PROVIDERS_PATH}/test"
TIERS_PATH = f"{SETTINGS_PATH}/tiers"
PROVIDERS_FRAGMENT = "fragments/settings/_providers.html"
PROVIDERS_TARGET = "#providers"
PROVIDERS_SAVED_FRAGMENT = "fragments/settings/_providers_saved.html"
TESTING_TOOLS_FRAGMENT = "fragments/settings/_testing_tools.html"
TESTING_TOOLS_ID = "testing-tools"
TESTING_TOOLS_PATH = f"{SETTINGS_PATH}/testing-tools"  # the section alone (M2.24)
# The section shows a blocked action: it fetches itself again when a budget changes.
TESTING_TOOLS_TRIGGER = f"{USAGE_CHANGED_EVENT} from:body"
PRACTICE_PATH = "/tasks/practice"
PRACTICE_START = "#practice-form button[type=submit]"  # gets focus back from the dialog
PRACTICE_REASON_ID = "practice-budget-reason"  # why a start is blocked or was refused
PRACTICE_FLAG = "practice"  # ?practice=<key of PRACTICE_STATUS>, after a plain start
PRACTICE_STATUS: Mapping[str, str] = MappingProxyType(
    {
        "started": "Practice task started.",
        "already": "A practice task is already running. Wait for it to finish, then start another.",
    }
)


def register_settings_pages(app: FastAPI) -> None:
    """Add the Settings routes directly to the app (not through an included router)."""
    app.add_api_route(
        SETTINGS_PATH,
        settings_page,
        methods=["GET", "HEAD"],
        tags=[PAGE_TAG],
        response_class=HTMLResponse,
        name="page_settings",
    )
    app.add_api_route(  # not a page: it needs the token
        TESTING_TOOLS_PATH,
        testing_tools,
        methods=["GET"],
        response_class=HTMLResponse,
        name="settings_testing_tools",
    )
    app.add_api_route(
        PROVIDERS_TEST_PATH,
        test_providers,
        methods=["POST"],
        response_class=HTMLResponse,
        name="settings_providers_test",
    )
    app.add_api_route(
        TIERS_PATH,
        save_tiers,
        methods=["POST"],
        response_class=HTMLResponse,
        name="settings_tiers",
    )
    app.add_api_route(
        PROVIDERS_PATH,
        save_providers,
        methods=["POST"],
        response_class=HTMLResponse,
        name="settings_providers",
    )


def settings_page(request: Request) -> HTMLResponse:
    # After a plain practice start: one of two fixed words; anything else shows nothing.
    flag = request.query_params.get(PRACTICE_FLAG, "")
    return _render(request, section_only=False, practice_status=PRACTICE_STATUS.get(flag))


def testing_tools(request: Request) -> HTMLResponse:
    """The Testing tools section alone, with the choices the page sent still selected. A
    plain function: FastAPI runs it in its thread pool, so the database reads never block
    the event loop."""
    practice: PracticeService = request.app.state.practice
    sent = {name: request.query_params.getlist(name) for name in PRACTICE_FIELDS}
    choice = choice_from_form(sent)
    ctx = {
        "practice": _practice_context(
            request,
            offered=practice.offered(),
            selected=None if choice is None else choice.as_form(),
        )
    }
    return templates.TemplateResponse(request, TESTING_TOOLS_FRAGMENT, ctx)


def practice_started_url(flag: str) -> str:
    """Where a plain practice start goes back to; ``flag`` is a key of ``PRACTICE_STATUS``."""
    return f"{SETTINGS_PATH}?{PRACTICE_FLAG}={flag}#{TESTING_TOOLS_ID}"


async def save_providers(request: Request) -> Response:
    form = await read_form(request)
    if form is None:
        return HTMLResponse("", status_code=413)
    # Blocking (database, keychain): never on the event loop.
    result: StepResult = await run_in_threadpool(_handler(request).submit, form)
    if not result.ok:
        return await run_in_threadpool(_refused, request, result.errors, form)
    if result.view is not None:  # nothing changed: a removal to confirm, or kept (200)
        response = await run_in_threadpool(_render, request, step_view=result.view)
        if is_htmx(request) and result.message:
            response.headers["HX-Trigger"] = json.dumps({"announce": result.message})
        return response
    if not is_htmx(request):
        return RedirectResponse(SETTINGS_PATH, status_code=303)
    # The saved kind may change whether Testing tools is offered: refresh it out of band.
    response = await run_in_threadpool(
        _render, request, stayed=True, fragment=PROVIDERS_SAVED_FRAGMENT
    )
    response.headers["HX-Trigger"] = json.dumps({"announce": result.message or "Saved."})
    return response


async def save_tiers(request: Request) -> Response:
    form = await read_form(request)
    if form is None:
        return HTMLResponse("", status_code=413)
    result: StepResult = await run_in_threadpool(_handler(request).save_tiers, form)
    if not result.ok:  # nothing submitted is shown back
        return await run_in_threadpool(_refused, request, result.errors, None)
    if not is_htmx(request):
        return RedirectResponse(SETTINGS_PATH, status_code=303)
    response = await run_in_threadpool(_render, request, tiers_saved=True)
    response.headers["HX-Trigger"] = json.dumps({"announce": result.message or "Saved."})
    return response


async def test_providers(request: Request) -> Response:
    form = await read_form(request)
    if form is None:
        return HTMLResponse("", status_code=413)
    # Awaited here (ADR-063); the handler runs its blocking parts in worker threads.
    check: ConnectionCheck = await _handler(request).test(form, mark_done=False)
    if check.errors:
        return await run_in_threadpool(_refused, request, check.errors, form)
    response = await run_in_threadpool(_render, request, connection=check)
    if is_htmx(request):
        response.headers["HX-Trigger"] = json.dumps({"announce": check.message})
    return response


def testing_tools_response(
    request: Request,
    *,
    status: str | None = None,
    error: str | None = None,
    selected: Mapping[str, str] | None = None,
    note: str | None = None,
    refusal: Refusal | None = None,
) -> HTMLResponse:
    """The Testing tools section for htmx (the full Settings page otherwise), with the
    result of a start (``status``, and ``note`` when a confirmed budget warning is passed)
    or why it was refused (``error``, or ``refusal``: the budget gate's answer, shown as
    its dialog, the blocked button or a reason with a link to the budgets); ``selected`` is
    the accepted choice per field, kept in the selects. Blocking."""
    return _render(
        request,
        fragment=TESTING_TOOLS_FRAGMENT,
        practice_status=status,
        practice_error=error,
        practice_selected=selected,
        practice_note=note,
        practice_refusal=refusal,
    )


# --- helpers ---------------------------------------------------------------------------------


def _handler(request: Request) -> ProviderStep:
    handler: ProviderStep = request.app.state.provider_settings
    return handler


def _refused(request: Request, errors: Mapping[str, str], submitted: Form | None) -> HTMLResponse:
    response = _render(request, errors=errors, submitted=submitted)
    response.status_code = 422
    return response


def _render(
    request: Request,
    *,
    section_only: bool = True,
    errors: Mapping[str, str] | None = None,
    submitted: Form | None = None,
    stayed: bool = False,
    connection: ConnectionCheck | None = None,
    step_view: Mapping[str, str] | None = None,
    tiers_saved: bool = False,
    fragment: str | None = None,
    practice_status: str | None = None,
    practice_error: str | None = None,
    practice_selected: Mapping[str, str] | None = None,
    practice_note: str | None = None,
    practice_refusal: Refusal | None = None,
) -> HTMLResponse:
    """The full Providers and AI page, or for htmx the Providers section (after a POST), the
    main content (a page GET) or ``fragment``. Blocking."""
    handler = _handler(request)
    practice: PracticeService = request.app.state.practice
    offered = practice.offered()  # a settings read; writes nothing
    if fragment is None:
        fragment = PROVIDERS_FRAGMENT if section_only else AREA_FRAGMENT
    registry: StepRegistry = request.app.state.setup_registry
    # One focus target: a result, an error, a saved status, a confirmation or a saved tier map in
    # Providers (or a practice start) takes it instead of the heading.
    heading_focus = not (
        connection
        or errors
        or stayed
        or step_view
        or tiers_saved
        or practice_status
        or practice_error
    )
    form: dict[str, Any] = dict(handler.context())
    if submitted:
        echo_submitted(form, handler, submitted)
    return render(
        request,
        page=AREA_PAGE,
        fragment=fragment,
        ctx={
            "area": area("settings"),
            "areas": AREAS,
            **area_ctx("providers", heading_focus=heading_focus),
            "form": form,
            "errors": dict(errors or {}),
            "stayed": stayed,  # saved: focus moves to the credential status
            "connection": connection,  # a test just ran: focus moves to its result
            "step_view": dict(step_view or {}),  # a removal to confirm, or kept: focus there
            "tiers_saved": tiers_saved,  # the tier map was saved: focus moves to its status
            "search_provider": request.app.state.prep.settings_search_info(),
            "tiers_url": TIERS_PATH,
            # The provider step's fragment, pointed at the Settings routes.
            "provider_context": "settings",
            "provider_post_url": PROVIDERS_PATH,
            "provider_test_url": PROVIDERS_TEST_PATH,
            "provider_target": PROVIDERS_TARGET,
            "guide_url": setup_step_path(registry.first.id),  # re-run the setup guide
            # Testing tools (M2.17): shown only while the practice task is offered.
            "practice": _practice_context(
                request,
                offered=offered,
                status=practice_status,
                error=practice_error,
                selected=practice_selected,
                note=practice_note,
                refusal=practice_refusal,
                oob=fragment == PROVIDERS_SAVED_FRAGMENT,
            ),
        },
    )


def _practice_context(
    request: Request,
    *,
    offered: bool,
    status: str | None = None,
    error: str | None = None,
    selected: Mapping[str, str] | None = None,
    note: str | None = None,
    refusal: Refusal | None = None,
    oob: bool = False,
) -> dict[str, Any]:
    """The ``practice`` template value: everything the Testing tools section shows. Blocking."""
    return {
        "offered": offered,
        "url": PRACTICE_PATH,
        "refresh_url": TESTING_TOOLS_PATH,
        "refresh_on": TESTING_TOOLS_TRIGGER,
        "fields": PRACTICE_FIELDS,
        "status": status,  # a start just happened: focus moves there
        "error": error,  # a refused start: focus moves there
        "selected": dict(selected or {}),  # accepted values only
        "oob": oob,  # swapped next to Providers
        **_practice_budget(request, offered, refusal, selected or {}),
        "note": note,  # past a confirmed budget warning (M2.24)
    }


def _practice_budget(
    request: Request, offered: bool, refusal: Refusal | None, selected: Mapping[str, str]
) -> dict[str, Any]:
    """The budget gate's part of Testing tools (M2.24): whether the Start button is blocked
    (whenever the fake service is at its blocking point; a read), the reason shown above the
    button (the block, or why the gate refused a start it has no question about), and the
    dialog for a start the gate has a question about. Blocking."""
    gate: BudgetGate = request.app.state.budget_gate
    block = refusal if isinstance(refusal, BlockedAction) else None
    if block is None and offered:
        block = gate.blocked(PRACTICE_SERVICE)
    dialog = None
    if refusal is not None and block is None:
        dialog = dialog_context(
            refusal,
            dialog_id="practice-budget-dialog",
            action=PRACTICE_PATH,
            target=f"#{TESTING_TOOLS_ID}",
            fields=selected,
            return_focus=PRACTICE_START,
        )
    reason = None
    if block is not None:
        reason = reason_context(
            block.message, reason_id=PRACTICE_REASON_ID, focus=refusal is not None
        )
    elif refusal is not None and dialog is None:
        message = refusal_message(refusal)
        reason = reason_context(message, reason_id=PRACTICE_REASON_ID, focus=True)
    return {"blocked": block is not None, "dialog": dialog, "budget_reason": reason}
