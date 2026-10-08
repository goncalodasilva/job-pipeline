"""Recorded Claude CLI frames parse through the real SDK offline (M1.1, spec 4.2).

``ReplayTransport`` replaces the CLI subprocess, so ``claude_agent_sdk.query``
runs its real control protocol and message parser with no process and no
network. With a custom transport, API errors don't raise: they arrive as
``AssistantMessage.error`` plus a ``ResultMessage`` with ``is_error=True`` and
``api_error_status``. All frames are fictional.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, query

from app.modules.ai.providers.claude_sdk import build_options
from tests.fakes.claude_transport import ReplayTransport, load_frames

PROMPT = "fictional prompt"
TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class RecordedCase:
    assistant_error: str | None
    is_error: bool
    api_error_status: int | None


CASES = {
    "success": RecordedCase(assistant_error=None, is_error=False, api_error_status=None),
    "auth_failed": RecordedCase(
        assistant_error="authentication_failed", is_error=True, api_error_status=401
    ),
    "rate_limit": RecordedCase(assistant_error="rate_limit", is_error=True, api_error_status=429),
}


async def _run(transport: ReplayTransport, workdir: Path) -> list[Any]:
    # The app's real options go through the SDK's initialize handshake too.
    options = build_options(
        mode="existing_login",
        model="claude-haiku-fictional",
        system="Fictional system prompt.",
        output_schema={"type": "object"},
        workdir=workdir,
        credential=None,
        environ={},
    )
    messages: list[Any] = []
    with anyio.fail_after(TIMEOUT_SECONDS):
        async for message in query(prompt=PROMPT, options=options, transport=transport):
            messages.append(message)
    return messages


@pytest.mark.parametrize("name", sorted(CASES))
async def test_sdk_recorded_messages_parse(name: str, tmp_path: Path) -> None:
    case = CASES[name]
    transport = ReplayTransport(load_frames(name))

    messages = await _run(transport, tmp_path)

    assistants = [message for message in messages if isinstance(message, AssistantMessage)]
    assert len(assistants) == 1, messages
    assert assistants[0].error == case.assistant_error

    assert isinstance(messages[-1], ResultMessage), messages
    result = messages[-1]
    assert result.is_error is case.is_error
    assert result.api_error_status == case.api_error_status

    if name == "success":
        assert assistants[0].model == "claude-haiku-fictional"
        assert result.subtype == "success"
        assert result.structured_output == {"answer": "ok"}
        assert result.usage is not None
        assert result.usage["input_tokens"] == 12
        assert result.usage["output_tokens"] == 3
    else:
        assert result.terminal_reason == "api_error"

    # The SDK spoke the control protocol to the fake: one initialize, one prompt.
    assert len(transport.control_requests("initialize")) == 1
    user_frames = transport.user_frames()
    assert len(user_frames) == 1
    assert user_frames[0]["message"]["content"] == PROMPT
