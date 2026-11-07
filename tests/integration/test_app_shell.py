"""App shell: base template, vendored htmx, placeholder pages (M0.17; ADR-029, ADR-039).

Spec 2.9 rules 2 and 12, 3.9 and 3.10; the htmx-ui skill. Every page route is registered by
``app.api.pages.register_pages`` with ``tags=[PAGE_TAG]``, loads without the launch token,
renders the full shell normally and only the fragment for ``HX-Request: true`` (which needs
the token). Structure is checked with the standard-library HTML parser. Every value here is
fictional.
"""

import hashlib
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import RuntimeConfig
from app.main import create_app
from tests.fakes.clock import FrozenClock
from tests.page_token import TOKEN_HEADER_NAME, page_token
from tests.setup_harness import complete_setup

pytestmark = pytest.mark.integration

START = datetime(2026, 1, 3, 9, 0, 0, tzinfo=UTC)
PORT = 8000
BASE_URL = f"http://127.0.0.1:{PORT}"  # must pass the host check (spec 2.9 rule 1)

# Sidebar order from the detailed design: five areas, then Settings pinned at the bottom.
AREAS: list[tuple[str, str]] = [
    ("/", "Today"),
    ("/inbox", "Job inbox"),
    ("/board", "Board"),
    ("/facts", "Fact bank"),
    ("/companies", "Companies"),
    ("/settings", "Settings"),
]
PAGE_PATHS = [path for path, _ in AREAS]
AREA_IDS = [name.lower().replace(" ", "-") for _, name in AREAS]
# Settings (M9.2, ADR-131 item 7) is a set of area pages, not a placeholder: the "Coming later"
# card that was its one empty state is gone, so the empty-state tests cover the other areas. Its
# page, title, current sidebar entry and caching stay covered by the tests above and below.
EMPTY_STATE_AREAS = [pair for pair in AREAS if pair[1] != "Settings"]
EMPTY_STATE_IDS = [name.lower().replace(" ", "-") for _, name in EMPTY_STATE_AREAS]

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = REPO_ROOT / "app" / "static"
VENDOR_DIR = STATIC_DIR / "vendor"
TEMPLATES_DIR = REPO_ROOT / "app" / "templates"
HTMX_URL = "/static/vendor/htmx.min.js"
APP_JS_URL = "/static/js/app.js"


def _title(area: str) -> str:
    return f"<title>{area} · Job Search Pipeline</title>"


# --- fixtures ---------------------------------------------------------------------------------


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


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    complete_setup(tmp_path / "data", tmp_path / "cfg", clock=FrozenClock(START))  # AC-M1.2
    return create_app(_config(tmp_path), clock=FrozenClock(START))


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, base_url=BASE_URL) as test_client:
        yield test_client


# --- a tiny HTML outline built with the standard-library parser -------------------------------

_VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "source",
    "track",
    "wbr",
}
_FOCUSABLE_TAGS = {"button", "input", "select", "textarea", "summary", "iframe"}


@dataclass
class Element:
    tag: str
    attrs: dict[str, str | None]
    parent: "Element | None"
    parts: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join("".join(self.parts).split())

    @property
    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def ancestors(self) -> Iterator["Element"]:
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def within(self, other: "Element") -> bool:
        return any(node is other for node in self.ancestors())

    def is_focusable(self) -> bool:
        if self.tag == "a" and self.attrs.get("href") is not None:
            return True
        if self.tag in _FOCUSABLE_TAGS:
            return self.attrs.get("type") != "hidden"
        tabindex = self.attrs.get("tabindex")
        return tabindex is not None and tabindex.strip().lstrip("+").isdigit()


class _Outline(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[Element] = []
        self._open: list[Element] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        parent = self._open[-1] if self._open else None
        element = Element(tag=tag, attrs=dict(attrs), parent=parent)
        self.elements.append(element)
        if tag not in _VOID:
            self._open.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        parent = self._open[-1] if self._open else None
        self.elements.append(Element(tag=tag, attrs=dict(attrs), parent=parent))

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


def find_all(elements: list[Element], predicate: Callable[[Element], bool]) -> list[Element]:
    return [element for element in elements if predicate(element)]


def find_one(elements: list[Element], predicate: Callable[[Element], bool], what: str) -> Element:
    found = find_all(elements, predicate)
    assert len(found) == 1, f"expected exactly one {what}, found {len(found)}"
    return found[0]


def main_nav(elements: list[Element]) -> Element:
    return find_one(
        elements,
        lambda e: e.tag == "nav" and e.attrs.get("aria-label") == "Main",
        '<nav aria-label="Main">',
    )


def nav_links(elements: list[Element]) -> list[Element]:
    nav = main_nav(elements)
    return find_all(elements, lambda e: e.tag == "a" and e.within(nav))


def _get_page(client: TestClient, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code, response.text[:200])
    assert response.headers["content-type"].startswith("text/html"), path
    return response.text


# --- shell structure --------------------------------------------------------------------------


def test_root_renders_shell_with_sidebar_in_design_order(client: TestClient) -> None:
    page = _get_page(client, "/")
    elements = outline(page)

    assert _title("Today") in page

    body = find_one(elements, lambda e: e.tag == "body", "<body>")
    in_body = [e for e in elements if e.within(body)]

    # Skip link: the first focusable element in the body, pointing at <main id="main">.
    focusable = [e for e in in_body if e.is_focusable()]
    assert focusable, "the page has no focusable elements"
    skip = focusable[0]
    assert skip.tag == "a" and skip.attrs.get("href") == "#main", (
        f"first focusable element is <{skip.tag} {skip.attrs}>, not the skip link"
    )
    assert skip.text, "the skip link has no visible text"
    main = find_one(elements, lambda e: e.tag == "main", "<main>")
    assert main.attrs.get("id") == "main"
    assert main.attrs.get("tabindex") == "-1"

    # Sidebar: design order, Settings last and alone in the pinned group.
    links = nav_links(elements)
    assert [(link.attrs.get("href"), link.text) for link in links] == AREAS
    pinned_groups = find_all(elements, lambda e: "sidebar-pinned" in e.classes)
    assert len(pinned_groups) == 1, "expected one element with class sidebar-pinned"
    pinned = pinned_groups[0]
    assert [link.text for link in links if link.within(pinned)] == ["Settings"]
    assert all(link.within(main_nav(elements)) for link in links)

    # Only the current area is marked.
    current = [link.text for link in links if link.attrs.get("aria-current") == "page"]
    assert current == ["Today"]

    # Top bar with placeholders, outside the main content.
    topbar = find_one(
        in_body, lambda e: e.tag == "header" and "topbar" in e.classes, '<header class="topbar">'
    )
    assert not topbar.within(main)

    # Global live region for announcements.
    announcer = find_one(elements, lambda e: e.attrs.get("id") == "announcer", "#announcer")
    assert announcer.tag == "div"
    assert announcer.attrs.get("aria-live") == "polite"
    assert not announcer.within(main), "the live region must survive main-content swaps"

    # Scripts: vendored htmx and the app script.
    sources = [(e.attrs.get("src") or "").split("?")[0] for e in elements if e.tag == "script"]
    assert HTMX_URL in sources
    assert APP_JS_URL in sources


@pytest.mark.parametrize(("path", "area"), AREAS, ids=AREA_IDS)
def test_sidebar_marks_only_current_area(client: TestClient, path: str, area: str) -> None:
    page = _get_page(client, path)
    links = nav_links(outline(page))

    assert _title(area) in page
    assert [link.text for link in links if link.attrs.get("aria-current") == "page"] == [area]


@pytest.mark.parametrize("path", PAGE_PATHS, ids=AREA_IDS)
def test_page_embeds_launch_token_in_body_hx_headers(
    app: FastAPI, client: TestClient, path: str
) -> None:
    page = _get_page(client, path)

    assert page_token(page) == app.state.launch_token


def test_page_routes_registered_as_page_routes(app: FastAPI) -> None:
    """All six areas are GET/HEAD page routes on the app itself (ADR-039)."""
    from fastapi.routing import APIRoute

    from app.api.security import PAGE_TAG

    pages = {
        route.path: route
        for route in app.router.routes
        if isinstance(route, APIRoute) and PAGE_TAG in route.tags
    }
    assert set(PAGE_PATHS) <= set(pages), sorted(pages)
    for path in PAGE_PATHS:
        assert pages[path].methods == {"GET", "HEAD"}, path


# --- vendored htmx and app.js -----------------------------------------------------------------


def _readme_value(label: str) -> str:
    readme = (VENDOR_DIR / "README.md").read_text(encoding="utf-8")
    match = re.search(rf"^- {re.escape(label)}:\s*(\S+)\s*$", readme, re.MULTILINE)
    assert match is not None, f"app/static/vendor/README.md has no '- {label}:' line"
    return match.group(1)


def test_vendored_htmx_served_and_matches_pinned_version(app: FastAPI, client: TestClient) -> None:
    version = _readme_value("Version")
    expected_sha256 = _readme_value("SHA-256 of `htmx.min.js`")
    assert re.fullmatch(r"2\.0\.\d+", version), f"ADR-029 pins htmx 2.0.x, README says {version}"
    assert (VENDOR_DIR / "htmx.LICENSE").is_file(), "the htmx license must be vendored"

    response = client.get(HTMX_URL)  # no token: /static/ is token-free (ADR-039)

    assert response.status_code == 200, response.text[:200]
    assert "javascript" in response.headers["content-type"]
    assert hashlib.sha256(response.content).hexdigest() == expected_sha256
    assert f'version:"{version}"' in response.text
    assert app.url_path_for("static", path="vendor/htmx.min.js") == HTMX_URL

    page = _get_page(client, "/")
    sources = [(e.attrs.get("src") or "").split("?")[0] for e in outline(page) if e.tag == "script"]
    assert HTMX_URL in sources


def test_app_js_served_and_handles_stale_token(client: TestClient) -> None:
    """After an app restart the page's token is stale and htmx requests get 403.

    app.js listens for ``htmx:responseError`` and, on a 403, shows the "Reload the page"
    message (rendered by the template, spec 3.10) in a visible status area and in
    ``#announcer``; a server that stopped answering (``htmx:sendError``) and other failed
    requests get their own messages. Only the wiring is checked here; the browser behaviour
    gets e2e coverage in M0.18 (``test_stale_token_403_announces_reload``).
    """
    response = client.get(APP_JS_URL)

    assert response.status_code == 200, response.text[:200]
    assert "javascript" in response.headers["content-type"]
    script = response.text
    for needle in ("htmx:responseError", "htmx:sendError", "403", "announcer", "app-status"):
        assert needle in script, needle

    elements = outline(_get_page(client, "/"))
    main = find_one(elements, lambda e: e.tag == "main", "<main>")
    status = find_one(elements, lambda e: e.attrs.get("id") == "app-status", "#app-status")
    assert not status.within(main), "the status area must survive main-content swaps"
    assert "hidden" in status.attrs, "the status area starts hidden"
    assert "Reload the page" in (status.attrs.get("data-msg-restarted") or "")
    assert status.attrs.get("data-msg-offline")
    assert status.attrs.get("data-msg-failed")
    reload_buttons = find_all(
        elements, lambda e: e.tag == "button" and e.within(status) and "Reload" in e.text
    )
    assert len(reload_buttons) == 1
    assert reload_buttons[0].attrs.get("type") == "button"


def test_app_js_moves_focus_to_data_autofocus_after_swap(client: TestClient, app: FastAPI) -> None:
    """htmx-ui skill: after a swap, focus moves to the element marked ``data-autofocus``."""
    script = client.get(APP_JS_URL).text
    assert "htmx:afterSettle" in script
    assert "data-autofocus" in script

    token = {TOKEN_HEADER_NAME: app.state.launch_token}
    fragment = client.get("/inbox", headers={**token, "HX-Request": "true"})
    headings = [e for e in outline(fragment.text) if e.tag == "h1"]
    assert len(headings) == 1
    assert "data-autofocus" in headings[0].attrs


# --- htmx configuration -----------------------------------------------------------------------

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "app" / "templates"
SETTLE_MODIFIER = re.compile(r"settle:\s*([0-9.]+)")

# Hardened htmx settings (security review M0.17): no eval or swapped-in scripts, requests only
# to this origin, no page HTML copied into browser storage, history restores are page loads.
HTMX_CONFIG = {
    "selfRequestsOnly": True,
    "allowEval": False,
    "allowScriptTags": False,
    "historyCacheSize": 0,
    "refreshOnHistoryMiss": True,
    "historyRestoreAsHxRequest": False,
}


@pytest.mark.parametrize("path", PAGE_PATHS, ids=AREA_IDS)
def test_pages_pin_hardened_htmx_config(client: TestClient, path: str) -> None:
    elements = outline(_get_page(client, path))
    meta = find_one(
        elements,
        lambda e: e.tag == "meta" and e.attrs.get("name") == "htmx-config",
        '<meta name="htmx-config">',
    )
    config = json.loads(meta.attrs.get("content") or "")
    assert {key: config.get(key) for key in HTMX_CONFIG} == HTMX_CONFIG

    # htmx reads the meta tag when it loads, so it must come before the htmx script.
    tags = [e for e in elements if e is meta or (e.tag == "script")]
    assert tags[0] is meta


@pytest.mark.parametrize("path", PAGE_PATHS, ids=AREA_IDS)
def test_pages_settle_swapped_content_at_once(client: TestClient, path: str) -> None:
    """TD-320, ADR-138: htmx binds swapped content when it settles, so with a settle delay a
    form pressed right after a swap is submitted natively, without the launch token."""
    meta = find_one(
        outline(_get_page(client, path)),
        lambda e: e.tag == "meta" and e.attrs.get("name") == "htmx-config",
        '<meta name="htmx-config">',
    )
    assert json.loads(meta.attrs.get("content") or "")["defaultSettleDelay"] == 0


def test_no_template_delays_settling() -> None:
    """A ``settle:`` modifier above 0 would bring back TD-320's window for that swap."""
    delays = sorted(
        f"{path.relative_to(TEMPLATES_DIR)}: {match.group(0)}"
        for path in TEMPLATES_DIR.rglob("*.html")
        for match in SETTLE_MODIFIER.finditer(path.read_text(encoding="utf-8"))
        if float(match.group(1)) != 0
    )
    assert not delays, delays


def test_history_restore_request_gets_full_page(app: FastAPI, client: TestClient) -> None:
    """A history restore replaces the whole body, so it must never get a bare fragment."""
    headers = {
        TOKEN_HEADER_NAME: app.state.launch_token,
        "HX-Request": "true",
        "HX-History-Restore-Request": "true",
    }

    response = client.get("/inbox", headers=headers)

    assert response.status_code == 200
    for marker in _FULL_PAGE_MARKERS:
        assert marker in response.text, marker


def test_static_paths_dispatch_to_the_static_mount(app: FastAPI) -> None:
    """/static/ skips the token by path prefix (ADR-039), so it must reach the static mount.

    The first route that fully matches decides dispatch; no earlier route may catch /static/.
    """
    from starlette.routing import Match, Mount

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/static/vendor/htmx.min.js",
        "root_path": "",
        "headers": [],
        "query_string": b"",
    }
    first = next(r for r in app.router.routes if r.matches(scope)[0] is Match.FULL)
    assert isinstance(first, Mount)
    assert first.name == "static"


# --- page versus fragment rendering -----------------------------------------------------------

_FULL_PAGE_MARKERS = ("<html", "<body", "<nav", 'id="announcer"')


@pytest.mark.parametrize(("path", "area"), [AREAS[0], AREAS[1]], ids=AREA_IDS[:2])
def test_render_returns_fragment_for_hx_request(
    app: FastAPI, client: TestClient, path: str, area: str
) -> None:
    token = {TOKEN_HEADER_NAME: app.state.launch_token}

    fragment = client.get(path, headers={**token, "HX-Request": "true"})

    assert fragment.status_code == 200, fragment.text[:200]
    assert fragment.headers["content-type"].startswith("text/html")
    for marker in _FULL_PAGE_MARKERS:
        assert marker not in fragment.text, f"fragment for {path} contains {marker!r}"
    headings = [e.text for e in outline(fragment.text) if e.tag == "h1"]
    assert any(area.lower() in heading.lower() for heading in headings), headings

    full = client.get(path)
    assert full.status_code == 200
    for marker in _FULL_PAGE_MARKERS:
        assert marker in full.text, f"full page for {path} lacks {marker!r}"

    # Only the exact value "true" selects the fragment.
    not_htmx = client.get(path, headers={**token, "HX-Request": "false"})
    assert not_htmx.status_code == 200
    assert "<body" in not_htmx.text


@pytest.mark.parametrize("path", PAGE_PATHS, ids=AREA_IDS)
def test_page_with_hx_request_and_no_token_403(client: TestClient, path: str) -> None:
    response = client.get(path, headers={"HX-Request": "true"})

    assert response.status_code == 403


# --- templates --------------------------------------------------------------------------------


def test_autoescape_on_for_templates() -> None:
    from starlette.templating import Jinja2Templates

    from app.api.templating import render, templates

    assert callable(render)
    assert isinstance(templates, Jinja2Templates)
    autoescape = templates.env.autoescape
    for name in ("x.html", "base.html", "fragments/x/_y.html"):
        enabled = autoescape(name) if callable(autoescape) else autoescape
        assert enabled, f"autoescape must be on for {name}"

    hostile = "<script>alert('fictional')</script>"
    rendered = templates.env.from_string("<p>{{ v }}</p>").render(v=hostile)
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    # The token path through the real base template is covered by
    # tests/integration/test_pages.py::test_root_page_escapes_hostile_token_value.


def test_templates_layout_pages_extend_base_fragments_do_not() -> None:
    """htmx-ui skill: pages extend base.html; fragments never extend anything."""
    assert (TEMPLATES_DIR / "base.html").is_file()
    pages = sorted((TEMPLATES_DIR / "pages").glob("*.html"))
    assert len(pages) >= len(AREAS), [p.name for p in pages]
    extends = re.compile(r"{%-?\s*extends\s+[\"']base\.html[\"']\s*-?%}")
    for page in pages:
        assert extends.search(page.read_text(encoding="utf-8")), f"{page.name} must extend base"
    for fragment in (TEMPLATES_DIR / "fragments").rglob("*.html"):
        assert "extends" not in fragment.read_text(encoding="utf-8"), fragment.name


# --- caching ----------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("path", PAGE_PATHS, ids=AREA_IDS)
def test_pages_send_no_store(client: TestClient, path: str, method: str) -> None:
    response = client.request(method, path)

    assert response.status_code == 200, (method, path)
    assert response.headers.get("cache-control") == "no-store", (method, path)


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_static_files_served_without_forced_no_store(client: TestClient, method: str) -> None:
    response = client.request(method, HTMX_URL)

    assert response.status_code == 200, (method, HTMX_URL)
    assert response.headers.get("cache-control") != "no-store"


# --- placeholder empty states -----------------------------------------------------------------


def _empty_state(page: str) -> tuple[Element, list[Element]]:
    elements = outline(page)
    main = find_one(elements, lambda e: e.tag == "main", "<main>")
    states = find_all(elements, lambda e: "empty-state" in e.classes and e.within(main))
    assert len(states) == 1, f"expected one .empty-state inside <main>, found {len(states)}"
    return states[0], elements


@pytest.mark.parametrize(("path", "area"), EMPTY_STATE_AREAS, ids=EMPTY_STATE_IDS)
def test_placeholder_pages_have_empty_state(client: TestClient, path: str, area: str) -> None:
    page = _get_page(client, path)
    state, elements = _empty_state(page)

    assert len([e for e in elements if e.tag == "h1"]) == 1, "one <h1> per page"
    # The Fact bank leads with its own <h1> and tabs; its empty state is the empty Library's.
    # The Job inbox (M4.7) leads with its own <h1> and "+ Add job"; its empty state is the
    # empty queue's (ADR-098 item 9).
    # The Companies page (M4.10) leads with its own <h1> and the table; its empty state is the
    # empty list's (ADR-098 item 9).
    # The Board (M5.4) leads with its own <h1> and the Board/Table toggle; its empty state is
    # the empty board's, with a link to the inbox (ADR-102 item 11).
    own_page = area in ("Fact bank", "Job inbox", "Companies", "Board", "Today")
    headings = [e for e in elements if e.tag == "h1" and (own_page or e.within(state))]
    assert headings, "the empty state has no <h1>"
    assert area.lower() in headings[0].text.lower(), headings[0].text

    sentences = [e.text for e in elements if e.tag == "p" and e.within(state)]
    assert any(len(text.split()) >= 4 for text in sentences), (
        f"the {area} empty state must explain what will appear there; got {sentences}"
    )

    # No broken links (ADR-029): every sidebar link opens a page.
    for link in nav_links(elements):
        href = link.attrs.get("href")
        assert href is not None
        assert client.get(href).status_code == 200, href


def test_empty_state_explanations_differ_per_area(client: TestClient) -> None:
    """Each empty state explains its own area, not one generic sentence for all."""
    explanations = []
    for path, _ in EMPTY_STATE_AREAS:
        state, elements = _empty_state(_get_page(client, path))
        explanations.append(" ".join(e.text for e in elements if e.tag == "p" and e.within(state)))

    assert len(set(explanations)) == len(EMPTY_STATE_AREAS), explanations


# --- favicon ---------------------------------------------------------------------------------

FAVICON_URL = "/static/favicon.svg"


def test_pages_link_a_served_svg_favicon(client: TestClient) -> None:
    """Without a declared icon, browsers request /favicon.ico, which needs the token (403)."""
    elements = outline(_get_page(client, "/"))
    icons = [
        e for e in elements if e.tag == "link" and "icon" in (e.attrs.get("rel") or "").split()
    ]
    assert [(e.attrs.get("href"), e.attrs.get("type")) for e in icons] == [
        (FAVICON_URL, "image/svg+xml")
    ]

    response = client.get(FAVICON_URL)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.text.lstrip().startswith("<svg")
    assert "<script" not in response.text.lower()


def test_inbox_empty_state_offers_adding_a_job(client: TestClient) -> None:
    """Done's "Add a job" opens the inbox (ADR-064); with no jobs it says how to add one
    (replaces the placeholder test, ADR-098 item 9)."""
    response = client.get("/inbox")
    assert response.status_code == 200
    text = " ".join(response.text.split())
    assert "No jobs yet. Add one by link or by pasting the ad." in text
    assert "+ Add job" in text
