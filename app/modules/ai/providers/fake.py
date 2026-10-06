"""Scripted AI provider for tests and demos (spec 4.2; ADR-044 item 2).

Wiring offers it only when ``JOBPIPELINE_ALLOW_FAKE_PROVIDERS=1``, labelled "Fake (testing
only)". In the setup guide, fictional keywords in the pasted "key" choose how the connection
test behaves (``outcome_for_key``). Content is returned as data; nothing is interpreted.
"""

import asyncio
import json
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from app.domain.errors import (
    AppError,
    InvalidModelOutput,
    NetworkUnavailable,
    ProviderAuthError,
    ProviderNotInstalled,
    UsageLimitReached,
)
from app.modules.ai import (
    AICapabilities,
    AIRequest,
    AIResponse,
    CallCancelled,
    CancelToken,
    ConnectionResult,
    ModelChoice,
    TokenUsage,
)

FakeOutcome = Literal["connected", "auth", "not_installed", "usage_limit", "network"]
FAKE_FAST_MODEL: Final = "fake-fast"
FAKE_THOROUGH_MODEL: Final = "fake-thorough"
KNOWN_MODELS: Final = (
    ModelChoice(FAKE_FAST_MODEL, "Fake fast"),
    ModelChoice(FAKE_THOROUGH_MODEL, "Fake thorough"),
)

_FAILURES: Final[Mapping[str, type[AppError]]] = {
    "auth": ProviderAuthError,
    "not_installed": ProviderNotInstalled,
    "usage_limit": UsageLimitReached,
    "network": NetworkUnavailable,
}
_KEY_KEYWORDS: Final[tuple[tuple[str, FakeOutcome], ...]] = (
    ("fake-auth-rejected", "auth"),
    ("fake-not-installed", "not_installed"),
    ("fake-usage-limit", "usage_limit"),
    ("fake-offline", "network"),
    ("fake-connected", "connected"),
)


@dataclass(frozen=True)
class ScriptedResponse:
    data: Mapping[str, Any] | None = None  # returned as data; raw_text is its JSON
    raw_text: str | None = None  # used verbatim when given
    usage: TokenUsage | None = None
    failure: FakeOutcome | None = None  # raise the mapped error instead of answering
    delay_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.failure == "connected":
            raise ValueError("'connected' is not a failure.")
        if self.failure is None and self.data is None and self.raw_text is None:
            raise ValueError("A scripted response needs data, raw_text or a failure.")


def outcome_for_key(key: str | None) -> FakeOutcome:
    """The outcome a pasted fictional key asks for; anything else is rejected like a bad key."""
    for keyword, outcome in _KEY_KEYWORDS:
        if key and keyword in key:
            return outcome
    return "auth"


class FakeAIProvider:
    name: Final = "fake"
    capabilities: Final = AICapabilities(reports_token_usage=True)

    def __init__(
        self,
        *,
        connection: FakeOutcome = "connected",
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        app_mode: bool = False,
    ) -> None:
        """``app_mode`` (set by wiring): keep no requests in memory, since prompts carry
        personal data, and answer an unscripted task with a plain error, not a crash."""
        self._connection = connection
        self._sleep = sleep
        self._app_mode = app_mode
        self._scripts: dict[str, deque[ScriptedResponse]] = {}
        self.requests: list[AIRequest] = []

    def script(self, task: str, *responses: ScriptedResponse) -> None:
        """Queue responses for a task; each run takes the next, and the last one repeats."""
        self._scripts[task] = deque(responses)

    async def run(self, request: AIRequest, cancel: CancelToken) -> AIResponse:
        if cancel.cancelled:
            raise CallCancelled
        if not self._app_mode:
            self.requests.append(request)
        response = self._next(request.task)
        if response.delay_seconds:
            await self._sleep(response.delay_seconds)
            if cancel.cancelled:  # cancelled while the call was running
                raise CallCancelled
        if response.failure is not None:
            raise _error(response.failure)
        raw_text = response.raw_text
        if raw_text is None:
            raw_text = json.dumps(dict(response.data or {}))
        try:
            data = json.loads(raw_text)
        except ValueError:
            raise InvalidModelOutput(detail="response is not JSON") from None
        if not isinstance(data, dict):
            raise InvalidModelOutput(detail="response is not a JSON object")
        return AIResponse(raw_text=raw_text, data=data, model=request.model, usage=response.usage)

    async def test_connection(self, model: str) -> ConnectionResult:
        if self._connection == "connected":
            return ConnectionResult(ok=True, message="Connected", model=model)
        return ConnectionResult(ok=False, message=_error(self._connection).user_message)

    def known_models(self) -> list[ModelChoice]:
        return list(KNOWN_MODELS)

    def _next(self, task: str) -> ScriptedResponse:
        queue = self._scripts.get(task)
        if not queue:
            if self._app_mode:
                raise InvalidModelOutput(detail="fake provider: no script for this task")
            raise LookupError(f"No scripted response for task {task!r}.")
        return queue.popleft() if len(queue) > 1 else queue[0]


def _error(outcome: FakeOutcome) -> AppError:
    if outcome == "connected":
        raise ValueError("'connected' is not a failure.")
    return _FAILURES[outcome](detail=f"fake provider: {outcome}")
