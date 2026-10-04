"""Send every request to the setup guide while setup is incomplete (AC-M1.2; ADR-050).

A pure ASGI middleware added inside ``SecurityMiddleware``, so host, token and origin checks
reject first and every redirect keeps the protective headers. It reads only the cached
decision in ``OnboardingState`` (no database, keychain or file reads per request).

Exempt are exactly ``/setup``, ``/setup/...``, ``/static/...`` and ``/health``. Everything else,
including unknown paths and state-changing requests, is redirected to ``/setup/<step>``: htmx
requests (``HX-Request: true``) get ``200`` with an empty body and ``HX-Redirect`` (htmx
ignores ``HX-Redirect`` on a 3xx); other requests get ``303 See Other``. The query string is
never carried over, and no redirect may be cached (``Cache-Control: no-store``).
"""

from collections.abc import Callable

from starlette.types import ASGIApp, Receive, Scope, Send

from app.onboarding.startup import OnboardingState

SETUP_PATH = "/setup"
_EXEMPT_PATHS = frozenset({SETUP_PATH, "/health"})
_EXEMPT_PREFIXES = (f"{SETUP_PATH}/", "/static/")


def setup_step_path(step_id: str) -> str:
    return f"{SETUP_PATH}/{step_id}"


class SetupRedirectMiddleware:
    def __init__(self, app: ASGIApp, *, onboarding: Callable[[], OnboardingState | None]) -> None:
        self._app = app
        self._onboarding = onboarding

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        target = self._target(scope)
        if target is None:
            await self._app(scope, receive, send)
            return
        if _is_htmx(scope):
            status, header = 200, b"hx-redirect"
        else:
            status, header = 303, b"location"
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (header, target.encode("ascii")),
                    (b"content-length", b"0"),
                    # The target changes as setup progresses, so no cache may keep it.
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    def _target(self, scope: Scope) -> str | None:
        if scope["type"] != "http":
            return None
        path: str = scope["path"]
        if path in _EXEMPT_PATHS or path.startswith(_EXEMPT_PREFIXES):
            return None
        onboarding = self._onboarding()
        if onboarding is None:  # start-up hasn't finished
            return None
        step = onboarding.guide_step()
        return setup_step_path(step) if step is not None else None


def _is_htmx(scope: Scope) -> bool:
    values = [value for name, value in scope.get("headers", []) if name.lower() == b"hx-request"]
    return values == [b"true"]
