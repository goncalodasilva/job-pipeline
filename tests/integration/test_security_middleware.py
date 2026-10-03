"""Security middleware: host, launch token and origin checks (M0.14, spec 2.9 rules 1-3, ADR-023).

There are no page, fragment or action routes yet, so the fixture adds test-only routes that
count how often they run. A rejected request must never reach a route.
"""

import asyncio
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.testclient import TestClient
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect

from app.api.security import PAGE_TAG, TOKEN_HEADER
from app.config import RuntimeConfig
from app.main import create_app
from tests.fakes.clock import FrozenClock

pytestmark = pytest.mark.integration

START = datetime(2026, 1, 3, 9, 0, 0, tzinfo=UTC)
PORT = 8000
HOST = f"127.0.0.1:{PORT}"
# The counting test page; the app's own placeholder page keeps "/".
PAGE_PATH = "/page-demo"
ORIGIN = f"http://{HOST}"
STATIC_TEXT = "body { color: black; }\n"


@dataclass
class Harness:
    app: FastAPI
    client: TestClient
    token: str
    hits: Counter[str]

    def headers(self, **extra: str) -> dict[str, str]:
        """Token plus matching Origin for the default Host, merged with ``extra``."""
        base = {TOKEN_HEADER: self.token, "origin": ORIGIN}
        base.update(extra)
        return base


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        port=PORT,
        log_level="INFO",
        demo=False,
        setup=False,
        no_browser=False,
    )


def _add_test_routes(app: FastAPI, hits: Counter[str], static_dir: Path) -> None:
    def page() -> HTMLResponse:
        hits["page"] += 1
        return HTMLResponse("<p>page</p>")

    def fragment() -> HTMLResponse:
        hits["fragment"] += 1
        return HTMLResponse("<p>fragment</p>")

    def action() -> JSONResponse:
        hits["action"] += 1
        return JSONResponse({"done": True})

    def api() -> JSONResponse:
        hits["api"] += 1
        return JSONResponse({"items": []})

    async def socket(websocket: WebSocket) -> None:
        hits["ws"] += 1
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    app.add_api_route(PAGE_PATH, page, methods=["GET", "HEAD"], tags=[PAGE_TAG])
    app.add_api_route("/fragments/demo", fragment, methods=["GET"])
    app.add_api_route("/actions/demo", action, methods=["POST", "PUT", "PATCH", "DELETE"])
    app.add_api_route("/api/demo", api, methods=["GET"])
    app.add_api_websocket_route("/ws/demo", socket)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "app.css").write_text(STATIC_TEXT, encoding="utf-8")
    app = create_app(_config(tmp_path), clock=FrozenClock(START))
    hits: Counter[str] = Counter()
    _add_test_routes(app, hits, static_dir)
    with TestClient(app, base_url=f"http://{HOST}") as client:
        yield Harness(app=app, client=client, token=app.state.launch_token, hits=hits)


def _assert_safe_body(response: Any, token: str) -> None:
    """Rejections are short plain text and never echo the token or request headers."""
    assert response.headers["content-type"].startswith("text/plain")
    assert len(response.text) <= 200
    assert token not in response.text
    for echoed in ("evil.example", "X-JP-Token", "x-jp-token", "null", "Referer", "Origin:"):
        assert echoed not in response.text


# Rule 1: Host header (AC-M0.2)

FOREIGN_HOSTS = [
    "evil.example:8000",
    "127.0.0.1:9999",
    "127.0.0.1",
    "localhost",
    "localhost.evil.example:8000",
    "127.0.0.1.evil.example:8000",
    "[::1]:8000",
    "0.0.0.0:8000",
    "192.168.1.10:8000",
    "127.0.0.1:8000:8000",
    "",
]


@pytest.mark.parametrize("host", FOREIGN_HOSTS)
def test_ac_m0_2_foreign_host_rejected(harness: Harness, host: str) -> None:
    for method, path in (("GET", PAGE_PATH), ("GET", "/health"), ("POST", "/actions/demo")):
        response = harness.client.request(
            method, path, headers=harness.headers(host=host, origin=f"http://{host}")
        )
        assert response.status_code == 400, (method, path)
        _assert_safe_body(response, harness.token)
    assert harness.hits["page"] == 0
    assert harness.hits["action"] == 0


def test_ac_m0_2_missing_host_header_rejected(harness: Harness) -> None:
    """httpx always sends Host, so drive the ASGI app directly with a header-less scope."""
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": PAGE_PATH,
        "raw_path": PAGE_PATH.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", PORT),
        "state": {},
    }
    asyncio.run(harness.app(scope, receive, send))

    start = next(m for m in messages if m["type"] == "http.response.start")
    assert start["status"] == 400
    assert harness.hits["page"] == 0


@pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost:8000", "LOCALHOST:8000"])
def test_ac_m0_2_loopback_hosts_allowed(harness: Harness, host: str) -> None:
    page = harness.client.get(PAGE_PATH, headers={"host": host})
    assert page.status_code == 200
    assert harness.hits["page"] == 1

    action = harness.client.post(
        "/actions/demo", headers=harness.headers(host=host, origin=f"http://{host}")
    )
    assert action.status_code == 200
    assert harness.hits["action"] == 1


def test_host_check_runs_before_token_check(harness: Harness) -> None:
    """A foreign Host is a 400 even when the token is right."""
    response = harness.client.get(
        "/fragments/demo",
        headers={"host": "evil.example:8000", TOKEN_HEADER: harness.token, "HX-Request": "true"},
    )
    assert response.status_code == 400
    assert harness.hits["fragment"] == 0


# Rule 2: launch token (AC-M0.3)


def test_ac_m0_3_fragment_without_token_403(harness: Harness) -> None:
    response = harness.client.get("/fragments/demo", headers={"HX-Request": "true"})
    assert response.status_code == 403
    _assert_safe_body(response, harness.token)
    assert harness.hits["fragment"] == 0

    ok = harness.client.get(
        "/fragments/demo", headers={"HX-Request": "true", TOKEN_HEADER: harness.token}
    )
    assert ok.status_code == 200
    assert harness.hits["fragment"] == 1


def test_ac_m0_3_api_get_without_token_403(harness: Harness) -> None:
    response = harness.client.get("/api/demo")
    assert response.status_code == 403
    _assert_safe_body(response, harness.token)
    assert harness.hits["api"] == 0

    ok = harness.client.get("/api/demo", headers={TOKEN_HEADER: harness.token})
    assert ok.status_code == 200
    assert harness.hits["api"] == 1


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_ac_m0_3_post_without_token_403(harness: Harness, method: str) -> None:
    response = harness.client.request(method, "/actions/demo", headers={"origin": ORIGIN})
    assert response.status_code == 403
    _assert_safe_body(response, harness.token)
    assert harness.hits["action"] == 0


def test_post_to_health_without_token_403(harness: Harness) -> None:
    """Only GET/HEAD /health is token-free (ADR-023)."""
    response = harness.client.post("/health", headers={"origin": ORIGIN})
    assert response.status_code == 403


@pytest.mark.parametrize(
    "bad_token",
    ["", "wrong-token-value", "x" * 43],
    ids=["empty", "wrong", "same-length-wrong"],
)
def test_ac_m0_3_wrong_token_403(harness: Harness, bad_token: str) -> None:
    fragment = harness.client.get(
        "/fragments/demo", headers={"HX-Request": "true", TOKEN_HEADER: bad_token}
    )
    action = harness.client.post(
        "/actions/demo", headers={TOKEN_HEADER: bad_token, "origin": ORIGIN}
    )
    assert fragment.status_code == 403
    assert action.status_code == 403
    _assert_safe_body(action, harness.token)
    assert harness.hits["fragment"] == 0
    assert harness.hits["action"] == 0


def test_token_from_another_app_instance_403(harness: Harness, tmp_path: Path) -> None:
    other = create_app(_config(tmp_path / "other"), clock=FrozenClock(START))
    response = harness.client.post(
        "/actions/demo", headers=harness.headers(**{TOKEN_HEADER: other.state.launch_token})
    )
    assert response.status_code == 403
    assert harness.hits["action"] == 0


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_ac_m0_3_post_with_token_and_origin_handled(harness: Harness, method: str) -> None:
    response = harness.client.request(method, "/actions/demo", headers=harness.headers())
    assert response.status_code == 200
    assert response.json() == {"done": True}
    assert harness.hits["action"] == 1


# Rule 3: Origin or Referer on state-changing requests


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.example:8000",
        "http://localhost:8000",  # Host is 127.0.0.1:8000; must match the Host used
        "http://127.0.0.1:9999",
        "http://127.0.0.1",
        "https://127.0.0.1:8000",
        "http://127.0.0.1:8000.evil.example",
        "http://127.0.0.1:8000/",
    ],
)
def test_post_origin_mismatch_403(harness: Harness, origin: str) -> None:
    response = harness.client.post("/actions/demo", headers=harness.headers(origin=origin))
    assert response.status_code == 403
    _assert_safe_body(response, harness.token)
    assert harness.hits["action"] == 0


def test_post_origin_matches_host_case_insensitively(harness: Harness) -> None:
    response = harness.client.post(
        "/actions/demo",
        headers=harness.headers(host="LOCALHOST:8000", origin="http://localhost:8000"),
    )
    assert response.status_code == 200
    assert harness.hits["action"] == 1


@pytest.mark.parametrize(
    "referer",
    [
        "http://127.0.0.1:8000/",
        "http://127.0.0.1:8000/jobs/42?tab=notes#top",
        "http://127.0.0.1:8000",
    ],
)
def test_post_referer_fallback_ok(harness: Harness, referer: str) -> None:
    response = harness.client.post(
        "/actions/demo", headers={TOKEN_HEADER: harness.token, "referer": referer}
    )
    assert response.status_code == 200
    assert harness.hits["action"] == 1


@pytest.mark.parametrize(
    "referer",
    [
        "http://evil.example:8000/",
        "http://localhost:8000/",
        "https://127.0.0.1:8000/",
        "http://127.0.0.1:9999/",
        "http://127.0.0.1:8000.evil.example/",
        "http://127.0.0.1:8000@evil.example/",
        "not a url",
        "",
    ],
)
def test_post_referer_mismatch_403(harness: Harness, referer: str) -> None:
    response = harness.client.post(
        "/actions/demo", headers={TOKEN_HEADER: harness.token, "referer": referer}
    )
    assert response.status_code == 403
    assert harness.hits["action"] == 0


def test_post_wrong_origin_does_not_fall_back_to_referer(harness: Harness) -> None:
    response = harness.client.post(
        "/actions/demo",
        headers=harness.headers(origin="http://evil.example:8000", referer=f"{ORIGIN}/"),
    )
    assert response.status_code == 403
    assert harness.hits["action"] == 0


def test_post_without_origin_or_referer_403(harness: Harness) -> None:
    response = harness.client.post("/actions/demo", headers={TOKEN_HEADER: harness.token})
    assert response.status_code == 403
    _assert_safe_body(response, harness.token)
    assert harness.hits["action"] == 0


def test_origin_null_403(harness: Harness) -> None:
    for headers in (
        harness.headers(origin="null"),
        harness.headers(origin="null", referer=f"{ORIGIN}/"),
    ):
        response = harness.client.post("/actions/demo", headers=headers)
        assert response.status_code == 403
        _assert_safe_body(response, harness.token)
    assert harness.hits["action"] == 0


# Token-free routes (ADR-023)


def test_page_get_without_token_ok(harness: Harness) -> None:
    response = harness.client.get(PAGE_PATH)
    assert response.status_code == 200
    assert response.text == "<p>page</p>"

    head = harness.client.head(PAGE_PATH)
    assert head.status_code == 200
    assert harness.hits["page"] == 2


def test_page_get_needs_no_origin(harness: Harness) -> None:
    """Safe methods skip the origin check, even with a foreign Origin."""
    response = harness.client.get(PAGE_PATH, headers={"origin": "http://evil.example:8000"})
    assert response.status_code == 200


def test_health_and_static_without_token_ok(harness: Harness) -> None:
    health = harness.client.get("/health")
    assert health.status_code == 200
    assert set(health.json()) == {"version", "database", "runner"}

    health_hx = harness.client.get("/health", headers={"HX-Request": "true"})
    assert health_hx.status_code == 200

    static = harness.client.get("/static/app.css")
    assert static.status_code == 200
    assert static.text == STATIC_TEXT

    static_hx = harness.client.get("/static/app.css", headers={"HX-Request": "true"})
    assert static_hx.status_code == 200


def test_lifespan_still_runs_through_middleware(harness: Harness) -> None:
    assert harness.app.state.runner.status == "running"


def test_websocket_refused(harness: Harness) -> None:
    with (
        pytest.raises(WebSocketDisconnect),
        harness.client.websocket_connect(
            "/ws/demo", headers={"host": HOST, TOKEN_HEADER: harness.token, "origin": ORIGIN}
        ) as ws,
    ):
        ws.receive_text()
    assert harness.hits["ws"] == 0


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_generated_api_docs_are_not_served(harness: Harness, path: str) -> None:
    # Page GETs skip the token, so FastAPI's docs would expose the route list token-free.
    without_token = harness.client.get(path)
    with_token = harness.client.get(path, headers={TOKEN_HEADER: harness.token})

    assert without_token.status_code == 403
    assert with_token.status_code == 404


# --- server-side page allowlist (review regressions) ---------------------------------------


def test_untagged_get_without_hx_request_still_needs_token(harness: Harness) -> None:
    # Dropping HX-Request must not make a fragment-only route token-free.
    without_token = harness.client.get("/fragments/demo")
    with_token = harness.client.get("/fragments/demo", headers={TOKEN_HEADER: harness.token})

    assert without_token.status_code == 403
    _assert_safe_body(without_token, harness.token)
    assert with_token.status_code == 200
    assert harness.hits["fragment"] == 1


def test_page_route_with_hx_request_needs_token(harness: Harness) -> None:
    response = harness.client.get(PAGE_PATH, headers={"HX-Request": "true"})

    assert response.status_code == 403
    assert harness.hits["page"] == 0


@pytest.mark.parametrize("path", ["/api", "/api/demo", "/unknown"])
def test_paths_outside_page_routes_need_token(harness: Harness, path: str) -> None:
    response = harness.client.get(path)

    assert response.status_code == 403
    assert harness.hits["api"] == 0


def _raw_request(app: FastAPI, method: str, headers: list[tuple[bytes, bytes]]) -> int:
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    path = "/actions/demo" if method == "POST" else PAGE_PATH
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", PORT),
        "state": {},
    }
    asyncio.run(app(scope, receive, send))
    return int(next(m for m in messages if m["type"] == "http.response.start")["status"])


@pytest.mark.parametrize("duplicated", ["host", "origin", "token"])
def test_repeated_guard_headers_rejected(harness: Harness, duplicated: str) -> None:
    host = (b"host", HOST.encode())
    origin = (b"origin", f"http://{HOST}".encode())
    token = (TOKEN_HEADER.lower().encode(), harness.token.encode())
    headers = [host, origin, token]
    headers.append({"host": host, "origin": origin, "token": token}[duplicated])

    status = _raw_request(harness.app, "POST", headers)

    assert status == (400 if duplicated == "host" else 403)
    assert harness.hits["action"] == 0


def test_token_header_name_is_case_insensitive(harness: Harness) -> None:
    response = harness.client.get("/fragments/demo", headers={TOKEN_HEADER.lower(): harness.token})

    assert response.status_code == 200


def test_port_80_accepts_hosts_without_port(tmp_path: Path) -> None:
    config = RuntimeConfig(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        port=80,
        log_level="INFO",
        demo=False,
        setup=False,
        no_browser=False,
    )
    app = create_app(config, clock=FrozenClock(START))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/health").status_code == 200
        assert client.get("/health", headers={"host": "localhost:80"}).status_code == 200
        assert client.get("/health", headers={"host": "127.0.0.1:8000"}).status_code == 400


# --- protective response headers (spec 2.9 rule 11) -------------------------------------------

PROTECTIVE_HEADERS = {
    "x-frame-options": "DENY",
    "content-security-policy": "frame-ancestors 'none'",
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
}


@pytest.mark.parametrize(
    ("method", "path", "headers", "status"),
    [
        ("GET", PAGE_PATH, {}, 200),
        ("GET", "/health", {}, 200),
        ("GET", "/static/app.css", {}, 200),
        ("GET", "/fragments/demo", {}, 403),
        ("GET", PAGE_PATH, {"host": "evil.example:8000"}, 400),
    ],
    ids=["page", "health", "static", "rejected-403", "rejected-400"],
)
def test_every_response_forbids_framing_and_sniffing(
    harness: Harness, method: str, path: str, headers: dict[str, str], status: int
) -> None:
    response = harness.client.request(method, path, headers=headers)

    assert response.status_code == status
    for name, value in PROTECTIVE_HEADERS.items():
        assert response.headers.get_list(name) == [value], name


# --- page decision follows real dispatch (M0.16 review regressions) --------------------------


def _create(tmp_path: Path) -> FastAPI:
    return create_app(_config(tmp_path), clock=FrozenClock(START))


def test_non_page_get_in_an_included_router_needs_token(tmp_path: Path) -> None:
    from fastapi import APIRouter

    app = _create(tmp_path)
    router = APIRouter()
    router.add_api_route("/included/data", lambda: {"secret": "fictional"}, methods=["GET"])
    app.include_router(router)

    with TestClient(app, base_url=f"http://{HOST}") as client:
        assert client.get("/included/data").status_code == 403
        token = {TOKEN_HEADER: app.state.launch_token}
        assert client.get("/included/data", headers=token).status_code == 200


def test_page_tag_in_another_included_router_is_not_token_free(tmp_path: Path) -> None:
    # Pages must be registered with app.api.pages.register_pages; anything else needs the token.
    from fastapi import APIRouter

    app = _create(tmp_path)
    router = APIRouter()
    router.add_api_route("/elsewhere", lambda: {"page": False}, methods=["GET"], tags=[PAGE_TAG])
    app.include_router(router)

    with TestClient(app, base_url=f"http://{HOST}") as client:
        assert client.get("/elsewhere").status_code == 403


def test_earlier_non_page_route_on_a_page_path_needs_token(tmp_path: Path) -> None:
    # The route dispatch actually picks decides, not "any page route matches".
    from fastapi.routing import APIRoute

    app = _create(tmp_path)
    shadow = APIRoute("/", lambda: {"secret": "fictional"}, methods=["GET"])
    app.router.routes.insert(0, shadow)

    with TestClient(app, base_url=f"http://{HOST}") as client:
        response = client.get("/")
        assert response.status_code == 403
        assert "fictional" not in response.text


def test_every_page_path_dispatches_to_its_page_without_token(tmp_path: Path) -> None:
    from fastapi.routing import APIRoute

    app = _create(tmp_path)
    pages = [
        route
        for route in app.router.routes
        if isinstance(route, APIRoute) and PAGE_TAG in route.tags
    ]
    assert pages, "the app must register at least the root page"
    with TestClient(app, base_url=f"http://{HOST}") as client:
        for route in pages:
            response = client.get(route.path)
            assert response.status_code == 200, route.path
            assert response.headers["content-type"].startswith("text/html"), route.path


def test_included_routers_hold_no_page_routes(tmp_path: Path) -> None:
    # Uses FastAPI's private _IncludedRouter on purpose: an upgrade that changes it fails here.
    from fastapi.routing import APIRoute

    app = _create(tmp_path)
    for route in app.router.routes:
        original = getattr(route, "original_router", None)
        if original is None:
            continue
        for inner in original.routes:
            assert not (isinstance(inner, APIRoute) and PAGE_TAG in inner.tags), inner.path


def test_page_responses_are_never_cached(harness: Harness) -> None:
    response = harness.client.get(PAGE_PATH)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
