"""Root page (M0.16, templated in M0.17; spec 2.9 rules 2, 11, 12; ADR-023, ADR-039).

``GET /`` (Today) is registered as a page route. It loads without the launch token and hands
the token to htmx through ``<body hx-headers>``. The rest of the shell: test_app_shell.py.
Every value here is fictional.
"""

import html
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import RuntimeConfig
from app.main import create_app
from tests.fakes.clock import FrozenClock
from tests.page_token import body_hx_headers, page_token, raw_body_hx_headers

pytestmark = pytest.mark.integration

START = datetime(2026, 1, 3, 9, 0, 0, tzinfo=UTC)
PORT = 8000
BASE_URL = f"http://127.0.0.1:{PORT}"  # must pass the host check (spec 2.9 rule 1)
TITLE = "<title>Today \u00b7 Job Search Pipeline</title>"
PROTECTIVE_HEADERS = {
    "x-frame-options": "DENY",
    "content-security-policy": "frame-ancestors 'none'",
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
}
# A fictional value full of characters that would break out of an attribute or a JSON string.
HOSTILE_TOKEN = "fict\"ional'<tok>&amp;</body><script>x</script>\\u0022"  # noqa: S105 - fictional


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
    return create_app(_config(tmp_path), clock=FrozenClock(START))


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, base_url=BASE_URL) as test_client:
        yield test_client


def test_root_page_loads_without_token(app: FastAPI, client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200, response.text[:200]
    assert response.headers["content-type"].startswith("text/html")
    assert TITLE in response.text
    assert response.headers.get("cache-control") == "no-store"
    for name, value in PROTECTIVE_HEADERS.items():
        assert response.headers.get_list(name) == [value], name


def test_root_page_embeds_launch_token_in_body_hx_headers(app: FastAPI, client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200, response.text[:200]
    assert body_hx_headers(response.text) == {"X-JP-Token": app.state.launch_token}
    assert page_token(response.text) == app.state.launch_token


def test_root_page_hx_headers_attribute_is_html_escaped_json(
    app: FastAPI, client: TestClient
) -> None:
    response = client.get("/")
    assert response.status_code == 200, response.text[:200]

    raw = raw_body_hx_headers(response.text)
    assert json.loads(html.unescape(raw)) == {"X-JP-Token": app.state.launch_token}


def test_root_page_escapes_hostile_token_value(app: FastAPI, client: TestClient) -> None:
    """The page reads the token at request time; a hostile value must stay inside the attribute."""
    app.state.launch_token = HOSTILE_TOKEN

    response = client.get("/")
    assert response.status_code == 200, response.text[:200]

    assert "<script>x</script>" not in response.text
    raw = raw_body_hx_headers(response.text)
    for unsafe in ("<", ">", '"', "'"):
        assert unsafe not in raw, f"unescaped {unsafe!r} in the hx-headers attribute"
    assert json.loads(html.unescape(raw)) == {"X-JP-Token": HOSTILE_TOKEN}
    assert page_token(response.text) == HOSTILE_TOKEN


def test_root_page_with_hx_request_and_no_token_403(client: TestClient) -> None:
    response = client.get("/", headers={"HX-Request": "true"})

    assert response.status_code == 403


def test_root_page_head_without_token_ok(client: TestClient) -> None:
    response = client.head("/")

    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-store"
