"""Jinja2 templates and the shared page-or-fragment ``render`` helper (htmx-ui skill).

Autoescaping is always on (spec 2.9 rule 4). Full pages carry the launch token in
``<body hx-headers>`` so htmx sends it on every request (spec 2.9 rule 2).
"""

import json
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from starlette.templating import Jinja2Templates

from app.api.security import TOKEN_HEADER

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
    return env


templates = Jinja2Templates(env=_environment())


def render(request: Request, *, page: str, fragment: str, ctx: dict[str, Any]) -> HTMLResponse:
    """Render ``fragment`` for htmx requests (``HX-Request: true``), else the full ``page``.

    A history restore replaces the whole body, so it always gets the full page.
    """
    headers = request.headers
    if headers.get("HX-Request") == "true" and headers.get("HX-History-Restore-Request") != "true":
        return templates.TemplateResponse(request, fragment, ctx)
    hx_headers = json.dumps({TOKEN_HEADER: request.app.state.launch_token})
    return templates.TemplateResponse(request, page, {**ctx, "hx_headers": hx_headers})
