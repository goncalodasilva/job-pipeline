"""The guarded job page fetcher (M4.1; AC-M4.1; spec 2.9 rule 6; ADR-020, ADR-036, ADR-098
item 2).

``GuardedJobPageFetcher(resolver=..., transport=...)`` in
``app.modules.fetch.providers.guarded`` is the only ``httpx`` user. For every hop it checks
the URL with the guard, then sends one request (redirects are never followed by ``httpx``)
to the checked address, keeping the original ``Host`` header and, for https, the TLS server
name (``sni_hostname`` request extension). Redirects are checked again, at most
``max_redirects``. Limits are clamped to ``FetchLimits()``: callers can only lower them.
No cookies, no environment proxies, text content types only, the size cap counted on
decoded bytes, one overall deadline that includes DNS. Error statuses raise
``FetchRefused``; connection failures and the deadline raise ``NetworkUnavailable``.

The resolver is a fake and HTTP goes to ``httpx.MockTransport``; nothing touches the network.
The addresses used as "public" answers are only numbers here. Every host and page is
fictional. The new names are imported inside the tests.
"""

import asyncio
import gzip
import importlib
import logging
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any

import httpx
import pytest

from app.domain.errors import FetchRefused, NetworkUnavailable
from app.modules.fetch import FetchedPage, FetchLimits

PUBLIC_V4 = "93.184.215.14"
PUBLIC_V4_OTHER = "151.101.1.69"
PUBLIC_V6 = "2a00:1450:4001:829::200e"
HOST = "jobs.example.com"
OTHER_HOST = "careers.example.org"
PAGE = b"<html><body><h1>Fictional Widget Engineer</h1><p>Build widgets.</p></body></html>"
HTML = "text/html; charset=utf-8"
# Bounds every await in these tests, so a missing deadline fails instead of hanging.
SAFETY_SECONDS = 5.0

Handler = Callable[[httpx.Request], httpx.Response]


def _guarded() -> Any:
    return importlib.import_module("app.modules.fetch.providers.guarded")


class FakeResolver:
    """Answers per host; a list of answer lists is used one per call (the last one repeats)."""

    def __init__(self, answers: Mapping[str, Sequence[str] | list[Sequence[str]]]) -> None:
        self._answers = answers
        self.calls: list[str] = []

    async def __call__(self, host: str) -> Sequence[str]:
        self.calls.append(host)
        found = self._answers.get(host, ())
        if found and not isinstance(found[0], str):
            rounds: list[Sequence[str]] = list(found)  # type: ignore[arg-type]
            index = min(self.calls.count(host) - 1, len(rounds) - 1)
            return list(rounds[index])
        return list(found)  # type: ignore[arg-type]


class Recorder:
    """A ``MockTransport`` handler that records requests and answers from a script."""

    def __init__(self, respond: Handler) -> None:
        self._respond = respond
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def _ok(body: bytes = PAGE, content_type: str = HTML, **headers: str) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": content_type, **headers}, content=body)


def _redirect(location: str, status: int = 302, **headers: str) -> httpx.Response:
    return httpx.Response(status, headers={"location": location, **headers})


def _fetcher(resolver: Any, recorder: Recorder) -> Any:
    return _guarded().GuardedJobPageFetcher(resolver=resolver, transport=recorder.transport)


async def _fetch(fetcher: Any, url: str, limits: FetchLimits | None = None) -> FetchedPage:
    page: FetchedPage = await asyncio.wait_for(
        fetcher.fetch(url, limits if limits is not None else FetchLimits()),
        timeout=SAFETY_SECONDS,
    )
    return page


def _resolver() -> FakeResolver:
    return FakeResolver({HOST: [PUBLIC_V4], OTHER_HOST: [PUBLIC_V4_OTHER]})


# --- redirects ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://internal.example.com/",
        "http://2130706433/",
    ],
    ids=["loopback", "metadata", "ipv6_loopback", "private_dns", "decimal_host"],
)
async def test_ac_m4_1_redirect_to_private_address_refused(location: str) -> None:
    resolver = FakeResolver({HOST: [PUBLIC_V4], "internal.example.com": ["10.0.0.5"]})
    recorder = Recorder(lambda request: _redirect(location))

    with pytest.raises(FetchRefused):
        await _fetch(_fetcher(resolver, recorder), f"https://{HOST}/postings/1")

    assert len(recorder.requests) == 1, "the refused hop was never requested"
    assert all(request.url.host == PUBLIC_V4 for request in recorder.requests)


@pytest.mark.parametrize(
    "location",
    [
        "file:///etc/passwd",
        f"ftp://{HOST}/postings/1",
        "javascript:alert(1)",
        "data:text/html,<p>Fictional</p>",
        f"gopher://{HOST}/1",
    ],
    ids=["file", "ftp", "javascript", "data", "gopher"],
)
async def test_redirect_to_other_scheme_refused(location: str) -> None:
    recorder = Recorder(lambda request: _redirect(location))

    with pytest.raises(FetchRefused):
        await _fetch(_fetcher(_resolver(), recorder), f"https://{HOST}/postings/1")

    assert len(recorder.requests) == 1


def _chain(hops: int, final: bytes = PAGE) -> Handler:
    """``/hop/0`` redirects to ``/hop/1`` and so on; ``/hop/<hops>`` answers the page.

    Hops 1, 5, 9... move to the other host and hops 3, 7... back, with absolute URLs; even
    hops use a relative Location on the current host. So the hosts are HOST, OTHER, OTHER,
    HOST, HOST, OTHER, OTHER and both forms are resolved and rechecked.
    """

    def respond(request: httpx.Request) -> httpx.Response:
        index = int(request.url.path.rsplit("/", 1)[-1])
        if index >= hops:
            return _ok(final)
        following = index + 1
        if following % 2 == 1:
            target = OTHER_HOST if following % 4 == 1 else HOST
            return _redirect(f"https://{target}/hop/{following}", status=302)
        return _redirect(f"/hop/{following}", status=301 if following % 4 == 0 else 307)

    return respond


async def test_redirects_rechecked_and_capped_at_5(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    # Five redirects are followed, each hop rechecked against its own host.
    resolver = _resolver()
    recorder = Recorder(_chain(5))
    page = await _fetch(_fetcher(resolver, recorder), f"https://{HOST}/hop/0")

    assert len(recorder.requests) == 6
    assert resolver.calls == [HOST, OTHER_HOST, OTHER_HOST, HOST, HOST, OTHER_HOST]
    assert [request.url.host for request in recorder.requests] == [
        PUBLIC_V4,
        PUBLIC_V4_OTHER,
        PUBLIC_V4_OTHER,
        PUBLIC_V4,
        PUBLIC_V4,
        PUBLIC_V4_OTHER,
    ]
    assert [request.headers["host"] for request in recorder.requests] == resolver.calls
    assert page.requested_url == f"https://{HOST}/hop/0"
    assert page.final_url == f"https://{OTHER_HOST}/hop/5"
    assert page.status == 200

    # A sixth redirect is refused without being requested.
    recorder = Recorder(_chain(6))
    with pytest.raises(FetchRefused):
        await _fetch(_fetcher(_resolver(), recorder), f"https://{HOST}/hop/0")
    assert len(recorder.requests) == 6

    # A redirect without a Location is refused.
    recorder = Recorder(lambda request: httpx.Response(302))
    with pytest.raises(FetchRefused):
        await _fetch(_fetcher(_resolver(), recorder), f"https://{HOST}/hop/0")

    # The URL is never logged: no host or path of any hop appears in any record.
    logged = "\n".join(record.getMessage() for record in caplog.records)
    for secret_part in (HOST, OTHER_HOST, "/hop/", PUBLIC_V4, PUBLIC_V4_OTHER):
        assert secret_part not in logged


async def test_connection_pinned_to_checked_address_with_host_and_tls_name() -> None:
    # https on the default port, with a query.
    recorder = Recorder(lambda request: _ok())
    resolver = _resolver()
    page = await _fetch(_fetcher(resolver, recorder), f"https://{HOST}/postings/1?ref=feed&x=2")

    (request,) = recorder.requests
    assert request.url.scheme == "https"
    assert request.url.host == PUBLIC_V4
    assert request.url.port is None
    assert request.url.path == "/postings/1"
    assert request.url.query == b"ref=feed&x=2"
    assert request.headers["host"] == HOST
    assert request.extensions.get("sni_hostname") == HOST
    assert page.final_url == f"https://{HOST}/postings/1?ref=feed&x=2"
    assert resolver.calls == [HOST]

    # A non-default port is kept on the address and in the Host header.
    recorder = Recorder(lambda request: _ok())
    await _fetch(_fetcher(_resolver(), recorder), f"https://{HOST}:8443/x")
    (request,) = recorder.requests
    assert (request.url.host, request.url.port) == (PUBLIC_V4, 8443)
    assert request.headers["host"] == f"{HOST}:8443"
    assert request.extensions.get("sni_hostname") == HOST

    # An IPv6 answer is used as a bracketed literal.
    recorder = Recorder(lambda request: _ok())
    await _fetch(_fetcher(FakeResolver({HOST: [PUBLIC_V6]}), recorder), f"http://{HOST}/x")
    (request,) = recorder.requests
    assert request.url.host == PUBLIC_V6
    assert f"[{PUBLIC_V6}]" in str(request.url)
    assert request.headers["host"] == HOST

    # DNS rebinding: public on the first answer, private afterwards. One lookup per hop, and
    # the connection uses that lookup's answer.
    rebinding = FakeResolver({HOST: [[PUBLIC_V4], ["127.0.0.1"]]})
    recorder = Recorder(lambda request: _ok())
    await _fetch(_fetcher(rebinding, recorder), f"https://{HOST}/postings/1")
    assert rebinding.calls == [HOST]
    assert [request.url.host for request in recorder.requests] == [PUBLIC_V4]

    # A redirect back to the same host is looked up again and the private answer refused.
    rebinding = FakeResolver({HOST: [[PUBLIC_V4], ["127.0.0.1"]]})
    recorder = Recorder(
        lambda request: _redirect("/second") if request.url.path == "/first" else _ok()
    )
    with pytest.raises(FetchRefused):
        await _fetch(_fetcher(rebinding, recorder), f"https://{HOST}/first")
    assert rebinding.calls == [HOST, HOST]
    assert [request.url.host for request in recorder.requests] == [PUBLIC_V4]


# --- size, time and limits ----------------------------------------------------------------


class EndlessStream(httpx.AsyncByteStream):
    """Yields chunks until told to stop; counts what was taken."""

    def __init__(self, chunk: bytes = b"a" * 65536) -> None:
        self._chunk = chunk
        self.chunks_sent = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            self.chunks_sent += 1
            yield self._chunk


class ChunkedStream(httpx.AsyncByteStream):
    """Yields ``body`` in fixed-size raw chunks, as a server would; counts what was taken."""

    def __init__(self, body: bytes, chunk_size: int = 512) -> None:
        self._body = body
        self._chunk_size = chunk_size
        self.chunks_sent = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for start in range(0, len(self._body), self._chunk_size):
            self.chunks_sent += 1
            yield self._body[start : start + self._chunk_size]


def _streamed(body: bytes, content_type: str, encoding: str | None = None) -> httpx.Response:
    """A streamed response (``content=`` would make httpx decode it when built)."""
    headers = {"content-type": content_type}
    if encoding is not None:
        headers["content-encoding"] = encoding
    return httpx.Response(200, headers=headers, stream=ChunkedStream(body))


async def test_body_over_5mb_stops_reading_including_gzip_bomb() -> None:
    # At the lowered limit exactly: accepted. One byte more: refused.
    limits = FetchLimits(max_bytes=1000)
    recorder = Recorder(lambda request: _ok(b"a" * 1000, "text/plain"))
    page = await _fetch(_fetcher(_resolver(), recorder), f"https://{HOST}/a", limits)
    assert len(page.text) == 1000
    recorder = Recorder(lambda request: _ok(b"a" * 1001, "text/plain"))
    with pytest.raises(FetchRefused):
        await _fetch(_fetcher(_resolver(), recorder), f"https://{HOST}/a", limits)

    # Counted on decoded bytes: a small gzip body that inflates past the limit, streamed in
    # chunks so the fetcher (not the mock) does the decoding.
    bomb = gzip.compress(b"a" * 50_000)
    assert len(bomb) < 1000
    recorder = Recorder(lambda request: _streamed(bomb, "text/plain", "gzip"))
    with pytest.raises(FetchRefused):
        await _fetch(_fetcher(_resolver(), recorder), f"https://{HOST}/a", limits)

    # The same at the default 5 MB cap: about 6 MB of zeros in a few KB.
    big_bomb = gzip.compress(b"\0" * (6 * 1024 * 1024))
    recorder = Recorder(lambda request: _streamed(big_bomb, "text/plain", "gzip"))
    with pytest.raises(FetchRefused):
        await _fetch(_fetcher(_resolver(), recorder), f"https://{HOST}/a")

    # Reading stops once over the limit: an endless body is abandoned early.
    stream = EndlessStream()
    recorder = Recorder(
        lambda request: httpx.Response(200, headers={"content-type": "text/plain"}, stream=stream)
    )
    with pytest.raises(FetchRefused):
        await _fetch(
            _fetcher(_resolver(), recorder), f"https://{HOST}/a", FetchLimits(max_bytes=100_000)
        )
    assert stream.chunks_sent <= 4


class StalledStream(httpx.AsyncByteStream):
    """Sends one chunk, then waits for an event that never comes (no sleep)."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"<html><body><p>Fictional start"
        await asyncio.Event().wait()


async def test_overall_deadline_15s() -> None:
    assert FetchLimits().timeout_seconds == 15.0
    limits = FetchLimits(timeout_seconds=0.2)

    # A DNS lookup that never answers counts against the deadline.
    async def stalled_resolver(host: str) -> Sequence[str]:
        await asyncio.Event().wait()
        return [PUBLIC_V4]

    recorder = Recorder(lambda request: _ok())
    with pytest.raises(NetworkUnavailable):
        await _fetch(_fetcher(stalled_resolver, recorder), f"https://{HOST}/a", limits)
    assert recorder.requests == []

    # The deadline covers the whole fetch, not each hop: three redirects of 0.1 s each pass
    # a 0.2 s limit.
    async def slow_hops(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.1)
        index = int(request.url.path.rsplit("/", 1)[-1])
        return _redirect(f"/hop/{index + 1}") if index < 3 else _ok()

    slow = _guarded().GuardedJobPageFetcher(
        resolver=_resolver(), transport=httpx.MockTransport(slow_hops)
    )
    with pytest.raises(NetworkUnavailable):
        await _fetch(slow, f"https://{HOST}/hop/0", limits)

    # A body that stalls halfway counts too.
    recorder = Recorder(
        lambda request: httpx.Response(200, headers={"content-type": HTML}, stream=StalledStream())
    )
    with pytest.raises(NetworkUnavailable):
        await _fetch(_fetcher(_resolver(), recorder), f"https://{HOST}/a", limits)


async def test_limits_can_only_be_lowered() -> None:
    # Asking for 50 MB still stops at 5 MB.
    body = b"a" * (5 * 1024 * 1024 + 1)
    recorder = Recorder(lambda request: _ok(body, "text/plain"))
    with pytest.raises(FetchRefused):
        await _fetch(
            _fetcher(_resolver(), recorder),
            f"https://{HOST}/a",
            FetchLimits(max_bytes=50 * 1024 * 1024),
        )
    # Exactly 5 MB is fine.
    recorder = Recorder(lambda request: _ok(body[:-1], "text/plain"))
    page = await _fetch(_fetcher(_resolver(), recorder), f"https://{HOST}/a")
    assert len(page.text) == 5 * 1024 * 1024

    # Asking for 50 redirects still stops after 5.
    recorder = Recorder(_chain(6))
    with pytest.raises(FetchRefused):
        await _fetch(
            _fetcher(_resolver(), recorder), f"https://{HOST}/hop/0", FetchLimits(max_redirects=50)
        )
    assert len(recorder.requests) == 6

    # Lower limits take effect.
    recorder = Recorder(_chain(3))
    with pytest.raises(FetchRefused):
        await _fetch(
            _fetcher(_resolver(), recorder), f"https://{HOST}/hop/0", FetchLimits(max_redirects=2)
        )
    assert len(recorder.requests) == 3
    recorder = Recorder(_chain(2))
    page = await _fetch(
        _fetcher(_resolver(), recorder), f"https://{HOST}/hop/0", FetchLimits(max_redirects=2)
    )
    assert page.status == 200
    recorder = Recorder(_chain(1))
    with pytest.raises(FetchRefused):
        await _fetch(
            _fetcher(_resolver(), recorder), f"https://{HOST}/hop/0", FetchLimits(max_redirects=0)
        )
    recorder = Recorder(lambda request: _ok(b"a" * 101, "text/plain"))
    with pytest.raises(FetchRefused):
        await _fetch(
            _fetcher(_resolver(), recorder), f"https://{HOST}/a", FetchLimits(max_bytes=100)
        )


# --- cookies and proxies ------------------------------------------------------------------


PROXY_VARIABLES = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy")


async def test_no_cookies_and_env_proxies_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in PROXY_VARIABLES:
        monkeypatch.setenv(name, "http://proxy.invalid:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/first":
            return _redirect("/second", **{"set-cookie": "session=fictional-0001; Path=/"})
        return _ok(**{"set-cookie": "tracker=fictional-0002; Path=/"})

    recorder = Recorder(respond)
    fetcher = _fetcher(_resolver(), recorder)
    await _fetch(fetcher, f"https://{HOST}/first")
    # The same fetcher again: nothing remembered from the first fetch.
    await _fetch(fetcher, f"https://{HOST}/first")

    assert len(recorder.requests) == 4, "every request reached the transport, not a proxy"
    for request in recorder.requests:
        assert "cookie" not in request.headers
        assert "proxy-authorization" not in request.headers

    # Without an injected transport, no transport is built for the environment's proxy.
    built_with_proxy: list[object] = []
    handled: list[httpx.Request] = []
    original_init = httpx.AsyncHTTPTransport.__init__

    def recording_init(self: httpx.AsyncHTTPTransport, *args: Any, **kwargs: Any) -> None:
        if kwargs.get("proxy") is not None:
            built_with_proxy.append(kwargs["proxy"])
        original_init(self, *args, **kwargs)

    async def answer(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        handled.append(request)
        return _ok()

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "__init__", recording_init)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", answer)

    default = _guarded().GuardedJobPageFetcher(resolver=_resolver())
    page = await _fetch(default, f"https://{HOST}/postings/1")

    assert page.status == 200
    assert built_with_proxy == []
    assert [request.url.host for request in handled] == [PUBLIC_V4]


# --- content type, status, errors ---------------------------------------------------------


@pytest.mark.parametrize(
    "content_type",
    [
        "application/json",
        "application/pdf",
        "image/png",
        "application/octet-stream",
        "application/javascript",
        "multipart/form-data; boundary=x",
        "texty/html",
        None,
    ],
)
async def test_non_text_content_type_refused(content_type: str | None) -> None:
    headers = {} if content_type is None else {"content-type": content_type}
    recorder = Recorder(lambda request: httpx.Response(200, headers=headers, content=PAGE))

    with pytest.raises(FetchRefused):
        await _fetch(_fetcher(_resolver(), recorder), f"https://{HOST}/a")


@pytest.mark.parametrize(
    ("content_type", "media_type"),
    [
        ("text/html; charset=utf-8", "text/html"),
        ("text/plain", "text/plain"),
        ("application/xhtml+xml; charset=utf-8", "application/xhtml+xml"),
        ("TEXT/HTML", "text/html"),
    ],
)
async def test_text_content_types_accepted(content_type: str, media_type: str) -> None:
    recorder = Recorder(lambda request: _ok(PAGE, content_type))

    page = await _fetch(_fetcher(_resolver(), recorder), f"https://{HOST}/a")

    assert isinstance(page, FetchedPage)
    assert page.content_type == media_type
    assert page.requested_url == page.final_url == f"https://{HOST}/a"
    assert page.status == 200
    assert "Fictional Widget Engineer" in page.text


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 429, 500, 502, 503])
async def test_http_error_status_refused(status: int) -> None:
    recorder = Recorder(lambda request: httpx.Response(status, headers={"content-type": HTML}))

    with pytest.raises(FetchRefused):
        await _fetch(_fetcher(_resolver(), recorder), f"https://{HOST}/a")


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError, httpx.RemoteProtocolError],
)
async def test_connect_error_maps_to_network_unavailable(error: type[httpx.HTTPError]) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise error("fictional failure", request=request)  # type: ignore[call-arg]

    with pytest.raises(NetworkUnavailable):
        await _fetch(_fetcher(_resolver(), Recorder(respond)), f"https://{HOST}/a")


# --- decoding -----------------------------------------------------------------------------


def _decoded(body: bytes, content_type: str) -> str:
    recorder = Recorder(lambda request: _ok(body, content_type))
    page = asyncio.run(_fetch(_fetcher(_resolver(), recorder), f"https://{HOST}/a"))
    return page.text


def test_declared_charset_then_meta_then_utf8() -> None:
    cafe_latin1 = "Café Fictício".encode("iso-8859-1")
    cafe_utf8 = "Café Fictício".encode()
    expected = "Café Fictício"

    # The charset in the Content-Type header comes first.
    assert expected in _decoded(b"<p>" + cafe_latin1 + b"</p>", "text/html; charset=iso-8859-1")
    meta_latin1 = b'<html><head><meta charset="iso-8859-1"></head><body><p>'
    assert expected in _decoded(meta_latin1 + cafe_utf8 + b"</p>", "text/html; charset=utf-8")

    # Then a meta charset or http-equiv in the first 2 KB.
    assert expected in _decoded(meta_latin1 + cafe_latin1 + b"</p>", "text/html")
    http_equiv = (
        b'<html><head><meta http-equiv="Content-Type" '
        b'content="text/html; charset=windows-1252"></head><body><p>'
    )
    assert "“quoted”" in _decoded(http_equiv + b"\x93quoted\x94" + b"</p>", "text/html")

    # A meta tag past the first 2 KB is not looked at: UTF-8 with replacement instead.
    late_meta = b"<!--" + b"x" * 2100 + b'--><meta charset="iso-8859-1"><p>'
    assert "Caf�" in _decoded(late_meta + cafe_latin1 + b"</p>", "text/html")

    # Nothing declared: UTF-8, and bytes that aren't UTF-8 become replacement characters.
    assert expected in _decoded(b"<p>" + cafe_utf8 + b"</p>", "text/html")
    assert "Caf�" in _decoded(b"<p>" + cafe_latin1 + b"</p>", "text/html")
    assert "Caf�" in _decoded(cafe_latin1, "text/plain")
