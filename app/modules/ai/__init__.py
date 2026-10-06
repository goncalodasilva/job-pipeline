"""AI module interface (spec 2.4). Providers live in `providers/` and are chosen in wiring.

Services call an injected `AITaskRunner` (the task executor, built in wiring), which renders
the task's prompt, builds an `AIRequest` and runs it on the configured `AIProvider`. Untrusted
content in `AIRequest.user` is already delimited (spec 3.3).
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Literal, Protocol

from app.domain.errors import (
    AppError,
    InvalidModelOutput,
    NetworkUnavailable,
    ProviderAuthError,
    ProviderNotInstalled,
    ProviderRequestRejected,
    UsageLimitReached,
)
from app.domain.settings import AIProviderSettings, ProviderKind, Tier


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


# What a failed connection test was (spec 2.11), so callers word it without matching text:
# ProviderAuthError, ProviderNotInstalled, UsageLimitReached, NetworkUnavailable (also a
# timeout), ProviderRequestRejected, InvalidModelOutput.
ConnectionFailure = Literal[
    "auth", "not_installed", "usage_limit", "network", "rejected", "invalid_output"
]
_FAILURE_BY_ERROR: Final[Mapping[type[AppError], ConnectionFailure]] = MappingProxyType(
    {
        ProviderAuthError: "auth",
        ProviderNotInstalled: "not_installed",
        UsageLimitReached: "usage_limit",
        NetworkUnavailable: "network",
        ProviderRequestRejected: "rejected",
        InvalidModelOutput: "invalid_output",
    }
)


def connection_failure(error: AppError) -> ConnectionFailure | None:
    """The failure kind of a mapped domain error, or None for any other error."""
    for kind, failure in _FAILURE_BY_ERROR.items():
        if isinstance(error, kind):
            return failure
    return None


@dataclass(frozen=True)
class ConnectionResult:
    ok: bool
    message: str  # plain text shown to the user
    model: str | None = None
    failure: ConnectionFailure | None = None  # set by every provider on failure; None if ok


class CancelToken(Protocol):
    @property
    def cancelled(self) -> bool: ...


@dataclass(frozen=True)
class AIRequest:
    task: str  # e.g. "tailor_cv"
    prompt_id: str
    prompt_version: str  # "<v>" or "<v>+override" (spec 3.6)
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
    data: Mapping[str, Any]  # parsed JSON object; the task executor validates the schema (M1.12)
    model: str
    usage: TokenUsage | None  # when the provider reports it


class CallCancelled(Exception):
    """The call was cancelled before or while it ran; nothing to retry."""


TaskKind = Literal["extraction", "generation", "feedback", "summary"]


@dataclass(frozen=True)
class TaskResult:
    data: Mapping[str, Any]  # read-only plain JSON copy, validated against the task's schema
    prompt_id: str
    prompt_version: str
    model: str  # the model the provider reported (ADR-044 item 9)
    tier: Tier  # the tier the task resolved to (spec 3.7)
    attempts: int  # 1, or 2 after the one retry (spec 3.6)
    usage: tuple[TokenUsage | None, ...]  # one entry per provider call that returned


class AITaskRunner(Protocol):
    """Runs one AI task end to end. Services get it injected (built in wiring), so they
    never import the prompt module or its template engine (ADR-055)."""

    async def run_task(
        self, task: str, inputs: Mapping[str, object], cancel: CancelToken
    ) -> TaskResult: ...


class AIProvider(Protocol):
    # Read-only members, so implementations may use plain or narrower attributes or properties.
    @property
    def name(self) -> str: ...

    @property
    def capabilities(self) -> AICapabilities: ...

    async def test_connection(self, model: str) -> ConnectionResult: ...

    def known_models(self) -> list[ModelChoice]: ...

    async def run(self, request: AIRequest, cancel: CancelToken) -> AIResponse: ...


@dataclass(frozen=True)
class ProviderChoice:
    """An AI provider the setup guide and Settings offer."""

    kind: ProviderKind
    label: str


ProviderStatus = Literal["ok", "not_configured", "not_allowed", "missing_sign_in", "cli_missing"]


@dataclass(frozen=True)
class ProviderHealth:
    """Whether the configured provider can be used, judged from configuration alone."""

    status: ProviderStatus

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class AIProviderFactory(Protocol):
    """Builds AI providers from settings (implemented in wiring, spec 2.3 rule 7).

    ``ai_provider`` and ``fake_for_key`` raise ``ValueError`` for a provider that isn't on
    offer; callers check the chosen kind against ``provider_choices()`` first.
    """

    def provider_choices(self) -> tuple[ProviderChoice, ...]: ...

    def ai_provider(self, settings: AIProviderSettings, *, data_dir: Path) -> AIProvider: ...

    def fake_for_key(self, key: str | None) -> AIProvider: ...

    def provider_health(self, settings: AIProviderSettings) -> ProviderHealth: ...

    def connection_model(self, settings: AIProviderSettings) -> str:
        """The model a connection test asks for: the settings' Fast tier model, else the
        configured provider's Fast default."""
        ...

    def known_models(self, kind: ProviderKind | None) -> tuple[ModelChoice, ...]:
        """The models the provider a kind stands for offers (no provider is built); the
        Claude models when nothing is chosen yet, like ``connection_model``."""
        ...

    def default_models(self, kind: ProviderKind | None) -> Mapping[Tier, str]:
        """The default model per tier of the provider a kind stands for (no provider is
        built); Claude's when nothing is chosen yet, like ``known_models``."""
        ...

    def environment_keys(self) -> frozenset[str]:
        """The secret keys the environment provides (key names only, never values)."""
        ...

    def task_runner(
        self,
        provider: AIProvider,
        settings: Callable[[], AIProviderSettings],
        *,
        data_dir: Path,
    ) -> AITaskRunner: ...
