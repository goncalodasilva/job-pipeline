"""Which pages open the event stream, and what the browser script is allowed to know (M2.1).

``app/templates/base.html`` puts ``data-events-url="/events"`` on ``<body>`` for the app's pages
and not for the setup guide (the guide runs before there is a workspace, and its ``/events``
would only be redirected back to the guide). ``app/static/js/app.js`` runs its reader only when
that attribute is present, takes the URL from it (so the path is written once, in the template),
and sends the launch token from the body's ``hx-headers`` as ``X-JP-Token`` (a plain
``EventSource`` can't send headers, and the token never goes in a URL, spec 2.9 rule 12).
The reader itself runs in a browser, so it is exercised in ``tests/e2e/test_events_reader.py``.
All folders live under ``tmp_path``; values are fictional.
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import RuntimeConfig
from app.main import create_app
from tests.fakes.clock import FrozenClock
from tests.page_token import page_token
from tests.setup_harness import complete_setup

pytestmark = pytest.mark.integration

START = datetime(2026, 1, 3, 9, 0, 0, tzinfo=UTC)
PORT = 8000
BASE_URL = f"http://127.0.0.1:{PORT}"
EVENTS_ATTRIBUTE = "data-events-url"
APP_PAGES = ("/", "/inbox", "/board", "/facts", "/companies", "/settings")
GUIDE_STEPS = ("welcome", "provider", "preferences", "done")
APP_JS = "/static/js/app.js"


class _BodyAttributes(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.bodies: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "body":
            self.bodies.append(dict(attrs))


def body_attributes(page: str) -> dict[str, str | None]:
    parser = _BodyAttributes()
    parser.feed(page)
    parser.close()
    assert len(parser.bodies) == 1, f"expected one <body>, found {len(parser.bodies)}"
    return parser.bodies[0]


@contextmanager
def client_for(tmp_path: Path, *, complete: bool) -> Iterator[TestClient]:
    config = RuntimeConfig(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        port=PORT,
        log_level="INFO",
        demo=False,
        setup=False,
        no_browser=False,
        data_dir_source="cli",
    )
    if complete:
        complete_setup(config.data_dir, config.config_dir, clock=FrozenClock(START))
    app = create_app(config, clock=FrozenClock(START))
    with TestClient(app, base_url=BASE_URL, follow_redirects=False) as client:
        yield client


def assert_app_pages_have_events_url(client: TestClient) -> None:
    """The positive control for the guide tests below: without it, "the guide has no events
    URL" would also pass if the attribute were simply misspelled everywhere."""
    attributes = body_attributes(client.get("/").text)
    assert attributes.get(EVENTS_ATTRIBUTE) == "/events", "app pages carry the events URL"


@pytest.mark.parametrize("path", APP_PAGES)
def test_app_pages_carry_events_url_on_body(tmp_path: Path, path: str) -> None:
    with client_for(tmp_path, complete=True) as client:
        response = client.get(path)

        assert response.status_code == 200, path
        attributes = body_attributes(response.text)
        assert attributes.get(EVENTS_ATTRIBUTE) == "/events"
        token = page_token(response.text)  # the reader reads it from hx-headers
        assert token not in (attributes.get(EVENTS_ATTRIBUTE) or ""), "never in a URL"
        assert "?" not in (attributes.get(EVENTS_ATTRIBUTE) or "")


@pytest.mark.parametrize("step", GUIDE_STEPS)
def test_guide_pages_do_not_carry_events_url(tmp_path: Path, step: str) -> None:
    with client_for(tmp_path, complete=True) as client:
        assert_app_pages_have_events_url(client)
        response = client.get(f"/setup/{step}")

        assert response.status_code == 200, step
        assert EVENTS_ATTRIBUTE not in body_attributes(response.text)


def test_guide_not_found_page_does_not_carry_events_url(tmp_path: Path) -> None:
    with client_for(tmp_path, complete=True) as client:
        assert_app_pages_have_events_url(client)
        response = client.get("/setup/no-such-step")

        assert response.status_code == 404
        assert EVENTS_ATTRIBUTE not in body_attributes(response.text)


def test_guide_page_does_not_carry_events_url_while_setup_is_incomplete(tmp_path: Path) -> None:
    with client_for(tmp_path / "done", complete=True) as finished:
        assert_app_pages_have_events_url(finished)  # the positive control, on a finished setup
    with client_for(tmp_path / "fresh", complete=False) as client:
        response = client.get("/setup/welcome")

        assert response.status_code == 200
        assert EVENTS_ATTRIBUTE not in body_attributes(response.text)
        assert page_token(response.text), "the guide still embeds the token for its own requests"


def test_app_js_reads_the_url_and_token_from_the_page_and_never_uses_event_source(
    tmp_path: Path,
) -> None:
    with client_for(tmp_path, complete=True) as client:
        response = client.get(APP_JS)

    assert response.status_code == 200
    script = response.text
    assert "X-JP-Token" in script, "the reader sends the launch token as a header"
    assert "hx-headers" in script, "and takes it from the body's hx-headers"
    assert "tasks:resync" in script, "the event the rest of the page listens for"
    assert "text/event-stream" in script, "it asks for, and checks for, an event stream"
    assert EVENTS_ATTRIBUTE in script or "eventsUrl" in script, "the URL comes from the page"
    # Comments may name the path or EventSource; code must not use them.
    assert not re.search(r"""["'`][^"'`\n]*/events""", script), "the path is in the template"
    assert not re.search(r"\bEventSource\s*\(", script), "EventSource can't send the token header"
