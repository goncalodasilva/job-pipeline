"""Start the real app in a subprocess for smoke and end-to-end tests (spec 4.1; M0.16).

The app runs as ``sys.executable -m app`` with the project root on ``PYTHONPATH``, not through
the ``jobpipeline`` console script: on macOS a venv's ``.pth`` files can carry the ``hidden``
flag, which Python 3.14 skips, so the console script may fail to import ``app``.

Every ``JOBPIPELINE_*``, ``ANTHROPIC_*`` and ``CLAUDE_*`` variable from the caller's
environment is dropped, the home and platform folders point into the test's temporary folder,
and so do the data and config folders (ADR-024), so the real user folders and any real
credentials are never read or written. Any inherited ``PYTHON_KEYRING_BACKEND`` is replaced
by the file-backed test keyring (``tests/fakes/keyring_backends.py``) whose JSON file sits in
the test's temporary folder, so the app never touches the real system keychain (ADR-044).
``JOBPIPELINE_ALLOW_FAKE_PROVIDERS=1`` is set, so the fake provider the harness saves is
healthy and selectable (ADR-054). The
server binds to 127.0.0.1 only; nothing here uses the network beyond loopback.

``setup`` chooses the folders' starting state (M1.9): ``"complete"`` (the default) runs
``tests/setup_harness.complete_setup`` on them first, so the app opens straight into its pages;
``"fresh"`` writes nothing beforehand. The data folder always comes from
``JOBPIPELINE_DATA_DIR``, so a fresh app is the explicit-folder case, not AC-M1.1 case 1: it
creates the folder and its database at start-up with no step done, and every page redirects to
the guide's first step (AC-M1.2). ``"first_run"`` is AC-M1.1 case 1: nothing is written
beforehand and ``JOBPIPELINE_DATA_DIR`` is not set, so the data folder is the platform default
under the test's temporary home; the app starts without creating it and step 1 of the guide does
(M1.16). Its path is computed with the app's own environment and must lie inside the test's
temporary folder, otherwise the app is not started. ``"provider_step"`` writes ``config.toml``
and a database with only step 1 done and no provider saved, so the guide opens at the AI provider
step (M1.17). ``"preferences_step"`` writes ``config.toml`` and a database with steps 1 and 2
done and the fake provider saved, so the guide opens at the job preferences step (M1.19).
``AppStart`` combines a state with ``allow_fakes=False`` for runs where
``JOBPIPELINE_ALLOW_FAKE_PROVIDERS`` is unset (ADR-059).

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
from typing import IO, Literal

import httpx

from app import __version__
from app.config import APP_NAME, write_bootstrap
from tests.setup_harness import HARNESS_PROVIDER, complete_setup, seed_workspace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOOPBACK = "127.0.0.1"
DROPPED_ENV_PREFIXES = ("JOBPIPELINE_", "ANTHROPIC_", "CLAUDE_")
KEYRING_BACKEND_VARIABLE = "PYTHON_KEYRING_BACKEND"
TEST_KEYRING_BACKEND = "tests.fakes.keyring_backends.FileKeyring"
TEST_KEYRING_FILE_VARIABLE = "JOBPIPELINE_TEST_KEYRING_FILE"
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
ALLOWED_CLI_ARGS = frozenset({"--setup"})  # fixed flags only; never paths or user data

SetupState = Literal["complete", "fresh", "first_run", "provider_step", "preferences_step"]
SETUP_STATES: tuple[SetupState, ...] = (
    "complete",
    "fresh",
    "first_run",
    "provider_step",
    "preferences_step",
)


@dataclass(frozen=True)
class AppStart:
    """How ``running_app`` starts the app when a state name alone isn't enough."""

    setup: SetupState = "complete"
    allow_fakes: bool = True
    cli_args: tuple[str, ...] = ()  # extra fixed flags such as "--setup" (M1.20)


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


def isolated_env(
    data_dir: Path, config_dir: Path, *, allow_fakes: bool = True, explicit_data_dir: bool = True
) -> dict[str, str]:
    """The caller's environment without app or credential variables, pointed at temp folders.

    ``allow_fakes=False`` leaves ``JOBPIPELINE_ALLOW_FAKE_PROVIDERS`` unset;
    ``explicit_data_dir=False`` leaves ``JOBPIPELINE_DATA_DIR`` unset (a true first run), while
    the home folders still point next to ``data_dir``."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(DROPPED_ENV_PREFIXES)
        and key.upper() not in (KEYRING_BACKEND_VARIABLE, TEST_KEYRING_FILE_VARIABLE)
    }
    env[KEYRING_BACKEND_VARIABLE] = TEST_KEYRING_BACKEND
    env[TEST_KEYRING_FILE_VARIABLE] = str(data_dir.parent / "keyring.json")
    home = data_dir.parent / "home"
    home.mkdir(parents=True, exist_ok=True)
    for name in HOME_VARIABLES:
        env[name] = str(home)
    if explicit_data_dir:
        env["JOBPIPELINE_DATA_DIR"] = str(data_dir)
    env["JOBPIPELINE_CONFIG_DIR"] = str(config_dir)
    # The harness saves the fake provider; allowing fakes keeps a completed setup healthy, and
    # the setup guide's e2e tests choose the fake (ADR-044 item 2, ADR-054).
    if allow_fakes:
        env["JOBPIPELINE_ALLOW_FAKE_PROVIDERS"] = "1"
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


def start_app(
    tmp_path: Path,
    *,
    setup: SetupState = "complete",
    allow_fakes: bool = True,
    cli_args: tuple[str, ...] = (),
) -> RunningApp:
    """Start the app on a free loopback port and wait until ``/health`` answers 200.

    ``allow_fakes=False`` starts it without ``JOBPIPELINE_ALLOW_FAKE_PROVIDERS``; ``cli_args``
    adds flags from ``ALLOWED_CLI_ARGS`` (such as ``--setup``) to the command line."""
    if unknown := [arg for arg in cli_args if arg not in ALLOWED_CLI_ARGS]:
        raise ValueError(f"unsupported app flags {unknown!r}")
    if setup not in SETUP_STATES:
        raise ValueError(f"unknown setup state {setup!r}")
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    if setup == "complete":
        complete_setup(data_dir, config_dir)
    elif setup == "provider_step":
        write_bootstrap(config_dir, data_dir=data_dir, version=__version__)
        seed_workspace(data_dir, done=("welcome",))
    elif setup == "preferences_step":
        write_bootstrap(config_dir, data_dir=data_dir, version=__version__)
        seed_workspace(data_dir, done=("welcome", "provider"), provider=HARNESS_PROVIDER)
    explicit = setup != "first_run"
    env = isolated_env(data_dir, config_dir, allow_fakes=allow_fakes, explicit_data_dir=explicit)
    if not explicit:
        data_dir = platform_default_data_dir(env, tmp_path)
    port = free_loopback_port()
    stdout_path = tmp_path / "app-stdout.txt"
    stderr_path = tmp_path / "app-stderr.txt"
    command = [sys.executable, "-m", "app", "--no-browser", "--port", str(port), *cli_args]
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

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


def platform_default_data_dir(env: dict[str, str], tmp_path: Path) -> Path:
    """The default data folder the app will choose with ``env`` (``platformdirs``, run in a
    child process so this process's environment is untouched). Refuses any folder outside
    ``tmp_path``, so a first run never points at the real user folders."""
    script = "import sys, platformdirs; sys.stdout.write(platformdirs.user_data_dir(sys.argv[1]))"
    result = subprocess.run(  # noqa: S603 - fixed argv: this interpreter and a fixed script
        [sys.executable, "-c", script, APP_NAME],
        env=env,
        capture_output=True,
        check=True,
        timeout=READY_TIMEOUT_SECONDS,
    )
    folder = Path(result.stdout.decode("utf-8"))
    if not folder.is_absolute() or not folder.resolve().is_relative_to(tmp_path.resolve()):
        raise AppDidNotStart("the default data folder is outside the test's temporary folder")
    return folder


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
