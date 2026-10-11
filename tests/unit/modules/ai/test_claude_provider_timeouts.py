"""Claude provider: cancel while the CLI is silent, and the overall run timeout (M2.13;
AC-M2.4; ADR-045, ADR-057 follow-ups).

The provider gets ``hanging()`` from ``tests/fakes/claude_query.py``: the SDK's real
``ClaudeSDKClient`` over a transport that answers the control protocol and then never sends
a frame after the prompt, like a CLI that went quiet. Nothing starts a process or touches
the network. Every credential is fictional and built at runtime.

Assumed API: ``ClaudeSdkProvider(..., run_timeout: timedelta | None = None)``; ``run`` gives
up after ``run_timeout`` with ``NetworkUnavailable`` (detail: a type only) and closes the
client; wiring passes ``app.wiring.PROVIDER_RUN_TIMEOUT``. Cancelling the asyncio task that
awaits ``run`` raises ``asyncio.CancelledError`` in it (the runner cancels handler tasks,
M2.11) after the client is closed.
"""

import asyncio
import inspect
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest

from app.domain.errors import NetworkUnavailable
from app.domain.redaction import Redactor
from app.domain.settings import AIProviderSettings, ProviderKind
from app.modules.ai import connection_failure
from app.modules.ai.providers.claude_sdk import ClaudeSdkProvider
from app.modules.secrets import CLAUDE_CODE_OAUTH_TOKEN
from app.modules.secrets.providers.memory import MemorySecretStore
from app.wiring import PROVIDER_RUN_TIMEOUT, build_wiring
from tests.fakes.ai import NotCancelled, make_request
from tests.fakes.claude_query import HangingQuery, hanging
from tests.fakes.keyring_backends import CountingKeyring

FAKE_TOKEN = "fake-" + "token-" + "t" * 10
MODEL = "haiku"
# Bounds every wait in this file, so a missing cancel or timeout fails instead of hanging.
TEST_BOUND_SECONDS = 10.0
# How long a cancelled or timed-out call may take to close the client and return.
PROMPT_CLOSE_SECONDS = 2.0
TINY_TIMEOUT = timedelta(milliseconds=200)
TYPE_ONLY_DETAIL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,60}$")


@pytest.fixture
def temp_root(tmp_path: Path) -> Path:
    folder = tmp_path / "temp-root"
    folder.mkdir()
    return folder


@pytest.fixture
def cli(tmp_path: Path) -> Path:
    # Never run: the fake client factory ignores it.
    return tmp_path / "fictional-cli" / "claude"


def _provider(query: HangingQuery, temp_root: Path, cli: Path, **extra: Any) -> ClaudeSdkProvider:
    secrets = MemorySecretStore()
    secrets.set(CLAUDE_CODE_OAUTH_TOKEN, FAKE_TOKEN)
    return ClaudeSdkProvider(
        secrets=secrets,
        mode="subscription_token",
        client_factory=query,
        cli_path=lambda: cli,
        environ={},
        temp_root=temp_root,
        **extra,
    )


async def _until_prompt_sent(query: HangingQuery) -> None:
    """Wait (bounded) until the silent transport has received the user prompt."""
    with anyio.fail_after(TEST_BOUND_SECONDS):
        while not (query.calls and query.last.transport.user_frames()):
            await asyncio.sleep(0)


async def test_cancel_while_cli_silent_closes_client_promptly(temp_root: Path, cli: Path) -> None:
    query = hanging()
    # A long overall timeout, so only the cancel can end the call.
    provider = _provider(query, temp_root, cli, run_timeout=timedelta(hours=1))
    call = asyncio.create_task(provider.run(make_request(model=MODEL), NotCancelled()))
    await _until_prompt_sent(query)
    assert not call.done(), "the silent CLI must keep the call waiting"

    call.cancel()
    started = asyncio.get_running_loop().time()
    with anyio.fail_after(TEST_BOUND_SECONDS), pytest.raises(asyncio.CancelledError):
        await call
    elapsed = asyncio.get_running_loop().time() - started

    assert call.cancelled()
    assert elapsed < PROMPT_CLOSE_SECONDS
    transport = query.last.transport
    assert transport.closed is True, "the client (the CLI subprocess) must be closed"
    assert transport.close_count == 1
    # Closed by the cancelled call itself, while its working folder still existed.
    assert transport.close_task is not None
    assert transport.workdir_existed_at_close is True
    assert list(temp_root.iterdir()) == []


async def test_run_overall_timeout_maps_to_network_unavailable_and_closes(
    temp_root: Path, cli: Path
) -> None:
    query = hanging()
    provider = _provider(query, temp_root, cli, run_timeout=TINY_TIMEOUT)

    started = asyncio.get_running_loop().time()
    with anyio.fail_after(TEST_BOUND_SECONDS), pytest.raises(NetworkUnavailable) as raised:
        await provider.run(make_request(model=MODEL), NotCancelled())
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < TINY_TIMEOUT.total_seconds() + PROMPT_CLOSE_SECONDS
    error = raised.value
    detail = str(error)  # AppError keeps its detail as the exception text
    assert TYPE_ONLY_DETAIL.fullmatch(detail), detail
    assert FAKE_TOKEN not in detail
    assert error.__cause__ is None
    assert error.__context__ is None
    transport = query.last.transport
    assert transport.user_frames(), "the prompt was sent before the CLI went quiet"
    assert transport.closed is True, "the client (the CLI subprocess) must be closed"
    assert transport.close_count == 1
    assert transport.close_task == anyio.get_current_task().id
    assert transport.workdir_existed_at_close is True
    assert list(temp_root.iterdir()) == []


def test_run_timeout_is_a_constructor_keyword() -> None:
    parameter = inspect.signature(ClaudeSdkProvider).parameters.get("run_timeout")

    assert parameter is not None, "ClaudeSdkProvider takes run_timeout"
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.parametrize(
    "kind", ["claude_subscription_token", "claude_existing_login", "claude_api_key"]
)
def test_run_timeout_from_wiring(
    kind: ProviderKind, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[dict[str, Any]] = []
    original = ClaudeSdkProvider.__init__

    def recording(self: ClaudeSdkProvider, **kwargs: Any) -> None:
        built.append(kwargs)
        original(self, **kwargs)

    monkeypatch.setattr(ClaudeSdkProvider, "__init__", recording)
    wiring = build_wiring(
        environ={}, demo=False, redactor=Redactor(), keyring_backend=CountingKeyring()
    )

    wiring.ai_provider(AIProviderSettings(kind=kind), data_dir=tmp_path / "data")

    assert len(built) == 1
    assert built[0].get("run_timeout") == PROVIDER_RUN_TIMEOUT
    assert timedelta(minutes=5) == PROVIDER_RUN_TIMEOUT


SHORT_SECONDS = 0.2


async def _timed_connection_test(provider: ClaudeSdkProvider) -> tuple[Any, float]:
    started = asyncio.get_running_loop().time()
    with anyio.fail_after(TEST_BOUND_SECONDS):
        result = await provider.test_connection(MODEL)
    return result, asyncio.get_running_loop().time() - started


async def _assert_timed_out(query: HangingQuery, result: Any, temp_root: Path) -> None:
    expected = connection_failure(NetworkUnavailable(detail="timeout"))
    assert result.ok is False
    assert result.failure == expected
    assert result.model is None
    transport = query.last.transport
    assert transport.user_frames(), "the prompt was sent before the CLI went quiet"
    assert transport.close_count == 1
    assert list(temp_root.iterdir()) == []


async def test_connection_test_ends_at_run_timeout_when_shorter(temp_root: Path, cli: Path) -> None:
    query = hanging()
    # The connection timeout alone would outlast the test bound.
    provider = _provider(
        query,
        temp_root,
        cli,
        run_timeout=timedelta(seconds=SHORT_SECONDS),
        connection_timeout=3600.0,
    )

    result, elapsed = await _timed_connection_test(provider)

    assert elapsed < SHORT_SECONDS + PROMPT_CLOSE_SECONDS
    await _assert_timed_out(query, result, temp_root)


async def test_connection_test_ends_at_connection_timeout_when_shorter(
    temp_root: Path, cli: Path
) -> None:
    query = hanging()
    # The run timeout alone would outlast the test bound.
    provider = _provider(
        query,
        temp_root,
        cli,
        run_timeout=timedelta(hours=1),
        connection_timeout=SHORT_SECONDS,
    )

    result, elapsed = await _timed_connection_test(provider)

    assert elapsed < SHORT_SECONDS + PROMPT_CLOSE_SECONDS
    await _assert_timed_out(query, result, temp_root)
