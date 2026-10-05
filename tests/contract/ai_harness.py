"""Harnesses that drive each AI provider through the shared contract suite (spec 4.1).

A harness hides how a provider is scripted, so the Claude provider (M1.13) can join the
suite with a fake transport. All values are fictional.

Two protocols: ``ProviderHarness`` covers running calls and the model list (every
provider); ``ConnectionHarness`` adds ``failing`` for the connection tests. Both harnesses
implement both (the Claude harness joined the connection tests in M1.14: its ``failing``
replays the fictional failure fixtures or raises the SDK error through the replaying
client factory).

``tools_sent`` must report what the provider actually offered (for the Claude provider, the
built options or command line), not echo the request's flag as the fake harness can.
"""

from pathlib import Path
from typing import Any, Literal, Protocol

from claude_agent_sdk import CLINotFoundError

from app.modules.ai import AIProvider, AIRequest, AIResponse, CancelToken
from app.modules.ai.providers.claude_sdk import ClaudeSdkProvider
from app.modules.ai.providers.fake import FakeAIProvider, ScriptedResponse
from app.modules.secrets import CLAUDE_CODE_OAUTH_TOKEN
from app.modules.secrets.providers.memory import MemorySecretStore
from tests.fakes.claude_query import ReplayingQuery, replaying, success_frames
from tests.fakes.claude_transport import load_frames

CONTRACT_ANSWER = {"answer": "ok"}

# The connection failures every provider maps (spec 2.11), independent of any provider.
Failure = Literal["auth", "not_installed", "usage_limit", "network"]


class ProviderHarness(Protocol):
    def provider(self) -> AIProvider:
        """A provider that answers the next run with ``CONTRACT_ANSWER`` for any task."""
        ...

    def reported_model(self, requested: str) -> str:
        """The model the provider reports for a call that asked for ``requested`` (records
        keep the reported model, ADR-044 item 9)."""
        ...

    def tools_sent(self) -> list[bool]:
        """Per recorded call: whether any tool (web search) was offered."""
        ...

    def calls_made(self) -> int: ...

    def cancelled_mid_call(self) -> tuple[AIProvider, CancelToken]:
        """A provider and a token that turns cancelled once the provider's call is under
        way (for the Claude provider: after the first message arrives)."""
        ...

    def released_before_return(self) -> bool:
        """Whether everything the last call held was released before ``run`` returned (for
        the Claude provider: the transport closed in the calling task while its working
        folder still existed, and the folder is gone)."""
        ...


class _CancelWhenFlipped:
    def __init__(self) -> None:
        self.cancelled = False

    def flip(self, *_ignored: Any) -> None:
        self.cancelled = True


class ConnectionHarness(ProviderHarness, Protocol):
    def failing(self, outcome: Failure) -> AIProvider:
        """A provider whose ``test_connection`` fails with ``outcome``."""
        ...


class FakeHarness:
    def __init__(self) -> None:
        self._provider: FakeAIProvider | None = None

    def provider(self) -> AIProvider:
        fake = _AnyTaskFake()
        self._provider = fake
        return fake

    def cancelled_mid_call(self) -> tuple[AIProvider, CancelToken]:
        token = _CancelWhenFlipped()

        async def flip_while_sleeping(_seconds: float) -> None:
            token.flip()

        fake = _AnyTaskFake(sleep=flip_while_sleeping, delay_seconds=0.01)
        self._provider = fake
        return fake, token

    def released_before_return(self) -> bool:
        return True  # the fake holds nothing

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

    def __init__(self, *, delay_seconds: float = 0.0, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._delay_seconds = delay_seconds

    async def run(self, request: AIRequest, cancel: CancelToken) -> AIResponse:
        self.script(
            request.task,
            ScriptedResponse(data=CONTRACT_ANSWER, delay_seconds=self._delay_seconds),
        )
        return await super().run(request, cancel)


CLAUDE_REPORTED_MODEL = "claude-haiku-fictional"
_CONTRACT_TOKEN = "fake-" + "token-" + "c" * 10  # fictional, built at runtime
_FICTIONAL_CLI = Path("/fictional/claude-cli/claude")  # never run: the query is fake
# Fictional recorded runs in tests/fixtures/ai/claude_sdk/ for each connection failure.
_CLAUDE_FAILURE_FIXTURES: dict[Failure, str] = {
    "auth": "auth_failed",
    "usage_limit": "rate_limit",
    "network": "connection",
}


class ClaudeTransportHarness:
    """``ClaudeSdkProvider`` with a fake query that replays a success frame (built in code)
    through the SDK's real parser. ``tools_sent`` reads the options the provider built."""

    def __init__(self, temp_root: Path | None = None) -> None:
        self._queries: list[ReplayingQuery] = []
        self._temp_root = temp_root

    def provider(self) -> AIProvider:
        return self._build(replaying(self._frames()))

    def cancelled_mid_call(self) -> tuple[AIProvider, CancelToken]:
        token = _CancelWhenFlipped()
        return self._build(replaying(self._frames(), on_message=token.flip)), token

    def released_before_return(self) -> bool:
        call = self._queries[-1].last
        transport = call.transport
        folder_gone = call.cwd is not None and not call.cwd.exists()
        root_empty = self._temp_root is None or not any(self._temp_root.iterdir())
        return (
            transport.closed
            and transport.close_task == call.caller_task
            and transport.workdir_existed_at_close is True
            and folder_gone
            and root_empty
        )

    def failing(self, outcome: Failure) -> AIProvider:
        if outcome == "not_installed":
            return self._build(replaying([], raise_exc=CLINotFoundError("Fictional: no CLI")))
        return self._build(replaying(load_frames(_CLAUDE_FAILURE_FIXTURES[outcome])))

    def _frames(self) -> list[dict[str, Any]]:
        return success_frames(dict(CONTRACT_ANSWER), model=CLAUDE_REPORTED_MODEL)

    def _build(self, query: ReplayingQuery) -> AIProvider:
        secrets = MemorySecretStore()
        secrets.set(CLAUDE_CODE_OAUTH_TOKEN, _CONTRACT_TOKEN)
        self._queries.append(query)
        return ClaudeSdkProvider(
            secrets=secrets,
            mode="subscription_token",
            client_factory=query,
            cli_path=lambda: _FICTIONAL_CLI,
            environ={},
            temp_root=self._temp_root,
        )

    def reported_model(self, requested: str) -> str:
        return CLAUDE_REPORTED_MODEL  # the model in the replayed assistant frame

    def tools_sent(self) -> list[bool]:
        return [bool(call.options.tools) for query in self._queries for call in query.calls]

    def calls_made(self) -> int:
        return sum(len(query.calls) for query in self._queries)
