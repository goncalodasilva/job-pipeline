"""Settings page and Settings > Providers (M1.21; AC-M1.3, AC-M1.4; detailed design "Settings").

``GET /settings`` is a page route (``PAGE_TAG``, like ``register_pages``): with
``HX-Request: true`` it returns the main content only. Its Providers section reuses the
provider step's handler (``ProviderStep``, over ``ProviderSetupService``; ADR-062) and its
fragment, posting to the Settings routes instead of the guide's. GET and HEAD have no side
effects (spec 2.9 rule 12): the credential status comes from the settings row and cached facts,
never the keychain. A link re-runs the setup guide from its first step (detailed design; ADR-066:
the finished guide offers "Back to the app" and resets nothing). The rest of Settings arrives
later and has a small empty state after Providers.

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
"""

import json
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.forms import echo_submitted, is_htmx, read_form
from app.api.pages import AREAS, area
from app.api.security import PAGE_TAG
from app.api.setup_redirect import setup_step_path
from app.api.templating import render
from app.onboarding.registry import ConnectionCheck, Form, StepRegistry, StepResult
from app.onboarding.steps.provider import ProviderStep

SETTINGS_PATH = "/settings"
PROVIDERS_PATH = f"{SETTINGS_PATH}/providers"
PROVIDERS_TEST_PATH = f"{PROVIDERS_PATH}/test"
TIERS_PATH = f"{SETTINGS_PATH}/tiers"
SETTINGS_PAGE = "pages/settings.html"
SETTINGS_FRAGMENT = "fragments/settings/_settings.html"
PROVIDERS_FRAGMENT = "fragments/settings/_providers.html"
PROVIDERS_TARGET = "#providers"


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
    return _render(request, section_only=False)


async def save_providers(request: Request) -> Response:
    form = await read_form(request)
    if form is None:
        return HTMLResponse("", status_code=413)
    # Blocking (database, keychain): never on the event loop.
    result: StepResult = await run_in_threadpool(_handler(request).submit, form)
    if not result.ok:
        return _refused(request, result.errors, form)
    if result.view is not None:  # nothing changed: a removal to confirm, or kept (200)
        response = _render(request, step_view=result.view)
        if is_htmx(request) and result.message:
            response.headers["HX-Trigger"] = json.dumps({"announce": result.message})
        return response
    if not is_htmx(request):
        return RedirectResponse(SETTINGS_PATH, status_code=303)
    response = _render(request, stayed=True)
    response.headers["HX-Trigger"] = json.dumps({"announce": result.message or "Saved."})
    return response


async def save_tiers(request: Request) -> Response:
    form = await read_form(request)
    if form is None:
        return HTMLResponse("", status_code=413)
    result: StepResult = await run_in_threadpool(_handler(request).save_tiers, form)
    if not result.ok:  # nothing submitted is shown back
        return _refused(request, result.errors, None)
    if not is_htmx(request):
        return RedirectResponse(SETTINGS_PATH, status_code=303)
    response = _render(request, tiers_saved=True)
    response.headers["HX-Trigger"] = json.dumps({"announce": result.message or "Saved."})
    return response


async def test_providers(request: Request) -> Response:
    form = await read_form(request)
    if form is None:
        return HTMLResponse("", status_code=413)
    # Awaited here (ADR-063); the handler runs its blocking parts in worker threads.
    check: ConnectionCheck = await _handler(request).test(form, mark_done=False)
    if check.errors:
        return _refused(request, check.errors, form)
    response = _render(request, connection=check)
    if is_htmx(request):
        response.headers["HX-Trigger"] = json.dumps({"announce": check.message})
    return response


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
) -> HTMLResponse:
    """The full Settings page, or for htmx the Providers section (after a POST) or the main
    content (a page GET)."""
    handler = _handler(request)
    registry: StepRegistry = request.app.state.setup_registry
    form: dict[str, Any] = dict(handler.context())
    if submitted:
        echo_submitted(form, handler, submitted)
    return render(
        request,
        page=SETTINGS_PAGE,
        fragment=PROVIDERS_FRAGMENT if section_only else SETTINGS_FRAGMENT,
        ctx={
            "area": area("settings"),
            "areas": AREAS,
            "form": form,
            "errors": dict(errors or {}),
            "stayed": stayed,  # saved: focus moves to the credential status
            "connection": connection,  # a test just ran: focus moves to its result
            "step_view": dict(step_view or {}),  # a removal to confirm, or kept: focus there
            "tiers_saved": tiers_saved,  # the tier map was saved: focus moves to its status
            "tiers_url": TIERS_PATH,
            # The provider step's fragment, pointed at the Settings routes.
            "provider_context": "settings",
            "provider_post_url": PROVIDERS_PATH,
            "provider_test_url": PROVIDERS_TEST_PATH,
            "provider_target": PROVIDERS_TARGET,
            "guide_url": setup_step_path(registry.first.id),  # re-run the setup guide
        },
    )
