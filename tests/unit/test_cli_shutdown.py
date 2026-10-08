"""Ctrl+C with event streams open: the CLI ends the streams before uvicorn waits (M2.1).

uvicorn's graceful shutdown waits for open connections to finish their responses, and an event
stream never finishes by itself, so the CLI wraps ``Server.handle_exit``: it sets the app's
shutdown signal first (which ends every stream), then runs uvicorn's own handler, which keeps
its behaviour (the first signal asks for a stop, a second Ctrl+C forces it).
``timeout_graceful_shutdown`` is the backstop. These tests use a real ``uvicorn.Server`` that is
never run, and a signal that records what uvicorn's state was when it was set. Every value here
is fictional.
"""

import signal
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI

from app.api.events import ShutdownSignal
from app.cli import signal_streams_on_exit

PORT = 8124
GRACEFUL_SHUTDOWN_SECONDS = 5


def make_server(app: FastAPI | None = None) -> uvicorn.Server:
    config = uvicorn.Config(
        app if app is not None else FastAPI(), host="127.0.0.1", port=PORT, log_config=None
    )
    return uvicorn.Server(config)


def recording_signal() -> Any:
    """A real ``ShutdownSignal`` that notes ``(should_exit, force_exit)`` of ``.server`` each
    time it is set, which shows whether uvicorn's own handler had run yet."""

    class RecordingSignal(ShutdownSignal):
        def __init__(self) -> None:
            super().__init__()
            self.server: uvicorn.Server | None = None
            self.calls: list[tuple[bool, bool]] = []

        def set(self) -> None:
            assert self.server is not None
            self.calls.append((self.server.should_exit, self.server.force_exit))
            super().set()

    return RecordingSignal()


def hooked(server: uvicorn.Server) -> Any:
    shutdown = recording_signal()
    shutdown.server = server
    signal_streams_on_exit(server, shutdown)
    return shutdown


def test_cli_handle_exit_signals_streams_before_waiting() -> None:
    server = make_server()
    shutdown = hooked(server)
    assert shutdown.is_set() is False, "installing the hook must not end any stream"
    assert server.should_exit is False

    server.handle_exit(signal.SIGINT, None)

    assert shutdown.calls == [(False, False)], (
        "the streams must be told before uvicorn's handler marks the server as exiting"
    )
    assert shutdown.is_set() is True
    assert server.should_exit is True
    assert server.force_exit is False


def test_cli_handle_exit_sigterm_signals_streams_before_waiting() -> None:
    server = make_server()
    shutdown = hooked(server)

    server.handle_exit(signal.SIGTERM, None)

    assert shutdown.calls == [(False, False)]
    assert shutdown.is_set() is True
    assert server.should_exit is True
    assert server.force_exit is False


def test_cli_handle_exit_second_sigint_still_forces_exit() -> None:
    server = make_server()
    shutdown = hooked(server)

    server.handle_exit(signal.SIGINT, None)
    assert server.force_exit is False
    server.handle_exit(signal.SIGINT, None)

    assert server.should_exit is True
    assert server.force_exit is True, "uvicorn's second Ctrl+C -> force quit must survive"
    assert shutdown.is_set() is True


def test_cli_handle_exit_second_sigterm_does_not_force_exit() -> None:
    server = make_server()
    hooked(server)

    server.handle_exit(signal.SIGTERM, None)
    server.handle_exit(signal.SIGTERM, None)

    assert server.should_exit is True
    assert server.force_exit is False, "only Ctrl+C forces a quit, as in uvicorn"


def test_cli_handle_exit_calls_the_original_handler_once_per_signal() -> None:
    server = make_server()
    original = server.handle_exit
    seen: list[int] = []

    def recording(sig: int, frame: Any) -> None:
        seen.append(sig)
        original(sig, frame)

    server.handle_exit = recording  # type: ignore[method-assign]
    shutdown = hooked(server)

    server.handle_exit(signal.SIGINT, None)
    assert seen == [signal.SIGINT]
    server.handle_exit(signal.SIGTERM, None)

    assert seen == [signal.SIGINT, signal.SIGTERM]
    assert len(shutdown.calls) == 2, "the signal is set once per call, before the original runs"


def test_cli_handle_exit_runs_the_original_handler_even_when_setting_the_signal_fails() -> None:
    """A failure in ``ShutdownSignal.set`` must never leave Ctrl+C unable to stop the server:
    uvicorn's handler runs (once) whatever happens. The error is not swallowed: it propagates
    after the handler has run (``try``/``finally``), so a bug in ``set`` stays visible."""

    class BrokenSignal(ShutdownSignal):
        def set(self) -> None:
            raise RuntimeError("fictional failure")

    server = make_server()
    original = server.handle_exit
    seen: list[int] = []

    def recording(sig: int, frame: Any) -> None:
        seen.append(sig)
        original(sig, frame)

    server.handle_exit = recording  # type: ignore[method-assign]
    signal_streams_on_exit(server, BrokenSignal())

    with pytest.raises(RuntimeError, match="fictional failure"):
        server.handle_exit(signal.SIGINT, None)

    assert seen == [signal.SIGINT], "uvicorn's handler ran exactly once"
    assert server.should_exit is True


def test_cli_handle_exit_hook_touches_only_the_server_it_was_given() -> None:
    server, other = make_server(), make_server()
    shutdown = hooked(server)
    original_class_handler = uvicorn.Server.handle_exit

    other.handle_exit(signal.SIGINT, None)

    assert uvicorn.Server.handle_exit is original_class_handler
    assert shutdown.is_set() is False, "another server's Ctrl+C must not end this app's streams"
    assert other.should_exit is True


def test_server_config_sets_graceful_shutdown_timeout() -> None:
    from app.cli import server_config

    config = server_config(FastAPI(), PORT)

    assert config.timeout_graceful_shutdown == GRACEFUL_SHUTDOWN_SECONDS


def test_default_launcher_serve_hooks_handle_exit_before_running_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``serve`` builds the server, takes the signal from ``config.app.state.shutdown_signal``
    and hooks ``handle_exit`` before ``run()``. uvicorn is not run: the replacement for
    ``Server.run`` plays the part of a signal arriving while the server runs."""
    from app.cli import default_launcher, server_config

    app = FastAPI()
    shutdown = recording_signal()
    app.state.shutdown_signal = shutdown
    ran: list[uvicorn.Server] = []

    def fake_run(self: uvicorn.Server, sockets: Any = None) -> None:
        ran.append(self)
        shutdown.server = self
        self.started = True
        self.handle_exit(signal.SIGTERM, None)  # what uvicorn's signal capture would call

    monkeypatch.setattr(uvicorn.Server, "run", fake_run)

    started = default_launcher().serve(server_config(app, PORT))

    assert started is True, "serve still reports whether the server started"
    assert len(ran) == 1
    assert shutdown.calls == [(False, False)], (
        "the app's signal must be set from handle_exit, before uvicorn's own handler ran"
    )
    assert ran[0].should_exit is True
