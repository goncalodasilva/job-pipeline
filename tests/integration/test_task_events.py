"""Task changes on the event stream (M2.18; AC-M2.9; ADR-077 follow-ups; spec 2.9 rule 12).

API assumed (new names are imported inside the tests):

- Every change the task service publishes after a commit (``TaskChange``: queued, running,
  succeeded, failed, cancelled, a retry's queued; never a heartbeat) reaches every open
  ``GET /events`` stream as one frame::

      event: task-updated
      data: {"id": "...", "type": "...", "status": "..."}
      <blank line>

  The JSON object has exactly the keys ``id``, ``type`` and ``status`` and sits on one line.
- ``app.api.events.task_updated_frame(change: TaskChange) -> bytes`` is the closed serializer:
  it writes the fields of ``TaskChange`` (a frozen dataclass of two ids and an enum) and nothing
  else, and refuses a status that is not a ``TaskStatus``.
- ``app.state.task_events`` is the publisher, made in ``create_app`` like the shutdown signal.
  ``publish(change)`` may be called from any thread; it only hands the change to each stream's
  loop (``call_soon_threadsafe``), never blocks, never raises, and drops the change without a
  warning when the app is shutting down or a stream's loop has closed.
- ``create_app(..., event_queue_size=N)`` (keyword only, default about 100): each stream has
  its own queue of at most N undelivered frames. A stream whose client stops reading overflows
  and is ended by the server, so the client reconnects and gets ``resync``; the publisher and
  the other streams are not held up.
- Every connection still starts with ``resync``; nothing published before is replayed.

The real app runs with its lifespan on the test's loop (``tests/asgi_stream.py``), with real
SQLite under ``tmp_path``, a frozen clock and fictional task handlers. No sleeps: every wait
has a deadline. Every value is fictional.
"""

import asyncio
import dataclasses
import gc
import inspect
import json
import logging
import threading
import warnings
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import Message

from app.api.security import TOKEN_HEADER
from app.config import RuntimeConfig
from app.domain.tasks import TaskChange, TaskStatus, TaskTypeSpec
from app.main import create_app
from tests.asgi_stream import Stream, app_lifespan, http_scope, open_stream
from tests.fakes.clock import FrozenClock
from tests.setup_harness import complete_setup
from tests.waiting import wait_until

pytestmark = pytest.mark.integration

START = datetime(2026, 4, 7, 9, 0, 0, tzinfo=UTC)
PORT = 8000
BASE_URL = f"http://127.0.0.1:{PORT}"
EVENTS = "/events"
RESYNC = b"event: resync\ndata: {}\n\n"
TASK_UPDATED = "task-updated"
KEYS = {"id", "type", "status"}
DEADLINE = 3.0  # generous: the awaited thing normally happens within milliseconds
QUIET_KEEPALIVE = 60.0  # no keepalive within a test, so every later chunk is an event
SMALL_QUEUE = 8
HELD = "fictional_held"  # runs until the test releases it
FAILING = "fictional_failing"  # raises at once
BEATING = "fictional_beating"  # three heartbeats with progress, then held
ERROR_TEXT = "fictional handler error text that must stay out of the stream"
TARGET_TEXT = "fictional-target-that-must-stay-out-of-the-stream"
INPUT_TEXT = "fictional-input-ref-that-must-stay-out-of-the-stream"
EVENTS_ATTRIBUTE = "data-events-url"
TASKS_SLOT = 'data-slot="tasks"'
APP_PAGES = ("/", "/inbox", "/board", "/facts", "/companies", "/settings")
GUIDE_PAGES = ("/setup/welcome", "/setup/provider", "/setup/no-such-step")


# --- the app, with fictional handlers ----------------------------------------------------------


class Handlers:
    """Fictional task handlers. They run on the app's loop, which is the test's loop here, so
    plain ``asyncio.Event`` objects made inside the test gate them."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.beaten = asyncio.Event()
        self.extra: dict[str, Any] = {}  # more handlers by task type, for one test

    async def held(self, ctx: Any) -> str:
        await self.release.wait()
        return f"fictional-result-{ctx.task_id}"

    async def failing(self, ctx: Any) -> str:
        raise RuntimeError(ERROR_TEXT)

    async def beating(self, ctx: Any) -> str:
        for progress in (10, 20, 30):
            await ctx.heartbeat(progress)
        self.beaten.set()
        await self.release.wait()
        return f"fictional-result-{ctx.task_id}"

    def registry(self) -> Any:
        from app.tasks.handlers import HandlerRegistry

        registry = HandlerRegistry()
        spec = TaskTypeSpec(timeout=timedelta(minutes=10), label="Fictional")
        registry.register(HELD, spec, self.held)
        registry.register(FAILING, spec, self.failing)
        registry.register(BEATING, spec, self.beating)
        for task_type, handler in self.extra.items():
            registry.register(task_type, spec, handler)
        return registry


@dataclass
class Served:
    app: FastAPI
    handlers: Handlers

    @property
    def token(self) -> str:
        token: str = self.app.state.launch_token
        return token

    @property
    def tasks(self) -> Any:
        return self.app.state.task_service

    def stream(self) -> Any:
        return open_stream(self.app, EVENTS, port=PORT, headers={TOKEN_HEADER: self.token})

    async def off_loop(self, call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run a blocking call in a worker thread (as the runner and the routes do); fails
        if it has not returned by the deadline."""
        return await asyncio.wait_for(asyncio.to_thread(call, *args, **kwargs), DEADLINE)


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        port=PORT,
        log_level="INFO",
        demo=False,
        setup=False,
        no_browser=False,
        data_dir_source="cli",
    )


def _build(tmp_path: Path, handlers: Handlers, **options: Any) -> FastAPI:
    config = _config(tmp_path)
    complete_setup(config.data_dir, config.config_dir, clock=FrozenClock(START))
    if "event_queue_size" in options:
        assert "event_queue_size" in inspect.signature(create_app).parameters, (
            "create_app needs the event_queue_size keyword (the per-stream queue bound)"
        )
    return create_app(
        config,
        clock=FrozenClock(START),
        task_registry=handlers.registry(),
        event_keepalive_seconds=QUIET_KEEPALIVE,
        **options,
    )


@asynccontextmanager
async def serving(tmp_path: Path, **options: Any) -> AsyncIterator[Served]:
    """The real app with its lifespan running on this loop. Held handlers are let go at the
    end, so stopping never waits for them."""
    handlers = Handlers()
    app = _build(tmp_path, handlers, **options)
    async with app_lifespan(app):
        try:
            yield Served(app=app, handlers=handlers)
        finally:
            handlers.release.set()


# --- reading frames ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Frame:
    event: str
    data: dict[str, Any]
    raw: bytes

    @property
    def key(self) -> tuple[str, str]:
        return str(self.data.get("id")), str(self.data.get("status"))


@dataclass
class Reader:
    """Collects the frames one stream sends. Comment lines (keepalives) are skipped."""

    stream: Stream
    frames: list[Frame] = field(default_factory=list)
    received: bytes = b""
    _pending: bytes = b""

    async def opened(self) -> "Reader":
        first = await self.stream.next_chunk(DEADLINE)
        assert first == RESYNC, "every connection starts with exactly the resync event"
        self.received += first
        return self

    def _feed(self, chunk: bytes) -> None:
        self.received += chunk
        *messages, self._pending = (self._pending + chunk).split(b"\n\n")
        for message in messages:
            lines = [line for line in message.split(b"\n") if not line.startswith(b":")]
            if not lines:
                continue
            fields = dict(line.decode("utf-8").split(": ", 1) for line in lines)
            self.frames.append(
                Frame(fields.get("event", ""), json.loads(fields.get("data", "{}")), message)
            )

    def updates(self, task_id: str | None = None) -> list[Frame]:
        return [
            frame
            for frame in self.frames
            if frame.event == TASK_UPDATED and task_id in (None, frame.data.get("id"))
        ]

    def statuses(self, task_id: str) -> list[str]:
        return [str(frame.data.get("status")) for frame in self.updates(task_id)]

    async def until(self, what: str, wanted: Callable[[], bool]) -> None:
        """Read chunks until ``wanted()`` holds; fail by the deadline, saying what arrived."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + DEADLINE
        while not wanted():
            remaining = deadline - loop.time()
            arrived = [(frame.event, frame.key) for frame in self.frames]
            assert remaining > 0, f"{what}: not within {DEADLINE} s; frames so far: {arrived}"
            try:
                self._feed(await self.stream.next_chunk(remaining))
            except AssertionError as error:
                raise AssertionError(f"{what}: {error}; frames so far: {arrived}") from None

    async def saw(self, task_id: str, *statuses: str) -> None:
        """Wait until a ``task-updated`` frame arrived for each of ``statuses`` of the task
        (in any order: changes from two threads may arrive out of commit order)."""
        await self.until(
            f"task-updated {statuses} for {task_id}",
            lambda: set(statuses) <= set(self.statuses(task_id)),
        )


def assert_closed_payload(frame: Frame) -> None:
    assert frame.event == TASK_UPDATED
    assert set(frame.data) == KEYS, f"the payload has exactly id, type and status: {frame.data}"
    assert all(isinstance(value, str) for value in frame.data.values()), frame.data
    assert frame.data["status"] in {status.value for status in TaskStatus}
    lines = frame.raw.split(b"\n")
    assert len(lines) == 2, f"one event line and one data line: {frame.raw!r}"
    assert lines[0] == b"event: task-updated"
    assert lines[1].startswith(b"data: {") and lines[1].endswith(b"}")


# --- AC-M2.9: transitions reach open streams ---------------------------------------------------


async def test_transition_published_to_open_stream(tmp_path: Path) -> None:
    """Queued, running, a cancel, the retry's queued, succeeded and failed each arrive."""
    async with serving(tmp_path) as served, served.stream() as stream:
        reader = await Reader(stream).opened()

        first = served.tasks.enqueue(HELD, "run-1").id
        await reader.saw(first, "queued", "running")
        served.tasks.request_cancel(first)
        await reader.saw(first, "cancelled")
        served.handlers.release.set()
        assert served.tasks.retry(first) is not None
        await reader.saw(first, "succeeded")
        second = served.tasks.enqueue(FAILING, "run-2").id
        await reader.saw(second, "queued", "running", "failed")

        for frame in reader.updates():
            assert_closed_payload(frame)
        assert {frame.data["id"] for frame in reader.updates()} == {first, second}
        assert {frame.data["type"] for frame in reader.updates(first)} == {HELD}
        assert {frame.data["type"] for frame in reader.updates(second)} == {FAILING}
        counts = Counter(reader.statuses(first))
        assert counts["queued"] == 2, "the enqueue and the retry"
        assert counts["running"] >= 2, "a claim per attempt (a cancel request may add one)"
        assert counts["cancelled"] == 1 and counts["succeeded"] == 1
        assert set(counts) == {"queued", "running", "cancelled", "succeeded"}
        assert Counter(reader.statuses(second)) == {"queued": 1, "running": 1, "failed": 1}
        assert [frame.event for frame in reader.frames].count("resync") == 0, (
            "resync is sent once, at the start of the connection"
        )
        assert not stream.finished


async def test_event_payload_has_only_id_type_status(tmp_path: Path) -> None:
    """Nothing but the id, the type and the status: not the target, the input ref, the error,
    progress, attempt, times or the launch token."""
    async with serving(tmp_path) as served, served.stream() as stream:
        reader = await Reader(stream).opened()

        ref = served.tasks.enqueue(FAILING, TARGET_TEXT, input_ref=INPUT_TEXT)
        await reader.saw(ref.id, "queued", "running", "failed")

        row = served.tasks.get(ref.id)
        assert row.error, "the failure was stored, so there was an error to leave out"
        for frame in reader.updates():
            assert_closed_payload(frame)
            assert frame.data["id"] == ref.id
            assert frame.data["type"] == FAILING
            assert json.loads(frame.raw.split(b"data: ", 1)[1]) == frame.data
        for unwanted in (TARGET_TEXT, INPUT_TEXT, ERROR_TEXT, str(row.error), served.token):
            assert unwanted.encode() not in reader.received, "only id, type and status are sent"
        for name in ("target", "dedupe", "input", "error", "progress", "attempt", "version"):
            assert name.encode() not in reader.received, name


def test_task_event_serializer_key_set_is_pinned() -> None:
    """The closed serializer: the payload is the fields of ``TaskChange`` and nothing else.
    Adding a field to ``TaskChange``, or a key to the frame, fails here."""
    from app.api import events

    serializer = getattr(events, "task_updated_frame", None)
    assert serializer is not None, "app.api.events needs task_updated_frame(change) -> bytes"

    fields = dataclasses.fields(TaskChange)
    assert [item.name for item in fields] == ["id", "type", "status"]
    assert TaskChange.__dataclass_params__.frozen is True
    assert {item.name: item.type for item in fields} == {
        "id": str,
        "type": str,
        "status": TaskStatus,
    }

    for status in TaskStatus:
        frame = serializer(TaskChange("task-0001", "fictional_type", status))
        assert isinstance(frame, bytes)
        head, _, rest = frame.partition(b"\n")
        assert head == b"event: task-updated"
        assert rest.startswith(b"data: ") and rest.endswith(b"\n\n")
        assert rest.count(b"\n") == 2, "one data line, then the blank line"
        payload = json.loads(rest[len(b"data: ") :])
        assert payload == {"id": "task-0001", "type": "fictional_type", "status": status.value}
        assert list(payload) == ["id", "type", "status"]


def test_task_event_serializer_refuses_anything_outside_the_closed_set() -> None:
    from app.api import events

    serializer = getattr(events, "task_updated_frame", None)
    assert serializer is not None, "app.api.events needs task_updated_frame(change) -> bytes"

    # A subclass that carries more: the extra field never reaches the frame.
    @dataclasses.dataclass(frozen=True)
    class Wider(TaskChange):
        error: str = ERROR_TEXT

    try:
        frame = serializer(Wider("task-0001", "fictional_type", TaskStatus.FAILED))
    except (TypeError, ValueError):
        pass  # refusing the wider object is as good as dropping the field
    else:
        assert set(json.loads(frame.split(b"data: ", 1)[1])) == KEYS
        assert ERROR_TEXT.encode() not in frame and b"error" not in frame

    # The status is an enum member, never free text (which could carry anything).
    for status in ("queued", "queued\n\nevent: resync\ndata: {}", ERROR_TEXT, None, 3):
        with pytest.raises((TypeError, ValueError)):
            serializer(TaskChange("task-0001", "fictional_type", status))  # type: ignore[arg-type]

    # An id and a type are ids, never prose (M2.18 security review): anything that isn't
    # 1 to 64 characters of a-z, 0-9, "_", ".", ":" and "-" (not first) is refused, so free
    # text, line breaks that could start a second event, and over-long values never get out.
    hostile = "fictional\r\n\r\nevent: resync\ndata: {}\n\n"
    refused = (
        hostile,
        "A free-text type, such as a job title",
        "fictional type",
        "Fictional",
        "fictional\n",
        "-fictional",
        "",
        "a" * 65,
        "task-" + "0" * 60,
        ERROR_TEXT,
        None,
        7,
    )
    for value in refused:
        with pytest.raises(TypeError):
            serializer(TaskChange(value, "fictional_type", TaskStatus.QUEUED))  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            serializer(TaskChange("task-0001", value, TaskStatus.QUEUED))  # type: ignore[arg-type]

    # What the app really sends fits: uuid4 text, the test ids, the practice type, 64 characters.
    import uuid

    from app.domain.ids import UuidIdFactory
    from app.services.practice import PRACTICE

    fitting_ids = (UuidIdFactory().new(), str(uuid.UUID(int=0)), "task-0001", "a" * 64, "0")
    for task_id in fitting_ids:
        for task_type in (PRACTICE, "connection_test", "fictional_type", "a" * 64):
            frame = serializer(TaskChange(task_id, task_type, TaskStatus.QUEUED))
            assert frame.count(b"\n") == 3 and frame.endswith(b"\n\n")
            assert json.loads(frame.split(b"data: ", 1)[1]) == {
                "id": task_id,
                "type": task_type,
                "status": "queued",
            }


async def test_publish_refuses_a_change_outside_the_closed_set_quietly(tmp_path: Path) -> None:
    """A change the serializer refuses is dropped by ``publish`` (it never raises into the
    committing thread) and nothing of it reaches a stream; the next good change does."""
    async with serving(tmp_path) as served, served.stream() as stream:
        reader = await Reader(stream).opened()
        publisher = served.app.state.task_events

        publisher.publish(TaskChange("task-0001", ERROR_TEXT, TaskStatus.FAILED))
        publisher.publish(TaskChange("a" * 65, HELD, TaskStatus.FAILED))
        publisher.publish(TaskChange("task-0002", HELD, TaskStatus.QUEUED))

        await reader.saw("task-0002", "queued")
        assert len(reader.updates()) == 1
        assert ERROR_TEXT.encode() not in reader.received and b"aaaa" not in reader.received


async def test_publish_from_a_plain_thread_uses_the_thread_safe_hop(tmp_path: Path) -> None:
    """The hop from the committing thread to the stream's loop is ``call_soon_threadsafe``.
    A plain ``call_soon`` from another thread only works by luck (the loop runs it when
    something else wakes it up). With the loop in debug mode asyncio refuses that outright,
    so this fails for a ``call_soon`` publisher whatever the timing. Nothing but
    ``next_chunk`` is awaited between the publish and the frame: the thread is not joined and
    nothing else is scheduled on the loop in between."""
    async with serving(tmp_path) as served, served.stream() as stream:
        await Reader(stream).opened()
        publisher = served.app.state.task_events
        loop = asyncio.get_running_loop()
        change = TaskChange("task-from-a-thread", HELD, TaskStatus.QUEUED)
        thread = threading.Thread(target=publisher.publish, args=(change,))
        was_debug = loop.get_debug()
        loop.set_debug(True)
        try:
            thread.start()
            chunk = await stream.next_chunk(DEADLINE)
        finally:
            loop.set_debug(was_debug)
            thread.join(DEADLINE)
        assert not thread.is_alive()
        assert chunk.startswith(b"event: task-updated\n") and b"task-from-a-thread" in chunk


async def test_publish_from_worker_thread_reaches_stream(tmp_path: Path) -> None:
    """``on_change`` runs on the committing thread (M2.7): a change committed off the loop,
    by ``asyncio.to_thread`` or by a plain thread, still reaches the stream."""
    async with serving(tmp_path) as served, served.stream() as stream:
        reader = await Reader(stream).opened()
        loop_thread = threading.get_ident()
        committed_on: list[int] = []

        def enqueue(target: str) -> str:
            committed_on.append(threading.get_ident())
            task_id: str = served.tasks.enqueue(HELD, target).id
            return task_id

        first = await served.off_loop(enqueue, "run-1")
        await reader.saw(first, "queued")

        made: list[str] = []
        thread = threading.Thread(target=lambda: made.append(enqueue("run-2")))
        thread.start()
        await served.off_loop(thread.join, DEADLINE)
        assert made, "the plain thread's enqueue returned"
        await reader.saw(made[0], "queued")

        assert loop_thread not in committed_on, "both commits ran off the loop"
        for frame in reader.updates():
            assert_closed_payload(frame)


async def test_two_streams_both_receive_every_transition(tmp_path: Path) -> None:
    async with serving(tmp_path) as served, served.stream() as one, served.stream() as two:
        readers = [await Reader(one).opened(), await Reader(two).opened()]

        task_id = served.tasks.enqueue(FAILING, "run-1").id

        for reader in readers:
            await reader.saw(task_id, "queued", "running", "failed")
            assert Counter(reader.statuses(task_id)) == {"queued": 1, "running": 1, "failed": 1}
        # One stream leaving doesn't disturb the other.
        await one.disconnect()
        await one.wait_finished(DEADLINE)
        again = served.tasks.enqueue(FAILING, "run-2").id
        await readers[1].saw(again, "failed")


async def test_heartbeat_publishes_nothing(tmp_path: Path) -> None:
    """Heartbeats with progress are not state changes (M2.6): between ``running`` and
    ``succeeded`` the stream stays silent, so progress shows on the next refetch."""
    async with serving(tmp_path) as served, served.stream() as stream:
        reader = await Reader(stream).opened()

        task_id = served.tasks.enqueue(BEATING, "run-1").id
        await asyncio.wait_for(served.handlers.beaten.wait(), DEADLINE)
        assert served.tasks.get(task_id).progress == 30, "the three heartbeats were stored"
        served.handlers.release.set()
        await reader.saw(task_id, "succeeded")

        assert Counter(reader.statuses(task_id)) == {"queued": 1, "running": 1, "succeeded": 1}
        assert len(reader.updates()) == 3, "no frame for a heartbeat"
        assert b"progress" not in reader.received


# --- every connection starts with resync -------------------------------------------------------


async def test_every_connection_starts_with_resync(tmp_path: Path) -> None:
    """A connection opened after changes were published starts with ``resync`` alone: nothing
    is replayed. Changes made after it opened follow."""
    async with serving(tmp_path) as served:
        earlier: list[str] = []
        for number in range(3):
            async with served.stream() as stream:
                reader = await Reader(stream).opened()  # the first chunk is exactly resync
                task_id = served.tasks.enqueue(FAILING, f"run-{number}").id
                await reader.saw(task_id, "queued", "running", "failed")
                assert {frame.data["id"] for frame in reader.updates()} == {task_id}, (
                    f"nothing from before the connection is replayed: {earlier}"
                )
                earlier.append(task_id)


async def test_ac_m2_9_reconnected_stream_resyncs_then_gets_new_events(tmp_path: Path) -> None:
    """AC-M2.9 (server side): a task changes while no stream is open; the next connection
    says ``resync`` first (the page then refetches and sees that change), and later changes
    arrive as events."""
    async with serving(tmp_path) as served:
        async with served.stream() as stream:
            reader = await Reader(stream).opened()
            held = served.tasks.enqueue(HELD, "run-1").id
            await reader.saw(held, "queued", "running")

        served.handlers.release.set()  # the stream is down while the task finishes
        missed = served.tasks.enqueue(FAILING, "run-2").id

        def both_ended() -> bool:
            rows = [served.tasks.get(held), served.tasks.get(missed)]
            return [str(row.status.value) for row in rows] == ["succeeded", "failed"]

        await asyncio.to_thread(
            wait_until, both_ended, timeout=DEADLINE, message="both tasks ended with no stream"
        )

        async with served.stream() as stream:
            reader = await Reader(stream).opened()
            later = served.tasks.enqueue(FAILING, "run-3").id
            await reader.saw(later, "failed")
            assert {frame.data["id"] for frame in reader.updates()} == {later}


# --- a slow stream overflows and ends; nothing else waits for it --------------------------------


class StalledClient:
    """A raw ASGI client for ``GET /events`` that stops reading after the first chunk: its
    ``send`` does not return until ``resume()``, as uvicorn's does when the socket is full."""

    def __init__(self, app: FastAPI, token: str) -> None:
        self.status = 0
        self.chunks: list[bytes] = []
        self.complete = False
        self._first = asyncio.Event()
        self._resume = asyncio.Event()
        self._gone = asyncio.Event()
        self._sent_request = False
        scope = http_scope(EVENTS, port=PORT, headers={TOKEN_HEADER: token})
        self.task = asyncio.create_task(app(scope, self._receive, self._send))

    async def _receive(self) -> Message:
        if not self._sent_request:
            self._sent_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._gone.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.status = int(message["status"])
            return
        body = bytes(message.get("body", b""))
        if not message.get("more_body", False):
            self.complete = True
            if body:
                self.chunks.append(body)
            return
        if body:
            self.chunks.append(body)
            self._first.set()
            await self._resume.wait()  # the client is not reading

    async def stalled(self) -> None:
        await asyncio.wait_for(self._first.wait(), DEADLINE)
        assert self.status == 200 and self.chunks == [RESYNC]

    def resume(self) -> None:
        self._resume.set()

    async def close(self) -> None:
        self._resume.set()
        self._gone.set()
        if not self.task.done():
            _, pending = await asyncio.wait({self.task}, timeout=DEADLINE)
            for task in pending:
                task.cancel()
            await asyncio.wait({self.task}, timeout=DEADLINE)
        if self.task.done() and not self.task.cancelled():
            self.task.exception()  # seen, so nothing is logged at garbage collection

    def task_updates(self) -> int:
        return b"".join(self.chunks).count(b"event: task-updated")


def test_event_queue_size_is_a_bounded_keyword_of_create_app() -> None:
    parameters = inspect.signature(create_app).parameters
    assert "event_queue_size" in parameters, "create_app needs the event_queue_size keyword"
    parameter = parameters["event_queue_size"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert isinstance(parameter.default, int) and 16 <= parameter.default <= 1000


@pytest.mark.parametrize("size", [0, -1])
def test_create_app_refuses_an_event_queue_without_a_bound(tmp_path: Path, size: int) -> None:
    """Zero would mean "no limit" to ``asyncio.Queue``: an unread stream could grow forever."""
    assert "event_queue_size" in inspect.signature(create_app).parameters
    with pytest.raises(ValueError, match="event_queue_size"):
        create_app(_config(tmp_path), clock=FrozenClock(START), event_queue_size=size)


async def test_slow_stream_overflow_ends_stream_without_blocking_runner(tmp_path: Path) -> None:
    """One client stops reading. Each change is still committed and published at once (the
    worker thread's call returns by the deadline), a healthy stream gets every one of them,
    and the stalled stream is ended once more than ``event_queue_size`` frames wait for it."""
    total = SMALL_QUEUE * 3
    async with serving(tmp_path, event_queue_size=SMALL_QUEUE) as served:
        stalled = StalledClient(served.app, served.token)
        try:
            await stalled.stalled()
            async with served.stream() as healthy_stream:
                healthy = await Reader(healthy_stream).opened()
                ids: list[str] = []
                for number in range(total):
                    # One at a time, so the healthy stream is never behind by more than a
                    # frame or two and can't overflow itself.
                    ref = await served.off_loop(served.tasks.enqueue, FAILING, f"run-{number}")
                    ids.append(ref.id)
                    await healthy.saw(ref.id, "queued", "running", "failed")
                assert len(healthy.updates()) == total * 3, "the healthy stream missed nothing"
                assert not healthy_stream.finished
                assert not stalled.task.done() and stalled.chunks == [RESYNC]

                stalled.resume()  # the client reads again: it finds the stream ended

                await asyncio.wait({stalled.task}, timeout=DEADLINE)
                assert stalled.task.done(), "the overflowed stream was ended by the server"
                assert stalled.task.exception() is None
                assert stalled.complete, "ended as a normal end of response (more_body false)"
                assert stalled.task_updates() <= SMALL_QUEUE + 1 < total * 3, (
                    "at most the queued frames were delivered before the end"
                )
                assert not served.app.state.shutdown_signal.is_set()

                # The client reconnects: resync first, then events again.
                async with served.stream() as again_stream:
                    again = await Reader(again_stream).opened()
                    after = served.tasks.enqueue(FAILING, "run-after").id
                    await again.saw(after, "failed")
                    await healthy.saw(after, "failed")
                    assert {frame.data["id"] for frame in again.updates()} == {after}
        finally:
            await stalled.close()


async def _until(what: str, wanted: Callable[[], bool]) -> None:
    """Let the loop run until ``wanted()`` holds; fail by the deadline."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + DEADLINE
    while not wanted():
        assert loop.time() < deadline, f"{what}: not within {DEADLINE} s"
        await asyncio.sleep(0)


async def test_stream_subscribes_before_it_sends_resync(tmp_path: Path) -> None:
    """A change committed right after the resync chunk left the server is never missed, even
    when the client is slow to take that chunk: the stream is subscribed before it says
    resync. (The page refetches on resync; what changes after that must arrive as an event.)"""
    async with serving(tmp_path) as served:
        slow = StalledClient(served.app, served.token)
        try:
            await slow.stalled()  # resync is out; the server's send has not returned yet

            served.app.state.task_events.publish(
                TaskChange("task-right-after-resync", HELD, TaskStatus.QUEUED)
            )
            slow.resume()

            await _until("the change published during the resync send", slow.task_updates)
            assert b"task-right-after-resync" in b"".join(slow.chunks)
            assert slow.chunks[0] == RESYNC
        finally:
            await slow.close()


async def test_disconnected_and_overflowed_streams_are_forgotten_at_once(tmp_path: Path) -> None:
    """The publisher's table of streams holds open, healthy streams only: a stream leaves it
    when its client disconnects (while idle, or while a send is stuck) and when it overflows,
    without waiting for the garbage collector to finalise the generator."""
    gc.disable()
    try:
        async with serving(tmp_path, event_queue_size=SMALL_QUEUE) as served:
            publisher = served.app.state.task_events
            assert len(publisher._streams) == 0

            async with served.stream() as idle:
                await Reader(idle).opened()
                assert len(publisher._streams) == 1
                await idle.disconnect()
                await idle.wait_finished(DEADLINE)
                assert len(publisher._streams) == 0, "an idle stream that disconnected"

            stuck = StalledClient(served.app, served.token)
            await stuck.stalled()
            assert len(publisher._streams) == 1
            await stuck.close()  # the client goes away while the send is stuck
            assert stuck.task.done()
            assert len(publisher._streams) == 0, "a stream that disconnected in mid-send"

            stalled = StalledClient(served.app, served.token)
            try:
                await stalled.stalled()
                for number in range(SMALL_QUEUE + 1):
                    publisher.publish(TaskChange(f"task-{number}", HELD, TaskStatus.QUEUED))
                await _until("the overflowed stream is forgotten", lambda: not publisher._streams)
                assert not stalled.task.done(), "its client still hasn't read on"
            finally:
                await stalled.close()
            assert len(publisher._streams) == 0
    finally:
        gc.enable()


# --- shutdown and a closed loop: dropped quietly -------------------------------------------------


def _loud(records: list[logging.LogRecord]) -> list[str]:
    return [f"{r.levelname} {r.name}: {r.getMessage()}" for r in records if r.levelno >= 30]


async def test_publish_after_shutdown_is_dropped_quietly(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Once shutdown has started, streams end (M2.1, unchanged) and later changes go nowhere:
    no exception in the committing thread, no warning per change."""
    caplog.set_level(logging.DEBUG)
    async with serving(tmp_path) as served:
        async with served.stream() as stream:
            reader = await Reader(stream).opened()
            before = served.tasks.enqueue(HELD, "run-0").id
            await reader.saw(before, "queued")  # the control: publishing works before

            served.app.state.shutdown_signal.set()

            await stream.wait_finished(DEADLINE)
            assert stream.response_complete

        caplog.clear()
        for number in range(1, 51):
            served.tasks.enqueue(HELD, f"run-{number}")  # on the loop's own thread
        for number in range(51, 101):
            await served.off_loop(served.tasks.enqueue, HELD, f"run-{number}")
        assert _loud(caplog.records) == [], "dropped changes are not worth a warning each"

        # A stream opened during shutdown still gets resync and the end, and no events.
        async with served.stream() as late:
            assert await late.next_chunk(DEADLINE) == RESYNC
            assert await late.read_to_end(DEADLINE) == b""


def test_publish_after_the_loop_closed_is_dropped_quietly(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A hard stop (a second Ctrl+C) closes the loop with a stream still open, while worker
    threads may still be committing. Publishing to that stream's closed loop is dropped: no
    exception reaches the committing thread and nothing is logged, however many changes."""
    caplog.set_level(logging.DEBUG)
    loop = asyncio.new_event_loop()

    async def run_and_abandon_a_stream() -> tuple[Any, StalledClient]:
        handlers = Handlers()
        app = _build(tmp_path, handlers)
        token = app.state.launch_token
        async with app_lifespan(app):
            publisher = getattr(app.state, "task_events", None)
            assert publisher is not None, "create_app sets app.state.task_events (the publisher)"
            abandoned = StalledClient(app, token)  # never reads on, never leaves
            await abandoned.stalled()
            async with open_stream(app, EVENTS, port=PORT, headers={TOKEN_HEADER: token}) as live:
                reader = await Reader(live).opened()
                publisher.publish(TaskChange("task-control", HELD, TaskStatus.QUEUED))
                await reader.saw("task-control", "queued")  # the control: it reaches a stream
        return publisher, abandoned

    try:
        publisher, abandoned = loop.run_until_complete(run_and_abandon_a_stream())
        assert not abandoned.task.done(), "the abandoned stream is still open on that loop"
    finally:
        loop.close()
    # The task can never finish now; asyncio's "destroyed but pending" note is not the app's.
    abandoned.task._log_destroy_pending = False  # type: ignore[attr-defined]
    caplog.clear()
    errors: list[BaseException] = []

    def publish_many(first: int) -> None:
        try:
            for number in range(first, first + 200):
                publisher.publish(TaskChange(f"task-{number}", HELD, TaskStatus.QUEUED))
        except BaseException as error:  # the test reports whatever escaped
            errors.append(error)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        publish_many(0)
        thread = threading.Thread(target=publish_many, args=(200,))
        thread.start()
        thread.join(DEADLINE)
    assert not thread.is_alive()
    assert errors == [], f"publish raised after the loop closed: {errors!r}"
    assert _loud(caplog.records) == [], "no warning flood for changes nobody can receive"
    assert [str(item.message) for item in caught] == [], "no warning either"


# --- ADR-077 follow-up: stream frames in the leak scan ------------------------------------------


async def test_ac_m1_4_event_stream_frames_carry_no_secret(tmp_path: Path) -> None:
    """AC-M1.4, extended to the stream: fictional key-shaped secrets sit in everything the
    task row holds beyond id, type and status (the target, the input ref and the handler's
    error text). The frames and the stream's headers hold none of them, nor any 8-character
    piece of them, nor the launch token."""
    from tests.leak_scan import TOKEN_PREFIX, Evidence, Secret, fictional_secret, find_leaks

    in_target = fictional_secret(TOKEN_PREFIX, "M2.18 secret in a task target")
    in_input = fictional_secret(TOKEN_PREFIX, "M2.18 secret in an input ref")
    in_error = fictional_secret(TOKEN_PREFIX, "M2.18 secret in an error message")
    evidence = Evidence()
    handlers = Handlers()

    async def leaky(ctx: Any) -> str:
        raise RuntimeError(f"fictional failure quoting {in_error.value}")

    handlers.extra["fictional_leaky"] = leaky
    app = _build(tmp_path, handlers)
    async with app_lifespan(app):
        token = app.state.launch_token
        launch = Secret(label="the launch token", value=token, public_prefix="")
        async with open_stream(app, EVENTS, port=PORT, headers={TOKEN_HEADER: token}) as stream:
            reader = await Reader(stream).opened()
            ref = app.state.task_service.enqueue(
                "fictional_leaky", in_target.value, input_ref=in_input.value
            )
            await reader.saw(ref.id, "queued", "running", "failed")
            app.state.task_service.retry(ref.id)
            await reader.until("the retry's frames", lambda: len(reader.updates(ref.id)) >= 6)
            evidence.add_stream("task events", stream.headers, reader.received)

    assert reader.received.count(b"event: task-updated") >= 6, "the scan saw real frames"
    leaks = [
        leak
        for secret in (in_target, in_input, in_error, launch)
        for leak in find_leaks(secret, evidence)
    ]
    assert leaks == [], "secret leaked into the event stream:\n" + "\n".join(leaks)


def test_leak_scan_finds_a_secret_planted_in_stream_frames() -> None:
    """The scan's own control: a secret (or an 8-character piece of it) planted in frames or
    in a stream header is found. Fails first until the serializer exists, so that this file
    stays red as a whole until M2.18 is built."""
    from app.api import events
    from tests.leak_scan import TOKEN_PREFIX, Evidence, fictional_secret, find_leaks

    serializer = getattr(events, "task_updated_frame", None)
    assert serializer is not None, "app.api.events needs task_updated_frame(change) -> bytes"
    secret = fictional_secret(TOKEN_PREFIX, "M2.18 planted secret")
    clean = serializer(TaskChange("task-0001", "fictional_type", TaskStatus.QUEUED))

    evidence = Evidence()
    evidence.add_stream("clean", {"content-type": ["text/event-stream"]}, RESYNC + clean)
    assert find_leaks(secret, evidence) == []

    piece = secret.value[-12:]
    # Built by hand: the serializer itself refuses an id of this shape (mixed case).
    planted = clean.replace(b"task-0001", f"task-{piece}".encode())
    assert planted != clean and planted.startswith(b"event: task-updated\n")
    in_frames = Evidence()
    in_frames.add_stream("planted", {}, RESYNC + planted)
    assert len(find_leaks(secret, in_frames)) == 1

    in_header = Evidence()
    in_header.add_stream("planted", {"x-fictional": [secret.value]}, RESYNC)
    assert len(find_leaks(secret, in_header)) == 1


# --- ADR-077 follow-up: data-events-url is opt-in ------------------------------------------------


def test_events_url_is_opt_in_and_only_on_pages_with_the_tasks_slot(tmp_path: Path) -> None:
    """``base.html`` no longer opens a stream by default: a page that extends it and says
    nothing gets no ``data-events-url``. The app shell, whose top bar holds the tasks slot
    (the live task indicator, M2.20), opts in; the setup guide doesn't. So a page carries the
    attribute exactly when it carries the tasks slot."""
    from app.api.templating import templates
    from tests.integration.test_events_page_wiring import body_attributes

    silent = templates.env.from_string(
        '{% extends "base.html" %}{% block title %}Fictional{% endblock %}'
        "{% block navigation %}{% endblock %}{% block banners %}{% endblock %}"
    ).render(hx_headers="{}")
    assert EVENTS_ATTRIBUTE not in body_attributes(silent), (
        "a page that doesn't opt in opens no event stream"
    )

    config = _config(tmp_path)
    complete_setup(config.data_dir, config.config_dir, clock=FrozenClock(START))
    app = create_app(config, clock=FrozenClock(START))
    with TestClient(app, base_url=BASE_URL, follow_redirects=False) as client:
        for path in (*APP_PAGES, *GUIDE_PAGES):
            page = client.get(path).text
            attributes = body_attributes(page)
            if path in APP_PAGES:
                assert TASKS_SLOT in page, f"{path}: the app shell has the tasks slot"
                assert attributes.get(EVENTS_ATTRIBUTE) == EVENTS, path
            else:
                assert TASKS_SLOT not in page, f"{path}: the guide has no tasks slot"
                assert EVENTS_ATTRIBUTE not in attributes, path
