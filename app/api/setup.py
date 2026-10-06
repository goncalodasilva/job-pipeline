"""Setup guide pages and step submits (detailed design "Today and the setup guide"; AC-M1.2).

Page routes are registered on the app itself with ``tags=[PAGE_TAG]`` (ADR-039), like
``register_pages``: they load without the launch token, and with ``HX-Request: true`` they
return the guide fragment (step list and current step), so moving between steps re-renders the
list (ADR-053). GET and HEAD have no side effects (spec 2.9 rule 12): they read the registry,
the cached start-up decision and the step handler's context, which may read stored settings
(such as saved job preferences) but never writes.

Step submits are ``POST /setup/{step_id}``: not page-tagged, so the launch token and origin
checks apply. The body is a URL-encoded form (parsed with the standard library). A saved step
answers 303 to the next step, or for htmx 200 with the next step's guide fragment,
``HX-Push-Url`` and an "announce" trigger; a refused submit answers 422 with the errors. A
step saved but not finished (``StepResult.stay``, such as the provider step before "Connected")
answers 303 back to itself, or for htmx 200 with its own fragment and an "announce" trigger
carrying the handler's message, without ``HX-Push-Url``. A finished guide
(``StepResult.redirect``, from the Done step's fixed table) answers 303 to that local path, or
for htmx 200 with ``HX-Redirect``. A refused submit shows back only the fields the handler names
(``echo_fields``, one value each; ``echo_multi_fields``, every value).

``POST /setup/leave`` (M1.20; not page-tagged, POST only, registered before the step submit)
is "Back to the app", offered whenever setup is finished: on a guide reopened by ``--setup``
it ends the reopening for the rest of the run; reached from a banner it has nothing to end.
Stored data never changes; it answers 303 to ``/``, or for htmx 200 with ``HX-Redirect: /``.
On a guide never finished it changes nothing and points back to the guide.

``POST /setup/provider/test`` (M1.18; not page-tagged, POST only) saves the provider form and
awaits the connection test inside the request. A refused save answers 422 like a submit; a
result, connected or not, answers 200 with the provider step and the result focused: htmx gets
the fragment and an "announce" trigger with the result text, a plain request the full page
(a redirect would lose the result).
"""

import json
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.forms import echo_submitted, is_htmx, read_form
from app.api.security import PAGE_TAG
from app.api.setup_redirect import SETUP_PATH, setup_step_path
from app.api.templating import render
from app.domain.errors import UnknownSetupStep
from app.onboarding.detection import StartupDecision
from app.onboarding.registry import (
    PROVIDER,
    ConnectionCheck,
    ConnectionTester,
    Form,
    StepDefinition,
    StepHandler,
    StepRegistry,
    StepResult,
)
from app.onboarding.startup import OnboardingState

SETUP_PAGE = "pages/setup.html"
GUIDE_FRAGMENT = "fragments/setup/_guide_swap.html"
NOT_FOUND_PAGE = "pages/setup_not_found.html"
NOT_FOUND_FRAGMENT = "fragments/setup/_not_found.html"
LEAVE_PATH = f"{SETUP_PATH}/leave"  # "Back to the app" (M1.20)
APP_HOME = "/"


def register_setup_pages(app: FastAPI) -> None:
    """Add the guide's routes directly to the app (not through an included router)."""
    app.add_api_route(
        SETUP_PATH,
        setup_root,
        methods=["GET", "HEAD"],
        tags=[PAGE_TAG],
        response_class=RedirectResponse,
        name="setup",
    )
    app.add_api_route(
        f"{SETUP_PATH}/{{step_id}}",
        setup_step,
        methods=["GET", "HEAD"],
        tags=[PAGE_TAG],
        response_class=HTMLResponse,
        name="setup_step",
    )
    app.add_api_route(
        f"{setup_step_path(PROVIDER.id)}/test",
        test_provider_connection,
        methods=["POST"],
        response_class=HTMLResponse,
        name="setup_provider_test",
    )
    app.add_api_route(
        LEAVE_PATH,
        leave_setup,
        methods=["POST"],
        response_class=Response,
        name="setup_leave",
    )
    app.add_api_route(
        f"{SETUP_PATH}/{{step_id}}",
        submit_step,
        methods=["POST"],
        response_class=HTMLResponse,
        name="setup_step_submit",
    )


def setup_root(request: Request) -> RedirectResponse:
    """The step the guide is at; its first step once setup is done (re-running the guide)."""
    registry: StepRegistry = request.app.state.setup_registry
    onboarding: OnboardingState = request.app.state.onboarding
    step = onboarding.guide_step() or registry.first.id
    return RedirectResponse(setup_step_path(step), status_code=303)


def setup_step(request: Request, step_id: str) -> HTMLResponse:
    registry: StepRegistry = request.app.state.setup_registry
    try:
        step = registry.get(step_id)
    except UnknownSetupStep:
        return _not_found(request)
    return _render_step(request, step)


async def submit_step(request: Request, step_id: str) -> Response:
    registry: StepRegistry = request.app.state.setup_registry
    try:
        step = registry.get(step_id)
    except UnknownSetupStep:
        return _not_found(request)
    form = await read_form(request)
    if form is None:
        return HTMLResponse("", status_code=413)
    handler: StepHandler = request.app.state.setup_handlers[step.id]
    # Handlers block (folders, database, keychain): never on the event loop.
    result: StepResult = await run_in_threadpool(handler.submit, form)
    if not result.ok:
        response = _render_step(request, step, errors=result.errors, submitted=form)
        response.status_code = 422
        return response
    if result.redirect is not None:  # finished: leave the guide (M1.19)
        target = _local_path(result.redirect)
        if not is_htmx(request):
            return RedirectResponse(target, status_code=303)
        return Response(status_code=200, headers={"HX-Redirect": target})
    if result.stay:  # saved, but the step isn't finished: stay on it (M1.17)
        if not is_htmx(request):
            return RedirectResponse(setup_step_path(step.id), status_code=303)
        response = _render_step(request, step, stayed=True)
        response.headers["HX-Trigger"] = json.dumps({"announce": result.message or "Saved."})
        return response
    following = _next_step(registry, step)
    url = setup_step_path(following.id)
    if not is_htmx(request):
        return RedirectResponse(url, status_code=303)
    position = registry.steps.index(following) + 1
    announcement = f"Step {position} of {len(registry.steps)}: {following.title}"
    response = _render_step(request, following)
    response.headers["HX-Push-Url"] = url
    response.headers["HX-Trigger"] = json.dumps({"announce": announcement})
    return response


def leave_setup(request: Request) -> Response:
    """Back to the app once setup is finished (no I/O: cached state)."""
    onboarding: OnboardingState = request.app.state.onboarding
    onboarding.leave_reopened()
    step = onboarding.guide_step()  # still set when the guide was never finished
    target = setup_step_path(step) if step is not None else APP_HOME
    if not is_htmx(request):
        return RedirectResponse(target, status_code=303)
    return Response(status_code=200, headers={"HX-Redirect": target})


async def test_provider_connection(request: Request) -> Response:
    registry: StepRegistry = request.app.state.setup_registry
    step = registry.get(PROVIDER.id)
    form = await read_form(request)
    if form is None:
        return HTMLResponse("", status_code=413)
    tester: ConnectionTester = request.app.state.connection_tester
    # Awaited here (ADR-044 item 6); the tester runs its blocking parts in worker threads.
    check = await tester.test(form)
    if check.errors:
        response = _render_step(request, step, errors=check.errors, submitted=form)
        response.status_code = 422
        return response
    response = _render_step(request, step, connection=check)
    if is_htmx(request):
        response.headers["HX-Trigger"] = json.dumps({"announce": check.message})
    return response


# --- helpers ---------------------------------------------------------------------------------


def _render_step(
    request: Request,
    step: StepDefinition,
    *,
    errors: Mapping[str, str] | None = None,
    submitted: Form | None = None,
    stayed: bool = False,
    connection: ConnectionCheck | None = None,
) -> HTMLResponse:
    registry: StepRegistry = request.app.state.setup_registry
    onboarding: OnboardingState = request.app.state.onboarding
    handler: StepHandler = request.app.state.setup_handlers[step.id]
    form: dict[str, Any] = dict(handler.context())
    if submitted and not form.get("folder_fixed"):
        echo_submitted(form, handler, submitted)
    return render(
        request,
        page=SETUP_PAGE,
        fragment=GUIDE_FRAGMENT,
        ctx={
            "step": step,
            "steps": registry.steps,
            "statuses": onboarding.statuses,
            # Setup finished (reopened with --setup, or opened from a banner): offer the way back.
            "setup_finished": _setup_finished(onboarding.decision),
            "attention": frozenset(onboarding.decision.banners),  # list says "Needs attention"
            "form": form,
            "errors": dict(errors or {}),
            "stayed": stayed,  # saved and staying on the step: focus moves to its status
            "connection": connection,  # a connection test just ran: focus moves to its result
            "next_step": following if (following := _next_step(registry, step)) != step else None,
        },
    )


def _setup_finished(decision: StartupDecision) -> bool:
    return decision.reopened or decision.kind in ("app", "app_with_banner")


def _local_path(target: str) -> str:
    """A path on this app; anything else (a URL, ``//host``, ``/\\host``, control characters,
    which browsers strip) goes to Today."""
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in target):
        return "/"
    if target.startswith("/") and not target.startswith(("//", "/\\")):
        return target
    return "/"


def _next_step(registry: StepRegistry, step: StepDefinition) -> StepDefinition:
    steps = registry.steps
    index = steps.index(step)
    return steps[min(index + 1, len(steps) - 1)]


def _not_found(request: Request) -> HTMLResponse:
    # The id is never echoed: it comes from the URL.
    response = render(request, page=NOT_FOUND_PAGE, fragment=NOT_FOUND_FRAGMENT, ctx={})
    response.status_code = 404
    return response
