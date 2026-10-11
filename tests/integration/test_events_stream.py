"""The event stream ``GET /events`` (M2.1; ADR-076 item 1; groundwork for AC-M2.9).

``/events`` is not a page, so the launch token is required with or without ``HX-Request``; it
has no side effects; it answers ``text/event-stream`` with ``Cache-Control: no-store``, starts
with a ``resync`` event, sends a comment line every ``event_keepalive_seconds`` while idle, and
ends when the app's shutdown signal is set (the CLI sets it from uvicorn's signal handler
before uvicorn waits for connections). The stream never ends by itself, so these tests drive
the full app (both middlewares and the lifespan) with ``tests/asgi_stream.py`` instead of
``TestClient``, which would block. All folders live under ``tmp_path``; values are fictional.
"""

import contextlib
import inspect
import math
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from app.api.security import TOKEN_HEADER
from app.config import RuntimeConfig
from app.main import create_app
from tests.asgi_stream import Stream, app_lifespan, open_stream
from tests.fakes.clock import FrozenClock
from tests.setup_harness import complete_setup

pytestmark = pytest.mark.integration

START = datetime(2026, 1, 3, 9, 0, 0, tzinfo=UTC)
PORT = 8000
ORIGIN = f"http://127.0.0.1:{PORT}"
WELCOME = "/setup/welcome"
RESYNC = b"event: resync\ndata: {}\n\n"
KEEPALIVE = b": keepalive\n\n"
DEADLINE = 2.0  # generous: the awaited thing normally happens within milliseconds
TINY_KEEPALIVE = 0.01
PROTECTIVE_HEADERS = {
    "x-frame-options": "DENY",
    "content-security-policy": "frame-ancestors 'none'",
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
}


@dataclass
class Served:
    app: FastAPI
    data_dir: Path

    @property
    def token(self) -> str:
        token: str = self.app.state.launch_token
        return token

    def headers(self, **extra: str) -> dict[str, str]:
        """The launch token, as the browser's reader sends it, plus ``extra`` headers."""
        return {TOKEN_HEADER: self.token, **extra}

    def stream(self, method: str = "GET", **extra: str) -> Any:
        """``open_stream`` on ``/events`` with the token (and ``extra`` headers)."""
        return open_stream(
            self.app, "/events", port=PORT, method=method, headers=self.headers(**extra)
        )


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


@asynccontextmanager
async def serving(
    tmp_path: Path, *, complete: bool = True, keepalive: float | None = None
) -> AsyncIterator[Served]:
    """The real app with its lifespan running. ``complete`` finishes the setup guide first;
    ``keepalive`` (seconds) is passed as ``event_keepalive_seconds`` when given."""
    config = _config(tmp_path)
    if complete:
        complete_setup(config.data_dir, config.config_dir, clock=FrozenClock(START))
    options: dict[str, Any] = {} if keepalive is None else {"event_keepalive_seconds": keepalive}
    app = create_app(config, clock=FrozenClock(START), **options)
    async with app_lifespan(app):
        yield Served(app=app, data_dir=config.data_dir)


def is_event_stream(stream: Stream) -> bool:
    return (stream.header("content-type") or "").split(";")[0].strip() == "text/event-stream"


# --- authorisation: the token is required, never in the URL ------------------------------------


async def test_events_without_token_403(tmp_path: Path) -> None:
    async with serving(tmp_path) as served:
        token_in_url = f"/events?token={served.token}"
        cases: list[tuple[str, str, dict[str, str]]] = [
            ("no token", "/events", {}),
            ("page-style navigation without a token", "/events", {"Accept": "text/html"}),
            ("no token, htmx", "/events", {"HX-Request": "true"}),
            ("wrong token", "/events", {TOKEN_HEADER: "not-the-token"}),
            ("wrong token, htmx", "/events", {TOKEN_HEADER: "nope", "HX-Request": "true"}),
            ("empty token", "/events", {TOKEN_HEADER: ""}),
            ("the token only in the URL", token_in_url, {}),
        ]
        for label, path, headers in cases:
            async with open_stream(served.app, path, port=PORT, headers=headers) as stream:
                assert stream.status == 403, label
                assert not is_event_stream(stream), label
                body = await stream.read_to_end(DEADLINE)
                assert RESYNC not in body and served.token.encode() not in body, label

        # The same path with the token streams, so the refusals above are the guard's decision
        # on a route that exists (an unknown path would be refused the same way).
        async with served.stream() as stream:
            assert stream.status == 200
            assert is_event_stream(stream)


async def test_events_with_token_streams_resync_first(tmp_path: Path) -> None:
    async with serving(tmp_path) as served:
        for extra in ({}, {"Accept": "text/event-stream"}):
            async with served.stream(**extra) as stream:
                assert stream.status == 200
                assert is_event_stream(stream)
                first = await stream.next_chunk(DEADLINE)
                assert first == RESYNC, "the first chunk is exactly the resync event"
                assert served.token.encode() not in first
                assert not stream.finished and not stream.response_complete


async def test_events_has_protective_and_no_store_headers(tmp_path: Path) -> None:
    async with serving(tmp_path) as served, served.stream() as stream:
        assert stream.status == 200
        for name, value in PROTECTIVE_HEADERS.items():
            assert stream.headers.get(name) == [value], name
        assert stream.headers.get("cache-control") == ["no-store"]
        assert "content-length" not in stream.headers, "a stream has no fixed length"
        assert not [name for name in stream.headers if name.startswith("access-control-")], (
            "no CORS headers (spec 2.9 rule 12)"
        )


async def test_events_post_405(tmp_path: Path) -> None:
    async with serving(tmp_path) as served:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            # Token and Origin are supplied, so the guard lets it through and the 405 is the
            # route's own.
            async with served.stream(method, origin=ORIGIN) as stream:
                assert stream.status == 405, method
                assert "GET" in (stream.header("allow") or ""), method
                assert not is_event_stream(stream), method
                await stream.wait_finished(DEADLINE)

        # A state-changing request without Origin is refused by the guard before the route.
        async with served.stream("POST") as stream:
            assert stream.status == 403


# --- streaming: chunks pass through unbuffered, keepalives while idle ---------------------------


async def test_events_chunks_not_buffered_by_middlewares(tmp_path: Path) -> None:
    """Through the whole app (``SecurityMiddleware`` and ``SetupRedirectMiddleware`` included)
    the first chunk reaches the server while the response is still open, and a later keepalive
    follows it; a buffering middleware would hold both back until the response ended."""
    async with serving(tmp_path, keepalive=TINY_KEEPALIVE) as served, served.stream() as stream:
        assert await stream.next_chunk(DEADLINE) == RESYNC
        assert stream.finished is False
        assert stream.response_complete is False
        assert await stream.next_chunk(DEADLINE) == KEEPALIVE
        assert stream.finished is False


async def test_events_sends_comment_keepalive_when_idle(tmp_path: Path) -> None:
    async with serving(tmp_path, keepalive=TINY_KEEPALIVE) as served, served.stream() as stream:
        assert await stream.next_chunk(DEADLINE) == RESYNC
        for _ in range(3):
            assert await stream.next_chunk(DEADLINE) == KEEPALIVE


async def test_events_idle_stream_is_quiet_until_the_keepalive_is_due(tmp_path: Path) -> None:
    async with serving(tmp_path, keepalive=60.0) as served, served.stream() as stream:
        assert await stream.next_chunk(DEADLINE) == RESYNC
        with pytest.raises(AssertionError, match="no chunk arrived"):
            await stream.next_chunk(0.2)
        assert not stream.finished


def test_events_default_keepalive_is_15_seconds() -> None:
    parameters = inspect.signature(create_app).parameters
    assert "event_keepalive_seconds" in parameters, "create_app needs the keepalive keyword"
    parameter = parameters["event_keepalive_seconds"]

    assert parameter.default == 15.0
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.parametrize("keepalive", [0, -1.0, math.nan, math.inf], ids=str)
def test_create_app_refuses_a_keepalive_that_is_not_positive_and_finite(
    tmp_path: Path, keepalive: float
) -> None:
    with pytest.raises(ValueError, match="keepalive_seconds"):
        create_app(_config(tmp_path), clock=FrozenClock(START), event_keepalive_seconds=keepalive)


async def test_events_stream_leaves_the_server_responsive(tmp_path: Path) -> None:
    async with serving(tmp_path) as served, served.stream() as stream:
        assert await stream.next_chunk(DEADLINE) == RESYNC
        async with open_stream(served.app, "/health", port=PORT) as health:
            assert health.status == 200
            await health.wait_finished(DEADLINE)
        assert not stream.finished


# --- ending: shutdown, client disconnect ------------------------------------------------------


def test_create_app_makes_the_shutdown_signal_before_any_lifespan(tmp_path: Path) -> None:
    from app.api.events import ShutdownSignal

    app = create_app(_config(tmp_path), clock=FrozenClock(START))

    shutdown = app.state.shutdown_signal  # the CLI reads it before the server starts
    assert isinstance(shutdown, ShutdownSignal)
    assert shutdown.is_set() is False


async def test_events_ends_when_app_shutting_down(tmp_path: Path) -> None:
    """The default 15 s keepalive is in force, so ending within the deadline shows that the
    signal woke the stream rather than a timer."""
    async with serving(tmp_path) as served, served.stream() as stream:
        assert await stream.next_chunk(DEADLINE) == RESYNC
        assert not stream.finished

        served.app.state.shutdown_signal.set()

        await stream.wait_finished(DEADLINE)
        assert stream.response_complete, "the response ended normally (more_body false)"


async def test_events_opened_after_shutdown_started_ends_right_after_resync(
    tmp_path: Path,
) -> None:
    async with serving(tmp_path) as served:
        served.app.state.shutdown_signal.set()

        async with served.stream() as stream:
            assert stream.status == 200
            assert await stream.next_chunk(DEADLINE) == RESYNC
            await stream.wait_finished(DEADLINE)
            assert stream.response_complete


async def test_events_all_open_streams_end_when_app_shutting_down(tmp_path: Path) -> None:
    async with serving(tmp_path) as served, contextlib.AsyncExitStack() as stack:
        streams = [await stack.enter_async_context(served.stream()) for _ in range(3)]
        for stream in streams:
            assert await stream.next_chunk(DEADLINE) == RESYNC

        served.app.state.shutdown_signal.set()

        for stream in streams:
            await stream.wait_finished(DEADLINE)
            assert stream.response_complete


@pytest.mark.parametrize("keepalive", [None, TINY_KEEPALIVE], ids=["idle", "chatty"])
async def test_events_ends_on_client_disconnect_without_error(
    tmp_path: Path, keepalive: float | None
) -> None:
    async with serving(tmp_path, keepalive=keepalive) as served:
        shutdown = served.app.state.shutdown_signal
        async with served.stream() as stream:
            assert await stream.next_chunk(DEADLINE) == RESYNC

            await stream.disconnect()

            await stream.wait_finished(DEADLINE)  # raises if the app raised
            assert not shutdown.is_set(), "a client leaving is not a shutdown"

        shutdown.set()  # must cope with the waiter the gone client left behind

        async with served.stream() as again:
            assert await again.next_chunk(DEADLINE) == RESYNC
            await again.wait_finished(DEADLINE)  # the signal is set, so it ends at once


# --- setup incomplete, side effects ------------------------------------------------------------


async def test_events_redirected_while_setup_incomplete(tmp_path: Path) -> None:
    async with serving(tmp_path / "fresh", complete=False) as fresh:
        async with fresh.stream() as stream:
            assert stream.status == 303
            assert stream.header("location") in (WELCOME, f"{ORIGIN}{WELCOME}")
            assert not is_event_stream(stream)
            assert await stream.read_to_end(DEADLINE) == b"", "no stream is opened"

        async with fresh.stream(**{"HX-Request": "true"}) as stream:
            assert stream.status == 200
            assert stream.header("hx-redirect") in (WELCOME, f"{ORIGIN}{WELCOME}")
            assert stream.header("location") is None
            assert not is_event_stream(stream)
            assert await stream.read_to_end(DEADLINE) == b""

    # The same request on a finished setup streams, so the redirects above are the setup
    # guard acting on a route that exists (any unknown path is redirected the same way).
    async with serving(tmp_path / "done") as done, done.stream() as stream:
        assert stream.status == 200
        assert is_event_stream(stream)


def _database_state(data_dir: Path) -> tuple[str, int, list[str]]:
    """Every table's rows, the write-ahead log's size and the files in the data folder."""
    database = data_dir / "app.db"
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as conn:
        dump = "\n".join(conn.iterdump())
    wal = data_dir / "app.db-wal"
    files = sorted(
        str(path.relative_to(data_dir)) for path in data_dir.rglob("*") if path.is_file()
    )
    return dump, wal.stat().st_size if wal.exists() else 0, files


async def test_events_get_has_no_side_effects(tmp_path: Path) -> None:
    """Opening and closing streams writes nothing (spec 2.9 rule 12): no row changes, no
    database write, no new file, no task."""
    async with serving(tmp_path, keepalive=TINY_KEEPALIVE) as served:
        before = _database_state(served.data_dir)

        for _ in range(2):
            async with served.stream() as stream:
                assert await stream.next_chunk(DEADLINE) == RESYNC
                assert await stream.next_chunk(DEADLINE) == KEEPALIVE

        assert _database_state(served.data_dir) == before
        assert served.app.state.shutdown_signal.is_set() is False
