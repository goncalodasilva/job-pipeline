"""The SSRF-guarded job page fetcher (spec 2.9 rule 6; ADR-020, ADR-036, ADR-098 item 2).

The only module that imports httpx. Every hop is checked by ``guard.check_url`` and sent to
the address that check answered, so a second DNS answer can't move the connection: the
request URL carries the checked IP, the ``Host`` header the original host (with its port
when it isn't the default), and for https the ``sni_hostname`` request extension makes the
TLS server name, and so certificate verification, the original host.

Requests go straight to an httpx transport, not through ``httpx.AsyncClient``: nothing
follows redirects, keeps cookies or logs URLs, and a bad ``Location`` is ours to refuse.
Each ``Location`` is checked again, at most ``max_redirects`` (5) times. The default
transport ignores the environment (``trust_env=False``: no proxies, no .netrc) and never
reuses a connection. Only text content types are read; the body is read raw and decoded by
``codings.BoundedBody``, so the size cap counts decoded bytes and nothing is inflated past
it. One deadline covers the whole fetch, DNS included. Error statuses, refused URLs, bad
codings and oversized bodies raise ``FetchRefused``; connection failures and the deadline
raise ``NetworkUnavailable``. The URL is never logged nor put into an error's detail.
"""

import asyncio
import logging
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final
from urllib.parse import urljoin, urlsplit

import httpx

from app.domain.errors import FetchRefused, NetworkUnavailable
from app.modules.fetch import FetchedPage, FetchLimits
from app.modules.fetch.codings import ACCEPT_ENCODING, BoundedBody, coding_of
from app.modules.fetch.content import clamp_limits, decode_body, parse_content_type
from app.modules.fetch.guard import CheckedUrl, Resolver, check_url

__all__ = ["GuardedJobPageFetcher", "system_resolver"]

REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
USER_AGENT: Final = f"python-httpx/{httpx.__version__}"
# httpcore logs its connections (with the address) at DEBUG; httpx logs requests at INFO.
_CLIENT_LOGGERS: Final = ("httpx", "httpcore")


async def system_resolver(host: str) -> list[str]:
    """The operating system's addresses for ``host``, in its order and without duplicates;
    a failure raises ``OSError``."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


class GuardedJobPageFetcher:
    """A ``JobPageFetcher`` over httpx. ``resolver`` and ``transport`` are for tests; by
    default the system resolver and a fresh httpx transport per fetch are used."""

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._resolver: Resolver = resolver if resolver is not None else system_resolver
        self._transport = transport
        _quiet_client_logs()

    async def fetch(self, url: str, limits: FetchLimits) -> FetchedPage:
        limits = clamp_limits(limits)
        try:
            async with asyncio.timeout(limits.timeout_seconds):
                return await self._follow(url, limits)
        except TimeoutError:
            raise NetworkUnavailable(detail="fetch deadline passed") from None

    async def _follow(self, url: str, limits: FetchLimits) -> FetchedPage:
        current = url
        async with self._transport_for_one_fetch() as transport:
            for _ in range(limits.max_redirects + 1):
                checked = await check_url(current, self._resolver)
                response = await _send(transport, checked, limits)
                try:
                    location = _redirect_location(response)
                    if location is None:
                        return await _page(response, url, current, limits)
                finally:
                    await response.aclose()
                current = _redirect_target(current, location)
        raise FetchRefused(detail="redirect limit")

    @asynccontextmanager
    async def _transport_for_one_fetch(self) -> AsyncIterator[httpx.AsyncBaseTransport]:
        if self._transport is not None:
            yield self._transport
            return
        # Hard requirements for pinning: httpcore pools connections by the pinned IP and
        # ignores ``sni_hostname`` when it reuses one, so a pooled connection could carry
        # another host's TLS session. Hence one transport per fetch, closed afterwards, and
        # no keep-alive, so every hop opens its own connection with its own server name.
        transport = httpx.AsyncHTTPTransport(
            trust_env=False, limits=httpx.Limits(max_keepalive_connections=0)
        )
        async with transport:
            yield transport


def _quiet_client_logs() -> None:
    for name in _CLIENT_LOGGERS:
        logger = logging.getLogger(name)
        if logger.level < logging.WARNING:  # NOTSET (0) included
            logger.setLevel(logging.WARNING)


async def _send(
    transport: httpx.AsyncBaseTransport, checked: CheckedUrl, limits: FetchLimits
) -> httpx.Response:
    """One GET to the checked address, keeping the original host for HTTP and TLS."""
    parsed = checked.parsed
    extensions: dict[str, object] = {"timeout": httpx.Timeout(limits.timeout_seconds).as_dict()}
    if parsed.scheme == "https":
        extensions["sni_hostname"] = parsed.host
    headers = {
        "Host": _authority(parsed.host, parsed.port, parsed.default_port),
        "Accept": "*/*",
        "Accept-Encoding": ACCEPT_ENCODING,
        "User-Agent": USER_AGENT,
    }
    try:
        request = httpx.Request("GET", _pinned_url(checked), headers=headers, extensions=extensions)
        return await transport.handle_async_request(request)
    except httpx.InvalidURL:
        raise FetchRefused(detail="malformed address") from None
    except httpx.TransportError as error:
        raise NetworkUnavailable(detail=type(error).__name__) from None


def _pinned_url(checked: CheckedUrl) -> str:
    """The checked URL with its host replaced by the checked address."""
    parsed = checked.parsed
    parts = urlsplit(parsed.url)
    authority = _authority(checked.address, parsed.port, parsed.default_port)
    query = f"?{parts.query}" if parts.query else ""
    return f"{parsed.scheme}://{authority}{parts.path or '/'}{query}"


def _authority(host: str, port: int, default_port: bool) -> str:
    bracketed = f"[{host}]" if ":" in host else host
    return bracketed if default_port else f"{bracketed}:{port}"


def _redirect_location(response: httpx.Response) -> str | None:
    """The ``Location`` of a redirect, ``None`` for any other response."""
    if response.status_code not in REDIRECT_STATUSES:
        return None
    location: str | None = response.headers.get("location")
    if not location:
        raise FetchRefused(detail="redirect without a location")
    return location


def _redirect_target(current: str, location: str) -> str:
    """``location`` resolved against the current URL; the guard checks it next."""
    try:
        return urljoin(current, location)
    except ValueError:
        raise FetchRefused(detail="malformed redirect") from None


async def _page(
    response: httpx.Response, requested_url: str, final_url: str, limits: FetchLimits
) -> FetchedPage:
    if not 200 <= response.status_code < 300:
        raise FetchRefused(detail=f"status {response.status_code}")
    media = parse_content_type(response.headers.get("content-type"))
    body = BoundedBody(coding_of(response.headers.get("content-encoding")), limits.max_bytes)
    await _read(response, body)
    return FetchedPage(
        requested_url=requested_url,
        final_url=final_url,
        status=response.status_code,
        content_type=media.media_type,
        text=decode_body(body.finish(), media.charset),
    )


async def _read(response: httpx.Response, body: BoundedBody) -> None:
    """Feed the raw body to ``body`` chunk by chunk; it refuses as soon as it is too large.

    The stream is read directly, not through ``aiter_raw``: a response built with its body
    already loaded (as a mock transport may answer) still has its raw bytes there.
    """
    stream = response.stream
    if not isinstance(stream, httpx.AsyncByteStream):  # pragma: no cover - async transports
        raise FetchRefused(detail="unreadable body")
    try:
        async for chunk in stream:
            body.feed(chunk)
    except httpx.TransportError as error:
        raise NetworkUnavailable(detail=type(error).__name__) from None
