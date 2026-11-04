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
from starlette.websockets import WebSocketDisconnect

from app.api.security import PAGE_TAG, TOKEN_HEADER
from app.config import RuntimeConfig
from app.main import create_app
from tests.fact_bank import seed_page_ids
from tests.fakes.clock import FrozenClock
from tests.setup_harness import complete_setup

pytestmark = pytest.mark.integration

START = datetime(2026, 1, 3, 9, 0, 0, tzinfo=UTC)
PORT = 8000
HOST = f"127.0.0.1:{PORT}"
# The counting test page; the app's own placeholder page keeps "/".
PAGE_PATH = "/page-demo"
ORIGIN = f"http://{HOST}"
# A real vendored file under the app's own /static mount (M0.17); /static/ is token-free.
STATIC_PATH = "/static/vendor/htmx.min.js"
STATIC_FILE = Path(__file__).resolve().parents[2] / "app" / "static" / "vendor" / "htmx.min.js"


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


def _add_test_routes(app: FastAPI, hits: Counter[str]) -> None:
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


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    complete_setup(tmp_path / "data", tmp_path / "cfg", clock=FrozenClock(START))  # AC-M1.2
    app = create_app(_config(tmp_path), clock=FrozenClock(START))
    hits: Counter[str] = Counter()
    _add_test_routes(app, hits)
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

    static = harness.client.get(STATIC_PATH)
    assert static.status_code == 200
    assert static.content == STATIC_FILE.read_bytes()

    static_hx = harness.client.get(STATIC_PATH, headers={"HX-Request": "true"})
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
        ("GET", STATIC_PATH, {}, 200),
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
    complete_setup(tmp_path / "data", tmp_path / "cfg", clock=FrozenClock(START))  # AC-M1.2
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
    # Fill path parameters with a value the page accepts (a registered setup step; for the
    # fact pages, a fictional typed fact and a fictional draft made by the app's own service).
    # M9.3 part B: ``task`` is the prompt shell page's parameter (an allow-listed task name).
    known_params = {"step_id": "welcome", "task": "summarize_job"}
    visited: dict[str, str] = {}
    with TestClient(app, base_url=f"http://{HOST}") as client:
        known_params.update(seed_page_ids(app.state.facts))
        from app.modules.storage import sessions_repo

        with app.state.workspace.db.transaction() as conn:
            sessions_repo.insert_session(
                conn, id="session-page", focus_kind="most_useful", length=3, now=START.isoformat()
            )
        known_params["sid"] = "session-page"
        from tests.job_fixtures import generate_jobs, seed_jobs

        seed_jobs(app.state.workspace.db, generate_jobs(1))
        known_params["job_id"] = "job-0001"
        known_params["company_id"] = "company-0001"  # made by the job fixtures (M4.10)
        known_params["application_id"] = app.state.applications.apply_to_job(
            "job-0001", expected_job_version=1
        ).application.id
        # M8.3 part A (ADR-124 item 11): a seeded interview for the debrief page's {event_id}.
        from app.modules.storage import applications_repo

        with app.state.workspace.db.transaction() as conn:
            applications_repo.insert_event(
                conn,
                id="page-debrief-event",
                application_id=known_params["application_id"],
                type="interview",
                starts_at="2026-01-02T09:00:00+00:00",
                ends_at="2026-01-02T10:00:00+00:00",
                time_zone="UTC",
                now=START.isoformat(),
            )
        known_params["event_id"] = "page-debrief-event"
        for route in pages:
            path = route.path
            for name in route.param_convertors:
                assert name in known_params, f"no value for {{{name}}} in {route.path}"
                path = path.replace(f"{{{name}}}", known_params[name])
            assert "{" not in path, path
            response = client.get(path)
            assert response.status_code == 200, route.path
            assert response.headers["content-type"].startswith("text/html"), route.path
            visited[route.path] = path
    assert visited["/facts/{fact_id}/edit"] == f"/facts/{known_params['fact_id']}/edit"
    assert visited["/facts/{draft_id}/merge"] == f"/facts/{known_params['draft_id']}/merge"
    assert visited["/settings/prompts/{task}"] == "/settings/prompts/summarize_job"
    assert visited["/jobs/{job_id}"] == "/jobs/job-0001"
    assert visited["/companies/{company_id}"] == "/companies/company-0001"
    assert visited["/applications/{application_id}/debriefs/{event_id}"] == (
        f"/applications/{known_params['application_id']}/debriefs/page-debrief-event"
    )


# --- the fact pages' addresses (M3.6): one safe segment, nothing else exempted ----------------


class _Untouchable:
    """Stands in for a service that must not be reached."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"looked up {name} for an id of another shape")


def test_fact_pages_need_token_with_hx_request(tmp_path: Path) -> None:
    app = _create(tmp_path)
    with TestClient(app, base_url=f"http://{HOST}") as client:
        ids = seed_page_ids(app.state.facts)
        token = {TOKEN_HEADER: app.state.launch_token}
        for path in (f"/facts/{ids['fact_id']}/edit", f"/facts/{ids['draft_id']}/merge"):
            for method in ("GET", "HEAD"):
                assert client.request(method, path).status_code == 200, (method, path)
                bare = client.request(method, path, headers={"HX-Request": "true"})
                assert bare.status_code == 403, (method, path)
            fragment = client.get(path, headers={"HX-Request": "true", **token})
            assert fragment.status_code == 200, path
            assert "<html" not in fragment.text, path


def test_fact_page_routes_exempt_no_other_path(tmp_path: Path) -> None:
    """Only ``/facts/<one segment>/edit`` and ``/merge`` load without the token: not a longer
    address ending the same way, not the routes beside them, not a POST to the same path."""
    app = _create(tmp_path)
    with TestClient(app, base_url=f"http://{HOST}", follow_redirects=False) as client:
        ids = seed_page_ids(app.state.facts)
        fact, draft = ids["fact_id"], ids["draft_id"]
        token = {TOKEN_HEADER: app.state.launch_token}
        not_pages = [
            "/facts/a/b/edit",
            "/facts/a/b/merge",
            f"/facts/{fact}/x/edit",
            f"/facts/x/{fact}/edit",
            f"/facts/{draft}/x/merge",
            f"/facts/a%2F{fact}/edit",
            f"/facts/{fact}/edit/",
            f"/facts/{draft}/merge/",
            f"/facts/{fact}/edit/history",
            f"/facts/{fact}/history",
            "/facts/x/history",
            f"/facts/{fact}/archive",
            f"/facts/{fact}/restore",
            f"/facts/{draft}/undo-merge",
            "/facts//edit",
        ]
        for path in not_pages:
            for method in ("GET", "HEAD"):
                bare = client.request(method, path)
                assert bare.status_code == 403, (method, path, bare.status_code)
            _assert_safe_body(client.get(path), app.state.launch_token)
            with_token = client.get(path, headers=token)
            # With the token the request is dispatched; only the history fragment is a GET.
            expected = (200,) if path == f"/facts/{fact}/history" else (307, 404, 405)
            assert with_token.status_code in expected, (path, with_token.status_code)
        for path in (f"/facts/{fact}/edit", f"/facts/{draft}/merge", "/facts/a/b/edit"):
            for headers in ({}, {"origin": ORIGIN}, token, {"HX-Request": "true"}):
                posted = client.post(path, data={"version": "1"}, headers=headers)
                assert posted.status_code == 403, (path, headers.keys(), posted.status_code)
        for path in (f"/facts/{fact}/edit", f"/facts/{fact}/archive"):
            assert client.options(path).status_code == 403, path
        # As observed: an address that ends in a newline is not exempt either.
        for path in (f"/facts/{fact}/history%0A", f"/facts/{fact}/archive%0A"):
            assert client.get(path).status_code == 403, path


@pytest.mark.parametrize("page", ["edit", "merge"])
@pytest.mark.parametrize(
    "bad_id",
    ["a.b", "a%20b", "..%2E", "%2E%2E", "a;b", "a%00b", "id%0A", "caf%C3%A9", "x" * 65],
    ids=["dot", "space", "dots", "dot-dot", "semicolon", "nul", "newline", "non-ascii", "long"],
)
def test_fact_page_id_of_another_shape_is_404_before_any_lookup(
    tmp_path: Path, page: str, bad_id: str
) -> None:
    app = _create(tmp_path)
    with TestClient(app, base_url=f"http://{HOST}") as client:
        unknown = client.get(f"/facts/no-such-fact/{page}")
        assert unknown.status_code == 404
        app.state.fact_editor = app.state.fact_merge = app.state.fact_history = _Untouchable()
        app.state.facts = _Untouchable()

        for headers in ({}, {TOKEN_HEADER: app.state.launch_token, "HX-Request": "true"}):
            response = client.get(f"/facts/{bad_id}/{page}", headers=headers)
            # The page is reached (its route matches one segment) and answers the fixed text.
            assert response.status_code == 404, response.status_code
            assert response.text == unknown.text
            assert response.headers["content-type"].startswith("text/plain")
        bare = client.get(f"/facts/{bad_id}/{page}", headers={"HX-Request": "true"})
        assert bare.status_code == 403


# As sent in an address, and as a form or query value.
ODD_IDS = [
    ("a.b", "a.b"),
    ("a%20b", "a b"),
    ("%2E%2E", ".."),
    ("a/b", "a/b"),
    ("id%0A", "id\n"),
    ("caf%C3%A9", "café"),
    ("x" * 65, "x" * 65),
]
ODD_ID_NAMES = ["dot", "space", "dot-dot", "slash", "newline", "non-ascii", "long"]
FACT_POSTS = ("edit", "archive", "restore", "merge", "undo-merge")
DRAFT_POSTS = ("confirm", "discard", "undo-discard", "confirm-edited")
POSTED = {
    "version": "1",
    "draft_version": "1",
    "target_version": "1",
    "target": "no-such-fact",
    "wording": "target",
    "type": "fact",
    "text": "Fictional wording.",
}


def _no_services(app: FastAPI) -> None:
    app.state.fact_editor = app.state.fact_merge = app.state.fact_history = _Untouchable()
    app.state.facts = app.state.fact_views = _Untouchable()


# What the router itself answers when no route matches, or only one for another method (a
# POST to a page's address): fixed as well.
NO_ROUTE = {404: '{"detail":"Not Found"}', 405: '{"detail":"Method Not Allowed"}'}


def _assert_fixed_404(
    response: Any, unknown: Any, sent: tuple[str, ...], where: object, *, routed: bool = True
) -> None:
    """The unknown fact's own answer; with ``routed`` false, the router's (no handler ran)."""
    if routed:
        assert response.status_code == 404, (where, response.status_code)
        assert response.text == unknown.text, where
        assert response.headers["content-type"].startswith("text/plain"), where
    else:
        expected = 405 if where in ("edit", "merge") else 404
        assert response.status_code == expected, (where, response.status_code)
        assert response.text == NO_ROUTE[expected], where
    for text in sent:
        assert text not in response.text, where


@pytest.mark.parametrize(("in_path", "raw"), ODD_IDS, ids=ODD_ID_NAMES)
def test_fact_action_id_of_another_shape_is_404_before_any_service(
    tmp_path: Path, in_path: str, raw: str
) -> None:
    """The history fragment and every POST that names a fact in its address (the editor's, the
    merge screen's and the draft actions) answer an id that isn't of the safe shape as they
    answer an unknown fact, without reaching a service. As observed, an id with a newline in
    it matches none of these routes (their ``{fact_id:path}`` stops at a newline), so the
    router answers its own fixed 404 (405 where only the page's GET matches): no handler
    runs at all."""
    app = _create(tmp_path)
    with TestClient(app, base_url=f"http://{HOST}", follow_redirects=False) as client:
        token = {TOKEN_HEADER: app.state.launch_token}
        posting = {**token, "origin": ORIGIN}
        unknown = client.get("/facts/no-such-fact/history", headers=token)
        assert unknown.status_code == 404
        _no_services(app)
        routed = "\n" not in raw

        for hx in ({}, {"HX-Request": "true"}):
            history = client.get(f"/facts/{in_path}/history", headers={**token, **hx})
            _assert_fixed_404(history, unknown, (in_path, raw), "history", routed=routed)
            for action in FACT_POSTS + DRAFT_POSTS:
                path = f"/facts/{in_path}/{action}"
                posted = client.post(path, data=POSTED, headers={**posting, **hx})
                _assert_fixed_404(posted, unknown, (in_path, raw), action, routed=routed)
                assert client.post(path, data=POSTED, headers=hx).status_code == 403, action


@pytest.mark.parametrize(("in_path", "raw"), ODD_IDS, ids=ODD_ID_NAMES)
def test_merge_post_target_of_another_shape_is_404_before_any_service(
    tmp_path: Path, in_path: str, raw: str
) -> None:
    app = _create(tmp_path)
    with TestClient(app, base_url=f"http://{HOST}", follow_redirects=False) as client:
        token = {TOKEN_HEADER: app.state.launch_token}
        unknown = client.get("/facts/no-such-fact/history", headers=token)
        _no_services(app)

        for hx in ({}, {"HX-Request": "true"}):
            posted = client.post(
                "/facts/some-draft/merge",
                data={**POSTED, "target": raw},
                headers={**token, "origin": ORIGIN, **hx},
            )
            _assert_fixed_404(posted, unknown, (raw,), hx)


@pytest.mark.parametrize(("in_path", "raw"), ODD_IDS, ids=ODD_ID_NAMES)
def test_merge_page_target_of_another_shape_is_an_unknown_one(
    tmp_path: Path, in_path: str, raw: str
) -> None:
    """``?target=`` of another shape: the search, with the note an unknown target gets, and
    nothing of the request."""
    app = _create(tmp_path)
    with TestClient(app, base_url=f"http://{HOST}") as client:
        draft = seed_page_ids(app.state.facts)["draft_id"]
        path = f"/facts/{draft}/merge"
        unknown = client.get(path, params={"target": "no-such-fact"})
        plain = client.get(path)
        assert unknown.status_code == plain.status_code == 200
        assert unknown.text != plain.text, "an unknown target has its note"

        response = client.get(path, params={"target": raw})

        assert response.status_code == 200
        assert response.text == unknown.text, "word for word the page made without it"


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


def test_fragment_action_and_api_responses_are_never_cached(harness: Harness) -> None:
    """Fragments and answers hold the user's own text, so no cache may keep them either."""
    served = {
        "fragment": harness.client.get(
            "/fragments/demo", headers={TOKEN_HEADER: harness.token, "HX-Request": "true"}
        ),
        "action": harness.client.post("/actions/demo", headers=harness.headers()),
        "api": harness.client.get("/api/demo", headers={TOKEN_HEADER: harness.token}),
        "health": harness.client.get("/health"),
        "not found": harness.client.get("/no-such-route", headers={TOKEN_HEADER: harness.token}),
    }
    for name, response in served.items():
        assert response.status_code in (200, 404), (name, response.status_code)
        assert response.headers.get_list("cache-control") == ["no-store"], name
    assert harness.hits["fragment"] == harness.hits["action"] == harness.hits["api"] == 1


def test_rejections_are_never_cached(harness: Harness) -> None:
    rejected = {
        "bad host": harness.client.get(PAGE_PATH, headers={"host": "evil.example"}),
        "no token": harness.client.get("/fragments/demo"),
        "other origin": harness.client.post(
            "/actions/demo", headers=harness.headers(origin="http://evil.example")
        ),
    }
    for name, response in rejected.items():
        assert response.status_code in (400, 403), (name, response.status_code)
        assert response.headers.get_list("cache-control") == ["no-store"], name
    assert not harness.hits


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_static_files_are_not_marked_no_store(harness: Harness, method: str) -> None:
    """Static files hold nothing of the user's and may be cached."""
    response = harness.client.request(method, STATIC_PATH)

    assert response.status_code == 200
    assert "no-store" not in response.headers.get("cache-control", "")
    assert response.headers["x-content-type-options"] == "nosniff", "the other headers stay"
