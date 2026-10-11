"""Smoke test: Ctrl+C with an event stream open ends cleanly and fast (M2.1; ADR-076 item 1).

uvicorn's graceful shutdown waits for open connections, and an event stream never ends by
itself. The CLI therefore sets the app's shutdown signal from uvicorn's signal handler, before
uvicorn starts waiting, which ends every stream (``timeout_graceful_shutdown`` of 5 s is only the
backstop). Without that, stopping the app takes the whole backstop and logs that running tasks
were cancelled. The real app runs in a subprocess with temporary folders; the client is plain
httpx on loopback. Every value here is fictional.
"""

import time
from collections.abc import Iterator

import pytest

from tests.page_token import page_token
from tests.running_app import RunningApp

pytestmark = pytest.mark.smoke

RESYNC = b"event: resync\ndata: {}\n\n"
STOP_DEADLINE_SECONDS = 5.0  # the graceful-shutdown backstop; a clean stop is far quicker
READ_TIMEOUT_SECONDS = 10.0
BAD_OUTPUT = (
    "Traceback",
    "Exception in ASGI application",
    "timeout graceful shutdown exceeded",
    "Task exception was never retrieved",
)


def read_until(chunks: Iterator[bytes], marker: bytes) -> bytes:
    received = b""
    for chunk in chunks:
        received += chunk
        if marker in received:
            return received
    raise AssertionError(f"the stream ended before {marker!r} arrived; got {received!r}")


def test_sigint_with_open_event_stream_exits_0_within_5_seconds(running_app: RunningApp) -> None:
    with running_app.client() as client:
        token = page_token(client.get("/").text)
        with client.stream(
            "GET", "/events", headers={"X-JP-Token": token}, timeout=READ_TIMEOUT_SECONDS
        ) as response:
            assert response.status_code == 200, response.read()[:200]
            assert response.headers["content-type"].startswith("text/event-stream")
            chunks = response.iter_bytes()
            assert read_until(chunks, RESYNC).startswith(RESYNC)

            began = time.monotonic()
            result = running_app.stop()  # Ctrl+C, with the stream still open
            elapsed = time.monotonic() - began

            assert not result.killed, running_app.output_tail()
            assert result.returncode == 0, running_app.output_tail()
            assert elapsed < STOP_DEADLINE_SECONDS, (
                f"stopping took {elapsed:.1f} s: the open stream held the shutdown up"
            )
            # The server ended the response properly (a cut connection would raise here).
            leftover = b"".join(chunks)
            assert b"event:" not in leftover, "nothing but keepalives may follow"

    outputs = {
        "stdout": running_app.stdout_text(),
        "stderr": running_app.stderr_text(),
        **{
            path.name: path.read_text(encoding="utf-8", errors="replace")
            for path in running_app.log_files()
        },
    }
    for name, text in outputs.items():
        for bad in BAD_OUTPUT:
            assert bad not in text, f"{bad!r} found in {name}"
