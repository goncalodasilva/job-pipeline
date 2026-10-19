"""The fake job page fetcher and its wiring (M4.1; ADR-054, ADR-098 item 2).

``FakeJobPageFetcher(pages)`` in ``app.modules.fetch.providers.fake`` serves fictional pages
(``FakePage(content_type, body, final_url=None)``) for ``example.test`` and its subdomains
only, after the same URL checks as the guard; anything else is ``FetchRefused``. The guard
refuses a local test server, so tests and the demo use this fake instead.

``Wiring.job_page_fetcher()`` answers the ``JobPageFetcher`` the app uses. Without fakes it
is the guarded fetcher and never the fake. With fakes allowed (the variable or the demo
workspace), ``*.example.test`` URLs are served by the fake (the app's own fictional demo
pages) and every other URL still goes through the guarded fetcher.

``socket.getaddrinfo`` is replaced by a recorder that fails, so no test can resolve or
connect. The keyring is an in-memory test backend. Every value is fictional.
"""

import importlib
import socket
from collections.abc import Callable
from typing import Any

import pytest

from app.domain.errors import FetchRefused
from app.domain.redaction import Redactor
from app.modules.fetch import FetchedPage, FetchLimits
from app.wiring import build_wiring
from tests.fakes.keyring_backends import CountingKeyring
from tests.job_pages import fake_pages, page_url

ALLOW_VARIABLE = "JOBPIPELINE_ALLOW_FAKE_PROVIDERS"


def _fake_module() -> Any:
    return importlib.import_module("app.modules.fetch.providers.fake")


def _guarded_class() -> type:
    found: type = importlib.import_module(
        "app.modules.fetch.providers.guarded"
    ).GuardedJobPageFetcher
    return found


@pytest.fixture
def lookups(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every host the code tried to resolve; each lookup fails as an unknown host."""
    seen: list[str] = []

    def refuse(host: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(host.decode() if isinstance(host, bytes) else str(host))
        raise socket.gaierror(socket.EAI_NONAME, "no network in tests")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    return seen


def _wiring(*, allow_fakes: bool) -> Any:
    environ = {ALLOW_VARIABLE: "1"} if allow_fakes else {}
    wiring = build_wiring(
        environ=environ, demo=False, redactor=Redactor(), keyring_backend=CountingKeyring()
    )
    assert wiring.allow_fakes is allow_fakes
    return wiring


async def _refused(fetch: Callable[..., Any], url: str) -> None:
    with pytest.raises(FetchRefused):
        await fetch(url, FetchLimits())


async def test_fake_fetcher_serves_only_example_test_and_only_when_fakes_allowed(
    lookups: list[str],
) -> None:
    fake = _fake_module()
    plain_url = page_url("plain")
    pages = dict(fake_pages(["plain", "latin1"]))
    # A page under a real-looking host is never served, even when it is in the mapping.
    pages["https://jobs.example.com/postings/1"] = fake.FakePage(
        content_type="text/html", body=b"<p>Not served</p>"
    )
    pages["https://jobs.lumenboard.example.test/moved"] = fake.FakePage(
        content_type="text/html; charset=utf-8",
        body=b"<p>Moved posting</p>",
        final_url="https://jobs.lumenboard.example.test/postings/moved-9001",
    )
    fetcher = fake.FakeJobPageFetcher(pages)

    # Served: a FetchedPage with the text decoded and the media type without parameters.
    page = await fetcher.fetch(plain_url, FetchLimits())
    assert isinstance(page, FetchedPage)
    assert (page.requested_url, page.final_url, page.status) == (plain_url, plain_url, 200)
    assert page.content_type == "text/html"
    assert "Senior Widget Engineer" in page.text
    latin1 = await fetcher.fetch(page_url("latin1"), FetchLimits())
    assert "São Bento" in latin1.text
    moved = await fetcher.fetch("https://jobs.lumenboard.example.test/moved", FetchLimits())
    assert moved.final_url == "https://jobs.lumenboard.example.test/postings/moved-9001"

    # Refused: other hosts, look-alike hosts, unknown pages and the guard's URL checks.
    for url in (
        "https://jobs.example.com/postings/1",
        "https://example.test.attacker.example.com/postings/1",
        "https://notexample.test/postings/1",
        "https://jobs.lumenboard.example.test/no-such-posting",
        "file:///etc/hosts",
        "ftp://jobs.lumenboard.example.test/postings/1",
        "https://user@jobs.lumenboard.example.test/postings/1",
        f"{plain_url}\\..",
        "http://127.0.0.1/",
    ):
        await _refused(fetcher.fetch, url)

    # Size limits apply as in the real fetcher.
    await _refused(lambda url, limits: fetcher.fetch(url, FetchLimits(max_bytes=100)), plain_url)

    # Without fakes, wiring builds only the guarded fetcher.
    guarded_class = _guarded_class()
    strict = _wiring(allow_fakes=False).job_page_fetcher()
    assert isinstance(strict, guarded_class)
    assert not isinstance(strict, fake.FakeJobPageFetcher)
    # An example.test URL is resolved like any other, and the (failing) lookup refuses it.
    await _refused(strict.fetch, "https://jobs.lumenboard.example.test/postings/1")
    assert lookups == ["jobs.lumenboard.example.test"]

    # With fakes, example.test is served by the fake with no lookup; other hosts still go
    # through the guarded fetcher.
    lookups.clear()
    relaxed = _wiring(allow_fakes=True).job_page_fetcher()
    await _refused(relaxed.fetch, "https://jobs.lumenboard.example.test/no-such-posting")
    assert lookups == []
    await _refused(relaxed.fetch, "https://jobs.example.com/postings/1")
    assert lookups == ["jobs.example.com"]
    await _refused(relaxed.fetch, "http://127.0.0.1/")
    await _refused(relaxed.fetch, "file:///etc/hosts")
    assert lookups == ["jobs.example.com"]
