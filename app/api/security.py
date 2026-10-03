"""Request guards: host check, launch token and origin check (spec 2.9 rules 1-3; ADR-023).

A pure ASGI middleware, so streamed responses (server-sent events in M2) pass through untouched.

1. Host: exactly ``127.0.0.1:<port>`` or ``localhost:<port>``, else 400 (defeats DNS rebinding).
2. Launch token: ``X-JP-Token`` must match, compared in constant time, else 403. Every route
   needs it by default. Exempt are page loads (GET or HEAD without ``HX-Request`` to a route the
   server registered with ``tags=[PAGE_TAG]``), ``/health`` and ``/static/``. The client can't
   widen the exemption: dropping ``HX-Request`` on any other route still needs the token.
3. Origin: state-changing requests need an ``Origin`` equal to ``http://<Host>``, or, when there
   is no ``Origin``, a ``Referer`` from that origin; else 403.
Every HTTP response, including rejections, forbids framing (clickjacking) and MIME sniffing.
WebSocket connections are refused; the app doesn't use them.
"""

import hmac
import secrets
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

from fastapi.routing import APIRoute
from starlette.routing import BaseRoute, Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

TOKEN_HEADER = "X-JP-Token"  # noqa: S105 - a header name, not a secret
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
PAGE_TAG = "page"  # routes that render a full page and may load without the token
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_PAGE_METHODS = frozenset({"GET", "HEAD"})
_TOKEN_FREE_PATHS = ("/health",)
_TOKEN_FREE_PREFIXES = ("/static/",)
PROTECTIVE_HEADERS = (
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"frame-ancestors 'none'"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
)


def generate_launch_token() -> str:
    """A fresh 32-byte URL-safe token; one per app instance."""
    return secrets.token_urlsafe(32)


def page_route_matcher(routes: Callable[[], Iterable[BaseRoute]]) -> Callable[[Scope], bool]:
    """Return a check: will dispatch hand this request to a route tagged ``PAGE_TAG``?

    Mirrors the router: the first route that fully matches decides. A page route only counts
    when it is that first match, so an earlier non-page route on the same path still needs the
    token. Routes inside included routers are opaque here, so pages must be registered on the
    app itself (``app.api.pages.register_pages``); a page anywhere else needs the token.
    """

    def is_page(scope: Scope) -> bool:
        for route in routes():
            match, _ = route.matches(scope)
            if match is Match.FULL:
                return isinstance(route, APIRoute) and PAGE_TAG in route.tags
        return False

    return is_page


class SecurityMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        port: int,
        launch_token: str,
        is_page: Callable[[Scope], bool],
    ) -> None:
        self._app = app
        hosts = {f"{host}:{port}" for host in LOOPBACK_HOSTS}
        if port == 80:  # browsers leave the default port out of Host and Origin
            hosts.update(LOOPBACK_HOSTS)
        self._allowed_hosts = frozenset(hosts)
        self._token = launch_token.encode("utf-8")
        self._is_page = is_page

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await receive()  # websocket.connect, then refuse
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        send = _with_protective_headers(send)
        headers = _headers(scope)
        host = _single(headers, b"host")
        if host is None or host.lower() not in self._allowed_hosts:
            await _plain(send, 400, "Invalid host.")
            return
        method = scope["method"].upper()
        page = method in _PAGE_METHODS and self._is_page(scope)
        if page:  # pages embed the launch token, so no cache may keep them
            send = _with_header(send, (b"cache-control", b"no-store"))
        if self._needs_token(scope, method, headers, page) and not self._token_ok(headers):
            await _plain(send, 403, "Forbidden.")
            return
        if method not in _SAFE_METHODS and not _same_origin(headers, host):
            await _plain(send, 403, "Forbidden.")
            return
        await self._app(scope, receive, send)

    def _needs_token(
        self, scope: Scope, method: str, headers: dict[bytes, list[bytes]], page: bool
    ) -> bool:
        if method not in _PAGE_METHODS:
            return True
        path = scope["path"]
        if path in _TOKEN_FREE_PATHS or path.startswith(_TOKEN_FREE_PREFIXES):
            return False
        return b"hx-request" in headers or not page

    def _token_ok(self, headers: dict[bytes, list[bytes]]) -> bool:
        values = headers.get(TOKEN_HEADER.lower().encode("latin-1"), [])
        return len(values) == 1 and hmac.compare_digest(values[0], self._token)


def _with_protective_headers(send: Send) -> Send:
    async def send_protected(message: Message) -> None:
        if message["type"] == "http.response.start":
            names = {name for name, _ in PROTECTIVE_HEADERS}
            kept = [(k, v) for k, v in message.get("headers", []) if k.lower() not in names]
            message = {**message, "headers": [*kept, *PROTECTIVE_HEADERS]}
        await send(message)

    return send_protected


def _with_header(send: Send, header: tuple[bytes, bytes]) -> Send:
    async def send_with_header(message: Message) -> None:
        if message["type"] == "http.response.start":
            kept = [(k, v) for k, v in message.get("headers", []) if k.lower() != header[0]]
            message = {**message, "headers": [*kept, header]}
        await send(message)

    return send_with_header


def _headers(scope: Scope) -> dict[bytes, list[bytes]]:
    headers: dict[bytes, list[bytes]] = {}
    for name, value in scope.get("headers", []):
        headers.setdefault(name.lower(), []).append(value)
    return headers


def _single(headers: dict[bytes, list[bytes]], name: bytes) -> str | None:
    """The header's value when it is present exactly once; repeated headers are refused."""
    values = headers.get(name, [])
    if len(values) != 1:
        return None
    return values[0].decode("latin-1")


def _same_origin(headers: dict[bytes, list[bytes]], host: str) -> bool:
    expected = f"http://{host}".lower()
    if b"origin" in headers:
        origin = _single(headers, b"origin")
        return origin is not None and origin.lower() == expected
    referer = _single(headers, b"referer")
    if referer is None:
        return False
    try:
        parts = urlsplit(referer)
    except ValueError:
        return False
    if parts.username is not None or parts.password is not None:
        return False
    return f"{parts.scheme}://{parts.netloc}".lower() == expected


async def _plain(send: Send, status: int, text: str) -> None:
    body = text.encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
