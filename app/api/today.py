"""Today page and protected fragment; GETs only read cached setup and service views."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response

from app.api.pages import AREAS, area
from app.api.security import PAGE_TAG
from app.api.templating import render, templates
from app.onboarding.registry import StepRegistry
from app.onboarding.startup import OnboardingState
from app.services.today import TodayService

CONTENT = "fragments/today/_content.html"


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


def _context(request: Request, *, focus: bool = False) -> dict[str, Any]:
    service: TodayService = request.app.state.today
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
        "focus": focus,
    }


def page(request: Request) -> Response:
    return render(request, page="pages/today.html", fragment=CONTENT, ctx=_context(request))


def content_response(request: Request, *, focus: bool = False) -> Response:
    return templates.TemplateResponse(request, CONTENT, _context(request, focus=focus))


def content(request: Request) -> Response:
    return content_response(request)
