"""Drive an ASGI app with raw messages, so streaming responses can be tested (M2.1).

httpx's ``ASGITransport`` buffers a whole response before returning it, and Starlette's
``TestClient`` blocks on a response that never ends. This driver calls the app as a server
would (``app(scope, receive, send)``) in a task and hands the test each ``http.response.body``
message as it arrives, so a test can see that a chunk reaches the server while the response is
still open, and can end the response from the client side.

Typical use (``port`` must match the app's configured port, or the host check refuses)::

    async with app_lifespan(app):
        async with open_stream(app, "/events", port=8000, headers={"X-JP-Token": token}) as stream:
            assert stream.status == 200
            assert await stream.next_chunk() == b"event: resync\\ndata: {}\\n\\n"
            assert not stream.finished  # the response is still open
            await stream.disconnect()
            await stream.wait_finished()

Behaviour worth knowing:

- ``open_stream`` returns once the response has started, so ``status`` and ``headers`` are set.
  It fails with a clear ``AssertionError`` (or the app's own exception) when no response starts.
- ``headers`` maps lower-case names to a list of values in the order sent, so a repeated header
  is visible. ``header(name)`` returns the single value (or ``None``) and fails on a repeat.
- ``receive()`` first returns an empty ``http.request`` (``more_body`` false), then blocks until
  ``disconnect()`` is called, when it returns ``http.disconnect``. As with uvicorn, it also
  returns ``http.disconnect`` once the app has sent its final body message.
- The scope has ``asgi.spec_version`` 2.3 like uvicorn, so Starlette's ``StreamingResponse``
  listens for the disconnect through ``receive`` the way it does under the real server.
- Every wait has a deadline and ends in an ``AssertionError`` naming what was missing; nothing
  sleeps for a fixed time and nothing can hang the test run.
- Leaving the ``async with`` delivers a disconnect and waits for the app task to end. If the
  app ignores the disconnect, the task is cancelled and the test fails, so no task is leaked.
"""

import asyncio
import contextlib
from collections import deque
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.types import ASGIApp, Message, Scope

DEFAULT_TIMEOUT = 2.0
LOOPBACK = "127.0.0.1"
CLIENT_PORT = 50000  # the pretend client's port


@asynccontextmanager
async def app_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the app's startup and shutdown (its lifespan) around the block."""
    async with app.router.lifespan_context(app):
        yield


class Stream:
    """One request in flight, seen from the client side. Create it with ``open_stream``."""

    def __init__(self, method: str, path: str) -> None:
        self.method = method
        self.path = path
        self.status = 0
        self.headers: dict[str, list[str]] = {}
        self._started: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._changed = asyncio.Event()  # a chunk arrived, the response ended or the task ended
        self._disconnected = asyncio.Event()
        self._chunks: deque[bytes] = deque()
        self._request_sent = False
        self._complete = False
        self._task: asyncio.Task[None] | None = None

    # --- what the test sees ---------------------------------------------------------------

    @property
    def finished(self) -> bool:
        """The app has returned (or failed): nothing more will arrive."""
        return self._app_task.done()

    @property
    def response_complete(self) -> bool:
        """The app sent its final body message (``more_body`` false)."""
        return self._complete

    def header(self, name: str) -> str | None:
        """The one value of a response header, ``None`` when absent; fails on a repeat."""
        values = self.headers.get(name.lower(), [])
        assert len(values) <= 1, f"response header {name!r} is repeated: {values}"
        return values[0] if values else None

    async def next_chunk(self, timeout: float = DEFAULT_TIMEOUT) -> bytes:
        """The next non-empty body chunk, as soon as the app sends it."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if self._chunks:
                return self._chunks.popleft()
            if self._complete or self._app_task.done():
                self._raise_app_error()
                raise AssertionError(
                    f"{self._label}: the response ended before another chunk arrived"
                )
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AssertionError(
                    f"{self._label}: no chunk arrived within {timeout} s "
                    f"(response open: {not self._complete})"
                )
            self._changed.clear()
            with contextlib.suppress(TimeoutError):  # the deadline check above reports it
                await asyncio.wait_for(self._changed.wait(), remaining)

    async def read_to_end(self, timeout: float = DEFAULT_TIMEOUT) -> bytes:
        """Wait for the response to end and return every chunk not yet taken, joined."""
        await self.wait_finished(timeout)
        body = b"".join(self._chunks)
        self._chunks.clear()
        return body

    async def disconnect(self) -> None:
        """The client goes away: ``receive()`` now returns ``http.disconnect``."""
        self._disconnected.set()
        await asyncio.sleep(0)  # let the app notice before the test goes on

    async def wait_finished(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        """Wait for the app to return; raise whatever the app raised."""
        done, _ = await asyncio.wait({self._app_task}, timeout=timeout)
        if not done:
            raise AssertionError(f"{self._label}: the app did not finish within {timeout} s")
        self._raise_app_error()

    # --- the ASGI side, called by the app -------------------------------------------------

    async def _receive(self) -> Message:
        if not self._request_sent:
            self._request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._disconnected.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: Message) -> None:
        kind = message["type"]
        if kind == "http.response.start":
            if self._started.done():
                raise RuntimeError("the app started the response twice")
            self.status = int(message["status"])
            for name, value in message.get("headers", []):
                self.headers.setdefault(bytes(name).decode("latin-1").lower(), []).append(
                    bytes(value).decode("latin-1")
                )
            self._started.set_result(None)
        elif kind == "http.response.body":
            if not self._started.done():
                raise RuntimeError("the app sent a body before the response started")
            if self._complete:
                raise RuntimeError("the app sent a body after the final message")
            body = bytes(message.get("body", b""))
            if body:
                self._chunks.append(body)
            if not message.get("more_body", False):
                self._complete = True
                self._disconnected.set()  # as uvicorn: a finished response reads as disconnected
            self._changed.set()
        else:
            raise RuntimeError(f"unsupported ASGI message {kind!r}")

    # --- plumbing -------------------------------------------------------------------------

    @property
    def _label(self) -> str:
        return f"{self.method} {self.path}"

    @property
    def _app_task(self) -> asyncio.Task[None]:
        assert self._task is not None, "the stream has not been started"
        return self._task

    def _raise_app_error(self) -> None:
        task = self._app_task
        if task.done() and not task.cancelled():
            error = task.exception()
            if error is not None:
                raise error

    def _attach(self, task: asyncio.Task[None]) -> None:
        self._task = task
        task.add_done_callback(lambda _: self._changed.set())

    async def _wait_for_response(self, timeout: float) -> None:
        task = self._app_task
        await asyncio.wait(
            {self._started, task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if self._started.done():
            return
        if task.done():
            self._raise_app_error()
            raise AssertionError(f"{self._label}: the app returned without sending a response")
        raise AssertionError(f"{self._label}: no response started within {timeout} s")

    async def _abort(self, timeout: float) -> None:
        """Cancel the app task if it still runs, and mark any error it raised as seen."""
        task = self._app_task
        if not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=timeout)
        if task.done() and not task.cancelled():
            task.exception()

    async def _close(self, timeout: float) -> None:
        """Disconnect, wait for the app to end, and never leave its task running."""
        task = self._app_task
        self._disconnected.set()
        try:
            await asyncio.wait({task}, timeout=timeout)
        finally:
            ignored = not task.done()
            await self._abort(timeout)
        if ignored:
            raise AssertionError(
                f"{self._label}: the app did not finish within {timeout} s of the client "
                "disconnecting (its task was cancelled)"
            )
        self._raise_app_error()


def http_scope(
    path: str, *, port: int, method: str = "GET", headers: Mapping[str, str] | None = None
) -> Scope:
    """An HTTP/1.1 request scope as uvicorn builds it, from a pretend loopback client.

    The ``host`` header is ``127.0.0.1:<port>`` unless ``headers`` names one. ``path`` may carry
    a query string after ``?``.
    """
    path, _, query = path.partition("?")
    sent = {"host": f"{LOOPBACK}:{port}"}
    sent.update({name.lower(): value for name, value in (headers or {}).items()})
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "root_path": "",
        "query_string": query.encode("latin-1"),
        "headers": [
            (name.encode("latin-1"), value.encode("latin-1")) for name, value in sent.items()
        ],
        "client": (LOOPBACK, CLIENT_PORT),
        "server": (LOOPBACK, port),
    }


@asynccontextmanager
async def open_stream(
    app: ASGIApp,
    path: str,
    *,
    port: int,
    headers: Mapping[str, str] | None = None,
    method: str = "GET",
    start_timeout: float = DEFAULT_TIMEOUT,
    close_timeout: float = DEFAULT_TIMEOUT,
) -> AsyncIterator[Stream]:
    """Send a request to ``app`` and yield the ``Stream`` once its response has started."""
    scope = http_scope(path, port=port, method=method, headers=headers)
    stream = Stream(method.upper(), path)

    async def run() -> None:
        await app(scope, stream._receive, stream._send)

    stream._attach(asyncio.create_task(run(), name=f"asgi {stream._label}"))
    try:
        await stream._wait_for_response(start_timeout)
    except BaseException:
        await stream._abort(close_timeout)
        raise
    try:
        yield stream
    finally:
        await stream._close(close_timeout)
