"""Shared ``JobPageFetcher`` contract (M4.1; AC-M4.1; spec 2.4, 2.9 rule 6, 4.1; ADR-098
item 2).

The guarded fetcher (with a fake resolver and ``httpx.MockTransport``) and the fake fetcher
behave the same way: a fixture page comes back as a ``FetchedPage`` with the requested and
final URL, status 200, the media type without parameters and the text decoded by its declared
charset; a ``file:`` URL and a private address are refused with ``FetchRefused``; a lowered
``max_bytes`` is respected. Every page is fictional and nothing touches the network. The new
names are imported inside the tests.
"""

import importlib
from collections.abc import Sequence
from typing import Any

import httpx
import pytest

from app.domain.errors import FetchRefused
from app.modules.fetch import FetchedPage, FetchLimits
from tests.job_pages import fake_pages, page_bytes, page_entries, page_url

pytestmark = pytest.mark.contract

# Only a number here: the mock transport answers for it, nothing connects.
PUBLIC_V4 = "93.184.215.14"


async def _public_resolver(host: str) -> Sequence[str]:
    return [PUBLIC_V4] if host.endswith(".example.test") else []


def _served_by_host_and_path() -> httpx.MockTransport:
    by_location = {}
    for page_id, entry in page_entries().items():
        url = httpx.URL(page_url(page_id))
        by_location[(url.host, url.raw_path)] = (entry["content_type"], page_bytes(page_id))

    def respond(request: httpx.Request) -> httpx.Response:
        found = by_location.get((request.headers["host"], request.url.raw_path))
        if found is None:
            return httpx.Response(404, headers={"content-type": "text/plain"})
        content_type, body = found
        return httpx.Response(200, headers={"content-type": content_type}, content=body)

    return httpx.MockTransport(respond)


def _fetcher(kind: str) -> Any:
    if kind == "guarded":
        guarded = importlib.import_module("app.modules.fetch.providers.guarded")
        return guarded.GuardedJobPageFetcher(
            resolver=_public_resolver, transport=_served_by_host_and_path()
        )
    fake = importlib.import_module("app.modules.fetch.providers.fake")
    return fake.FakeJobPageFetcher(fake_pages())


@pytest.mark.parametrize("kind", ["guarded", "fake"])
async def test_job_page_fetcher_contract(kind: str) -> None:
    fetcher = _fetcher(kind)

    # A fixture page, decoded.
    url = page_url("plain")
    page = await fetcher.fetch(url, FetchLimits())
    assert isinstance(page, FetchedPage)
    assert page.requested_url == url
    assert page.final_url == url
    assert page.status == 200
    assert page.content_type == "text/html"
    assert "Senior Widget Engineer" in page.text

    # The declared charset is used (ISO-8859-1 here).
    latin1 = await fetcher.fetch(page_url("latin1"), FetchLimits())
    assert "Café Fictício Lda" in latin1.text
    assert "�" not in latin1.text

    # Refused: a file URL and a private address.
    for refused in ("file:///etc/hosts", "http://127.0.0.1/", "http://[::1]/"):
        with pytest.raises(FetchRefused):
            await fetcher.fetch(refused, FetchLimits())

    # A lowered size limit is respected.
    with pytest.raises(FetchRefused):
        await fetcher.fetch(url, FetchLimits(max_bytes=100))
