"""The ASGI stream driver used by the event stream tests (M2.1; ``tests/asgi_stream.py``).

The driver is test infrastructure, so it is tested against tiny hand-written ASGI apps (and one
Starlette ``StreamingResponse`` route), never against the real application. Waits that are
expected to time out use a few hundredths of a second.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from starlette.types import Message, Receive, Scope, Send

from tests.asgi_stream import app_lifespan, open_stream

PORT = 8123
SHORT = 0.05


async def _start(
    send: Send, status: int = 200, headers: list[tuple[bytes, bytes]] | None = None
) -> None:
    await send({"type": "http.response.start", "status": status, "headers": headers or []})


async def _body(send: Send, body: bytes, *, more: bool = True) -> None:
    await send({"type": "http.response.body", "body": body, "more_body": more})


def two_step_app(release: asyncio.Event) -> Any:
    """Sends ``one`` at once and ``two`` (the end) after ``release`` is set."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await _start(
            send, 200, [(b"Content-Type", b"text/plain"), (b"X-Twice", b"a"), (b"x-twice", b"b")]
        )
        await _body(send, b"one")
        await release.wait()
        await _body(send, b"two", more=False)

    return app


def listening_app() -> Any:
    """Streams ``tick`` once, then waits for the client to disconnect and returns."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await _start(send)
        await _body(send, b"tick")
        while (await receive())["type"] != "http.disconnect":
            pass

    return app


def deaf_app() -> Any:
    """Streams ``tick`` and then never ends, whatever the client does."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await _start(send)
        await _body(send, b"tick")
        await asyncio.Event().wait()

    return app


# --- response and request shape --------------------------------------------------------------


async def test_status_and_headers_are_set_when_the_stream_opens() -> None:
    release = asyncio.Event()
    release.set()
    async with open_stream(two_step_app(release), "/x", port=PORT) as stream:
        assert stream.status == 200
        assert stream.headers["content-type"] == ["text/plain"], "names are lower-case lists"
        assert stream.headers["x-twice"] == ["a", "b"], "a repeated header keeps every value"
        assert stream.header("Content-Type") == "text/plain"
        assert stream.header("x-missing") is None
        with pytest.raises(AssertionError, match="repeated"):
            stream.header("X-Twice")


async def test_scope_carries_client_server_host_path_query_and_given_headers() -> None:
    seen: list[Scope] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope)
        await _start(send, 204)
        await _body(send, b"", more=False)

    headers = {"X-JP-Token": "fictional-token", "Origin": f"http://127.0.0.1:{PORT}"}
    async with open_stream(app, "/events?a=1", port=PORT, headers=headers, method="post") as stream:
        await stream.wait_finished()

    (scope,) = seen
    assert scope["type"] == "http" and scope["method"] == "POST"
    assert scope["path"] == "/events" and scope["query_string"] == b"a=1"
    assert scope["server"] == ("127.0.0.1", PORT)
    assert scope["client"][0] == "127.0.0.1"
    assert scope["asgi"]["spec_version"] == "2.3"
    sent = dict(scope["headers"])
    assert sent[b"host"] == f"127.0.0.1:{PORT}".encode()
    assert sent[b"x-jp-token"] == b"fictional-token", "header names are lower-cased"
    assert sent[b"origin"] == f"http://127.0.0.1:{PORT}".encode()


async def test_a_host_header_in_headers_replaces_the_default() -> None:
    seen: list[Scope] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope)
        await _start(send, 204)
        await _body(send, b"", more=False)

    async with open_stream(app, "/", port=PORT, headers={"Host": "evil.example:1"}):
        pass

    assert [value for name, value in seen[0]["headers"] if name == b"host"] == [b"evil.example:1"]


async def test_receive_gives_an_empty_request_first_then_waits_for_the_disconnect() -> None:
    received: list[Message] = []
    second_wait_started = asyncio.Event()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        received.append(await receive())
        await _start(send)
        await _body(send, b"go")
        second_wait_started.set()
        received.append(await receive())

    async with open_stream(app, "/", port=PORT) as stream:
        assert await stream.next_chunk() == b"go"
        await asyncio.wait_for(second_wait_started.wait(), 2.0)
        assert len(received) == 1, "the second receive() must block until the disconnect"
        await stream.disconnect()
        await stream.wait_finished()

    assert received == [
        {"type": "http.request", "body": b"", "more_body": False},
        {"type": "http.disconnect"},
    ]


async def test_receive_reports_disconnect_once_the_response_is_complete() -> None:
    received: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        received.append((await receive())["type"])
        await _start(send)
        await _body(send, b"done", more=False)
        received.append((await receive())["type"])  # like uvicorn: the client is "gone"

    async with open_stream(app, "/", port=PORT) as stream:
        await stream.wait_finished()

    assert received == ["http.request", "http.disconnect"]


# --- chunks, completion and deadlines --------------------------------------------------------


async def test_chunks_arrive_while_the_response_is_still_open() -> None:
    release = asyncio.Event()
    async with open_stream(two_step_app(release), "/x", port=PORT) as stream:
        assert await stream.next_chunk() == b"one"
        assert not stream.finished and not stream.response_complete
        release.set()
        assert await stream.next_chunk() == b"two"
        await stream.wait_finished()
        assert stream.finished and stream.response_complete


async def test_empty_body_messages_are_not_chunks() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await _start(send)
        await _body(send, b"")
        await _body(send, b"data")
        await _body(send, b"", more=False)

    async with open_stream(app, "/", port=PORT) as stream:
        assert await stream.next_chunk() == b"data"
        with pytest.raises(AssertionError, match="ended before another chunk"):
            await stream.next_chunk()
        assert stream.response_complete


async def test_read_to_end_returns_the_rest_of_a_finished_response() -> None:
    release = asyncio.Event()
    release.set()
    async with open_stream(two_step_app(release), "/x", port=PORT) as stream:
        assert await stream.next_chunk() == b"one"
        assert await stream.read_to_end() == b"two"
        assert await stream.read_to_end() == b"", "nothing is returned twice"


async def test_read_to_end_times_out_on_an_open_response() -> None:
    async with open_stream(listening_app(), "/x", port=PORT) as stream:
        with pytest.raises(AssertionError, match="did not finish within"):
            await stream.read_to_end(SHORT)


async def test_next_chunk_times_out_with_a_message_instead_of_hanging() -> None:
    async with open_stream(listening_app(), "/x", port=PORT) as stream:
        assert await stream.next_chunk() == b"tick"
        with pytest.raises(AssertionError, match=r"GET /x: no chunk arrived within 0\.05 s"):
            await stream.next_chunk(SHORT)


async def test_wait_finished_times_out_with_a_message() -> None:
    async with open_stream(listening_app(), "/x", port=PORT) as stream:
        with pytest.raises(AssertionError, match="did not finish within"):
            await stream.wait_finished(SHORT)
        assert not stream.finished


async def test_wait_finished_raises_what_the_app_raised() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await _start(send)
        await _body(send, b"first")
        raise ValueError("fictional app failure")

    with pytest.raises(ValueError, match="fictional app failure"):
        async with open_stream(app, "/", port=PORT) as stream:
            assert await stream.next_chunk() == b"first"
            await stream.wait_finished()


# --- leaving the block, and failures to start ------------------------------------------------


async def test_leaving_the_block_disconnects_and_waits_for_the_app() -> None:
    async with open_stream(listening_app(), "/", port=PORT) as stream:
        assert await stream.next_chunk() == b"tick"
        assert not stream.finished

    assert stream.finished, "the exit delivered a disconnect and the app returned"


async def test_an_app_that_ignores_the_disconnect_fails_the_test_and_is_cancelled() -> None:
    holder: list[Any] = []
    with pytest.raises(AssertionError, match=r"did not finish within 0\.05 s of the client"):
        async with open_stream(deaf_app(), "/", port=PORT, close_timeout=SHORT) as stream:
            holder.append(stream)
            assert await stream.next_chunk() == b"tick"

    assert holder[0].finished, "no task is left running"


async def test_open_stream_fails_when_no_response_starts() -> None:
    started = asyncio.Event()

    async def silent(scope: Scope, receive: Receive, send: Send) -> None:
        started.set()
        await asyncio.Event().wait()

    with pytest.raises(AssertionError, match=r"no response started within 0\.05 s"):
        async with open_stream(silent, "/", port=PORT, start_timeout=SHORT, close_timeout=SHORT):
            pytest.fail("the block must not run")

    assert started.is_set()
    leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert leftovers == [], "the app task was cancelled and awaited"


async def test_open_stream_fails_when_the_app_returns_without_responding() -> None:
    async def lazy(scope: Scope, receive: Receive, send: Send) -> None:
        return None

    with pytest.raises(AssertionError, match="returned without sending a response"):
        async with open_stream(lazy, "/", port=PORT):
            pytest.fail("the block must not run")


async def test_open_stream_raises_the_apps_error_when_it_fails_before_responding() -> None:
    async def broken(scope: Scope, receive: Receive, send: Send) -> None:
        raise KeyError("fictional")

    with pytest.raises(KeyError):
        async with open_stream(broken, "/", port=PORT):
            pytest.fail("the block must not run")


# --- the real thing it exists for ------------------------------------------------------------


@asynccontextmanager
async def _fastapi_stream_app() -> AsyncIterator[FastAPI]:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    ticks = asyncio.Event()

    async def endless() -> AsyncIterator[bytes]:
        yield b"first"
        await ticks.wait()  # never set: the stream only ends when the client goes away

    app.add_api_route("/endless", lambda: StreamingResponse(endless()), methods=["GET"])
    yield app


async def test_starlette_streaming_response_ends_when_the_driver_disconnects() -> None:
    async with _fastapi_stream_app() as app, open_stream(app, "/endless", port=PORT) as stream:
        assert stream.status == 200
        assert await stream.next_chunk() == b"first"
        assert not stream.finished, "an endless stream stays open"
        await stream.disconnect()
        await stream.wait_finished()  # returns normally: a disconnect is not an error
        assert not stream.response_complete, "no final message is sent to a gone client"


async def test_app_lifespan_runs_startup_before_and_shutdown_after_the_block() -> None:
    events: list[str] = []

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        events.append("startup")
        try:
            yield
        finally:
            events.append("shutdown")

    app = FastAPI(lifespan=lifespan)
    async with app_lifespan(app):
        events.append("inside")

    assert events == ["startup", "inside", "shutdown"]
