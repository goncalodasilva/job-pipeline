"""AI module interface (spec 2.4). Providers live in `providers/` and are chosen in wiring.

Services call an injected `AITaskRunner` (the task executor, built in wiring), which renders
the task's prompt, builds an `AIRequest` and runs it on the configured `AIProvider`. Untrusted
content in `AIRequest.user` is already delimited (spec 3.3).
"""

import logging
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
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

# Re-exported: services that count provider calls use these through this interface.
from app.modules.ai.uninterrupted import reserve_uninterrupted as reserve_uninterrupted
from app.modules.ai.uninterrupted import settled as settled

_LOG = logging.getLogger(__name__)


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


UsageUnit = Literal["requests", "fraction"]
_USAGE_UNITS: Final = frozenset({"requests", "fraction"})
_USAGE_WINDOW: Final = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


@dataclass(frozen=True)
class ProviderUsageSnapshot:
    """Usage a provider reported during a call (M2.2; ADR-076 item 6), provider-agnostic.

    ``unit="requests"``: ``used`` (and optionally ``limit``) requests in ``window``; a budget
    may compare it when unit and window match. ``unit="fraction"``: ``utilization`` of the
    provider's ``window`` (0 to 1), shown for information only, never compared with a request
    budget. Times are aware and kept in UTC. Validated when built: anything out of range
    raises ``ValueError``. Never holds a provider's raw payload.
    """

    service: str  # the budget service id, e.g. "claude_subscription"
    window: str  # the provider's window id, e.g. "five_hour", "month": a short token
    unit: UsageUnit
    used: int | None
    limit: int | None
    utilization: float | None
    resets_at: datetime | None
    observed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.service, str) or not self.service.strip():
            raise ValueError("A usage snapshot needs a service.")
        # The provider's own word, and it is stored: an id, never free text.
        if not isinstance(self.window, str) or not _USAGE_WINDOW.fullmatch(self.window):
            raise ValueError("A usage snapshot needs a window: a short id.")
        if self.unit not in _USAGE_UNITS:
            raise ValueError("Unknown usage unit.")
        for name in ("used", "limit"):
            count = getattr(self, name)
            if count is not None and (
                not isinstance(count, int) or isinstance(count, bool) or count < 0
            ):
                raise ValueError(f"A usage snapshot's {name} must be a non-negative integer.")
        utilization = self.utilization
        if utilization is not None and not _is_fraction(utilization):
            raise ValueError("A usage snapshot's utilization must be between 0 and 1.")
        if self.unit == "requests" and self.used is None:
            raise ValueError("A request-count snapshot needs a used count.")
        if self.unit == "fraction" and utilization is None:
            raise ValueError("A fraction snapshot needs a utilization.")
        object.__setattr__(self, "observed_at", _as_utc(self.observed_at, "observed_at"))
        if self.resets_at is not None:
            object.__setattr__(self, "resets_at", _as_utc(self.resets_at, "resets_at"))


def _is_fraction(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    if isinstance(value, int):  # compared exactly: a huge int can't become a float
        return 0 <= value <= 1
    return math.isfinite(value) and 0.0 <= value <= 1.0


def _as_utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"A usage snapshot's {name} must be a timezone-aware datetime.")
    return value.astimezone(UTC)


UsageCallback = Callable[[ProviderUsageSnapshot], None]


def report_usage(callback: UsageCallback | None, snapshot: ProviderUsageSnapshot) -> None:
    """Hand ``snapshot`` to a request's ``on_usage``. A failing callback never changes the
    call: its exception is swallowed and only its type name is logged (never its text)."""
    if callback is None:
        return
    try:
        callback(snapshot)
    except Exception as error:
        _LOG.warning("A usage callback failed (%s)", type(error).__name__)


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
    # Provider-reported usage, handed over during the call (M2.2). It runs on the event loop
    # inside the provider's receive loop, so it must only record or queue cheaply: never
    # block or do I/O (no database writes). An exception it raises is logged and ignored.
    on_usage: UsageCallback | None = None
    max_searches: int = 5
    on_search_usage: Callable[[int], None] | None = None


@dataclass(frozen=True)
class AIResponse:
    raw_text: str
    data: Mapping[str, Any]  # parsed JSON object; the task executor validates the schema (M1.12)
    model: str
    usage: TokenUsage | None  # when the provider reports it
    searched_urls: tuple[str, ...] = ()
    search_count: int = 0


class CallCancelled(Exception):
    """The call was cancelled before or while it ran; nothing to retry."""


TaskKind = Literal["extraction", "generation", "feedback", "summary"]

# What a usage record counts: a provider call, or one web search inside a call (spec 3.7).
UsageKind = Literal["request", "search"]
# The tool name ``AIRequest.on_tool_use`` is asked about for a web search.
WEB_SEARCH_TOOL: Final = "web_search"


class UsageMeter(Protocol):
    """Counts provider calls against the budgets (M2.16; implemented in ``app/services``).

    Every method blocks (database work): the executor calls them in worker threads, never on
    the event loop. ``reserve`` counts one request or search before it is made and returns
    its record id; it raises ``BudgetBlocked`` (or another error) and writes nothing when
    the call may not be made. ``complete`` records the outcome; ``snapshot`` keeps usage
    the provider reported."""

    def reserve(
        self, *, tier: str, task_type: str, task_id: str | None, kind: UsageKind
    ) -> str: ...

    def complete(self, record_id: str, *, success: bool, tokens: TokenUsage | None) -> None: ...

    def snapshot(self, snapshot: ProviderUsageSnapshot) -> None: ...

    def reported_searches(
        self, count: int, *, tier: str, task_type: str, task_id: str | None, success: bool
    ) -> None:
        """Count searches already performed, without blocking on a budget."""
        ...


@dataclass(frozen=True)
class TaskResult:
    data: Mapping[str, Any]  # read-only plain JSON copy, validated against the task's schema
    prompt_id: str
    prompt_version: str
    model: str  # the model the provider reported (ADR-044 item 9)
    tier: Tier  # the tier the task resolved to (spec 3.7)
    attempts: int  # 1, or 2 after the one retry (spec 3.6)
    usage: tuple[TokenUsage | None, ...]  # one entry per provider call that returned
    searched_urls: tuple[str, ...] = ()


class AITaskRunner(Protocol):
    """Runs one AI task end to end. Services get it injected (built in wiring), so they
    never import the prompt module or its template engine (ADR-055)."""

    async def run_task(
        self,
        task: str,
        inputs: Mapping[str, object],
        cancel: CancelToken,
        *,
        meter: UsageMeter,
        tier: Tier | None = None,
        task_id: str | None = None,
    ) -> TaskResult:
        """``meter`` counts every provider call (there is no uncounted run); ``tier``
        overrides the task's tier for this run only; ``task_id`` is the background task the
        usage records belong to."""
        ...


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
