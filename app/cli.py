"""The ``jobpipeline`` command (spec 7.1; ADR-024, 025, 030, 037, 039, 040).

Start-up order matters: resolve the config, create the data folder, set up redacted logging
and the exception hooks, build the app, check the port, then serve on 127.0.0.1 only. The
browser opens once ``/health`` answers. Errors print one plain line, never a traceback; the
log file keeps the details.

Exit codes: 0 after a normal stop, 1 when the port is taken or start-up fails, 2 for an
invalid setting or command-line option, 130 for Ctrl+C before the server starts (ADR-040).
"""

import argparse
import logging
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import uvicorn
from fastapi import FastAPI

from app import __version__
from app.config import CliOptions, RuntimeConfig, resolve_config
from app.domain.errors import ConfigError
from app.domain.redaction import Redactor
from app.logging_config import configure_logging, install_exception_hooks
from app.main import create_app

HOST = "127.0.0.1"
HEALTH_POLL_SECONDS = 0.25
HEALTH_POLL_ATTEMPTS = 40  # about 10 seconds
INTERRUPTED_EXIT = 130  # Ctrl+C before the server took over signal handling

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
    try:
        config = resolve_config(cli, os.environ if env is None else env)
    except ConfigError as error:
        _fail(2, f"{error.user_message} ({error})")
    try:
        _run(config, launcher if launcher is not None else default_launcher())
    except KeyboardInterrupt:
        raise SystemExit(INTERRUPTED_EXIT) from None


def _run(config: RuntimeConfig, launcher: Launcher) -> None:
    log_dir = config.data_dir / "logs"
    redactor = Redactor()
    try:
        config.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        logging_setup = configure_logging(log_dir, config.log_level, redactor)
    except OSError:
        _fail(
            1,
            "Couldn't create the data folder or its logs. "
            "Check --data-dir, JOBPIPELINE_DATA_DIR or the settings file.",
        )
    restore_hooks = install_exception_hooks()
    stop_polling = threading.Event()
    try:
        _start(config, launcher, redactor, stop_polling)
    except Exception:
        # Logged while the handlers are still attached: the file keeps the traceback.
        _log.critical("Start-up failed", exc_info=True)
        _fail(1, f"The app couldn't start. Details are in {log_dir}.")
    finally:
        stop_polling.set()
        restore_hooks()
        logging_setup.close()


def _start(
    config: RuntimeConfig, launcher: Launcher, redactor: Redactor, stop_polling: threading.Event
) -> None:
    app = create_app(config)
    redactor.register(app.state.launch_token)
    if not launcher.port_free(config.port):
        _fail(
            1,
            f"Port {config.port} is already in use. "
            f"Close the other program or start with --port {_other_port(config.port)}.",
        )
    if not config.no_browser:
        _open_browser_when_ready(launcher, config.port, stop_polling)
    try:
        started = launcher.serve(server_config(app, config.port))
    except SystemExit:  # uvicorn exits on a lifespan or bind failure after logging it
        started = False
    if not started:
        _fail(1, f"The app couldn't start. Details are in {config.data_dir / 'logs'}.")


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
