"""The ``jobpipeline`` command (M0.1 ``--version``; M0.15 flags, binding, browser; spec 7.1).

``main`` runs with a fake ``Launcher`` that records calls and never binds a socket, opens a
browser or starts a server. Every test passes its own environment pointing the data and
config folders into ``tmp_path``, so the real folders are never touched. Logging and the
exception hooks are restored after each test. Every value here is fictional.
"""

import importlib.metadata
import logging
import os
import socket
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")
FICTIONAL_PORT = 8123
JOIN_TIMEOUT = 5.0


# --- fixtures and helpers ------------------------------------------------------------------


@pytest.fixture(autouse=True)
def restore_logging_and_hooks() -> Iterator[None]:
    """Save and restore root and uvicorn logger state and the exception hooks."""
    root = logging.getLogger()
    saved_root = (list(root.handlers), list(root.filters), root.level)
    saved_uvicorn = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).propagate,
            logging.getLogger(name).level,
        )
        for name in UVICORN_LOGGERS
    }
    saved_hooks = (sys.excepthook, threading.excepthook)
    yield
    for handler in root.handlers:
        if handler not in saved_root[0]:
            handler.close()
    root.handlers[:] = saved_root[0]
    root.filters[:] = saved_root[1]
    root.setLevel(saved_root[2])
    for name, (handlers, propagate, level) in saved_uvicorn.items():
        logger = logging.getLogger(name)
        logger.handlers[:] = handlers
        logger.propagate = propagate
        logger.setLevel(level)
    sys.excepthook, threading.excepthook = saved_hooks
    logging.captureWarnings(False)


@pytest.fixture(autouse=True)
def restore_sdk_version_check_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    """``main`` sets ``CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK`` for the process (M1.15, ADR-058);
    monkeypatch puts the variable back as it was after each test."""
    monkeypatch.delenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", raising=False)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def env(tmp_path: Path, data_dir: Path) -> dict[str, str]:
    return {"JOBPIPELINE_DATA_DIR": str(data_dir), "JOBPIPELINE_CONFIG_DIR": str(tmp_path / "cfg")}


@dataclass
class FakeLaunch:
    """Records every launcher call; ``build()`` returns the real ``Launcher`` dataclass."""

    port_is_free: bool = True
    health_results: list[bool] = field(default_factory=list)
    serve_result: bool = True
    on_serve: Callable[[Any], object] | None = None
    events: list[tuple[str, object]] = field(default_factory=list)
    served: list[Any] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def port_free(self, port: int) -> bool:
        with self.lock:
            self.events.append(("port_free", port))
        return self.port_is_free

    def serve(self, config: Any) -> bool:
        with self.lock:
            self.events.append(("serve", config))
            self.served.append(config)
        if self.on_serve is not None:
            self.on_serve(config)
        return self.serve_result

    def health_ok(self, url: str) -> bool:
        with self.lock:
            self.events.append(("health_ok", url))
            return self.health_results.pop(0) if self.health_results else False

    def open_browser(self, url: str) -> object:
        with self.lock:
            self.events.append(("open_browser", url))
        return True

    def sleep(self, seconds: float) -> None:
        with self.lock:
            self.sleeps.append(seconds)

    def calls(self, name: str) -> list[object]:
        with self.lock:
            return [arg for kind, arg in self.events if kind == name]

    def build(self) -> Any:
        from app.cli import Launcher

        return Launcher(
            port_free=self.port_free,
            serve=self.serve,
            health_ok=self.health_ok,
            open_browser=self.open_browser,
            sleep=self.sleep,
        )


def run_main(argv: list[str], env: dict[str, str], fake: FakeLaunch) -> None:
    """Run ``main`` and then wait for any thread it started (the health poller) to end."""
    from app.cli import main

    before = set(threading.enumerate())
    try:
        main(argv, env=env, launcher=fake.build())
    finally:
        for thread in set(threading.enumerate()) - before:
            thread.join(JOIN_TIMEOUT)
            assert not thread.is_alive(), "a thread started by main never finished"


def exit_code(fn: Callable[[], None]) -> object:
    with pytest.raises(SystemExit) as exc_info:
        fn()
    return exc_info.value.code


def capture_create_app(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Wrap ``app.cli.create_app`` so the ``RuntimeConfig`` it receives can be inspected."""
    from app.main import create_app as real_create_app

    seen: list[Any] = []

    def wrapper(config: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(config)
        return real_create_app(config, *args, **kwargs)

    monkeypatch.setattr("app.cli.create_app", wrapper)
    return seen


# --- M0.1: --version and the console script -------------------------------------------------


def test_cli_version_prints_package_version_and_exits_0(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.cli import main

    expected_version = importlib.metadata.version("jobpipeline")

    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])

    assert exc_info.value.code == 0
    assert f"jobpipeline {expected_version}" in capsys.readouterr().out


def test_cli_console_script_points_to_main() -> None:
    scripts = importlib.metadata.entry_points(group="console_scripts")
    matches = [ep for ep in scripts if ep.name == "jobpipeline"]

    assert [ep.value for ep in matches] == ["app.cli:main"]


# --- M0.15: flags --------------------------------------------------------------------------


def test_cli_parses_all_flags(tmp_path: Path) -> None:
    from app.cli import parse_cli_options
    from app.config import CliOptions

    folder = tmp_path / "fictional-data"

    options = parse_cli_options(
        [
            "--port",
            str(FICTIONAL_PORT),
            "--data-dir",
            str(folder),
            "--no-browser",
            "--setup",
            "--demo",
        ]
    )

    assert options == CliOptions(
        data_dir=folder,
        port=FICTIONAL_PORT,
        log_level=None,
        demo=True,
        setup=True,
        no_browser=True,
    )
    assert isinstance(options.data_dir, Path)
    assert isinstance(options.port, int)


def test_cli_parses_no_flags_to_unset_options() -> None:
    from app.cli import parse_cli_options
    from app.config import CliOptions

    assert parse_cli_options([]) == CliOptions()


def test_cli_rejects_non_numeric_port(capsys: pytest.CaptureFixture[str]) -> None:
    from app.cli import parse_cli_options

    assert exit_code(lambda: parse_cli_options(["--port", "eighty"])) == 2
    assert "--port" in capsys.readouterr().err


def test_cli_has_no_host_flag(capsys: pytest.CaptureFixture[str]) -> None:
    from app.cli import build_parser, main

    option_strings = {s for action in build_parser()._actions for s in action.option_strings}
    # The parser must know the new flags, so this test can't pass on the bare M0.1 parser.
    assert {"--port", "--data-dir", "--no-browser", "--setup", "--demo", "--version"} <= (
        option_strings
    )
    assert not any("host" in s for s in option_strings)

    assert exit_code(lambda: main(["--host", "192.0.2.10"], env={})) == 2
    assert "--host" in capsys.readouterr().err


# --- M0.15: binding and the uvicorn config --------------------------------------------------


def test_cli_binds_127_0_0_1_only(env: dict[str, str]) -> None:
    import uvicorn

    fake = FakeLaunch()

    run_main(["--no-browser", "--port", str(FICTIONAL_PORT)], env, fake)

    assert len(fake.served) == 1
    config = fake.served[0]
    assert isinstance(config, uvicorn.Config)
    assert config.host == "127.0.0.1"
    assert config.port == FICTIONAL_PORT
    assert config.proxy_headers is False
    assert config.log_config is None
    assert config.root_path == ""
    assert fake.calls("port_free") == [FICTIONAL_PORT]


def test_server_config_is_a_real_uvicorn_config_bound_to_loopback() -> None:
    import uvicorn
    from fastapi import FastAPI

    from app.cli import server_config

    app = FastAPI()

    config = server_config(app, FICTIONAL_PORT)

    assert isinstance(config, uvicorn.Config)
    assert config.app is app
    assert config.host == "127.0.0.1"
    assert config.port == FICTIONAL_PORT
    assert config.proxy_headers is False
    assert config.log_config is None
    assert config.root_path == ""


def test_server_config_keeps_uvicorn_loggers_routed_through_ours(tmp_path: Path) -> None:
    from fastapi import FastAPI

    from app.cli import server_config
    from app.domain.redaction import Redactor
    from app.logging_config import configure_logging

    setup = configure_logging(tmp_path / "logs", "INFO", Redactor())
    try:
        server_config(FastAPI(), FICTIONAL_PORT)

        for name in UVICORN_LOGGERS:
            logger = logging.getLogger(name)
            assert logger.handlers == [], name
            assert logger.propagate is True, name
    finally:
        setup.close()


# --- M0.15: browser opening -----------------------------------------------------------------


def test_cli_opens_browser_once_after_health_200(env: dict[str, str]) -> None:
    opened = threading.Event()
    fake = FakeLaunch(health_results=[False, False, True])
    original_open = fake.open_browser

    def open_and_signal(url: str) -> object:
        result = original_open(url)
        opened.set()
        return result

    fake.open_browser = open_and_signal  # type: ignore[method-assign]
    fake.on_serve = lambda _config: opened.wait(JOIN_TIMEOUT)

    run_main(["--port", str(FICTIONAL_PORT)], env, fake)

    health_url = f"http://127.0.0.1:{FICTIONAL_PORT}/health"
    root_url = f"http://127.0.0.1:{FICTIONAL_PORT}/"
    assert fake.calls("health_ok") == [health_url] * 3
    assert fake.calls("open_browser") == [root_url]
    kinds = [kind for kind, _ in fake.events if kind in ("health_ok", "open_browser")]
    assert kinds == ["health_ok", "health_ok", "health_ok", "open_browser"]
    assert len(fake.sleeps) >= 2  # waited between the failed probes


def test_cli_gives_up_on_health_after_about_ten_seconds(env: dict[str, str]) -> None:
    fake = FakeLaunch(health_results=[])  # never healthy

    run_main(["--port", str(FICTIONAL_PORT)], env, fake)

    assert fake.calls("open_browser") == []
    assert fake.calls("health_ok")
    assert 5.0 <= sum(fake.sleeps) <= 15.0


def test_cli_no_browser_never_opens(env: dict[str, str]) -> None:
    fake = FakeLaunch(health_results=[True] * 10)

    run_main(["--no-browser", "--port", str(FICTIONAL_PORT)], env, fake)

    assert fake.calls("health_ok") == []
    assert fake.calls("open_browser") == []
    assert len(fake.served) == 1


# --- M0.15: port in use ---------------------------------------------------------------------


def test_cli_port_in_use_prints_hint_and_exits_1(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeLaunch(port_is_free=False)

    code = exit_code(lambda: run_main(["--port", str(FICTIONAL_PORT)], env, fake))

    assert code == 1
    err = capsys.readouterr().err
    assert str(FICTIONAL_PORT) in err
    assert "--port" in err
    assert "in use" in err
    assert "Traceback" not in err
    assert fake.served == []
    assert fake.calls("open_browser") == []


def test_default_launcher_port_free_is_false_for_a_taken_loopback_port() -> None:
    from app.cli import default_launcher

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        port = taken.getsockname()[1]

        assert default_launcher().port_free(port) is False


# --- M0.15: --setup and --demo --------------------------------------------------------------


def test_cli_setup_flag_sets_force_setup(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = capture_create_app(monkeypatch)

    run_main(["--setup", "--no-browser"], env, FakeLaunch())

    assert len(seen) == 1
    assert seen[0].setup is True


def test_cli_without_setup_flag_leaves_setup_off(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = capture_create_app(monkeypatch)

    run_main(["--no-browser"], env, FakeLaunch())

    assert len(seen) == 1
    assert seen[0].setup is False


def test_cli_demo_selects_demo_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "platform"

    def fake_data_dir(appname: str | None = None, *args: object, **kwargs: object) -> str:
        return str(root / "data" / (appname or ""))

    def fake_config_dir(appname: str | None = None, *args: object, **kwargs: object) -> str:
        return str(root / "config" / (appname or ""))

    monkeypatch.setattr("app.config.user_data_dir", fake_data_dir)
    monkeypatch.setattr("app.config.user_config_dir", fake_config_dir)
    seen = capture_create_app(monkeypatch)
    env = {"JOBPIPELINE_CONFIG_DIR": str(tmp_path / "cfg")}

    run_main(["--demo", "--no-browser"], env, FakeLaunch())

    demo_dir = root / "data" / "jobpipeline-demo"
    assert len(seen) == 1
    assert seen[0].demo is True
    assert seen[0].data_dir == demo_dir
    assert demo_dir.is_dir()
    assert not (root / "data" / "jobpipeline").exists()


# --- M0.15: data folder, logging, hooks ------------------------------------------------------


def test_cli_creates_private_data_dir_and_log_file(env: dict[str, str], data_dir: Path) -> None:
    from app.logging_config import LOG_FILE_NAME

    run_main(["--no-browser"], env, FakeLaunch())

    assert data_dir.is_dir()
    assert (data_dir / "logs" / LOG_FILE_NAME).is_file()
    if os.name == "posix":
        assert data_dir.stat().st_mode & 0o777 == 0o700


def test_cli_configures_logging_and_hooks_before_serving_and_restores_after(
    env: dict[str, str], data_dir: Path
) -> None:
    from app.logging_config import PrivateRotatingFileHandler

    original_hooks = (sys.excepthook, threading.excepthook)
    observed: dict[str, object] = {}

    def inspect(_config: Any) -> None:
        file_handlers = [
            h for h in logging.getLogger().handlers if isinstance(h, PrivateRotatingFileHandler)
        ]
        observed["log_files"] = [Path(h.baseFilename).parent for h in file_handlers]
        observed["sys_hook_replaced"] = sys.excepthook is not original_hooks[0]
        observed["thread_hook_replaced"] = threading.excepthook is not original_hooks[1]

    root_before = list(logging.getLogger().handlers)

    run_main(["--no-browser"], env, FakeLaunch(on_serve=inspect))

    assert observed["log_files"] == [data_dir / "logs"]
    assert observed["sys_hook_replaced"] is True
    assert observed["thread_hook_replaced"] is True
    assert (sys.excepthook, threading.excepthook) == original_hooks
    assert logging.getLogger().handlers == root_before


def test_cli_sdk_records_stay_reduced_after_logging_closes(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI keeps the SDK record reduction for the process: an SDK record made after the
    logging setup closed (a late reader task at shutdown) reaches ``logging.lastResort``
    reduced. The test starts from the plain factory (the session installs the reduction)."""
    secret = "-".join(["sk", "ant", "oat01", "FictionalCliShutdownValue0123456789abcdef"])
    saved = logging.getLogRecordFactory()
    logging.setLogRecordFactory(logging.LogRecord)
    logger = logging.getLogger("claude_agent_sdk.m123_after_shutdown")
    logger.propagate = False  # no handler anywhere: logging.lastResort writes it
    try:
        run_main(["--no-browser"], env, FakeLaunch())
        capsys.readouterr()

        logger.error(f"Fatal error in message reader: {secret}")

        assert logging.getLogRecordFactory() is not logging.LogRecord
    finally:
        logger.propagate = True
        logging.setLogRecordFactory(saved)
    err = capsys.readouterr().err
    assert secret not in err
    assert "ERROR from claude_agent_sdk.m123_after_shutdown" in err


def test_cli_launch_token_never_reaches_the_log_file(
    env: dict[str, str], data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.domain.redaction import REDACTED
    from app.logging_config import LOG_FILE_NAME

    tokens: list[str] = []

    def log_token(config: Any) -> None:
        token = config.app.state.launch_token
        tokens.append(token)
        logging.getLogger("jobpipeline.test.cli").warning("fictional leak of %s", token)

    run_main(["--no-browser"], env, FakeLaunch(on_serve=log_token))

    assert len(tokens) == 1 and tokens[0]
    text = (data_dir / "logs" / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert "fictional leak of" in text
    assert tokens[0] not in text
    assert REDACTED in text
    assert tokens[0] not in capsys.readouterr().err


# --- M0.15: errors print plain messages -----------------------------------------------------


def test_cli_config_error_prints_detail_and_exits_2(
    tmp_path: Path, data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.domain.errors import ConfigError

    fake = FakeLaunch()
    env = {
        "JOBPIPELINE_DATA_DIR": str(data_dir),
        "JOBPIPELINE_CONFIG_DIR": str(tmp_path / "cfg"),
        "JOBPIPELINE_PORT": "abc",
    }

    code = exit_code(lambda: run_main(["--no-browser"], env, fake))

    assert code == 2
    err = capsys.readouterr().err
    assert "jobpipeline:" in err
    assert ConfigError.message in err
    assert "JOBPIPELINE_PORT must be a whole number" in err
    assert "Traceback" not in err
    assert fake.events == []
    assert not data_dir.exists()


def test_cli_startup_failure_prints_plain_message_and_exits_1(
    env: dict[str, str], data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.logging_config import LOG_FILE_NAME

    def fail_like_uvicorn(_config: Any) -> None:
        # uvicorn 0.54 logs Starlette's traceback as message text, then calls sys.exit(3).
        logging.getLogger("uvicorn.error").error(
            "Traceback (most recent call last):\n"
            '  File "fictional.py", line 1, in startup\n'
            "RuntimeError: fictional migration failure"
        )
        logging.getLogger("uvicorn.error").error("Application startup failed. Exiting.")
        raise SystemExit(3)

    fake = FakeLaunch(on_serve=fail_like_uvicorn)

    code = exit_code(lambda: run_main(["--no-browser"], env, fake))

    assert code == 1
    err = capsys.readouterr().err
    assert "couldn't start" in err
    assert str(data_dir / "logs") in err
    assert "Traceback" not in err
    text = (data_dir / "logs" / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert "Traceback" in text
    assert "fictional migration failure" in text


# --- review regressions: real start-up failures ---------------------------------------------


def test_cli_bind_failure_after_precheck_prints_plain_message_and_exits_1(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    def bind_race(_config: Any) -> None:
        logging.getLogger("uvicorn.error").error(
            "[Errno 48] error while attempting to bind on address: address already in use"
        )
        raise SystemExit(1)

    code = exit_code(lambda: run_main(["--no-browser"], env, FakeLaunch(on_serve=bind_race)))

    assert code == 1
    err = capsys.readouterr().err
    assert "couldn't start" in err
    assert "Traceback" not in err


def test_cli_unexpected_error_is_logged_and_printed_plainly(
    env: dict[str, str],
    data_dir: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.logging_config import LOG_FILE_NAME

    def broken_create_app(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("fictional factory failure")

    monkeypatch.setattr("app.cli.create_app", broken_create_app)
    fake = FakeLaunch()

    code = exit_code(lambda: run_main(["--no-browser"], env, fake))

    assert code == 1
    err = capsys.readouterr().err
    assert "couldn't start" in err
    assert "Traceback" not in err
    assert fake.calls("serve") == []
    text = (data_dir / "logs" / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert "fictional factory failure" in text


def test_cli_data_dir_that_is_a_file_prints_plain_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    not_a_folder = tmp_path / "data"
    not_a_folder.write_text("fictional", encoding="utf-8")
    env = {"JOBPIPELINE_DATA_DIR": str(not_a_folder), "JOBPIPELINE_CONFIG_DIR": str(tmp_path)}

    code = exit_code(lambda: run_main(["--no-browser"], env, FakeLaunch()))

    assert code == 1
    err = capsys.readouterr().err
    assert "data folder" in err
    assert "Traceback" not in err


def test_cli_ctrl_c_before_serving_exits_quietly(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted(_port: int) -> bool:
        raise KeyboardInterrupt

    fake = FakeLaunch()
    launcher = fake.build()
    from dataclasses import replace

    from app.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main(["--no-browser"], env=env, launcher=replace(launcher, port_free=interrupted))

    assert exc_info.value.code == 130
    assert "Traceback" not in capsys.readouterr().err


def test_cli_browser_never_opens_after_serve_fails(env: dict[str, str]) -> None:
    fake = FakeLaunch(health_results=[], on_serve=lambda _c: (_ for _ in ()).throw(SystemExit(3)))

    exit_code(lambda: run_main([], env, fake))

    assert fake.calls("open_browser") == []


def test_default_launcher_port_free_is_true_for_a_free_loopback_port() -> None:
    from app.cli import default_launcher

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # Freed just now; a race with another process is possible but vanishingly unlikely.
    assert default_launcher().port_free(port) is True


def test_health_probe_ignores_proxy_settings_and_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import urllib.request

    from app.cli import health_opener

    monkeypatch.setenv("http_proxy", "http://proxy.invalid:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    opener = health_opener()

    proxies = [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert all(h.proxies == {} for h in proxies)  # type: ignore[attr-defined]
    redirects = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
    assert redirects, "a redirect handler that refuses redirects must be installed"
    assert all(type(h) is not urllib.request.HTTPRedirectHandler for h in redirects)


def test_real_uvicorn_lifespan_failure_prints_plain_message_and_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end with the real launcher (loopback only, no browser): app.db is a folder."""
    data = tmp_path / "data"
    (data / "app.db").mkdir(parents=True)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {"JOBPIPELINE_DATA_DIR": str(data), "JOBPIPELINE_CONFIG_DIR": str(tmp_path / "cfg")}
    from app.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main(["--no-browser", "--port", str(port)], env=env)

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "couldn't start" in err
    assert "Traceback" not in err


def test_cli_ctrl_c_after_start_is_a_normal_stop(env: dict[str, str]) -> None:
    # uvicorn re-raises the captured Ctrl+C after shutting down cleanly (ADR-040: exit 0).
    def stopped_by_ctrl_c(_config: Any) -> None:
        raise KeyboardInterrupt

    result = run_main(["--no-browser"], env, FakeLaunch(on_serve=stopped_by_ctrl_c))

    assert result is None  # returned normally: exit code 0


def test_cli_maps_windows_ctrl_break_to_keyboard_interrupt_while_serving(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # uvicorn re-raises Ctrl+Break after shutting down; Python's default for SIGBREAK would end
    # the process with code 3, so the CLI treats it like Ctrl+C while serving (ADR-040).
    import signal as real_signal
    import types

    installed: dict[int, object] = {}
    fake_signal = types.SimpleNamespace(
        SIGBREAK=21,
        default_int_handler=real_signal.default_int_handler,
        getsignal=lambda signum: installed.get(signum, "previous"),
        signal=lambda signum, handler: installed.__setitem__(signum, handler),
    )
    monkeypatch.setattr("app.cli.signal", fake_signal)
    seen: list[object] = []

    run_main(["--no-browser"], env, FakeLaunch(on_serve=lambda _c: seen.append(installed.get(21))))

    assert seen == [real_signal.default_int_handler]
    assert installed[21] == "previous"


# --- data folder ownership (security review M0.19; ADR-043) ---------------------------------


def test_cli_refuses_data_dir_owned_by_another_user(
    env: dict[str, str],
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Another account that owns the folder could replace files or plant symlinks in it."""
    data_dir.mkdir()
    other_user = data_dir.stat().st_uid + 1
    monkeypatch.setattr("app.cli.os.geteuid", lambda: other_user, raising=False)
    fake = FakeLaunch()

    code = exit_code(lambda: run_main(["--no-browser"], env, fake))

    assert code == 2
    err = capsys.readouterr().err
    assert err.startswith("jobpipeline: ")
    assert "another user" in err
    assert str(data_dir) in err
    assert "Traceback" not in err
    assert list(data_dir.iterdir()) == [], "nothing may be written into a foreign folder"
    assert fake.calls("serve") == []


def test_cli_accepts_existing_data_dir_owned_by_current_user(
    env: dict[str, str], data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir.mkdir()
    owner = data_dir.stat().st_uid
    monkeypatch.setattr("app.cli.os.geteuid", lambda: owner, raising=False)
    fake = FakeLaunch()

    run_main(["--no-browser"], env, fake)

    assert len(fake.calls("serve")) == 1


def test_cli_skips_owner_check_without_posix_user_ids(
    env: dict[str, str], data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows has no geteuid; ownership there is documented, not checked (ADR-043)."""
    data_dir.mkdir()
    monkeypatch.delattr("app.cli.os.geteuid", raising=False)
    fake = FakeLaunch()

    run_main(["--no-browser"], env, fake)

    assert len(fake.calls("serve")) == 1


def test_cli_checks_ownership_after_creating_the_folder(
    env: dict[str, str],
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A folder another account creates just before mkdir (a race) is refused too."""
    assert not data_dir.exists()
    monkeypatch.setattr("app.cli.os.geteuid", lambda: -1, raising=False)  # nobody owns it
    fake = FakeLaunch()

    code = exit_code(lambda: run_main(["--no-browser"], env, fake))

    assert code == 2
    assert "another user" in capsys.readouterr().err
    assert list(data_dir.iterdir()) == []
    assert fake.calls("serve") == []


# --- M1.5: deferred workspace on a first run (AC-M1.1 cases 1 and 2; ADR-044 item 3) ---------


@pytest.fixture
def platform_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the platformdirs defaults into ``tmp_path``; return their root."""
    root = tmp_path / "platform"

    def fake_data_dir(appname: str | None = None, *args: object, **kwargs: object) -> str:
        return str(root / "data" / (appname or ""))

    def fake_config_dir(appname: str | None = None, *args: object, **kwargs: object) -> str:
        return str(root / "config" / (appname or ""))

    monkeypatch.setattr("app.config.user_data_dir", fake_data_dir)
    monkeypatch.setattr("app.config.user_config_dir", fake_config_dir)
    return root


@pytest.fixture
def first_run_env(tmp_path: Path) -> dict[str, str]:
    """No JOBPIPELINE_DATA_DIR and a config folder without config.toml: a first run."""
    return {"JOBPIPELINE_CONFIG_DIR": str(tmp_path / "cfg")}


def capture_create_app_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, dict[str, Any]]]:
    """Wrap ``app.cli.create_app`` and record its config and keyword arguments."""
    from app.main import create_app as real_create_app

    calls: list[tuple[Any, dict[str, Any]]] = []

    def wrapper(config: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append((config, kwargs))
        return real_create_app(config, *args, **kwargs)

    monkeypatch.setattr("app.cli.create_app", wrapper)
    return calls


def _file_handler_folders() -> list[Path]:
    from app.logging_config import PrivateRotatingFileHandler

    return [
        Path(h.baseFilename).parent
        for h in logging.getLogger().handlers
        if isinstance(h, PrivateRotatingFileHandler)
    ]


def test_cli_first_run_does_not_create_default_data_folder(
    tmp_path: Path,
    platform_root: Path,
    first_run_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = capture_create_app_calls(monkeypatch)
    observed: dict[str, object] = {}

    def inspect(_config: Any) -> None:
        observed["file_logs"] = _file_handler_folders()

    run_main(["--no-browser"], first_run_env, FakeLaunch(on_serve=inspect))

    default_dir = platform_root / "data" / "jobpipeline"
    assert not default_dir.exists()
    assert not (platform_root / "data").exists()
    assert not (tmp_path / "cfg").exists(), "the CLI never writes the bootstrap config"
    assert len(calls) == 1
    config, kwargs = calls[0]
    assert config.data_dir == default_dir
    assert config.data_dir_source == "default"
    assert callable(kwargs.get("attach_log_folder")), "step 1 needs the log-folder callback"
    assert observed["file_logs"] == [], "no log file until the workspace is opened"


def test_cli_bootstrap_without_database_does_not_create_anything(
    tmp_path: Path,
    platform_root: Path,
    first_run_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import write_bootstrap

    moved = tmp_path / "moved-data"  # the folder the bootstrap names no longer exists
    write_bootstrap(tmp_path / "cfg", data_dir=moved, version="0.0.1-test")
    calls = capture_create_app_calls(monkeypatch)

    run_main(["--no-browser"], first_run_env, FakeLaunch())

    assert not moved.exists()
    assert calls[0][0].data_dir_source == "bootstrap"
    assert callable(calls[0][1].get("attach_log_folder"))


def test_cli_first_run_skips_ownership_check(
    platform_root: Path, first_run_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No folder is created at start-up, so there is nothing to check yet (step 1 checks)."""
    monkeypatch.setattr("app.cli.os.geteuid", lambda: -1, raising=False)
    monkeypatch.setattr("app.config.os.geteuid", lambda: -1, raising=False)
    fake = FakeLaunch()

    run_main(["--no-browser"], first_run_env, fake)

    assert len(fake.calls("serve")) == 1


def test_cli_explicit_data_dir_still_created(
    tmp_path: Path,
    platform_root: Path,
    first_run_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.logging_config import LOG_FILE_NAME

    chosen = tmp_path / "chosen"
    calls = capture_create_app_calls(monkeypatch)
    observed: dict[str, object] = {}

    def inspect(_config: Any) -> None:
        observed["file_logs"] = _file_handler_folders()

    run_main(
        ["--no-browser", "--data-dir", str(chosen)], first_run_env, FakeLaunch(on_serve=inspect)
    )

    assert chosen.is_dir()
    assert (chosen / "logs" / LOG_FILE_NAME).is_file()
    if os.name == "posix":
        assert chosen.stat().st_mode & 0o777 == 0o700
    assert observed["file_logs"] == [chosen / "logs"]
    assert calls[0][0].data_dir_source == "cli"
    assert not (platform_root / "data").exists()


def test_cli_existing_database_in_default_folder_logs_to_file_from_start(
    platform_root: Path, first_run_env: dict[str, str]
) -> None:
    from app.logging_config import LOG_FILE_NAME

    default_dir = platform_root / "data" / "jobpipeline"
    default_dir.mkdir(parents=True)
    (default_dir / "app.db").write_bytes(b"")
    observed: dict[str, object] = {}

    def inspect(_config: Any) -> None:
        observed["file_logs"] = _file_handler_folders()

    run_main(["--no-browser"], first_run_env, FakeLaunch(on_serve=inspect))

    assert observed["file_logs"] == [default_dir / "logs"]
    assert (default_dir / "logs" / LOG_FILE_NAME).is_file()


def test_cli_existing_database_in_foreign_default_folder_is_refused(
    platform_root: Path,
    first_run_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    default_dir = platform_root / "data" / "jobpipeline"
    default_dir.mkdir(parents=True)
    (default_dir / "app.db").write_bytes(b"")
    other_user = default_dir.stat().st_uid + 1
    monkeypatch.setattr("app.cli.os.geteuid", lambda: other_user, raising=False)
    fake = FakeLaunch()

    code = exit_code(lambda: run_main(["--no-browser"], first_run_env, fake))

    assert code == 2
    assert "another user" in capsys.readouterr().err
    assert sorted(p.name for p in default_dir.iterdir()) == ["app.db"]
    assert fake.calls("serve") == []


def test_cli_start_failure_without_log_folder_says_details_are_above(
    platform_root: Path,
    first_run_env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_like_uvicorn(_config: Any) -> None:
        logging.getLogger("uvicorn.error").error("Application startup failed. Exiting.")
        raise SystemExit(3)

    code = exit_code(
        lambda: run_main(["--no-browser"], first_run_env, FakeLaunch(on_serve=fail_like_uvicorn))
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "jobpipeline: The app couldn't start. Details are above." in err
    assert "Details are in" not in err
    assert "Application startup failed" in err  # the console kept the details
    assert "Traceback" not in err
    assert not (platform_root / "data").exists()


def test_cli_unexpected_error_without_log_folder_says_details_are_above(
    platform_root: Path,
    first_run_env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_create_app(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("fictional factory failure")

    monkeypatch.setattr("app.cli.create_app", broken_create_app)

    code = exit_code(lambda: run_main(["--no-browser"], first_run_env, FakeLaunch()))

    assert code == 1
    err = capsys.readouterr().err
    assert "jobpipeline: The app couldn't start. Details are above." in err
    assert "Start-up failed" in err
    assert "Traceback" not in err
    assert not (platform_root / "data").exists()


def test_cli_first_run_failure_console_names_exception_type_only(
    platform_root: Path,
    first_run_env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "Details are above" must be true: the console line names the failure's type, and only
    the type, since its text may hold paths or values (M1.5 review)."""

    class FictionalStartupFailure(Exception):
        pass

    def broken_create_app(*_args: object, **_kwargs: object) -> None:
        raise FictionalStartupFailure("fictional detail /fictional/path")

    monkeypatch.setattr("app.cli.create_app", broken_create_app)

    code = exit_code(lambda: run_main(["--no-browser"], first_run_env, FakeLaunch()))

    assert code == 1
    err = capsys.readouterr().err
    assert "Start-up failed (FictionalStartupFailure)" in err
    assert "fictional detail" not in err
    assert "/fictional/path" not in err
    assert "jobpipeline: The app couldn't start. Details are above." in err


def test_cli_passes_its_open_at_startup_decision_to_the_app(
    tmp_path: Path,
    platform_root: Path,
    first_run_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = capture_create_app_calls(monkeypatch)

    run_main(["--no-browser"], first_run_env, FakeLaunch())
    run_main(["--no-browser", "--data-dir", str(tmp_path / "chosen")], first_run_env, FakeLaunch())

    assert [kwargs.get("open_at_startup") for _config, kwargs in calls] == [False, True]


# --- M1.15: wiring built by the CLI (ADR-047, ADR-058) --------------------------------------

SKIP_VERSION_CHECK = "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK"
FICTIONAL_ENV_TOKEN = "FictionalCliEnvToken-0005-notreal"  # noqa: S105 - fictional


def test_cli_sets_sdk_version_check_skip(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SDK's own version check would run the CLI with the full environment; the app
    checks the version itself, so the CLI turns the SDK's check off before the app exists."""
    assert SKIP_VERSION_CHECK not in os.environ  # the autouse fixture removed it
    seen_at_create: list[str | None] = []
    from app.main import create_app as real_create_app

    def wrapper(config: Any, *args: Any, **kwargs: Any) -> Any:
        seen_at_create.append(os.environ.get(SKIP_VERSION_CHECK))
        return real_create_app(config, *args, **kwargs)

    monkeypatch.setattr("app.cli.create_app", wrapper)

    run_main(["--no-browser"], env, FakeLaunch())

    assert seen_at_create == ["1"], "set before the app is created"
    assert os.environ[SKIP_VERSION_CHECK] == "1"


def test_cli_passes_its_redactor_wiring_to_the_app(
    env: dict[str, str], data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.domain.redaction import REDACTED
    from app.logging_config import LOG_FILE_NAME
    from app.wiring import Wiring

    # Fictional and not credential-shaped, so only registration at build time can redact it.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", FICTIONAL_ENV_TOKEN)
    env_with_token = {**env, "CLAUDE_CODE_OAUTH_TOKEN": FICTIONAL_ENV_TOKEN}
    calls = capture_create_app_calls(monkeypatch)
    seen: list[Any] = []

    def log_values(config: Any) -> None:
        state = config.app.state
        seen.append(state)
        logging.getLogger("jobpipeline.test.cli").warning(
            "fictional leak of %s and %s", state.launch_token, FICTIONAL_ENV_TOKEN
        )

    run_main(["--no-browser"], env_with_token, FakeLaunch(on_serve=log_values))

    assert len(calls) == 1
    wiring = calls[0][1].get("wiring")
    assert isinstance(wiring, Wiring), "the CLI builds the wiring and passes it in"
    assert len(seen) == 1 and seen[0].wiring is wiring
    # One redactor: the CLI registered the launch token with the wiring's redactor.
    assert wiring.redactor.redact(seen[0].launch_token) == REDACTED
    text = (data_dir / "logs" / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert "fictional leak of" in text
    assert seen[0].launch_token not in text
    assert FICTIONAL_ENV_TOKEN not in text, "environment secrets reach the log redactor"


def test_cli_demo_builds_demo_wiring(
    tmp_path: Path, platform_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", FICTIONAL_ENV_TOKEN)
    calls = capture_create_app_calls(monkeypatch)
    env = {
        "JOBPIPELINE_CONFIG_DIR": str(tmp_path / "cfg"),
        "CLAUDE_CODE_OAUTH_TOKEN": FICTIONAL_ENV_TOKEN,
    }

    run_main(["--no-browser", "--demo"], env, FakeLaunch())

    wiring = calls[0][1]["wiring"]
    assert wiring.secrets.get("claude_code_oauth_token") is None, "demo ignores the environment"
    assert wiring.redactor.redact(FICTIONAL_ENV_TOKEN) == FICTIONAL_ENV_TOKEN


# --- M2.4: the data-folder lock (ADR-072, ADR-076 item 9) ------------------------------------


def _recording_lock(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> list[Any]:
    """Wrap ``app.cli.lock_data_folder``: record each lock taken and when it is released."""
    from app.modules.storage import lock_data_folder as real_lock

    taken: list[Any] = []

    def wrapper(folder: Path) -> Any:
        lock = real_lock(folder)
        real_release = lock.release

        def release() -> None:
            events.append("lock_released")
            real_release()

        lock.release = release  # type: ignore[method-assign]
        taken.append(lock)
        return lock

    monkeypatch.setattr("app.cli.lock_data_folder", wrapper)
    return taken


def test_cli_data_dir_in_use_prints_one_line_without_path(
    env: dict[str, str],
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.domain.errors import DataFolderInUse

    def in_use(_folder: Path) -> None:
        raise DataFolderInUse()

    monkeypatch.setattr("app.cli.lock_data_folder", in_use)
    fake = FakeLaunch()

    code = exit_code(lambda: run_main(["--no-browser"], env, fake))

    assert code == 1
    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if line.startswith("jobpipeline:")]
    assert lines == [f"jobpipeline: {DataFolderInUse().user_message}"], err
    assert str(data_dir) not in err and data_dir.name not in err
    assert "Traceback" not in err
    assert not (data_dir / "logs").exists(), "a second copy never logs into the folder"
    assert fake.calls("serve") == []


def test_cli_lock_unavailable_prints_create_failed_line(
    env: dict[str, str],
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.domain.errors import DataFolderUnavailable

    def unavailable(_folder: Path) -> None:
        raise DataFolderUnavailable(detail="can't lock (OSError)")

    monkeypatch.setattr("app.cli.lock_data_folder", unavailable)
    fake = FakeLaunch()

    code = exit_code(lambda: run_main(["--no-browser"], env, fake))

    assert code == 1
    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if line.startswith("jobpipeline:")]
    assert len(lines) == 1, err
    assert "Couldn't create the data folder or its logs" in lines[0]
    assert "Traceback" not in err
    assert not (data_dir / "logs").exists()
    assert fake.calls("serve") == []


def test_cli_logging_setup_failure_releases_the_lock(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    taken = _recording_lock(monkeypatch, events)

    def broken_logging(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("fictional logging failure")

    monkeypatch.setattr("app.cli.configure_logging", broken_logging)

    with pytest.raises(RuntimeError):
        run_main(["--no-browser"], env, FakeLaunch())

    assert len(taken) == 1
    assert not taken[0].held, "a failed logging set-up must not keep the folder locked"


def test_cli_lock_held_while_serving_and_released_after_logging_closes(
    env: dict[str, str], data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.logging_config import configure_logging as real_configure

    events: list[str] = []
    taken = _recording_lock(monkeypatch, events)

    def recording_configure(*args: Any, **kwargs: Any) -> Any:
        setup = real_configure(*args, **kwargs)
        real_close = setup.close

        def close() -> None:
            events.append("logging_closed")
            real_close()

        setup.close = close  # type: ignore[method-assign]
        return setup

    monkeypatch.setattr("app.cli.configure_logging", recording_configure)
    calls = capture_create_app_calls(monkeypatch)
    held_while_serving: list[bool] = []
    fake = FakeLaunch(on_serve=lambda _c: held_while_serving.append(taken[0].held))

    run_main(["--no-browser"], env, fake)

    assert held_while_serving == [True]
    assert calls[0][1].get("data_folder_lock") is taken[0], "the app reuses the CLI's lock"
    assert events[0] == "logging_closed", events
    assert "lock_released" in events
    assert not taken[0].held
