"""Claude provider with a silent CLI subprocess: cancel and the overall timeout leave no
process behind (M2.13; AC-M2.4; ADR-057 follow-ups).

The provider keeps the SDK's real ``ClaudeSDKClient`` and subprocess transport; the CLI is
``tests/fakes/fake_claude_cli.py`` behind an executable wrapper in ``tmp_path``, run in a
silent scenario: it answers the control protocol, then sends nothing after the prompt.
``silent`` exits when its stdin closes; ``silent_ignore_eof`` ignores that and only a
terminate or kill stops it. The fake writes its process id to a file, so the test checks
the process is gone. Offline, fictional data only.

POSIX only (ADR-061; the SDK launches only native ``.exe`` CLIs on Windows).

Assumed API: ``ClaudeSdkProvider(..., run_timeout: timedelta | None = None)``.
"""

import asyncio
import os
import sys
from datetime import timedelta
from pathlib import Path

import anyio
import pytest

from app.domain.errors import NetworkUnavailable
from app.modules.ai.providers.claude_sdk import ClaudeSdkProvider
from app.modules.secrets import CLAUDE_CODE_OAUTH_TOKEN
from app.modules.secrets.providers.memory import MemorySecretStore
from tests.fakes.ai import NotCancelled, make_request

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX only (ADR-061)"),
]

FAKE_CLI = Path(__file__).resolve().parents[2] / "fakes" / "fake_claude_cli.py"
FAKE_TOKEN = "fake-" + "token-" + "u" * 10
MODEL = "haiku"
# The SDK's close waits 5 s for the CLI to exit after stdin closes, then terminates it (and
# kills it 5 s later), so these bounds leave room for that escalation.
CALL_BOUND_SECONDS = 30.0
# Long enough for the fake CLI to start and receive the prompt, short enough to be quick.
RUN_TIMEOUT = timedelta(seconds=3)


@pytest.fixture
def fake_cli(tmp_path: Path) -> Path:
    body = f"import runpy\nrunpy.run_path({str(FAKE_CLI)!r}, run_name='__main__')\n"
    path = tmp_path / "bin" / "claude"
    path.parent.mkdir(parents=True)
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def pid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "fake-cli.pid"
    # The child inherits the process environment; JOBPIPELINE_* is not blanked.
    monkeypatch.setenv("JOBPIPELINE_FAKE_CLI_PID", str(path))
    monkeypatch.delenv("JOBPIPELINE_FAKE_CLI_REPORT", raising=False)
    monkeypatch.delenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", raising=False)
    return path


@pytest.fixture
def temp_root(tmp_path: Path) -> Path:
    folder = tmp_path / "temp-root"
    folder.mkdir()
    return folder


def _scenario(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv("JOBPIPELINE_FAKE_CLI_SCENARIO", name)


def _provider(fake_cli: Path, temp_root: Path, run_timeout: timedelta) -> ClaudeSdkProvider:
    secrets = MemorySecretStore()
    secrets.set(CLAUDE_CODE_OAUTH_TOKEN, FAKE_TOKEN)
    # Default client factory: the SDK's real ClaudeSDKClient and subprocess transport.
    return ClaudeSdkProvider(
        secrets=secrets,
        mode="subscription_token",
        cli_path=lambda: fake_cli,
        temp_root=temp_root,
        run_timeout=run_timeout,
    )


def _alive(pid: int) -> bool:
    """Whether ``pid`` is still a process (a reaped child is gone; a zombie still counts)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _pid_once_prompt_received(pid_file: Path) -> int:
    """The fake CLI's pid, waiting (bounded) until it has received the prompt."""
    with anyio.fail_after(CALL_BOUND_SECONDS):
        while not (pid_file.is_file() and pid_file.read_text(encoding="utf-8").strip()):
            await asyncio.sleep(0.01)
    return int(pid_file.read_text(encoding="utf-8"))


def _kill_if_left(pid: int | None) -> None:
    if pid is not None and _alive(pid):
        os.kill(pid, 9)  # never leave a stray fake behind, even when the test fails


@pytest.mark.parametrize("scenario", ["silent", "silent_ignore_eof"])
async def test_cancel_while_fake_cli_silent_leaves_no_subprocess(
    scenario: str,
    fake_cli: Path,
    pid_file: Path,
    temp_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _scenario(monkeypatch, scenario)
    # A long overall timeout, so only the cancel can end the call.
    provider = _provider(fake_cli, temp_root, timedelta(hours=1))
    call = asyncio.create_task(provider.run(make_request(model=MODEL), NotCancelled()))
    pid: int | None = None
    try:
        pid = await _pid_once_prompt_received(pid_file)
        assert _alive(pid)
        assert not call.done(), "the silent CLI must keep the call waiting"

        call.cancel()
        with anyio.fail_after(CALL_BOUND_SECONDS), pytest.raises(asyncio.CancelledError):
            await call

        # Closed before run() ended: the CLI is already gone (and reaped), not left for a
        # finaliser or the exit reaper.
        assert not _alive(pid), "the CLI subprocess outlived the cancelled call"
        assert list(temp_root.iterdir()) == []
    finally:
        if not call.done():
            call.cancel()
        _kill_if_left(pid)


async def test_run_timeout_kills_fake_cli(
    fake_cli: Path, pid_file: Path, temp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ignores stdin closing: only a terminate or kill stops it.
    _scenario(monkeypatch, "silent_ignore_eof")
    provider = _provider(fake_cli, temp_root, RUN_TIMEOUT)
    pid: int | None = None
    try:
        with anyio.fail_after(CALL_BOUND_SECONDS), pytest.raises(NetworkUnavailable) as raised:
            await provider.run(make_request(model=MODEL), NotCancelled())

        assert pid_file.is_file(), "the fake CLI never received the prompt"
        pid = int(pid_file.read_text(encoding="utf-8"))
        assert not _alive(pid), "the CLI subprocess outlived the timed-out call"
        assert raised.value.__cause__ is None
        assert FAKE_TOKEN not in str(raised.value)
        assert list(temp_root.iterdir()) == []
    finally:
        _kill_if_left(pid)
