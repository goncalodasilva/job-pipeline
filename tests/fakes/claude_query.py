"""A fake SDK client factory for the Claude provider's offline tests (M1.13, spec 4.2).

``ClaudeSdkProvider`` takes its client factory by injection. ``replaying(frames)`` returns
a stand-in that builds the SDK's real ``ClaudeSDKClient`` over a ``RecordingTransport``
(a ``ReplayTransport`` that streams lazily), so the control protocol, the message reader
and the parser all stay real and no process or network is involved. It:

- records every call: the options the provider built, and a snapshot taken when the client
  is built of whether ``options.cwd`` existed and was empty, of ``options.env``, of the
  folder named by ``CLAUDE_CONFIG_DIR`` in ``options.env`` (existed, empty) and of the
  system prompt file (text and permission bits); the prompt is read from the user frame the
  SDK wrote;
- optionally writes ``stderr_lines`` through the provider's ``options.stderr`` callback
  when the client connects, the way the CLI subprocess would;
- replays ``frames`` one at a time after the user message, calling ``on_message`` with each
  frame before the SDK reads it;
- then raises ``raise_exc`` from the transport's reader if given (the SDK's reader turns it
  into a stream error, as with the subprocess transport); a ``CLINotFoundError`` is raised
  from ``connect`` instead, where the subprocess transport raises it;
- records whether ``close()`` ran, in which task, and whether the working folder still
  existed then.

``hanging()`` returns a factory whose transport answers the control protocol but never
sends a message after the user frame, like a CLI that stays silent (M1.14 timeouts).
``unresponsive()`` returns one whose transport never answers the ``initialize`` control
request, so ``connect()`` itself waits (M1.14 review).

Options are checked through the record, since a custom transport ignores CLI flags
(ADR-045). All content is fictional.
"""

import json
import os
import stat
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import anyio
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, CLINotFoundError

from tests.fakes.claude_transport import ReplayTransport

DEFAULT_MODEL = "claude-haiku-fictional"
_REPLAY: dict[str, Any] = {"type": "__fictional_replay__"}


class RecordingTransport(ReplayTransport):
    """``ReplayTransport`` that streams frames lazily and records how it was closed."""

    def __init__(
        self,
        frames: Sequence[Mapping[str, Any]],
        *,
        workdir: Path | None,
        raise_exc: BaseException | None = None,
        on_message: Callable[[Any], None] | None = None,
        on_connect: Callable[[], None] | None = None,
    ) -> None:
        super().__init__([dict(frame) for frame in frames])
        self._workdir = workdir
        self._raise_exc = raise_exc
        self._on_message = on_message
        self._on_connect = on_connect
        self._user_seen = False
        self._hook_ids: dict[str, str] = {}
        self._hook_replies: dict[str, anyio.Event] = {}
        self.frames_read = 0
        self.closed = False
        self.close_count = 0
        self.close_task: int | None = None
        self.workdir_existed_at_close: bool | None = None

    async def connect(self) -> None:
        if self._on_connect is not None:
            self._on_connect()
        if isinstance(self._raise_exc, CLINotFoundError):
            raise self._raise_exc
        await super().connect()

    async def write(self, data: str) -> None:
        for line in data.splitlines():
            if not line.strip():
                continue
            frame = json.loads(line)
            if frame.get("type") == "user":
                self.written.append(frame)
                if not self._user_seen:
                    self._user_seen = True
                    await self._send.send(_REPLAY)
            else:
                if (
                    frame.get("type") == "control_request"
                    and frame["request"].get("subtype") == "initialize"
                ):
                    for event, matchers in (frame["request"].get("hooks") or {}).items():
                        self._hook_ids[event] = matchers[0]["hookCallbackIds"][0]
                await super().write(line + "\n")
                if frame.get("type") == "control_response":
                    reply = self._hook_replies.get(frame.get("response", {}).get("request_id"))
                    if reply is not None:
                        reply.set()

    async def _messages(self) -> AsyncIterator[dict[str, Any]]:
        async for frame in self._receive:
            if frame is None:
                return
            if frame is not _REPLAY:
                yield frame
                continue
            for recorded in self._frames:
                if self._on_message is not None:
                    self._on_message(recorded)
                self.frames_read += 1
                if "__hook_event__" in recorded:
                    event = recorded["__hook_event__"]
                    callback_id = self._hook_ids.get(event)
                    if callback_id is None:
                        raise AssertionError("The replay expected a registered SDK hook.")
                    request_id = f"fictional-hook-{self.frames_read}"
                    reply = anyio.Event()
                    self._hook_replies[request_id] = reply
                    yield {
                        "type": "control_request",
                        "request_id": request_id,
                        "request": {
                            "subtype": "hook_callback",
                            "callback_id": callback_id,
                            "tool_use_id": "fictional-search",
                            "input": {
                                "hook_event_name": event,
                                "tool_use_id": "fictional-search",
                                "tool_name": recorded["tool_name"],
                                "tool_input": {},
                                "tool_response": recorded.get("tool_response"),
                            },
                        },
                    }
                    with anyio.fail_after(2):
                        await reply.wait()
                else:
                    yield dict(recorded)
                await anyio.sleep(0)  # let the consumer run between frames
            if self._raise_exc is not None:
                raise self._raise_exc
            return

    async def close(self) -> None:
        self.close_count += 1
        if not self.closed:
            self.closed = True
            self.close_task = anyio.get_current_task().id
            self.workdir_existed_at_close = self._workdir is not None and self._workdir.is_dir()
        await super().close()
        self._receive.close()


class QueryCall:
    """One client the provider asked for, with what it carried at that moment."""

    def __init__(self, options: ClaudeAgentOptions, transport: RecordingTransport) -> None:
        self.options = options
        self.transport = transport
        self.caller_task = anyio.get_current_task().id
        self.cwd = None if options.cwd is None else Path(options.cwd)
        self.cwd_existed = self.cwd is not None and self.cwd.is_dir()
        self.cwd_was_empty = (
            self.cwd_existed and self.cwd is not None and not any(self.cwd.iterdir())
        )
        self.env: Mapping[str, str] = dict(options.env)
        config = self.env.get("CLAUDE_CONFIG_DIR")
        self.config_dir = Path(config) if config else None
        self.config_dir_existed = self.config_dir is not None and self.config_dir.is_dir()
        self.config_dir_was_empty = (
            self.config_dir_existed
            and self.config_dir is not None
            and not any(self.config_dir.iterdir())
        )
        self.system_prompt_path: Path | None = None
        self.system_prompt_text: str | None = None
        self.system_prompt_mode: int | None = None
        system = options.system_prompt
        if isinstance(system, dict) and system.get("type") == "file":
            path = Path(str(system.get("path")))
            self.system_prompt_path = path
            if path.is_file():
                self.system_prompt_text = path.read_text(encoding="utf-8")
                self.system_prompt_mode = stat.S_IMODE(os.stat(path).st_mode)

    @property
    def prompt(self) -> str | None:
        frames = self.transport.user_frames()
        if not frames:
            return None
        content = frames[0].get("message", {}).get("content")
        return content if isinstance(content, str) else None


class ReplayingQuery:
    def __init__(
        self,
        frames: Sequence[Mapping[str, Any]],
        *,
        raise_exc: BaseException | None = None,
        stderr_lines: Sequence[str] = (),
        on_message: Callable[[Any], None] | None = None,
    ) -> None:
        self._frames = [dict(frame) for frame in frames]
        self._raise_exc = raise_exc
        self._stderr_lines = list(stderr_lines)
        self._on_message = on_message
        self.calls: list[QueryCall] = []

    def __call__(self, options: ClaudeAgentOptions) -> ClaudeSDKClient:
        cwd = None if options.cwd is None else Path(options.cwd)

        def write_stderr() -> None:
            if options.stderr is not None:
                for line in self._stderr_lines:
                    options.stderr(line)

        transport = RecordingTransport(
            self._frames,
            workdir=cwd,
            raise_exc=self._raise_exc,
            on_message=self._on_message,
            on_connect=write_stderr,
        )
        self.calls.append(QueryCall(options, transport))
        return ClaudeSDKClient(options=options, transport=transport)

    @property
    def last(self) -> QueryCall:
        assert self.calls, "the client factory was never called"
        return self.calls[-1]


def replaying(
    frames: Sequence[Mapping[str, Any]],
    *,
    raise_exc: BaseException | None = None,
    stderr_lines: Sequence[str] = (),
    on_message: Callable[[Any], None] | None = None,
) -> ReplayingQuery:
    """A fake client factory that replays ``frames`` (see the module docstring)."""
    return ReplayingQuery(
        frames, raise_exc=raise_exc, stderr_lines=stderr_lines, on_message=on_message
    )


class SilentTransport(RecordingTransport):
    """Answers the control protocol, then never sends a message after the user frame."""

    async def _messages(self) -> AsyncIterator[dict[str, Any]]:
        async for frame in self._receive:
            if frame is None:
                return
            if frame is not _REPLAY:
                yield frame
            # The replay marker is swallowed: nothing follows until the transport closes.


class HangingQuery(ReplayingQuery):
    def __init__(self) -> None:
        super().__init__([])

    def __call__(self, options: ClaudeAgentOptions) -> ClaudeSDKClient:
        cwd = None if options.cwd is None else Path(options.cwd)
        transport = SilentTransport([], workdir=cwd)
        self.calls.append(QueryCall(options, transport))
        return ClaudeSDKClient(options=options, transport=transport)


def hanging() -> HangingQuery:
    """A fake client factory whose CLI never answers the prompt (see the module docstring)."""
    return HangingQuery()


class UnresponsiveTransport(SilentTransport):
    """Records control requests but never answers them, so ``initialize`` waits forever."""

    async def write(self, data: str) -> None:
        for line in data.splitlines():
            if line.strip():
                self.written.append(json.loads(line))


class UnresponsiveQuery(ReplayingQuery):
    def __init__(self) -> None:
        super().__init__([])

    def __call__(self, options: ClaudeAgentOptions) -> ClaudeSDKClient:
        cwd = None if options.cwd is None else Path(options.cwd)
        transport = UnresponsiveTransport([], workdir=cwd)
        self.calls.append(QueryCall(options, transport))
        return ClaudeSDKClient(options=options, transport=transport)


def unresponsive() -> UnresponsiveQuery:
    """A fake client factory whose CLI never answers initialize (see the module docstring)."""
    return UnresponsiveQuery()


def success_frames(
    structured_output: Any,
    *,
    model: str = DEFAULT_MODEL,
    usage: Mapping[str, int] | None = None,
    with_assistant: bool = True,
    model_usage: Mapping[str, Mapping[str, Any]] | None = None,
    assistant_error: str | None = None,
) -> list[dict[str, Any]]:
    """A fictional successful run whose result carries ``structured_output``.

    ``usage=None`` leaves usage out of the result frame; ``with_assistant=False`` leaves out
    the assistant message (so no model is reported); ``model_usage`` adds the result's
    per-model usage; ``assistant_error`` marks the assistant turn with an error code.
    """
    frames: list[dict[str, Any]] = [
        {
            "type": "system",
            "subtype": "init",
            "session_id": "session-fictional-0100",
            "uuid": "uuid-fictional-0101",
            "cwd": "/fictional/workdir",
            "model": model,
            "tools": [],
            "mcp_servers": [],
            "permissionMode": "dontAsk",
            "apiKeySource": "none",
        }
    ]
    if with_assistant:
        assistant: dict[str, Any] = {
            "type": "assistant",
            "session_id": "session-fictional-0100",
            "uuid": "uuid-fictional-0102",
            "parent_tool_use_id": None,
            "message": {
                "id": "msg-fictional-0100",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": "Fictional answer."}],
                "stop_reason": "end_turn",
                "usage": dict(usage or {"input_tokens": 0, "output_tokens": 0}),
            },
        }
        if assistant_error is not None:
            assistant["error"] = assistant_error
        frames.append(assistant)
    result: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 800,
        "duration_api_ms": 750,
        "num_turns": 1,
        "session_id": "session-fictional-0100",
        "uuid": "uuid-fictional-0103",
        "stop_reason": "end_turn",
        "result": "Fictional answer.",
        "terminal_reason": "completed",
    }
    if structured_output is not None:
        result["structured_output"] = structured_output
    if usage is not None:
        result["usage"] = dict(usage)
    if model_usage is not None:
        result["modelUsage"] = {name: dict(value) for name, value in model_usage.items()}
    frames.append(result)
    return frames
