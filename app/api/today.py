"""Today page and protected fragment; GETs only read cached setup and service views."""

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.forms import is_htmx, single_value_form
from app.api.pages import AREAS, area
from app.api.security import PAGE_TAG
from app.api.sessions import TODAY_SLOT
from app.api.templating import render, templates
from app.onboarding.registry import StepRegistry
from app.onboarding.startup import OnboardingState
from app.services.session_suggestion import SessionSuggestions
from app.services.today import TodayService

CONTENT = "fragments/today/_content.html"
DISMISS_PATH = "/today/suggestion/dismiss"
DISMISSED = "Session suggestion dismissed."


def register_today_routes(app: FastAPI) -> None:
    app.add_api_route(
        "/",
        page,
        methods=["GET", "HEAD"],
        tags=[PAGE_TAG],
        response_class=HTMLResponse,
        name="page_today",
    )
    app.add_api_route("/today/content", content, methods=["GET"])
    app.add_api_route(DISMISS_PATH, dismiss_suggestion, methods=["POST"])


def _context(request: Request, *, focus: bool = False) -> dict[str, Any]:
    service: TodayService = request.app.state.today
    suggestions: SessionSuggestions = request.app.state.session_suggestions
    onboarding: OnboardingState = request.app.state.onboarding
    registry: StepRegistry = request.app.state.setup_registry
    unfinished = tuple(
        step
        for step in registry.steps
        if step.id in {"web_search", "cv_import"} and onboarding.statuses.get(step.id) != "done"
    )
    return {
        "area": area("today"),
        "areas": AREAS,
        "today": service.view(),
        "unfinished": unfinished,
        "session_block": suggestions.block(),
        "session_slot": TODAY_SLOT,
        "focus": focus,
    }


def page(request: Request) -> Response:
    return render(request, page="pages/today.html", fragment=CONTENT, ctx=_context(request))


def content_response(request: Request, *, focus: bool = False) -> Response:
    return templates.TemplateResponse(request, CONTENT, _context(request, focus=focus))


def content(request: Request) -> Response:
    return content_response(request)


async def dismiss_suggestion(request: Request) -> Response:
    """Start the suggestion window now (``POST`` with no fields; the launch token and origin are
    checked before this). Stores only the reset."""
    form = await single_value_form(request, set())
    if isinstance(form, Response):
        return form
    suggestions: SessionSuggestions = request.app.state.session_suggestions
    await run_in_threadpool(suggestions.dismiss)
    if not is_htmx(request):
        return RedirectResponse("/", status_code=303)
    response = await run_in_threadpool(content_response, request, focus=True)
    response.headers["HX-Trigger"] = json.dumps({"announce": DISMISSED})
    return response
