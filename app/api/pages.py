"""Full-page routes (spec 2.9 rule 12). M0.16 has a placeholder ``/``; M0.17 adds templates.

Page routes are tagged ``PAGE_TAG`` and registered on the app itself by ``register_pages``,
so the security middleware can see them (ADR-039): they load without the launch token, and
the middleware marks their responses ``Cache-Control: no-store``.
"""

import html
import json

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from app.api.security import PAGE_TAG, TOKEN_HEADER

_PLACEHOLDER = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Job Search Pipeline</title></head>
<body hx-headers="{hx_headers}"><main><h1>Job Search Pipeline</h1></main></body>
</html>
"""


def register_pages(app: FastAPI) -> None:
    """Add every page route directly to the app (not through an included router)."""
    app.add_api_route(
        "/", home, methods=["GET", "HEAD"], tags=[PAGE_TAG], response_class=HTMLResponse
    )


def home(request: Request) -> HTMLResponse:
    hx_headers = json.dumps({TOKEN_HEADER: request.app.state.launch_token})
    return HTMLResponse(_PLACEHOLDER.format(hx_headers=html.escape(hx_headers, quote=True)))
