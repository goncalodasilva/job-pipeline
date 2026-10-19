"""Guarded fetcher: content codings, malformed redirects and the system resolver (M4.1;
spec 2.9 rule 6; ADR-098 item 2).

The fetcher asks for ``gzip, deflate`` and decodes the raw body itself, so decoded output in
memory never passes the size cap by more than a byte. Exactly one coding of ``identity``,
``gzip`` or ``deflate`` is accepted; stacked, unrequested (``br``, ``zstd``) and unknown
codings are refused before the body is read, and bad compressed data is refused. A redirect
``Location`` that can't be parsed is refused, never a crash or a network error.

Responses are streamed (``stream=``) because ``content=`` makes httpx decode the body when
the mock builds the response. The resolver is a fake; nothing touches the network.
"""

import asyncio
import gzip
import socket
import tracemalloc
import zlib
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx
import pytest

from app.domain.errors import FetchRefused, NetworkUnavailable
from app.modules.fetch import FetchedPage, FetchLimits
from app.modules.fetch.providers.guarded import GuardedJobPageFetcher, system_resolver

PUBLIC_V4 = "93.184.215.14"
PUBLIC_V6 = "2a00:1450:4001:829::200e"
HOST = "jobs.example.com"
URL = f"https://{HOST}/postings/1"
PAGE = "<html><body><h1>Fictional Gizmo Analyst</h1><p>Café Fictício.</p></body></html>"
SAFETY_SECONDS = 5.0


async def _resolver(host: str) -> Sequence[str]:
    return [PUBLIC_V4]


class Chunks(httpx.AsyncByteStream):
    """Yields ``body`` in raw chunks; optionally fails after the first one."""

    def __init__(self, body: bytes, chunk_size: int = 512, fail_after: int | None = None):
        self._body = body
        self._chunk_size = chunk_size
        self._fail_after = fail_after
        self.chunks_sent = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for start in range(0, len(self._body), self._chunk_size):
            if self._fail_after is not None and self.chunks_sent >= self._fail_after:
                raise httpx.ReadError("fictional connection reset")
            self.chunks_sent += 1
            yield self._body[start : start + self._chunk_size]


class Server:
    """A ``MockTransport`` handler answering one streamed response; records requests."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._response


def _response(stream: Chunks, encoding: str | None, content_type: str = "text/html") -> Any:
    headers = {"content-type": f"{content_type}; charset=utf-8"}
    if encoding is not None:
        headers["content-encoding"] = encoding
    return httpx.Response(200, headers=headers, stream=stream)


async def _fetch(server: Server, limits: FetchLimits | None = None) -> FetchedPage:
    fetcher = GuardedJobPageFetcher(resolver=_resolver, transport=httpx.MockTransport(server))
    page: FetchedPage = await asyncio.wait_for(
        fetcher.fetch(URL, limits if limits is not None else FetchLimits()), SAFETY_SECONDS
    )
    return page


def _deflate(data: bytes, wbits: int) -> bytes:
    compressor = zlib.compressobj(9, zlib.DEFLATED, wbits)
    return compressor.compress(data) + compressor.flush()


# --- codings that decode ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("encoding", "body"),
    [
        (None, PAGE.encode()),
        ("identity", PAGE.encode()),
        ("gzip", gzip.compress(PAGE.encode())),
        ("GZIP", gzip.compress(PAGE.encode())),
        ("deflate", _deflate(PAGE.encode(), zlib.MAX_WBITS)),  # zlib-wrapped, per the RFC
        ("deflate", _deflate(PAGE.encode(), -zlib.MAX_WBITS)),  # raw, as some servers send
    ],
    ids=["none", "identity", "gzip", "gzip_upper", "deflate_zlib", "deflate_raw"],
)
async def test_supported_codings_decode(encoding: str | None, body: bytes) -> None:
    server = Server(_response(Chunks(body, chunk_size=7), encoding))

    page = await _fetch(server)

    assert page.text == PAGE
    (request,) = server.requests
    assert request.headers["accept-encoding"] == "gzip, deflate"


# --- codings that are refused -------------------------------------------------------------


@pytest.mark.parametrize(
    "encoding",
    ["gzip, gzip", "gzip,deflate", "deflate, identity", "br", "zstd", "compress", "x-unknown"],
)
async def test_stacked_unrequested_and_unknown_codings_refused_before_reading(
    encoding: str,
) -> None:
    stream = Chunks(gzip.compress(gzip.compress(b"\0" * 1_000_000)))
    server = Server(_response(stream, encoding))

    with pytest.raises(FetchRefused):
        await _fetch(server)
    assert stream.chunks_sent == 0


async def test_gzip_bomb_in_chunks_stops_early_and_stays_small_in_memory() -> None:
    bomb = gzip.compress(b"\0" * (64 * 1024 * 1024))  # about 64 KB on the wire
    limits = FetchLimits(max_bytes=100_000)

    stream = Chunks(bomb, chunk_size=512)
    with pytest.raises(FetchRefused):
        await _fetch(Server(_response(stream, "gzip")), limits)
    assert stream.chunks_sent <= 3

    # One raw chunk holding the whole bomb: still never inflated past the cap in memory.
    stream = Chunks(bomb, chunk_size=len(bomb))
    tracemalloc.start()
    try:
        with pytest.raises(FetchRefused):
            await _fetch(Server(_response(stream, "gzip")), limits)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024, peak


@pytest.mark.parametrize(
    ("encoding", "body"),
    [
        ("gzip", b"\x1f\x8b\x08\x00" + b"not really gzip data" * 10),
        ("gzip", gzip.compress(PAGE.encode())[:-12]),  # cut short
        ("deflate", b"\x00\xffneither zlib nor raw deflate" * 4),
    ],
    ids=["gzip_garbage", "gzip_truncated", "deflate_garbage"],
)
async def test_bad_compressed_data_refused(encoding: str, body: bytes) -> None:
    with pytest.raises(FetchRefused):
        await _fetch(Server(_response(Chunks(body, chunk_size=16), encoding)))


async def test_read_error_mid_body_is_network_unavailable() -> None:
    for encoding, body in ((None, PAGE.encode() * 50), ("gzip", gzip.compress(PAGE.encode() * 50))):
        stream = Chunks(body, chunk_size=64, fail_after=1)
        with pytest.raises(NetworkUnavailable):
            await _fetch(Server(_response(stream, encoding)))


# --- malformed redirects ------------------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    [
        "http://[::1",
        "//[::1/x",
        "http://[not-an-address]/",
        "http://exa mple.com/",
        "http://jobs.example.com:99999/",
        "http://jobs.example.com:port/",
    ],
)
async def test_malformed_redirect_location_refused(location: str) -> None:
    server = Server(httpx.Response(302, headers={"location": location}))

    with pytest.raises(FetchRefused):
        await _fetch(server)
    assert len(server.requests) == 1


async def test_url_too_long_for_httpx_refused() -> None:
    server = Server(httpx.Response(200, headers={"content-type": "text/plain"}))
    fetcher = GuardedJobPageFetcher(resolver=_resolver, transport=httpx.MockTransport(server))

    with pytest.raises(FetchRefused):
        await fetcher.fetch(f"https://{HOST}/{'a' * 70_000}", FetchLimits())
    assert server.requests == []


# --- the system resolver ------------------------------------------------------------------


async def test_system_resolver_answers_addresses_in_order_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[tuple[Any, ...]] = []

    async def getaddrinfo(host: str, port: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        asked.append((host, port, kwargs.get("type")))
        stream, v4, v6 = socket.SOCK_STREAM, socket.AF_INET, socket.AF_INET6
        return [
            (v6, stream, 6, "", (PUBLIC_V6, 0, 0, 0)),
            (v4, stream, 6, "", (PUBLIC_V4, 0)),
            (v6, stream, 6, "", (PUBLIC_V6, 0, 0, 0)),
            (v4, stream, 6, "", ("151.101.1.69", 0)),
        ]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo)

    assert await system_resolver(HOST) == [PUBLIC_V6, PUBLIC_V4, "151.101.1.69"]
    assert asked == [(HOST, None, socket.SOCK_STREAM)]
