"""Claude provider through the SDK's real subprocess transport, offline (M1.14; AC-M1.3;
spec 2.9.7-10; ADR-045, ADR-057).

The provider keeps its default client factory (the SDK's ``ClaudeSDKClient``), so the SDK
builds the real command line and environment and starts a process. The process is
``tests/fakes/fake_claude_cli.py`` behind an executable wrapper written to ``tmp_path``
(``#!<python>`` shebang, mode 755); it speaks stream-json, never touches the network and
writes a JSON report of what it received. Credentials and shell values are fictional and
built at runtime.

Skipped on Windows only (user approved): the SDK launches only native ``.exe`` CLIs there,
so a script wrapper can't stand in; the live check (M1.25) covers Windows.
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import anyio
import pytest

from app.domain.errors import ProviderAuthError
from app.modules.ai import ConnectionResult
from app.modules.ai.providers import claude_sdk
from app.modules.ai.providers.claude_sdk import CLAUDE_FAST_DEFAULT, AuthMode, ClaudeSdkProvider
from app.modules.secrets import ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN
from app.modules.secrets.providers.memory import MemorySecretStore
from tests.fakes.ai import NotCancelled, make_request

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="the SDK launches only native .exe CLIs on Windows; covered by the live check "
        "(M1.25)",
    ),
]

FAKE_CLI = Path(__file__).resolve().parents[1] / "fakes" / "fake_claude_cli.py"
FAKE_CLI_MODEL = "claude-haiku-fictional-cli"
FAKE_CLI_VERSION = "2.1.286 (Claude Code)"
FAKE_TOKEN = "fake-" + "token-" + "x" * 10
FAKE_API_KEY = "fake-" + "key-" + "y" * 10
SHELL_API_KEY = "fake-" + "shell-key-" + "z" * 10
SHELL_BASE_URL = "http://127.0.0.1:9/fictional-gateway"
INHERITED_CONFIG_DIR = "/fictional/home/.claude-own"
CALL_BOUND_SECONDS = 60

SECRET_KEY = {"subscription_token": CLAUDE_CODE_OAUTH_TOKEN, "api_key": ANTHROPIC_API_KEY}
CREDENTIAL = {"subscription_token": FAKE_TOKEN, "api_key": FAKE_API_KEY}


def _executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def fake_cli(tmp_path: Path) -> Path:
    body = f"import runpy\nrunpy.run_path({str(FAKE_CLI)!r}, run_name='__main__')\n"
    return _executable(tmp_path / "bin" / "claude", body)


@pytest.fixture
def report_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "fake-cli-report.json"
    # The child inherits the process environment; JOBPIPELINE_* is not blanked.
    monkeypatch.setenv("JOBPIPELINE_FAKE_CLI_REPORT", str(path))
    monkeypatch.delenv("JOBPIPELINE_FAKE_CLI_SCENARIO", raising=False)
    monkeypatch.delenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", raising=False)
    return path


@pytest.fixture
def temp_root(tmp_path: Path) -> Path:
    folder = tmp_path / "temp-root"
    folder.mkdir()
    return folder


def _provider(fake_cli: Path, temp_root: Path, mode: AuthMode = "subscription_token") -> Any:
    secrets = MemorySecretStore()
    if mode != "existing_login":
        secrets.set(SECRET_KEY[mode], CREDENTIAL[mode])
    # Default client factory: the SDK's real ClaudeSDKClient and subprocess transport.
    # environ=None: the process environment, which the child inherits too.
    return ClaudeSdkProvider(
        secrets=secrets, mode=mode, cli_path=lambda: fake_cli, temp_root=temp_root
    )


def _read_report(path: Path) -> dict[str, Any]:
    assert path.is_file(), "the fake CLI never received a prompt"
    report: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return report


async def test_provider_runs_fake_cli_subprocess_offline(
    fake_cli: Path, report_path: Path, temp_root: Path
) -> None:
    provider = _provider(fake_cli, temp_root)

    with anyio.fail_after(CALL_BOUND_SECONDS):
        response = await provider.run(make_request(model=CLAUDE_FAST_DEFAULT), NotCancelled())
    assert dict(response.data) == {"ok": True}
    assert response.model == FAKE_CLI_MODEL
    assert list(temp_root.iterdir()) == []

    report_path.unlink()
    with anyio.fail_after(CALL_BOUND_SECONDS):
        result = await provider.test_connection(CLAUDE_FAST_DEFAULT)
    assert result == ConnectionResult(ok=True, message="Connected", model=FAKE_CLI_MODEL)
    assert _read_report(report_path)["prompt"]  # the tiny request reached the CLI
    assert list(temp_root.iterdir()) == []


@pytest.mark.parametrize("mode", ["subscription_token", "api_key"])
async def test_fake_cli_receives_isolated_env_and_empty_cwd(
    mode: AuthMode,
    fake_cli: Path,
    report_path: Path,
    temp_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A hostile shell: a key, a gateway and the user's own config folder.
    monkeypatch.setenv("ANTHROPIC_API_KEY", SHELL_API_KEY)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", SHELL_API_KEY)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", SHELL_BASE_URL)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", INHERITED_CONFIG_DIR)
    # An empty value would select the user's own keychain login (M1.14 review).
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "")
    request = make_request(model=CLAUDE_FAST_DEFAULT)

    with anyio.fail_after(CALL_BOUND_SECONDS):
        await _provider(fake_cli, temp_root, mode).run(request, NotCancelled())
    report = _read_report(report_path)

    env = report["env"]
    credential_variable = (
        "CLAUDE_CODE_OAUTH_TOKEN" if mode == "subscription_token" else "ANTHROPIC_API_KEY"
    )
    other_variable = (
        "ANTHROPIC_API_KEY" if mode == "subscription_token" else "CLAUDE_CODE_OAUTH_TOKEN"
    )
    # The credential only in its own variable; the shell's values never reach the CLI.
    assert env[credential_variable] == CREDENTIAL[mode]
    assert env[other_variable] in ("", None)
    assert env["ANTHROPIC_BASE_URL"] in ("", None)
    assert SHELL_API_KEY not in json.dumps(report)
    assert SHELL_BASE_URL not in json.dumps(report)
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"

    # An empty working folder and a private empty config folder, both in the call folder.
    cwd = Path(report["cwd"]).resolve()
    assert report["cwd_listing"] == []
    call_folder = cwd.parent
    assert call_folder.parent == temp_root.resolve()
    assert env["CLAUDE_CONFIG_DIR"] not in (None, "", INHERITED_CONFIG_DIR)
    config = Path(env["CLAUDE_CONFIG_DIR"]).resolve()
    assert config.parent == call_folder
    assert config != cwd
    assert report["config_dir_listing"] == []
    # The keychain item is chosen from the same private folder.
    assert env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == env["CLAUDE_CONFIG_DIR"]

    # Isolation flags on the real command line; the system prompt only in a file.
    argv: list[str] = report["argv"]
    assert "--setting-sources=" in argv
    tools_at = argv.index("--tools")
    assert argv[tools_at + 1] == ""
    assert "--strict-mcp-config" in argv
    assert "--no-session-persistence" in argv
    assert argv[argv.index("--model") + 1] == CLAUDE_FAST_DEFAULT
    assert "--system-prompt-file" in argv
    assert "--system-prompt" not in argv
    assert all(request.system not in value for value in argv)
    assert report["system_prompt_text"] == request.system
    assert Path(report["system_prompt_file"]).resolve().parent == call_folder
    for value in (FAKE_TOKEN, FAKE_API_KEY):
        assert all(value not in item for item in argv)
    assert report["prompt"] == request.user  # the user prompt went over stdin

    assert not call_folder.exists()
    assert list(temp_root.iterdir()) == []


async def test_fake_cli_existing_login_keeps_the_users_config_dir(
    fake_cli: Path, report_path: Path, temp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", SHELL_API_KEY)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", INHERITED_CONFIG_DIR)

    with anyio.fail_after(CALL_BOUND_SECONDS):
        result = await _provider(fake_cli, temp_root, "existing_login").test_connection(
            CLAUDE_FAST_DEFAULT
        )
    report = _read_report(report_path)

    assert result.ok is True
    assert report["env"]["CLAUDE_CONFIG_DIR"] == INHERITED_CONFIG_DIR
    assert report["env"]["CLAUDE_SECURESTORAGE_CONFIG_DIR"] is None  # not set by the app
    assert report["env"]["ANTHROPIC_API_KEY"] in ("", None)
    assert report["env"]["CLAUDE_CODE_OAUTH_TOKEN"] in ("", None)


async def test_fake_cli_auth_failure_maps_to_message(
    fake_cli: Path, report_path: Path, temp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOBPIPELINE_FAKE_CLI_SCENARIO", "auth_failed")
    provider = _provider(fake_cli, temp_root)

    with anyio.fail_after(CALL_BOUND_SECONDS):
        result = await provider.test_connection(CLAUDE_FAST_DEFAULT)
    assert result == ConnectionResult(
        ok=False, message=ProviderAuthError().user_message, model=None, failure="auth"
    )
    assert list(temp_root.iterdir()) == []

    with anyio.fail_after(CALL_BOUND_SECONDS), pytest.raises(ProviderAuthError) as raised:
        await provider.run(make_request(model=CLAUDE_FAST_DEFAULT), NotCancelled())
    assert "Fictional" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert list(temp_root.iterdir()) == []


# --- the version probe used for Windows' native claude.exe ------------------------------------


def test_cli_version_probe_reads_the_fake_cli_version(fake_cli: Path, report_path: Path) -> None:
    output = claude_sdk.probe_cli_version(fake_cli)

    assert output is not None
    assert FAKE_CLI_VERSION in output


def test_cli_version_probe_runs_with_a_minimal_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names_file = tmp_path / "probe-env.json"
    body = (
        "import json, os\n"
        f"open({str(names_file)!r}, 'w').write(json.dumps(dict(os.environ)))\n"
        "print('2.1.300 (Claude Code)')\n"
    )
    probe = _executable(tmp_path / "probe-bin" / "claude", body)
    monkeypatch.setenv("ANTHROPIC_API_KEY", SHELL_API_KEY)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("JOBPIPELINE_FICTIONAL_SECRET", FAKE_API_KEY)
    monkeypatch.setenv("NODE_OPTIONS", "--require /fictional/inject.js")

    output = claude_sdk.probe_cli_version(probe)

    assert output is not None and "2.1.300" in output
    seen = json.loads(names_file.read_text(encoding="utf-8"))
    for name in (
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "JOBPIPELINE_FICTIONAL_SECRET",
        "NODE_OPTIONS",
    ):
        assert name not in seen, name
    for value in (SHELL_API_KEY, FAKE_TOKEN, FAKE_API_KEY):
        assert value not in json.dumps(seen)


def test_cli_version_probe_times_out(tmp_path: Path) -> None:
    # The child would wait a minute; the probe gives up at its timeout and returns None.
    probe = _executable(tmp_path / "slow-bin" / "claude", "import time\ntime.sleep(60)\n")
    started = time.monotonic()

    output = claude_sdk.probe_cli_version(probe, timeout=0.5)

    assert output is None
    assert time.monotonic() - started < 15
    assert os.path.exists(probe)  # nothing removed; the child was stopped, not the file


def test_cli_version_probe_output_is_bounded(tmp_path: Path) -> None:
    # A child that floods stdout: the probe reads at most PROBE_READ_LIMIT bytes, stops the
    # child and never returns more than one short line.
    body = (
        "import sys\n"
        "sys.stdout.write('2.1.300 ' + 'x' * 3000 + '\\n')\n"
        "while True:\n"
        "    sys.stdout.write('y' * 65536)\n"
    )
    probe = _executable(tmp_path / "flood-bin" / "claude", body)
    started = time.monotonic()

    output = claude_sdk.probe_cli_version(probe, timeout=2.0)

    assert output is None or len(output) <= claude_sdk.PROBE_READ_LIMIT
    assert time.monotonic() - started < 15


def test_cli_version_probe_bounds_a_long_first_line(tmp_path: Path) -> None:
    body = "import sys\nsys.stdout.write('2.1.300 ' + 'x' * 3000 + '\\n')\n"
    probe = _executable(tmp_path / "long-bin" / "claude", body)

    output = claude_sdk.probe_cli_version(probe)

    assert output is not None and output.startswith("2.1.300")
    assert len(output) <= 200
