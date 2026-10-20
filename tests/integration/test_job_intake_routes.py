"""M4.3 route security and fetch-fallback contract, through the real middleware."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.domain.errors import FetchRefused, NetworkUnavailable
from app.main import create_app
from app.modules.fetch import FetchedPage
from app.modules.fetch.providers.fake import FakeJobPageFetcher
from app.services.job_intake import JobIntakeService
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory
from tests.integration.test_app_shell import BASE_URL, START, _config
from tests.job_pages import fake_pages
from tests.setup_harness import complete_setup

pytestmark = pytest.mark.integration


@pytest.fixture
def running(tmp_path: Path):
    clock = FrozenClock(START)
    complete_setup(tmp_path / "data", tmp_path / "cfg", clock=clock)
    app = create_app(_config(tmp_path), clock=clock)
    with TestClient(app, base_url=BASE_URL) as client:
        # Real service, injected offline fetcher only.
        app.state.job_intake = JobIntakeService(
            lambda: app.state.workspace.db,
            clock,
            SequentialIdFactory(),
            FakeJobPageFetcher(fake_pages()),
        )
        yield (
            app,
            client,
            {"X-JP-Token": app.state.launch_token, "Origin": BASE_URL, "HX-Request": "true"},
        )


@pytest.mark.parametrize("failure", ["refused", "offline", "no_text"])
def test_ac_m4_2_paste_text_works_when_fetch_fails(running, failure):
    app, client, headers = running

    class Fails:
        async def fetch(self, url, limits):
            if failure == "no_text":
                return FetchedPage(url, url, 200, "text/html", "<p>Loading</p>")
            raise FetchRefused() if failure == "refused" else NetworkUnavailable()

    app.state.job_intake._fetcher = Fails()
    url = "https://fictional.example.test/ad"
    response = client.post("/inbox/intake", data={"url": url}, headers=headers)
    assert response.status_code == 422
    assert "Paste the job text instead" in response.text
    assert "<textarea" in response.text
    assert url in response.text
    assert "data-autofocus" in response.text
    saved = client.post(
        "/inbox/intake", data={"text": "Fictional botanist", "url": url}, headers=headers
    )
    assert saved.status_code == 200
    assert (
        app.state.workspace.db.connection.execute("SELECT snapshot_text FROM jobs").fetchone()[0]
        == "Fictional botanist"
    )


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1/ad", "file:///tmp/posting", "https://redirect.example.test/private"]
)
def test_ac_m4_1_refused_url_answers_paste_text_message(running, url):
    from app.modules.fetch.providers.guarded import GuardedJobPageFetcher
    from tests.unit.modules.fetch.test_guarded_fetcher import FakeResolver, Recorder, _redirect

    app, client, headers = running
    resolver = FakeResolver({"redirect.example.test": ["93.184.215.14"]})
    recorder = Recorder(lambda request: _redirect("http://127.0.0.1/private"))
    app.state.job_intake._fetcher = GuardedJobPageFetcher(
        resolver=resolver, transport=recorder.transport
    )
    response = client.post("/inbox/intake", data={"url": url}, headers=headers)
    assert response.status_code == 422
    assert "Paste the job text instead" in response.text


@pytest.mark.parametrize("path", ["/inbox/intake", "/inbox/jobs/missing/not-duplicate"])
def test_intake_routes_need_token_and_origin(running, path):
    app, client, headers = running
    for dropped in ("X-JP-Token", "Origin"):
        response = client.post(
            path,
            data={"text": "Fictional job"},
            headers={k: v for k, v in headers.items() if k != dropped},
        )
        assert response.status_code == 403
    assert app.state.workspace.db.connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_intake_routes_oversize_plain_413_and_safe_ids(running):
    _, client, headers = running
    response = client.post("/inbox/intake", data={"text": "a" * 50_001}, headers=headers)
    assert response.status_code == 413
    assert response.headers["content-type"].startswith("text/plain")
    assert client.post("/inbox/jobs/missing/not-duplicate", headers=headers).status_code == 404
    assert client.post("/inbox/jobs/a/b/not-duplicate", headers=headers).status_code == 404


def test_intake_fallback_escapes_untrusted_link(running):
    _, client, headers = running
    response = client.post(
        "/inbox/intake",
        data={"url": 'https://fictional.example.test/"><script>alert(1)</script>'},
        headers=headers,
    )
    assert response.status_code == 422
    assert "<script>alert(1)</script>" not in response.text


def test_plain_intake_returns_full_shell_with_token(running):
    _, client, headers = running
    response = client.post(
        "/inbox/intake",
        data={"text": "Fictional gardener"},
        headers={k: v for k, v in headers.items() if k != "HX-Request"},
    )
    assert response.status_code == 200
    assert "<html" in response.text
    assert "hx-headers=" in response.text


def test_uppercase_link_scheme_route_and_split_post(running):
    app, client, headers = running
    service = app.state.job_intake
    text = "Fictional gardener growing imaginary plants"
    first = service.add_text(text)
    second = service.add_text(text)
    split = client.post(f"/inbox/jobs/{second.source_job_id}/not-duplicate", headers=headers)
    assert split.status_code == 200
    assert "restored as its own card" in split.text
    response = client.post(
        "/inbox/intake",
        data={"text": "Different fictional role", "url": "HTTPS://Jobs.Example.test/ad"},
        headers=headers,
    )
    assert response.status_code == 200
    assert service.add_text(text).job_id != first.job_id
