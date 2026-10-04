"""Setup guide pages (detailed design "Today and the setup guide"; AC-M1.2).

Page routes registered on the app itself with ``tags=[PAGE_TAG]`` (ADR-039), like
``register_pages``: they load without the launch token, and with ``HX-Request: true`` they
return only the step's fragment. GET and HEAD have no side effects (spec 2.9 rule 12): they
read the registry and the cached start-up decision, nothing else. Each step's form and
submit arrive with its own task (M1.16 to M1.19).
"""

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.api.security import PAGE_TAG
from app.api.setup_redirect import SETUP_PATH, setup_step_path
from app.api.templating import render
from app.domain.errors import UnknownSetupStep
from app.onboarding.registry import StepRegistry
from app.onboarding.startup import OnboardingState

SETUP_PAGE = "pages/setup.html"
NOT_FOUND_PAGE = "pages/setup_not_found.html"
NOT_FOUND_FRAGMENT = "fragments/setup/_not_found.html"


def register_setup_pages(app: FastAPI) -> None:
    """Add the guide's page routes directly to the app (not through an included router)."""
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
        # The id is never echoed: it comes from the URL.
        response = render(request, page=NOT_FOUND_PAGE, fragment=NOT_FOUND_FRAGMENT, ctx={})
        response.status_code = 404
        return response
    return render(
        request,
        page=SETUP_PAGE,
        fragment=step.template,
        ctx={"step": step, "steps": registry.steps},
    )
