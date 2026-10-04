"""Harnesses that drive each AI provider through the shared contract suite (spec 4.1).

A harness hides how a provider is scripted, so the Claude provider (M1.13) can join the
suite with a fake transport. All values are fictional.

``tools_sent`` must report what the provider actually offered (for the Claude provider, the
built options or command line), not echo the request's flag as the fake harness can.
"""

from typing import Literal, Protocol

from app.modules.ai import AIProvider, AIRequest, AIResponse, CancelToken
from app.modules.ai.providers.fake import FakeAIProvider, ScriptedResponse

CONTRACT_ANSWER = {"answer": "ok"}

# The connection failures every provider maps (spec 2.11), independent of any provider.
Failure = Literal["auth", "not_installed", "usage_limit", "network"]


class ProviderHarness(Protocol):
    def provider(self) -> AIProvider:
        """A provider that answers the next run with ``CONTRACT_ANSWER`` for any task."""
        ...

    def failing(self, outcome: Failure) -> AIProvider:
        """A provider whose ``test_connection`` fails with ``outcome``."""
        ...

    def reported_model(self, requested: str) -> str:
        """The model the provider reports for a call that asked for ``requested`` (records
        keep the reported model, ADR-044 item 9)."""
        ...

    def tools_sent(self) -> list[bool]:
        """Per recorded call: whether any tool (web search) was offered."""
        ...

    def calls_made(self) -> int: ...


class FakeHarness:
    def __init__(self) -> None:
        self._provider: FakeAIProvider | None = None

    def provider(self) -> AIProvider:
        fake = _AnyTaskFake()
        self._provider = fake
        return fake

    def failing(self, outcome: Failure) -> AIProvider:
        fake = FakeAIProvider(connection=outcome)
        self._provider = fake
        return fake

    def reported_model(self, requested: str) -> str:
        return requested  # the fake reports the model it was asked for

    def tools_sent(self) -> list[bool]:
        return [request.allow_web_search for request in self._requests()]

    def calls_made(self) -> int:
        return len(self._requests())

    def _requests(self) -> list[AIRequest]:
        return [] if self._provider is None else list(self._provider.requests)


class _AnyTaskFake(FakeAIProvider):
    """Scripts the contract answer for whichever task a run asks for."""

    async def run(self, request: AIRequest, cancel: CancelToken) -> AIResponse:
        self.script(request.task, ScriptedResponse(data=CONTRACT_ANSWER))
        return await super().run(request, cancel)
