"""``GET /events``: the live-update stream (ADR-076 item 1; spec 2.9 rules 11-13).

``/events`` is not a page, so the guard requires the launch token like any other route. The
browser reads the stream with ``fetch`` and sends ``X-JP-Token`` as a header (``app/static/js/
app.js``); there is no cookie, no token in a URL and no exemption in the middleware. The route
only reads the app's shutdown signal: it has no side effects and touches no database or service.

The stream starts with a ``resync`` event (the page reloads whatever it shows), then sends a
comment line every ``keepalive_seconds`` while idle, and ends when the shutdown signal is set.
uvicorn waits for open connections before it stops, and a stream never ends by itself, so the
CLI sets the signal from uvicorn's signal handler (``app.cli.signal_streams_on_exit``) before that
wait starts.

Between the two, every committed task change is one ``task-updated`` event (M2.18). The stream
carries only task ids, types and states (``task_updated_frame``, the closed serializer), never
job text, error text or anything else a task row holds. Each stream has a bounded queue of
frames not yet sent; a stream whose client stops reading overflows and is ended, so the page
reconnects and starts again from ``resync``.
"""

import asyncio
import contextlib
import json
import logging
import math
import re
from collections import deque
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.domain.tasks import TaskChange, TaskStatus

logger = logging.getLogger(__name__)

EVENTS_PATH = "/events"
DEFAULT_KEEPALIVE_SECONDS = 15.0
DEFAULT_EVENT_QUEUE_SIZE = 100  # frames one stream may have waiting before it is ended
# What an id or a task type on the stream looks like: an id, never prose (uuid4 text fits).
_CLOSED_VALUE = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,63}")
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

    def watch(self, future: asyncio.Future[None], loop: asyncio.AbstractEventLoop) -> None:
        """Have ``set`` wake ``future`` on ``loop``. The caller checks ``is_set`` afterwards
        (``set`` may have run just before) and calls ``unwatch`` when it stops waiting."""
        self._waiters[future] = loop

    def unwatch(self, future: asyncio.Future[None]) -> None:
        del self._waiters[future]

    async def wait(self) -> None:
        """Return when the signal is set (at once if it already is). Cancelling is safe."""
        if self._is_set:
            return
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        self.watch(future, loop)
        try:
            if not self._is_set:  # ``set`` may have run between the check above and now
                await future
        finally:
            self.unwatch(future)


def _wake(future: asyncio.Future[None]) -> None:
    if not future.done():  # a cancelled waiter is already gone
        future.set_result(None)


def task_updated_frame(change: TaskChange) -> bytes:
    """The one ``task-updated`` frame for a change: its id, type and status, as JSON on one
    line, and nothing else (the closed serializer; ADR-077).

    Only the three fields are read, so a wider object adds nothing. ``TypeError`` unless the
    status is a ``TaskStatus`` and the id and type are short ids (1 to 64 characters of a-z,
    0-9, ``_``, ``.``, ``:`` and ``-``, starting with a letter or digit): free text there
    could carry anything, and a line break could start a second event.
    """
    if not isinstance(change.status, TaskStatus):
        raise TypeError("a task change's status must be a TaskStatus")
    for value in (change.id, change.type):
        if not (isinstance(value, str) and _CLOSED_VALUE.fullmatch(value)):
            raise TypeError("a task change's id and type must be short ids")
    payload = {"id": change.id, "type": change.type, "status": change.status.value}
    return b"event: task-updated\ndata: " + json.dumps(payload).encode("ascii") + b"\n\n"


class _Subscription:
    """The frames one open stream has not sent yet. Everything here runs on that stream's
    loop: the publisher only schedules ``push`` there."""

    def __init__(self, size: int) -> None:
        self._size = size
        self.frames: deque[bytes] = deque()
        self.overflowed = False
        self.waiter: asyncio.Future[None] | None = None

    def push(self, frame: bytes) -> None:
        if self.overflowed:
            return
        if len(self.frames) >= self._size:  # the client isn't reading: the stream will end
            self.overflowed = True
            self.frames.clear()
        else:
            self.frames.append(frame)
        if self.waiter is not None:
            _wake(self.waiter)


class TaskEvents:
    """Hands every committed task change to every open event stream.

    Made in ``create_app`` like the shutdown signal, before any loop exists. ``publish`` runs
    on whichever thread committed the change (M2.7): it only schedules the frame on each
    stream's own loop with ``call_soon_threadsafe``, so it never blocks on a slow client, and
    it never raises into the committing thread. The table of streams is a ``dict`` used with
    single operations (atomic under the interpreter lock), for the reason given in
    ``ShutdownSignal``.

    Raises ``ValueError`` unless ``queue_size`` is at least 1: without a bound, a stream that
    is never read would grow for ever.
    """

    def __init__(self, shutdown: ShutdownSignal, *, queue_size: int = DEFAULT_EVENT_QUEUE_SIZE):
        if isinstance(queue_size, bool) or not isinstance(queue_size, int) or queue_size < 1:
            raise ValueError("event_queue_size must be a whole number of at least 1")
        self._shutdown = shutdown
        self._queue_size = queue_size
        self._streams: dict[_Subscription, asyncio.AbstractEventLoop] = {}

    def publish(self, change: TaskChange) -> None:
        """Send ``change`` to every open stream. Dropped quietly when the app is shutting
        down (streams are ending) and for a stream whose loop has closed."""
        if self._shutdown.is_set() or not self._streams:
            return
        try:
            frame = task_updated_frame(change)
        except TypeError:  # a programming error; never the committing thread's problem
            logger.debug("a task change that is not id, type and status was not published")
            return
        for subscription, loop in tuple(self._streams.items()):
            try:
                loop.call_soon_threadsafe(self._deliver, subscription, frame)
            except RuntimeError:  # that loop has closed: its stream is gone for good
                self._streams.pop(subscription, None)

    def _deliver(self, subscription: _Subscription, frame: bytes) -> None:
        """On the stream's loop: queue the frame; an overflowed stream is forgotten at once."""
        subscription.push(frame)
        if subscription.overflowed:
            self._streams.pop(subscription, None)

    def subscribe(self) -> _Subscription:
        """A queue for the stream being served on the running loop; ``unsubscribe`` it."""
        subscription = _Subscription(self._queue_size)
        self._streams[subscription] = asyncio.get_running_loop()
        return subscription

    def unsubscribe(self, subscription: _Subscription) -> None:
        self._streams.pop(subscription, None)


async def event_stream(
    shutdown: ShutdownSignal, keepalive_seconds: float, task_events: TaskEvents
) -> AsyncIterator[bytes]:
    """The body of ``GET /events``: resync first, then task changes as they are published and
    keepalives while idle, then the end.

    A signal that is already set still gets the resync first, then the stream ends. The stream
    subscribes before it says resync, so nothing published after the page was told to refetch
    is missed; nothing published earlier is replayed. A stream that overflowed ends like a
    shutdown does (a normal end of response): frames were lost, so the page has to reconnect
    and resync. When the client goes away the server cancels this generator, which leaves no
    waiter behind; ``_EventsResponse`` closes it, so no subscription stays behind either.
    """
    subscription = task_events.subscribe()
    try:
        yield RESYNC
        while not (shutdown.is_set() or subscription.overflowed):
            if subscription.frames:
                yield subscription.frames.popleft()
            elif not await _woken_within(shutdown, subscription, keepalive_seconds):
                yield KEEPALIVE
    finally:
        task_events.unsubscribe(subscription)


async def _woken_within(
    shutdown: ShutdownSignal, subscription: _Subscription, seconds: float
) -> bool:
    """Wait up to ``seconds`` for the shutdown signal or a published frame (or an overflow);
    ``True`` when one came in that time."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[None] = loop.create_future()
    subscription.waiter = future
    shutdown.watch(future, loop)
    try:
        if shutdown.is_set() or subscription.frames or subscription.overflowed:
            return True
        async with asyncio.timeout(seconds):
            await future
    except TimeoutError:
        return False
    finally:
        subscription.waiter = None
        shutdown.unwatch(future)
    return True


class _EventsResponse(StreamingResponse):
    """Closes the stream's generator as soon as the response is over. A client that goes away
    while a send is stuck cancels the send, not the generator, which would otherwise keep its
    subscription until the garbage collector finalised it."""

    def __init__(self, body: AsyncIterator[bytes]) -> None:
        super().__init__(
            body, media_type="text/event-stream", headers={"Cache-Control": "no-store"}
        )
        self._events = body

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            close = getattr(self._events, "aclose", None)
            if close is not None:
                await close()  # runs the generator's ``finally``; it awaits nothing


def register_events(
    app: FastAPI,
    *,
    shutdown: ShutdownSignal,
    keepalive_seconds: float,
    task_events: TaskEvents | None = None,
) -> None:
    """Add ``GET /events`` to the app. It is not tagged as a page, so the token is required.

    Raises ``ValueError`` unless ``keepalive_seconds`` is finite and greater than 0: zero or less
    would make an idle stream spin, NaN or infinity would make it send no keepalive at all.
    ``task_events`` (default: a publisher nobody publishes to) is where the streams get task
    changes from.
    """
    if not (math.isfinite(keepalive_seconds) and keepalive_seconds > 0):
        raise ValueError("keepalive_seconds must be a finite number greater than 0")
    publisher = task_events if task_events is not None else TaskEvents(shutdown)

    async def events() -> StreamingResponse:
        return _EventsResponse(event_stream(shutdown, keepalive_seconds, publisher))

    app.add_api_route(EVENTS_PATH, events, methods=["GET"], name="events")
