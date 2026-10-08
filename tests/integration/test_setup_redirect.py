"""Setup redirect, guide page shell and the completed-setup harness (M1.9; AC-M1.2).

AC-M1.2: while setup is incomplete, every route except the guide (``/setup``,
``/setup/...``), static files and ``/health`` redirects to the guide. The redirect is a pure
ASGI middleware running inside the security middleware (spec 2.9 rules 1-3 still reject first,
and rule 11's protective headers stay on), reading only the cached start-up decision in
``app.state.onboarding`` (no database, keychain or file reads per request). Plain requests get
``303`` with ``Location``; htmx requests (``HX-Request: true``) get ``200`` with an empty body
and ``HX-Redirect``, since htmx ignores ``HX-Redirect`` on a 3xx.

Guide pages are page routes registered on the app itself (``tags=[PAGE_TAG]``, ADR-039).
A "fresh" app is a first run (source ``default``, nothing created) or an explicit folder
(source ``cli``, a new database with no steps done); both are incomplete. Every folder lives
under ``tmp_path`` and every value is fictional.
"""

from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from httpx import Response
from starlette.routing import BaseRoute, Mount, Route

from app import __version__
from app.api.security import PAGE_TAG, TOKEN_HEADER
from app.config import RuntimeConfig, write_bootstrap
from app.main import create_app
from app.modules.storage import Database
from app.onboarding.detection import StartupDecision
from app.onboarding.progress import SetupProgress
from app.onboarding.registry import m1_registry
from app.services.setup_state import SetupStateService
from tests.fakes.clock import FrozenClock
from tests.page_token import page_token
from tests.setup_harness import complete_setup, seed_workspace

pytestmark = pytest.mark.integration

START = datetime(2026, 1, 3, 9, 0, 0, tzinfo=UTC)
PORT = 8000
HOST = f"127.0.0.1:{PORT}"
BASE_URL = f"http://{HOST}"  # must pass the host check (spec 2.9 rule 1)
ORIGIN = BASE_URL
WELCOME = "/setup/welcome"
PAGE_PATHS = ("/", "/inbox", "/board", "/facts", "/companies", "/settings")
STATIC_PATH = "/static/vendor/htmx.min.js"
ACTION_PATH = "/actions/fictional-probe"
FRESH_SOURCES = ("default", "cli")
STEP_TITLES = [(step.id, step.title) for step in m1_registry().steps]
PROTECTIVE_HEADERS = {
    "x-frame-options": "DENY",
    "content-security-policy": "frame-ancestors 'none'",
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
}


# --- helpers -------------------------------------------------------------------------------


@dataclass
class Running:
    app: FastAPI
    client: TestClient
    hits: Counter[str]

    @property
    def token(self) -> str:
        token: str = self.app.state.launch_token
        return token

    def with_token(self, **extra: str) -> dict[str, str]:
        return {TOKEN_HEADER: self.token, **extra}

    def htmx(self, **extra: str) -> dict[str, str]:
        return self.with_token(**{"HX-Request": "true", **extra})

    def action(self, **extra: str) -> dict[str, str]:
        """Token and a matching Origin, as htmx sends for a state-changing request."""
        return self.with_token(**{"origin": ORIGIN, **extra})

    @property
    def decision(self) -> StartupDecision:
        decision: StartupDecision = self.app.state.onboarding.decision
        return decision


def _config(
    tmp_path: Path, source: str, *, data_dir: Path | None = None, setup: bool = False
) -> RuntimeConfig:
    return RuntimeConfig(
        data_dir=data_dir if data_dir is not None else tmp_path / "data",
        config_dir=tmp_path / "cfg",
        port=PORT,
        log_level="INFO",
        demo=source == "demo",
        setup=setup,
        no_browser=False,
        data_dir_source=source,  # type: ignore[arg-type]
    )


@contextmanager
def running(
    tmp_path: Path, source: str = "default", *, data_dir: Path | None = None, setup: bool = False
) -> Iterator[Running]:
    """Start the app (lifespan included) with a counting test action route."""
    app = create_app(
        _config(tmp_path, source, data_dir=data_dir, setup=setup), clock=FrozenClock(START)
    )
    hits: Counter[str] = Counter()

    def action() -> JSONResponse:
        hits["action"] += 1
        return JSONResponse({"done": True})

    app.add_api_route(ACTION_PATH, action, methods=["POST", "PUT", "PATCH", "DELETE"])
    with TestClient(app, base_url=BASE_URL, follow_redirects=False) as client:
        yield Running(app=app, client=client, hits=hits)


def allow_harness_fake_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """The harness saves the fake provider; the default wiring reads the process environment,
    so allowing fakes keeps a completed setup's provider healthy (M1.15; ADR-054)."""
    monkeypatch.setenv("JOBPIPELINE_ALLOW_FAKE_PROVIDERS", "1")


def _redirect_target(location: str) -> str:
    """The path a Location points to; an absolute URL must stay on this origin."""
    if location.startswith(BASE_URL):
        return location[len(BASE_URL) :]
    assert location.startswith("/") and not location.startswith("//"), location
    return location


def assert_see_other(response: Response, target: str) -> None:
    assert response.status_code == 303, (
        response.request.method,
        response.request.url.path,
        response.status_code,
        response.text[:200],
    )
    assert _redirect_target(response.headers["location"]) == target
    assert "hx-redirect" not in response.headers


def assert_hx_redirect(response: Response, target: str) -> None:
    assert response.status_code == 200, (
        response.request.method,
        response.request.url.path,
        response.status_code,
        response.text[:200],
    )
    assert response.content == b"", "an htmx redirect has an empty body"
    assert _redirect_target(response.headers["hx-redirect"]) == target
    assert "location" not in response.headers


def assert_not_redirected(response: Response) -> None:
    assert "location" not in response.headers, response.headers.get("location")
    assert "hx-redirect" not in response.headers, response.headers.get("hx-redirect")


def _fill_path_params(path: str) -> str:
    """Replace ``{name}`` and ``{name:converter}`` with a fictional value."""
    filled = []
    rest = path
    while "{" in rest:
        before, _, after = rest.partition("{")
        _, _, rest = after.partition("}")
        filled.append(before + "1")
    filled.append(rest)
    return "".join(filled)


def _walk_routes(routes: Iterable[BaseRoute], prefix: str = "") -> Iterator[tuple[str, set[str]]]:
    """Every route's full path and methods, including routes inside included routers.

    FastAPI wraps an included router in a private object holding ``original_router`` and an
    ``include_context`` with its ``prefix``; this walk fails loudly if that shape changes, and
    on any route type it doesn't know, so no new route escapes the check.
    """
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            inner_prefix = getattr(getattr(route, "include_context", None), "prefix", None)
            assert isinstance(inner_prefix, str), f"unknown included-router shape: {route!r}"
            yield from _walk_routes(original.routes, prefix + inner_prefix)
            continue
        if isinstance(route, Mount) and route.name == "static":
            continue
        assert isinstance(route, Route), f"unknown route type {type(route).__name__}"
        yield prefix + route.path, set(route.methods or {"GET"})


def _resume_folders(tmp_path: Path, done: tuple[str, ...]) -> Path:
    """A bootstrap config naming a data folder whose database has ``done`` steps."""
    data_dir = tmp_path / "data"
    write_bootstrap(tmp_path / "cfg", data_dir=data_dir, version=__version__)
    seed_workspace(data_dir, done=done, clock=FrozenClock(START))
    return data_dir


def _progress(app: FastAPI) -> SetupProgress:
    db: Database = app.state.workspace.db
    return SetupProgress(app.state.setup_registry, SetupStateService(db, FrozenClock(START)))


# --- a small HTML outline ------------------------------------------------------------------

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source"}


@dataclass
class Element:
    tag: str
    attrs: dict[str, str | None]
    parent: "Element | None"
    parts: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join("".join(self.parts).split())

    def within(self, other: "Element") -> bool:
        node = self.parent
        while node is not None:
            if node is other:
                return True
            node = node.parent
        return False


class _Outline(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[Element] = []
        self._open: list[Element] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = Element(tag, dict(attrs), self._open[-1] if self._open else None)
        self.elements.append(element)
        if tag not in _VOID:
            self._open.append(element)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index].tag == tag:
                del self._open[index:]
                return

    def handle_data(self, data: str) -> None:
        for element in self._open:
            element.parts.append(data)


def outline(page: str) -> list[Element]:
    parser = _Outline()
    parser.feed(page)
    parser.close()
    return parser.elements


# --- AC-M1.2: redirects while setup is incomplete ------------------------------------------


@pytest.mark.parametrize("source", FRESH_SOURCES)
def test_ac_m1_2_page_redirects_to_guide_303(tmp_path: Path, source: str) -> None:
    with running(tmp_path, source) as run:
        for path in PAGE_PATHS:
            assert_see_other(run.client.get(path), WELCOME)
            assert_see_other(run.client.head(path), WELCOME)
        # A query string is neither kept nor echoed; the target is always the guide step.
        assert_see_other(run.client.get("/inbox?next=%2Fboard&q=fictional"), WELCOME)
        # A page load that happens to carry the token is redirected the same way.
        assert_see_other(run.client.get("/board", headers=run.with_token()), WELCOME)


@pytest.mark.parametrize("source", FRESH_SOURCES)
def test_ac_m1_2_htmx_request_gets_hx_redirect(tmp_path: Path, source: str) -> None:
    with running(tmp_path, source) as run:
        for path in PAGE_PATHS:
            assert_hx_redirect(run.client.get(path, headers=run.htmx()), WELCOME)
        assert_hx_redirect(run.client.post(ACTION_PATH, headers=run.htmx(origin=ORIGIN)), WELCOME)
        assert run.hits["action"] == 0, "the redirected request must not reach the route"

        # Only the exact value "true" marks an htmx request.
        assert_see_other(
            run.client.get("/inbox", headers=run.htmx(**{"HX-Request": "false"})), WELCOME
        )


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("source", FRESH_SOURCES)
def test_ac_m1_2_post_redirects_303(tmp_path: Path, source: str, method: str) -> None:
    with running(tmp_path, source) as run:
        response = run.client.request(method, ACTION_PATH, headers=run.action())
        assert_see_other(response, WELCOME)

        # A path with no route at all is redirected too, not a 404 or 405.
        unknown = run.client.request(method, "/fictional/unrouted", headers=run.action())
        assert_see_other(unknown, WELCOME)
        page_path = run.client.request(method, "/inbox", headers=run.action())
        assert_see_other(page_path, WELCOME)

    assert run.hits["action"] == 0, "the redirected request must not reach the route"


@pytest.mark.parametrize("source", FRESH_SOURCES)
def test_ac_m1_2_guide_static_and_health_exempt(tmp_path: Path, source: str) -> None:
    with running(tmp_path, source) as run:
        guide = run.client.get(WELCOME)
        assert guide.status_code == 200, guide.text[:200]
        assert guide.headers["content-type"].startswith("text/html")
        assert_not_redirected(guide)

        fragment = run.client.get(WELCOME, headers=run.htmx())
        assert fragment.status_code == 200, fragment.text[:200]
        assert_not_redirected(fragment)

        static = run.client.get(STATIC_PATH)
        assert static.status_code == 200
        assert "javascript" in static.headers["content-type"]
        assert_not_redirected(static)
        assert run.client.get(STATIC_PATH, headers={"HX-Request": "true"}).status_code == 200

        health = run.client.get("/health")
        assert health.status_code == 200
        assert set(health.json()) == {"version", "database", "runner"}
        assert_not_redirected(health)

        # The exemptions are exact: look-alike paths are redirected (with the token, so the
        # security checks let them through).
        for look_alike in ("/setupx", "/setup-evil", "/healthz", "/health/x", "/staticx"):
            assert_see_other(run.client.get(look_alike, headers=run.with_token()), WELCOME)


@pytest.mark.parametrize("source", FRESH_SOURCES)
def test_ac_m1_2_every_registered_route_redirects_while_incomplete(
    tmp_path: Path, source: str
) -> None:
    checked: list[str] = []
    exempt: list[str] = []
    with running(tmp_path, source) as run:
        for path, methods in _walk_routes(run.app.router.routes):
            if path == "/health" or path == "/setup" or path.startswith("/setup/"):
                exempt.append(path)
                continue
            url = _fill_path_params(path)
            for method in sorted(methods):
                safe = method in ("GET", "HEAD", "OPTIONS")
                headers = run.with_token() if safe else run.action()
                assert_see_other(run.client.request(method, url, headers=headers), WELCOME)
                if method != "HEAD":
                    htmx = run.client.request(
                        method, url, headers={**headers, "HX-Request": "true"}
                    )
                    assert_hx_redirect(htmx, WELCOME)
                checked.append(f"{method} {path}")

    for page in PAGE_PATHS:
        assert f"GET {page}" in checked, f"page {page} was not walked"
    assert f"POST {ACTION_PATH}" in checked
    assert "/health" in exempt, "routes inside included routers are walked too"
    assert run.hits["action"] == 0


def test_no_redirect_once_setup_complete(tmp_path: Path) -> None:
    complete_setup(tmp_path / "data", tmp_path / "cfg", clock=FrozenClock(START))

    with running(tmp_path, "bootstrap") as run:
        for path in PAGE_PATHS:
            page = run.client.get(path)
            assert page.status_code == 200, (path, page.status_code)
            assert_not_redirected(page)
        fragment = run.client.get("/inbox", headers=run.htmx())
        assert fragment.status_code == 200
        assert_not_redirected(fragment)
        action = run.client.post(ACTION_PATH, headers=run.action())
        assert action.status_code == 200
        assert run.hits["action"] == 1

        # A broken provider after setup is a banner, never the wizard (AC-M1.1 case 4).
        decision = run.app.state.onboarding.refresh(_progress(run.app), provider_healthy=False)
        assert decision.kind == "app_with_banner"
        banner_page = run.client.get("/")
        assert banner_page.status_code == 200
        assert_not_redirected(banner_page)


def test_redirect_runs_after_security_checks(tmp_path: Path) -> None:
    with running(tmp_path, "default") as run:
        # The redirect is active in this app...
        assert_see_other(run.client.get("/"), WELCOME)

        # ...but every security rejection comes first and is never turned into a redirect.
        foreign = run.client.get("/", headers={"host": "evil.example:8000"})
        assert foreign.status_code == 400
        assert_not_redirected(foreign)
        foreign_guide = run.client.get(WELCOME, headers={"host": "evil.example:8000"})
        assert foreign_guide.status_code == 400

        no_token_fragment = run.client.get("/inbox", headers={"HX-Request": "true"})
        assert no_token_fragment.status_code == 403
        assert_not_redirected(no_token_fragment)

        no_token_api = run.client.get("/api/fictional")
        assert no_token_api.status_code == 403
        assert_not_redirected(no_token_api)

        bad_origin = run.client.post(ACTION_PATH, headers=run.action(origin="http://evil.example"))
        assert bad_origin.status_code == 403
        assert_not_redirected(bad_origin)

        no_token_post = run.client.post(ACTION_PATH, headers={"origin": ORIGIN})
        assert no_token_post.status_code == 403
        assert_not_redirected(no_token_post)

        no_origin_post = run.client.post(ACTION_PATH, headers=run.with_token())
        assert no_origin_post.status_code == 403
        assert_not_redirected(no_origin_post)

        guide_fragment_without_token = run.client.get(WELCOME, headers={"HX-Request": "true"})
        assert guide_fragment_without_token.status_code == 403

    assert run.hits["action"] == 0


def test_redirect_response_has_protective_headers(tmp_path: Path) -> None:
    with running(tmp_path, "default") as run:
        responses = [
            run.client.get("/"),
            run.client.get("/inbox", headers=run.htmx()),
            run.client.post(ACTION_PATH, headers=run.action()),
        ]
        assert_see_other(responses[0], WELCOME)
        assert_hx_redirect(responses[1], WELCOME)
        assert_see_other(responses[2], WELCOME)
        for response in responses:
            for name, value in PROTECTIVE_HEADERS.items():
                assert response.headers.get_list(name) == [value], (response.request.url, name)
            # The target changes as setup progresses, so no cache may keep a redirect.
            assert response.headers.get_list("cache-control") == ["no-store"], response.request.url
            assert run.token not in response.text
            assert all(run.token not in value for value in response.headers.values())


def test_guide_pages_are_page_tagged_and_registered_on_app(tmp_path: Path) -> None:
    with running(tmp_path, "default") as run:
        # Only the GET pages: step submits share the path but are POST routes behind the
        # token and origin checks, never page-tagged (M1.16; ADR-053).
        setup_routes = {
            route.path: route
            for route in run.app.router.routes
            if isinstance(route, APIRoute)
            and route.path.startswith("/setup")
            and "GET" in route.methods
        }
        assert {"/setup", "/setup/{step_id}"} <= set(setup_routes), sorted(setup_routes)
        for path in ("/setup", "/setup/{step_id}"):
            assert PAGE_TAG in setup_routes[path].tags, path
            assert "GET" in setup_routes[path].methods, path

        # Pages inside included routers are invisible to the security middleware (ADR-039).
        for route in run.app.router.routes:
            original = getattr(route, "original_router", None)
            if original is None:
                continue
            for inner in original.routes:
                assert not getattr(inner, "path", "").startswith("/setup"), inner

        page = run.client.get(WELCOME)  # no token: a page load
        assert page.status_code == 200, page.text[:200]
        assert page.headers["content-type"].startswith("text/html")
        assert page.headers.get("cache-control") == "no-store"
        assert page_token(page.text) == run.token


@pytest.mark.parametrize("source", ["bootstrap", "environment", "cli"])
def test_completed_setup_harness_skips_guide(
    tmp_path: Path, source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow_harness_fake_provider(monkeypatch)
    complete_setup(tmp_path / "data", tmp_path / "cfg", clock=FrozenClock(START))

    with running(tmp_path, source) as run:
        assert run.decision == StartupDecision("app")
        root = run.client.get("/")
        assert root.status_code == 200
        assert_not_redirected(root)


# --- resume and moved folders ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("done", "target"),
    [
        (("welcome",), "/setup/provider"),
        (("welcome", "provider"), "/setup/preferences"),
        (("welcome", "provider", "preferences"), "/setup/done"),
    ],
    ids=["welcome_done", "provider_done", "required_done"],
)
def test_resume_redirects_to_first_unfinished_required_step(
    tmp_path: Path, done: tuple[str, ...], target: str
) -> None:
    _resume_folders(tmp_path, done)

    with running(tmp_path, "bootstrap") as run:
        assert run.decision == StartupDecision("guide", target.rsplit("/", 1)[1])
        assert_see_other(run.client.get("/"), target)
        assert_see_other(run.client.post(ACTION_PATH, headers=run.action()), target)
        assert_hx_redirect(run.client.get("/inbox", headers=run.htmx()), target)


def test_locate_or_fresh_redirects_to_first_step(tmp_path: Path) -> None:
    moved = tmp_path / "moved-away"
    write_bootstrap(tmp_path / "cfg", data_dir=moved, version=__version__)

    with running(tmp_path, "bootstrap", data_dir=moved) as run:
        assert run.decision.kind == "locate_or_fresh"
        assert_see_other(run.client.get("/"), WELCOME)
        assert_hx_redirect(run.client.get("/board", headers=run.htmx()), WELCOME)
        assert run.client.get(WELCOME).status_code == 200

    assert not moved.exists(), "loading pages must not create the missing folder"


def test_setup_flag_reopens_guide_after_completion(tmp_path: Path) -> None:
    complete_setup(tmp_path / "data", tmp_path / "cfg", clock=FrozenClock(START))

    with running(tmp_path, "bootstrap", setup=True) as run:
        assert run.decision == StartupDecision("guide", "welcome", reopened=True)
        assert_see_other(run.client.get("/"), WELCOME)


# --- the cached decision ---------------------------------------------------------------------


def test_get_requests_never_recompute_the_decision(tmp_path: Path) -> None:
    """The middleware reads the cached decision; only start-up and POSTs refresh it."""
    with running(tmp_path, "cli") as run:
        assert run.decision == StartupDecision("guide", "welcome")
        # Change the stored state behind the app's back: a bootstrap and every step done.
        write_bootstrap(tmp_path / "cfg", data_dir=tmp_path / "data", version=__version__)
        progress = _progress(run.app)
        for step_id, _ in STEP_TITLES:
            progress.mark_done(step_id)

        assert_see_other(run.client.get("/"), WELCOME)
        assert run.client.get(WELCOME).status_code == 200
        assert run.client.get("/health").status_code == 200
        assert run.decision == StartupDecision("guide", "welcome")


def test_open_workspace_refreshes_decision(tmp_path: Path) -> None:
    """Starting fresh in step 1 opens the workspace; the decision follows its stored steps."""
    moved = tmp_path / "moved-away"
    write_bootstrap(tmp_path / "cfg", data_dir=moved, version=__version__)

    with running(tmp_path, "bootstrap", data_dir=moved) as run:
        assert run.decision.kind == "locate_or_fresh"

        run.app.state.open_workspace(moved)

        assert run.decision == StartupDecision("guide", "welcome"), "no step is done yet"
        progress = _progress(run.app)
        progress.mark_done("welcome")
        assert run.app.state.onboarding.refresh(progress) == StartupDecision("guide", "provider")
        assert run.decision == StartupDecision("guide", "provider")
        assert_see_other(run.client.get("/"), "/setup/provider")


def test_open_workspace_on_located_folder_resumes_its_steps(tmp_path: Path) -> None:
    """Locating a moved folder that already has progress resumes where it left off."""
    moved = tmp_path / "moved-away"
    found = tmp_path / "found-again"
    write_bootstrap(tmp_path / "cfg", data_dir=moved, version=__version__)
    seed_workspace(found, done=("welcome", "provider"), clock=FrozenClock(START))

    with running(tmp_path, "bootstrap", data_dir=moved) as run:
        run.app.state.open_workspace(found)

        assert run.decision == StartupDecision("guide", "preferences")
        assert_see_other(run.client.get("/"), "/setup/preferences")


# --- guide page shell ------------------------------------------------------------------------


@pytest.mark.parametrize(("current", "title"), STEP_TITLES, ids=[s for s, _ in STEP_TITLES])
def test_setup_page_marks_current_step(tmp_path: Path, current: str, title: str) -> None:
    with running(tmp_path, "default") as run:
        response = run.client.get(f"/setup/{current}")
        assert response.status_code == 200, response.text[:200]
        elements = outline(response.text)

        assert "Job Search Pipeline</title>" in response.text
        assert f"<title>{title} · Set up · Job Search Pipeline</title>" in response.text
        assert page_token(response.text) == run.token, "the guide extends base.html"
        headings = [e for e in elements if e.tag == "h1"]
        assert len(headings) == 1, [h.text for h in headings]
        assert "set up" in headings[0].text.lower(), headings[0].text

        lists = [e for e in elements if e.tag == "ol"]
        step_lists = [
            ol
            for ol in lists
            if [e.text for e in elements if e.tag == "li" and e.within(ol)]
            and all(t in ol.text for _, t in STEP_TITLES)
        ]
        assert len(step_lists) == 1, "one ordered list names every step"
        items = [e.text for e in elements if e.tag == "li" and e.within(step_lists[0])]
        positions = [next(i for i, text in enumerate(items) if t in text) for _, t in STEP_TITLES]
        assert positions == sorted(positions), f"steps out of order: {items}"

        marked = [e for e in elements if e.attrs.get("aria-current") == "step"]
        assert len(marked) == 1, f"expected one aria-current=step, found {len(marked)}"
        assert title in marked[0].text, marked[0].text
        assert marked[0].within(step_lists[0])

        main = next(e for e in elements if e.tag == "main")
        step_headings = [e for e in elements if e.tag in ("h2", "h3") and e.within(main)]
        assert step_headings, "the step's fragment adds a heading inside <main>"

        fragment = run.client.get(f"/setup/{current}", headers=run.htmx())
        assert fragment.status_code == 200, fragment.text[:200]
        for marker in ("<html", "<body", 'id="announcer"'):
            assert marker not in fragment.text, marker


@pytest.mark.parametrize(
    "step_id",
    ["unknown", "WELCOME", "welcome_", "<img src=x onerror=alert(1)>"],
    ids=["unknown", "upper", "suffix", "hostile"],
)
def test_unknown_setup_step_is_404(tmp_path: Path, step_id: str) -> None:
    with running(tmp_path, "default") as run:
        response = run.client.get(f"/setup/{quote(step_id, safe='')}")

        assert response.status_code == 404, (response.status_code, response.text[:200])
        assert "<img" not in response.text, "an unknown id is never echoed unescaped"


def test_setup_root_redirects_to_current_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow_harness_fake_provider(monkeypatch)
    fresh = tmp_path / "fresh"
    with running(fresh, "default") as run:
        assert_see_other(run.client.get("/setup"), WELCOME)

    resumed = tmp_path / "resumed"
    _resume_folders(resumed, ("welcome",))
    with running(resumed, "bootstrap") as run:
        assert_see_other(run.client.get("/setup"), "/setup/provider")

    finished = tmp_path / "finished"
    complete_setup(finished / "data", finished / "cfg", clock=FrozenClock(START))
    with running(finished, "bootstrap") as run:
        assert run.decision.kind == "app"
        assert_see_other(run.client.get("/setup"), WELCOME)
        guide = run.client.get(WELCOME)
        assert guide.status_code == 200, "the guide can be re-run once setup is done"


def test_guide_get_has_no_side_effects(tmp_path: Path) -> None:
    """Spec 2.9 rule 12: loading guide pages on a first run creates nothing."""
    with running(tmp_path, "default") as run:
        before = sorted(tmp_path.rglob("*"))
        for step_id, _ in STEP_TITLES:
            assert run.client.get(f"/setup/{step_id}").status_code == 200
        assert run.client.get("/setup").status_code == 303
        assert run.client.get("/").status_code == 303
        assert sorted(tmp_path.rglob("*")) == before
        assert run.app.state.workspace is None
