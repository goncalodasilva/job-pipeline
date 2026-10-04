"""AI module interface (spec 2.4). Providers live in `providers/` and are chosen in wiring.

Services call task functions (M1, `tasks.py`), which build an `AIRequest` and run it on the
configured `AIProvider`. Untrusted content in `AIRequest.user` is already delimited (spec 3.3).
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class AICapabilities:
    web_search: bool = False
    speech_to_text: bool = False
    text_to_speech: bool = False
    reports_token_usage: bool = False


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class ModelChoice:
    id: str
    label: str


@dataclass(frozen=True)
class ConnectionResult:
    ok: bool
    message: str  # plain text shown to the user
    model: str | None = None


class CancelToken(Protocol):
    @property
    def cancelled(self) -> bool: ...


@dataclass(frozen=True)
class AIRequest:
    task: str  # e.g. "tailor_cv"
    prompt_id: str
    prompt_version: int
    system: str
    user: str  # rendered prompt; untrusted content already delimited (spec 3.3)
    output_schema: Mapping[str, Any]  # JSON Schema the response must satisfy
    model: str  # resolved from the task's tier (Fast or Thorough)
    # Plain AI tasks run with no tools; only explicit web questions allow search (spec 2.9.7).
    allow_web_search: bool = False
    on_tool_use: Callable[[str], Awaitable[bool]] | None = None  # budget hook; False blocks


@dataclass(frozen=True)
class AIResponse:
    raw_text: str
    data: Mapping[str, Any]  # parsed and schema-validated
    model: str
    usage: TokenUsage | None  # when the provider reports it


class AIProvider(Protocol):
    # Read-only members, so implementations may use plain or narrower attributes or properties.
    @property
    def name(self) -> str: ...

    @property
    def capabilities(self) -> AICapabilities: ...

    async def test_connection(self, model: str) -> ConnectionResult: ...

    def known_models(self) -> list[ModelChoice]: ...

    async def run(self, request: AIRequest, cancel: CancelToken) -> AIResponse: ...
