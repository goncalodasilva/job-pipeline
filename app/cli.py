"""The ``jobpipeline`` command (spec 7.1; ADR-024, 025, 030, 037, 039, 040).

Start-up order matters: resolve the config, create the data folder, set up redacted logging
and the exception hooks, build the app, check the port, then serve on 127.0.0.1 only. On a
first run (no database yet and no folder named by a flag or the environment) nothing is
created: logs are buffered in memory until step 1 opens the workspace (ADR-044 item 3), and a
start-up failure points at the console instead of a log folder. The
browser opens once ``/health`` answers. Errors print one plain line, never a traceback; the
log file keeps the details.

Exit codes: 0 after a normal stop, 1 when the port is taken or start-up fails, 2 for an
invalid setting or command-line option, 130 for Ctrl+C before serving begins (ADR-040). Once
serving has begun, Ctrl+C (or Ctrl+Break on Windows) is a normal stop.
"""

import argparse
import logging
import os
import signal
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import uvicorn
from fastapi import FastAPI

from app import __version__
from app.config import (
    CliOptions,
    RuntimeConfig,
    opens_workspace_at_startup,
    owned_by_another_user,
    resolve_config,
)
from app.domain.errors import ConfigError
from app.domain.redaction import Redactor
from app.logging_config import LoggingSetup, configure_logging, install_exception_hooks
from app.main import LOG_FOLDER, create_app
from app.wiring import Wiring, build_wiring

HOST = "127.0.0.1"
HEALTH_POLL_SECONDS = 0.25
HEALTH_POLL_ATTEMPTS = 40  # about 10 seconds
INTERRUPTED_EXIT = 130  # Ctrl+C before the server took over signal handling
# The SDK's own CLI version check runs with the full environment; the app checks the version
# itself, so the SDK's check is turned off for the process (ADR-058).
SDK_SKIP_VERSION_CHECK = "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK"

_log = logging.getLogger("jobpipeline")


@dataclass(frozen=True)
class Launcher:
    """The side effects of starting the app, injectable for tests."""

    port_free: Callable[[int], bool]
    serve: Callable[[uvicorn.Config], bool]
    health_ok: Callable[[str], bool]
    open_browser: Callable[[str], object]
    sleep: Callable[[float], None]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobpipeline",
        description="Run the Job Search Pipeline on this computer.",
    )
    parser.add_argument("--port", type=int, help="port to use (default 8000)")
    parser.add_argument("--data-dir", type=Path, help="folder for your data")
    parser.add_argument("--no-browser", action="store_true", help="don't open the browser")
    parser.add_argument("--setup", action="store_true", help="open the setup guide again")
    parser.add_argument("--demo", action="store_true", help="use a separate demo workspace")
    parser.add_argument("--version", action="version", version=f"jobpipeline {__version__}")
    return parser


def parse_cli_options(argv: Sequence[str] | None) -> CliOptions:
    args = build_parser().parse_args(argv)
    return CliOptions(
        data_dir=args.data_dir,
        port=args.port,
        demo=args.demo,
        setup=args.setup,
        no_browser=args.no_browser,
    )


def server_config(app: FastAPI, port: int) -> uvicorn.Config:
    """Loopback only; our logging, not uvicorn's; no proxy headers or root path (ADR-039)."""
    return uvicorn.Config(
        app, host=HOST, port=port, proxy_headers=False, log_config=None, root_path=""
    )


def default_launcher() -> Launcher:
    return Launcher(
        port_free=_port_free,
        serve=_serve,
        health_ok=_health_ok,
        open_browser=webbrowser.open,
        sleep=time.sleep,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    launcher: Launcher | None = None,
) -> None:
    cli = parse_cli_options(argv)
    environ = os.environ if env is None else env
    try:
        config = resolve_config(cli, environ)
    except ConfigError as error:
        _fail(2, f"{error.user_message} ({error})")
    try:
        _run(config, environ, launcher if launcher is not None else default_launcher())
    except KeyboardInterrupt:
        raise SystemExit(INTERRUPTED_EXIT) from None


@dataclass(frozen=True)
class _StartPlan:
    """Whether start-up opens the data folder, decided once (ADR-044 item 3)."""

    open_at_startup: bool
    attach_log_folder: Callable[[Path], None] | None
    failed: str  # the one line printed when start-up fails


def _run(config: RuntimeConfig, environ: Mapping[str, str], launcher: Launcher) -> None:
    os.environ[SDK_SKIP_VERSION_CHECK] = "1"
    redactor = Redactor()
    if opens_workspace_at_startup(config):
        logging_setup = _log_to_data_folder(config, redactor)
        plan = _StartPlan(
            True, None, f"The app couldn't start. Details are in {config.data_dir / LOG_FOLDER}."
        )
    else:
        # First run: create nothing yet; step 1 opens the folder and the buffered records
        # move into its log file then. Until that, the console has them.
        logging_setup = configure_logging(None, config.log_level, redactor)
        plan = _StartPlan(
            False, logging_setup.attach_log_folder, "The app couldn't start. Details are above."
        )
    restore_hooks = install_exception_hooks()
    stop_polling = threading.Event()
    try:
        # After logging is set up, so its fake-provider warning is kept; it registers the
        # environment secrets with the same redactor before anything can log them.
        wiring = build_wiring(environ=environ, demo=config.demo, redactor=redactor)
        _start(config, launcher, wiring, stop_polling, plan)
    except Exception as error:
        _log_start_failure(error, to_file=plan.open_at_startup)
        _fail(1, plan.failed)
    finally:
        stop_polling.set()
        restore_hooks()
        logging_setup.close()


def _log_start_failure(error: Exception, *, to_file: bool) -> None:
    """Logged while the handlers are still attached.

    With a log file it keeps the traceback (the console shows one line). Without one the
    console line names the exception type only: its text may hold paths or values.
    """
    if to_file:
        _log.critical("Start-up failed", exc_info=True)
    else:
        _log.critical("Start-up failed (%s)", type(error).__name__)


def _log_to_data_folder(config: RuntimeConfig, redactor: Redactor) -> LoggingSetup:
    """Create the data folder (0700) and log into its ``logs`` folder from the start."""
    create_failed = (
        "Couldn't create the data folder or its logs. "
        "Check --data-dir, JOBPIPELINE_DATA_DIR or the settings file."
    )
    try:
        config.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        _fail(1, create_failed)
    # Checked after mkdir (which writes nothing into an existing folder), so a folder someone
    # else created in the meantime is caught too; before logging writes anything (ADR-043).
    if owned_by_another_user(config.data_dir):
        _fail(
            2,
            f"The data folder {config.data_dir} belongs to another user. "
            "Choose a folder you own with --data-dir.",
        )
    try:
        return configure_logging(config.data_dir / LOG_FOLDER, config.log_level, redactor)
    except OSError:
        _fail(1, create_failed)


def _start(
    config: RuntimeConfig,
    launcher: Launcher,
    wiring: Wiring,
    stop_polling: threading.Event,
    plan: _StartPlan,
) -> None:
    app = create_app(
        config,
        attach_log_folder=plan.attach_log_folder,
        open_at_startup=plan.open_at_startup,
        wiring=wiring,
    )
    wiring.redactor.register(app.state.launch_token)  # the redactor logging uses
    if not launcher.port_free(config.port):
        _fail(
            1,
            f"Port {config.port} is already in use. "
            f"Close the other program or start with --port {_other_port(config.port)}.",
        )
    if not config.no_browser:
        _open_browser_when_ready(launcher, config.port, stop_polling)
    try:
        with _ctrl_break_as_interrupt():
            started = launcher.serve(server_config(app, config.port))
    except SystemExit:  # uvicorn exits on a lifespan or bind failure after logging it
        started = False
    except KeyboardInterrupt:  # uvicorn re-raises Ctrl+C (and Ctrl+Break) after shutting down
        started = True
    if not started:
        _fail(1, plan.failed)


@contextmanager
def _ctrl_break_as_interrupt() -> Iterator[None]:
    """On Windows, make the Ctrl+Break that uvicorn re-raises a KeyboardInterrupt.

    Python's default SIGBREAK action ends the process with code 3 and skips clean-up.
    """
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is None:
        yield
        return
    previous = signal.getsignal(sigbreak)
    signal.signal(sigbreak, signal.default_int_handler)
    try:
        yield
    finally:
        signal.signal(sigbreak, previous)


def _open_browser_when_ready(launcher: Launcher, port: int, stop: threading.Event) -> None:
    def poll() -> None:
        for _ in range(HEALTH_POLL_ATTEMPTS):
            if stop.is_set():
                return
            if launcher.health_ok(f"http://{HOST}:{port}/health"):
                launcher.open_browser(f"http://{HOST}:{port}/")
                return
            launcher.sleep(HEALTH_POLL_SECONDS)
        if not stop.is_set():
            _log.warning("Open http://%s:%d/ in your browser.", HOST, port)

    threading.Thread(target=poll, name="open-browser", daemon=True).start()


def _fail(code: int, message: str) -> NoReturn:
    print(f"jobpipeline: {message}", file=sys.stderr)
    raise SystemExit(code)


def _other_port(port: int) -> int:
    return port + 10 if port + 10 <= 65535 else port - 10


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if os.name == "posix":  # match uvicorn, which reuses ports in TIME_WAIT
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((HOST, port))
        except OSError:
            return False
    return True


def _serve(config: uvicorn.Config) -> bool:
    server = uvicorn.Server(config)
    server.run()
    return server.started


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def health_opener() -> urllib.request.OpenerDirector:
    """Talks to the local app directly: no proxy from the environment, no redirects."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _RefuseRedirects())


def _health_ok(url: str) -> bool:
    try:
        # A fixed loopback http URL built above, never user input.
        with health_opener().open(url, timeout=1) as response:
            return bool(response.status == 200)
    except OSError:
        return False
