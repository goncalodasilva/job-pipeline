"""A fake job page fetcher for tests and the demo (ADR-054, ADR-098 item 2).

The guard refuses a local test server, so tests and the demo fetch fictional pages from
this fake instead. It serves only ``example.test`` and its subdomains, after the guard's
URL checks, and treats limits, content types and charsets exactly like the guarded fetcher.
Wiring builds it only when fakes are allowed, behind ``FakeHostRouter`` so every other URL
still goes through the guarded fetcher. Nothing here touches the network.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from app.domain.errors import FetchRefused
from app.modules.fetch import FetchedPage, FetchLimits, JobPageFetcher
from app.modules.fetch.content import clamp_limits, decode_body, parse_content_type
from app.modules.fetch.guard import parse_url

__all__ = ["FAKE_DOMAIN", "FakeHostRouter", "FakeJobPageFetcher", "FakePage", "is_fake_host"]

FAKE_DOMAIN: Final = "example.test"


@dataclass(frozen=True)
class FakePage:
    """A fictional page as served: its ``Content-Type`` header, raw body and, when it was
    reached through a redirect, the final URL."""

    content_type: str
    body: bytes
    final_url: str | None = None


def is_fake_host(url: str) -> bool:
    """Whether ``url`` is a valid http(s) URL on ``example.test`` or one of its subdomains."""
    try:
        host = parse_url(url).host
    except FetchRefused:
        return False
    return host == FAKE_DOMAIN or host.endswith(f".{FAKE_DOMAIN}")


class FakeJobPageFetcher:
    """Serves ``pages`` (URL to page, matched exactly) on ``*.example.test`` only."""

    def __init__(self, pages: Mapping[str, FakePage]) -> None:
        self._pages = MappingProxyType(dict(pages))

    async def fetch(self, url: str, limits: FetchLimits) -> FetchedPage:
        limits = clamp_limits(limits)
        page = self._pages.get(url) if is_fake_host(url) else None
        if page is None:
            raise FetchRefused(detail="not a fake page")
        if len(page.body) > limits.max_bytes:
            raise FetchRefused(detail="page too large")
        media = parse_content_type(page.content_type)
        return FetchedPage(
            requested_url=url,
            final_url=page.final_url if page.final_url is not None else url,
            status=200,
            content_type=media.media_type,
            text=decode_body(page.body, media.charset),
        )


class FakeHostRouter:
    """Sends ``*.example.test`` URLs to ``fake`` and every other URL to ``real``."""

    def __init__(self, *, fake: JobPageFetcher, real: JobPageFetcher) -> None:
        self._fake = fake
        self._real = real

    async def fetch(self, url: str, limits: FetchLimits) -> FetchedPage:
        fetcher = self._fake if is_fake_host(url) else self._real
        return await fetcher.fetch(url, limits)
