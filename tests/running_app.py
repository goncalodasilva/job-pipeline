"""Start the real app in a subprocess for smoke and end-to-end tests (spec 4.1; M0.16).

The app runs as ``sys.executable -m app`` with the project root on ``PYTHONPATH``, not through
the ``jobpipeline`` console script: on macOS a venv's ``.pth`` files can carry the ``hidden``
flag, which Python 3.14 skips, so the console script may fail to import ``app``.

Every ``JOBPIPELINE_*``, ``ANTHROPIC_*`` and ``CLAUDE_*`` variable from the caller's
environment is dropped, the home and platform folders point into the test's temporary folder,
and so do the data and config folders (ADR-024), so the real user folders and any real
credentials are never read or written. The server binds to 127.0.0.1 only; nothing here uses
the network beyond loopback.

Stopping is graceful first: SIGINT on POSIX, CTRL_BREAK_EVENT on Windows (the child runs in
its own process group so the event reaches only it; it must share the test runner's console,
so runners without a console fall back to a kill and fail loudly). The process is killed only
if it does not exit within ``STOP_TIMEOUT_SECONDS``; the caller treats that as a failure.
"""

import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOOPBACK = "127.0.0.1"
DROPPED_ENV_PREFIXES = ("JOBPIPELINE_", "ANTHROPIC_", "CLAUDE_")
HOME_VARIABLES = (
    "HOME",
    "USERPROFILE",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "APPDATA",
    "LOCALAPPDATA",
)
READY_TIMEOUT_SECONDS = 20.0  # generous, so a slow start fails with a clear message
STOP_TIMEOUT_SECONDS = 10.0
POLL_INTERVAL_SECONDS = 0.05
REQUEST_TIMEOUT_SECONDS = 2.0
OUTPUT_TAIL_CHARS = 4000


class AppDidNotStart(RuntimeError):
    """The subprocess exited, or /health never answered 200, before the deadline."""


@dataclass(frozen=True)
class StopResult:
    returncode: int
    killed: bool


def free_loopback_port() -> int:
    """Ask the OS for a free port on 127.0.0.1, then release it for the app to take."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK, 0))
        port: int = probe.getsockname()[1]
    return port


def isolated_env(data_dir: Path, config_dir: Path) -> dict[str, str]:
    """The caller's environment without app or credential variables, pointed at temp folders."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(DROPPED_ENV_PREFIXES)
    }
    home = data_dir.parent / "home"
    home.mkdir(parents=True, exist_ok=True)
    for name in HOME_VARIABLES:
        env[name] = str(home)
    env["JOBPIPELINE_DATA_DIR"] = str(data_dir)
    env["JOBPIPELINE_CONFIG_DIR"] = str(config_dir)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(PROJECT_ROOT) if not existing else os.pathsep.join([str(PROJECT_ROOT), existing])
    )
    env["PYTHONUNBUFFERED"] = "1"
    return env


@dataclass
class RunningApp:
    base_url: str
    port: int
    data_dir: Path
    config_dir: Path
    start_time: float  # time.monotonic() just before the process was started
    ready_time: float  # time.monotonic() when /health first answered 200
    process: subprocess.Popen[bytes]
    stdout_path: Path
    stderr_path: Path
    _streams: tuple[IO[bytes], ...] = field(default=(), repr=False)
    _stop_result: StopResult | None = field(default=None, repr=False)

    @property
    def host(self) -> str:
        return f"{LOOPBACK}:{self.port}"

    @property
    def origin(self) -> str:
        return f"http://{self.host}"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    def client(self) -> httpx.Client:
        """An HTTP client for this app: loopback base URL, no proxies from the environment."""
        return httpx.Client(
            base_url=self.base_url,
            headers={"host": self.host},
            timeout=REQUEST_TIMEOUT_SECONDS,
            trust_env=False,
            follow_redirects=False,
        )

    def stop(self) -> StopResult:
        """Stop gracefully, kill only as a last resort. Safe to call more than once."""
        if self._stop_result is not None:
            return self._stop_result
        killed = False
        try:
            if self.process.poll() is None:
                _send_graceful_stop(self.process)
                try:
                    self.process.wait(timeout=STOP_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    killed = True
                    self.process.kill()
                    self.process.wait(timeout=STOP_TIMEOUT_SECONDS)
        finally:
            for stream in self._streams:
                stream.close()
        self._stop_result = StopResult(returncode=self.process.returncode, killed=killed)
        return self._stop_result

    def stdout_text(self) -> str:
        return _read_text(self.stdout_path)

    def stderr_text(self) -> str:
        return _read_text(self.stderr_path)

    def log_files(self) -> list[Path]:
        """Every log file, including rotated backups (``jobpipeline.log.1`` and so on)."""
        if not self.log_dir.is_dir():
            return []
        return sorted(path for path in self.log_dir.iterdir() if path.is_file())

    def output_tail(self) -> str:
        return (
            f"--- stdout ---\n{self.stdout_text()[-OUTPUT_TAIL_CHARS:]}\n"
            f"--- stderr ---\n{self.stderr_text()[-OUTPUT_TAIL_CHARS:]}"
        )


def start_app(tmp_path: Path) -> RunningApp:
    """Start the app on a free loopback port and wait until ``/health`` answers 200."""
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    port = free_loopback_port()
    stdout_path = tmp_path / "app-stdout.txt"
    stderr_path = tmp_path / "app-stderr.txt"
    command = [sys.executable, "-m", "app", "--no-browser", "--port", str(port)]
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    env = isolated_env(data_dir, config_dir)
    stdout = stdout_path.open("wb")
    stderr = stderr_path.open("wb")
    start_time = time.monotonic()
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv: this interpreter and our module
            command,
            cwd=tmp_path,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=creationflags,
        )
    except BaseException:
        stdout.close()
        stderr.close()
        raise
    running = RunningApp(
        base_url=f"http://{LOOPBACK}:{port}",
        port=port,
        data_dir=data_dir,
        config_dir=config_dir,
        start_time=start_time,
        ready_time=start_time,
        process=process,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        _streams=(stdout, stderr),
    )
    try:
        running.ready_time = _wait_until_healthy(running)
    except BaseException:  # including a pytest abort: never leave the app running
        running.stop()
        raise
    return running


def _wait_until_healthy(running: RunningApp) -> float:
    deadline = running.start_time + READY_TIMEOUT_SECONDS
    last_problem = "no response yet"
    with running.client() as client:
        while time.monotonic() < deadline:
            code = running.process.poll()
            if code is not None:
                raise AppDidNotStart(
                    f"the app exited with code {code} before /health answered\n"
                    f"{running.output_tail()}"
                )
            try:
                response = client.get("/health")
            except httpx.TransportError as error:
                last_problem = f"{type(error).__name__}"
            else:
                if response.status_code == 200:
                    return time.monotonic()
                last_problem = f"status {response.status_code}"
            time.sleep(POLL_INTERVAL_SECONDS)
    raise AppDidNotStart(
        f"/health did not answer 200 within {READY_TIMEOUT_SECONDS} s ({last_problem})\n"
        f"{running.output_tail()}"
    )


def _send_graceful_stop(process: subprocess.Popen[bytes]) -> None:
    if sys.platform == "win32":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        process.send_signal(signal.SIGINT)


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_bytes().decode("utf-8", errors="replace")
