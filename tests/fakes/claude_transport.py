"""A replaying stand-in for the Claude CLI subprocess (M1.1, spec 4.2).

``ReplayTransport`` plugs into ``claude_agent_sdk.query(..., transport=...)`` so
tests exercise the SDK's real control protocol and message parser without
starting a subprocess or touching the network:

- every ``control_request`` the SDK writes (for example ``initialize``) is
  answered with an empty success ``control_response``;
- once the ``user`` message is written, the recorded frames are replayed and
  the stream ends;
- every written frame is kept, parsed, in ``written``.

Frames live in ``tests/fixtures/ai/claude_sdk/<name>.jsonl`` (one stream-json object
per line, fictional content only). They are hand-written from the shapes in SDK 0.2.163
and CLI 2.1.286 (for example an API failure is ``subtype: "success"`` with
``is_error: true``), not captured from a live run; the M1.25 live check re-checks them
(ADR-045).

``Transport`` is an internal SDK API; the class may need updating when the SDK
version changes (the version is pinned and recorded in the M1.1 ADR).
"""

import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from claude_agent_sdk._internal.transport import Transport

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "ai" / "claude_sdk"


def load_frames(name: str) -> list[dict[str, Any]]:
    """Read the recorded frames of ``tests/fixtures/ai/claude_sdk/<name>.jsonl``."""
    path = FIXTURES_DIR / f"{name}.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


class ReplayTransport(Transport):
    def __init__(self, frames: Sequence[dict[str, Any]]) -> None:
        self._frames = [dict(frame) for frame in frames]
        self.written: list[dict[str, Any]] = []
        self._ready = False
        self._replayed = False
        self._send: MemoryObjectSendStream[dict[str, Any] | None]
        self._receive: MemoryObjectReceiveStream[dict[str, Any] | None]
        self._send, self._receive = anyio.create_memory_object_stream(max_buffer_size=1000)

    async def connect(self) -> None:
        self._ready = True

    async def write(self, data: str) -> None:
        for line in data.splitlines():
            if not line.strip():
                continue
            frame = json.loads(line)
            self.written.append(frame)
            if frame.get("type") == "control_request":
                await self._send.send(
                    {
                        "type": "control_response",
                        "response": {
                            "subtype": "success",
                            "request_id": frame["request_id"],
                            "response": {},
                        },
                    }
                )
            elif frame.get("type") == "user" and not self._replayed:
                self._replayed = True
                for recorded in self._frames:
                    await self._send.send(recorded)
                await self._send.send(None)

    async def _messages(self) -> AsyncIterator[dict[str, Any]]:
        async for frame in self._receive:
            if frame is None:
                return
            yield frame

    def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        return self._messages()

    async def close(self) -> None:
        self._ready = False
        await self._send.aclose()

    def is_ready(self) -> bool:
        return self._ready

    async def end_input(self) -> None:
        return None

    def control_requests(self, subtype: str) -> list[dict[str, Any]]:
        """The written control requests with the given ``request.subtype``."""
        return [
            frame
            for frame in self.written
            if frame.get("type") == "control_request"
            and frame.get("request", {}).get("subtype") == subtype
        ]

    def user_frames(self) -> list[dict[str, Any]]:
        return [frame for frame in self.written if frame.get("type") == "user"]
