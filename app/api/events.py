"""``GET /events``: the live-update stream (ADR-076 item 1; spec 2.9 rules 11-13).

``/events`` is not a page, so the guard requires the launch token like any other route. The
browser reads the stream with ``fetch`` and sends ``X-JP-Token`` as a header (``app/static/js/
app.js``); there is no cookie, no token in a URL and no exemption in the middleware. The route
only reads the app's shutdown signal: it has no side effects and touches no database or service.

The stream starts with a ``resync`` event (the page reloads whatever it shows), then sends a
comment line every ``keepalive_seconds`` while idle, and ends when the shutdown signal is set.
uvicorn waits for open connections before it stops, and a stream never ends by itself, so the
CLI sets the signal from uvicorn's signal handler (``app.cli.signal_streams_on_exit``) before that
wait starts. The stream carries only task ids, types and states (later tasks), never job text.
"""

import asyncio
import contextlib
import math
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

EVENTS_PATH = "/events"
DEFAULT_KEEPALIVE_SECONDS = 15.0
RESYNC = b"event: resync\ndata: {}\n\n"
KEEPALIVE = b": keepalive\n\n"  # a comment line: the page ignores it, proxies see traffic


class ShutdownSignal:
    """The app is shutting down: set once, by any thread or signal handler, and awaited by
    every open event stream on whichever event loop it runs.

    It is created in ``create_app``, before any loop exists, so nothing here binds to a loop
    until ``wait`` is called. ``set`` wakes each waiter through its own loop with
    ``call_soon_threadsafe``, which also writes to the loop's wake-up pipe: that matters when
    uvicorn's signal handler calls it on the loop's own thread while the loop sleeps in
    ``select``. The flag is a plain ``bool`` and the waiter table is a ``dict`` (single
    operations, atomic under the interpreter lock) on purpose: a lock, or ``threading.Event``,
    could deadlock if a second Ctrl+C interrupted the first handler inside ``set``.
    """

    def __init__(self) -> None:
        self._is_set = False
        self._waiters: dict[asyncio.Future[None], asyncio.AbstractEventLoop] = {}

    def set(self) -> None:
        """Mark the app as shutting down and wake every waiter. Idempotent; never blocks."""
        self._is_set = True  # before the snapshot: ``wait`` registers first, then checks this
        for future, loop in tuple(self._waiters.items()):
            with contextlib.suppress(RuntimeError):  # that waiter's loop has already closed
                loop.call_soon_threadsafe(_wake, future)

    def is_set(self) -> bool:
        return self._is_set

    async def wait(self) -> None:
        """Return when the signal is set (at once if it already is). Cancelling is safe."""
        if self._is_set:
            return
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        self._waiters[future] = loop
        try:
            if not self._is_set:  # ``set`` may have run between the check above and now
                await future
        finally:
            del self._waiters[future]


def _wake(future: asyncio.Future[None]) -> None:
    if not future.done():  # a cancelled waiter is already gone
        future.set_result(None)


async def event_stream(shutdown: ShutdownSignal, keepalive_seconds: float) -> AsyncIterator[bytes]:
    """The body of ``GET /events``: resync first, keepalives while idle, then the end.

    A signal that is already set still gets the resync first, then the stream ends. When the
    client goes away the server cancels this generator, which leaves no waiter behind.
    """
    yield RESYNC
    while not shutdown.is_set():
        if not await _set_within(shutdown, keepalive_seconds):
            yield KEEPALIVE


async def _set_within(shutdown: ShutdownSignal, seconds: float) -> bool:
    """Wait up to ``seconds`` for the signal; ``True`` when it was set in that time."""
    try:
        async with asyncio.timeout(seconds):
            await shutdown.wait()
    except TimeoutError:
        return False
    return True


def register_events(app: FastAPI, *, shutdown: ShutdownSignal, keepalive_seconds: float) -> None:
    """Add ``GET /events`` to the app. It is not tagged as a page, so the token is required.

    Raises ``ValueError`` unless ``keepalive_seconds`` is finite and greater than 0: zero or less
    would make an idle stream spin, NaN or infinity would make it send no keepalive at all.
    """
    if not (math.isfinite(keepalive_seconds) and keepalive_seconds > 0):
        raise ValueError("keepalive_seconds must be a finite number greater than 0")

    async def events() -> StreamingResponse:
        return StreamingResponse(
            event_stream(shutdown, keepalive_seconds),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store"},
        )

    app.add_api_route(EVENTS_PATH, events, methods=["GET"], name="events")
