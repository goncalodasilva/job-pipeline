"""Jinja2 templates and the shared page-or-fragment ``render`` helper (htmx-ui skill).

Autoescaping is always on (spec 2.9 rule 4). Full pages carry the launch token in
``<body hx-headers>`` so htmx sends it on every request (spec 2.9 rule 2), and the banners of
the cached start-up decision (AC-M1.1 case 4; ADR-050 rule 5): one per entry in
``StartupDecision.banners``, read from memory, never from the keychain or database.
"""

import json
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from starlette.templating import Jinja2Templates

from app.api.security import TOKEN_HEADER
from app.domain.contacts import LIMITS as CONTACT_LIMITS
from app.domain.contacts import NOTE_LIMIT
from app.domain.ids import is_safe_id
from app.onboarding.registry import PROVIDER, StepRegistry
from app.onboarding.startup import OnboardingState
from app.services.reminders import Notifications, ReminderService
from app.services.task_views import NONE_RUNNING, TaskViews, TopBarView

_APP_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = _APP_DIR / "templates"
STATIC_DIR = _APP_DIR / "static"
STATIC_URL = "/static"


def static(path: str) -> str:
    """The URL of a file under ``app/static``."""
    return f"{STATIC_URL}/{path}"


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=True,
        undefined=StrictUndefined,
    )
    env.globals["static"] = static
    env.globals["is_safe_id"] = is_safe_id
    env.globals["contact_limits"] = {**CONTACT_LIMITS, "interaction_note": NOTE_LIMIT}
    return env


templates = Jinja2Templates(env=_environment())


def render(request: Request, *, page: str, fragment: str, ctx: dict[str, Any]) -> HTMLResponse:
    """Render ``fragment`` for htmx requests (``HX-Request: true``), else the full ``page``.

    A history restore replaces the whole body, so it always gets the full page. Rendering a
    full app page reads the tasks for the top bar (blocking): a sync route calls this directly,
    an async route through ``run_in_threadpool``.
    """
    if renders_fragment(request):
        return templates.TemplateResponse(request, fragment, ctx)
    hx_headers = json.dumps({TOKEN_HEADER: request.app.state.launch_token})
    shell = {
        "hx_headers": hx_headers,
        "banners": _banners(request),
        "tasks_indicator": _tasks_indicator,
        "notifications": _notifications(request),
    }
    return templates.TemplateResponse(request, page, {**ctx, **shell})


def _notifications(request: Request) -> Notifications:
    service: ReminderService | None = getattr(request.app.state, "reminders", None)
    return service.notifications() if service else Notifications()


def renders_fragment(request: Request) -> bool:
    """Whether ``render`` answers this request with the fragment (else the full page)."""
    headers = request.headers
    return (
        headers.get("HX-Request") == "true" and headers.get("HX-History-Restore-Request") != "true"
    )


def _tasks_indicator(request: Request) -> TopBarView:
    """The top bar's count of running and queued tasks (M2.20) and the keys of the tasks
    that may need attention (M2.26): one read of the tasks, made only when a page with the
    app's top bar is rendered (the setup guide's never asks). Blocking, like ``render``
    itself for a full page: async routes call ``render`` in a worker thread."""
    views: TaskViews | None = getattr(request.app.state, "task_views", None)
    return views.top_bar() if views is not None else TopBarView(NONE_RUNNING)


def _banners(request: Request) -> list[dict[str, str]]:
    """The cached decision's banners in order (no I/O). Each has the step it links to, its
    ``kind`` ("provider" or "step") and, for the provider, the cached health code."""
    state = request.app.state
    onboarding: OnboardingState = state.onboarding
    registry: StepRegistry = state.setup_registry
    health = state.provider_health  # cached ProviderHealth, None before a workspace opens
    found: list[dict[str, str]] = []
    for step_id in onboarding.decision.banners:
        step = registry.get(step_id)
        is_provider = step.id == PROVIDER.id
        cause = str(health.status) if is_provider and health is not None else ""
        found.append(
            {
                "step": step.id,
                "title": step.title,
                "kind": "provider" if is_provider else "step",
                "cause": cause,
            }
        )
    return found
