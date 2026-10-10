"""Usage on every provider call: the executor's meter, the web-search hook and the fake's
tool calls (M2.16; AC-M2.6, AC-M2.8; spec 3.7; ADR-056 follow-up, ADR-076 items 3 and 12).

Assumed interface:

- ``app.modules.ai.UsageMeter`` (a Protocol; the usage service implements it through an
  adapter in ``app/services``): ``reserve(*, tier, task_type, task_id, kind) -> str`` with
  ``kind`` ``"request"`` or ``"search"`` (raises ``BudgetBlocked``),
  ``complete(record_id, *, success, tokens)`` and ``snapshot(ProviderUsageSnapshot)``.
- ``AITaskRunner.run_task(task, inputs, cancel, *, meter, tier=None, task_id=None)``: one
  ``request`` reservation before each provider call (``task_type`` is the task name, ``tier``
  the tier the call runs on) and one completion after it, whatever the call did. A retry is a
  second reservation; a block raises ``BudgetBlocked`` and makes no provider call. ``tier``
  overrides the resolved tier for that run only. The meter's methods are blocking, so they
  never run on the event loop's thread.
- Snapshots a provider reports through ``AIRequest.on_usage`` reach ``meter.snapshot`` before
  ``run_task`` returns or raises; a failing ``snapshot`` never changes the call.
- A task that allows web search gets an ``on_tool_use`` hook: one ``search`` reservation per
  tool call (completed by the time ``run_task`` returns), ``False`` when the reservation is
  refused for any reason (fails closed).
- ``ScriptedResponse(tool_calls=N)``: the fake asks ``on_tool_use("web_search")`` N times, each
  on its own, and lists every call in ``FakeAIProvider.tool_calls`` as ``(tool, allowed)``. A
  request that doesn't allow web search makes no tool calls.

The meter is ``tests.fakes.usage.RecordingMeter`` (in memory). Prompts come from
``tests/fixtures/prompts``. New names are imported inside the tests so each fails on its own
until the behaviour exists. All content is fictional.
"""

import os
import subprocess
import sys
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.domain.errors import (
    BudgetBlocked,
    InvalidModelOutput,
    NetworkUnavailable,
    UsageLimitReached,
    UsageLimitsUnreadable,
)
from app.domain.settings import AIProviderSettings, Tier
from app.modules.ai import (
    AICapabilities,
    AIRequest,
    AIResponse,
    CallCancelled,
    CancelToken,
    ProviderUsageSnapshot,
    TokenUsage,
)
from app.modules.ai.executor import TaskExecutor
from app.modules.ai.prompts import PromptLibrary
from app.modules.ai.providers.fake import (
    FAKE_FAST_MODEL,
    FAKE_THOROUGH_MODEL,
    FakeAIProvider,
    ScriptedResponse,
)
from app.modules.ai.tasks import TaskSpec
from tests.fakes.ai import NotCancelled, load_ai_fixture, make_request
from tests.fakes.usage import RecordingMeter

REPO_ROOT = Path(__file__).resolve().parents[4]
FIXTURE_PROMPTS = REPO_ROOT / "tests" / "fixtures" / "prompts"
TASK = "_test_task"
RENAMED_TASK = "fictional_task"  # a name AIProviderSettings.task_tiers accepts
PROVIDER_DEFAULTS: dict[Tier, str] = {"fast": FAKE_FAST_MODEL, "thorough": FAKE_THOROUGH_MODEL}
INPUTS: dict[str, object] = {
    "job_text": "Example Ltd is hiring a fictional lighthouse keeper in a fictional town.",
    "preferences": {"target_roles": ["Lighthouse Keeper"], "locations": ["Fictionville"]},
}
OK_DATA = {"answer": "A fictional answer about a fictional role."}
TASK_ID = "task-fictional-1"
WEB_SEARCH_TOOL = "web_search"
OBSERVED = datetime(2026, 2, 20, 15, 0, 0, tzinfo=UTC)
SNAPSHOT = ProviderUsageSnapshot(
    service="fake",
    window="month",
    unit="requests",
    used=7,
    limit=None,
    utilization=None,
    resets_at=None,
    observed_at=OBSERVED,
)


# --- helpers -----------------------------------------------------------------------------------


def _specs(task: str = TASK, *, allow_web_search: bool = False) -> dict[str, TaskSpec]:
    return {task: TaskSpec(task, "summary", allow_web_search=allow_web_search)}


def _executor(
    provider: Any,
    *,
    prompts: PromptLibrary | None = None,
    settings: AIProviderSettings | None = None,
    specs: Mapping[str, TaskSpec] | None = None,
) -> TaskExecutor:
    current = settings if settings is not None else AIProviderSettings()
    return TaskExecutor(
        provider=provider,
        prompts=prompts if prompts is not None else PromptLibrary(FIXTURE_PROMPTS),
        settings=lambda: current,
        provider_defaults=PROVIDER_DEFAULTS,
        specs=specs if specs is not None else _specs(),
    )


def _scripted(*names_or_responses: str | ScriptedResponse, task: str = TASK) -> FakeAIProvider:
    provider = FakeAIProvider()
    responses = [
        load_ai_fixture(TASK, item) if isinstance(item, str) else item
        for item in names_or_responses
    ]
    provider.script(task, *responses)
    return provider


def _renamed_library(tmp_path: Path) -> PromptLibrary:
    """The fixture prompt copied under ``RENAMED_TASK`` (same version, tier and schema)."""
    folder = tmp_path / "prompts" / RENAMED_TASK
    folder.mkdir(parents=True)
    text = (FIXTURE_PROMPTS / TASK / "prompt.md").read_text(encoding="utf-8")
    (folder / "prompt.md").write_text(text.replace(TASK, RENAMED_TASK), encoding="utf-8")
    schema = (FIXTURE_PROMPTS / TASK / "schema.json").read_text(encoding="utf-8")
    (folder / "schema.json").write_text(schema, encoding="utf-8")
    return PromptLibrary(tmp_path / "prompts")


def _with_tool_calls(count: int) -> ScriptedResponse:
    return ScriptedResponse(data=OK_DATA, tool_calls=count)  # type: ignore[call-arg]


def _web_executor(provider: Any) -> TaskExecutor:
    return _executor(provider, specs=_specs(allow_web_search=True))


async def _run(
    executor: TaskExecutor,
    meter: RecordingMeter,
    *,
    task: str = TASK,
    cancel: CancelToken | None = None,
    **kwargs: Any,
) -> Any:
    token = cancel if cancel is not None else NotCancelled()
    return await executor.run_task(task, INPUTS, token, meter=meter, **kwargs)  # type: ignore[call-arg]


class LoggingProvider:
    """Writes the start and end of every provider call into the meter's event log."""

    name = "fake"
    capabilities = AICapabilities()

    def __init__(self, inner: FakeAIProvider, events: list[str]) -> None:
        self._inner = inner
        self._events = events

    async def run(self, request: AIRequest, cancel: CancelToken) -> AIResponse:
        self._events.append("call:start")
        try:
            return await self._inner.run(request, cancel)
        finally:
            self._events.append("call:end")


CALL = ["reserve:request", "call:start", "call:end", "complete:request"]


# --- one record per provider call --------------------------------------------------------------


async def test_executor_reserves_before_and_completes_after_each_call() -> None:
    meter = RecordingMeter()
    provider = LoggingProvider(_scripted("usage"), meter.events)

    result = await _run(_executor(provider), meter, task_id=TASK_ID)

    assert meter.events == CALL, "reserved before the call, completed after it"
    (record,) = meter.requests
    assert (record.kind, record.task_type, record.task_id) == ("request", TASK, TASK_ID)
    assert record.tier == result.tier == "fast"
    assert (record.completed, record.success, record.completions) == (True, True, 1)
    assert record.tokens == TokenUsage(input_tokens=120, output_tokens=45)


async def test_record_without_task_id_or_token_counts() -> None:
    meter = RecordingMeter()

    await _run(_executor(_scripted("ok")), meter)

    (record,) = meter.requests
    assert record.task_id is None
    assert (record.completed, record.success, record.tokens) == (True, True, None)


async def test_meter_is_required() -> None:
    """Every provider call is counted (ADR-056 follow-up): there is no uncounted default."""
    import inspect

    from app.modules.ai import AITaskRunner

    for owner in (TaskExecutor, AITaskRunner):
        parameters = inspect.signature(owner.run_task).parameters
        assert "meter" in parameters, f"{owner.__name__}.run_task takes a meter"
        assert parameters["meter"].default is inspect.Parameter.empty, owner.__name__
        assert parameters["tier"].default is None
        assert parameters["task_id"].default is None


async def test_invalid_output_attempt_is_completed_and_counted() -> None:
    """A response that breaks the schema, twice: both calls stay counted, with their tokens."""
    meter = RecordingMeter()
    tokens = TokenUsage(input_tokens=30, output_tokens=4)
    bad = ScriptedResponse(data={"answer": 42}, usage=tokens)
    provider = LoggingProvider(_scripted(bad), meter.events)

    with pytest.raises(InvalidModelOutput):
        await _run(_executor(provider), meter)

    assert meter.events == CALL + CALL
    assert [(r.completed, r.completions) for r in meter.requests] == [(True, 1), (True, 1)]
    assert [r.tokens for r in meter.requests] == [tokens, tokens], "the provider answered"
    assert [r.success for r in meter.requests] == [False, False], "but not with valid output"


async def test_malformed_output_attempt_is_completed_unsuccessful() -> None:
    """The provider raised ``InvalidModelOutput`` (no response): counted, no tokens."""
    meter = RecordingMeter()

    with pytest.raises(InvalidModelOutput):
        await _run(_executor(_scripted("malformed")), meter)

    assert [(r.completed, r.success, r.tokens) for r in meter.requests] == [
        (True, False, None),
        (True, False, None),
    ]


async def test_adversarial_output_is_counted_like_any_other_call() -> None:
    meter = RecordingMeter()

    result = await _run(_executor(_scripted("adversarial")), meter)

    assert result.attempts == 1
    assert [(r.completed, r.success) for r in meter.requests] == [(True, True)]


async def test_retry_counts_as_second_request() -> None:
    meter = RecordingMeter()
    provider = LoggingProvider(_scripted("schema_invalid", "usage"), meter.events)

    result = await _run(_executor(provider), meter, task_id=TASK_ID)

    assert result.attempts == 2
    assert meter.events == CALL + CALL, "the first record is completed before the retry reserves"
    first, second = meter.requests
    assert first.id != second.id
    assert [r.task_id for r in meter.requests] == [TASK_ID, TASK_ID]
    assert [r.task_type for r in meter.requests] == [TASK, TASK]
    assert [r.tier for r in meter.requests] == ["fast", "fast"]
    assert [r.completed for r in meter.requests] == [True, True]
    assert second.success is True
    assert second.tokens == TokenUsage(input_tokens=120, output_tokens=45)


async def test_retry_blocked_raises_budget_blocked() -> None:
    meter = RecordingMeter(block_requests_after=1)
    inner = _scripted("schema_invalid", "ok")
    provider = LoggingProvider(inner, meter.events)

    with pytest.raises(BudgetBlocked):
        await _run(_executor(provider), meter)

    assert len(inner.requests) == 1, "no second provider call"
    assert meter.events == CALL
    (record,) = meter.requests
    assert record.completed is True, "the first call stays completed and counted"
    assert meter.refused == ["request"]


async def test_first_call_blocked_makes_no_provider_call() -> None:
    meter = RecordingMeter(block_requests_after=0)
    provider = _scripted("ok")

    with pytest.raises(BudgetBlocked):
        await _run(_executor(provider), meter)

    assert provider.requests == []
    assert meter.records == [] and meter.events == []


async def test_provider_error_completes_record_unsuccessful() -> None:
    meter = RecordingMeter()
    inner = _scripted(ScriptedResponse(failure="network"))
    provider = LoggingProvider(inner, meter.events)

    with pytest.raises(NetworkUnavailable):
        await _run(_executor(provider), meter)

    assert len(inner.requests) == 1, "a provider error is not retried"
    assert meter.events == CALL
    (record,) = meter.requests
    assert (record.completed, record.success, record.tokens) == (True, False, None)


async def test_unexpected_provider_error_completes_record_unsuccessful() -> None:
    class Broken:
        name = "fake"
        capabilities = AICapabilities()

        async def run(self, request: AIRequest, cancel: CancelToken) -> AIResponse:
            raise RuntimeError("a fictional crash inside the provider")

    meter = RecordingMeter()

    with pytest.raises(RuntimeError):
        await _run(_executor(Broken()), meter)

    (record,) = meter.requests
    assert (record.completed, record.success) == (True, False)


async def test_cancelled_call_record_completed_and_counted() -> None:
    class Token:
        cancelled = False

    token = Token()

    async def cancel_while_waiting(seconds: float) -> None:
        token.cancelled = True

    inner = FakeAIProvider(sleep=cancel_while_waiting)
    inner.script(TASK, ScriptedResponse(data=OK_DATA, delay_seconds=1.0))
    meter = RecordingMeter()
    provider = LoggingProvider(inner, meter.events)

    with pytest.raises(CallCancelled):
        await _run(_executor(provider), meter, cancel=token)

    assert meter.events == CALL, "no retry after a cancel"
    (record,) = meter.requests
    assert (record.completed, record.success, record.tokens) == (True, False, None)


async def test_cancel_after_invalid_output_reserves_no_retry() -> None:
    class Token:
        cancelled = False

    token = Token()

    class CancelsLate(FakeAIProvider):
        async def run(self, request: AIRequest, cancel: CancelToken) -> AIResponse:
            response = await super().run(request, cancel)
            token.cancelled = True  # the user cancels as the invalid answer arrives
            return response

    provider = CancelsLate()
    provider.script(TASK, load_ai_fixture(TASK, "schema_invalid"))
    meter = RecordingMeter()

    with pytest.raises(CallCancelled):
        await _run(_executor(provider), meter, cancel=token)

    (record,) = meter.requests
    assert record.completed is True
    assert meter.refused == []


# --- tier override ("run once on Fast") --------------------------------------------------------


async def test_tier_override_used_once(tmp_path: Path) -> None:
    provider = _scripted("ok", task=RENAMED_TASK)
    settings = AIProviderSettings(task_tiers={RENAMED_TASK: "thorough"})
    executor = _executor(
        provider, prompts=_renamed_library(tmp_path), settings=settings, specs=_specs(RENAMED_TASK)
    )
    meter = RecordingMeter()

    once = await _run(executor, meter, task=RENAMED_TASK, tier="fast")
    after = await _run(executor, meter, task=RENAMED_TASK)

    assert [request.model for request in provider.requests] == [
        FAKE_FAST_MODEL,
        FAKE_THOROUGH_MODEL,
    ]
    assert (once.tier, after.tier) == ("fast", "thorough"), "the override lasts one run"
    assert [record.tier for record in meter.requests] == ["fast", "thorough"]


async def test_tier_override_applies_to_the_retry_too() -> None:
    provider = _scripted("schema_invalid", "ok")
    meter = RecordingMeter()

    result = await _run(_executor(provider), meter, tier="thorough")

    assert result.tier == "thorough"
    assert [request.model for request in provider.requests] == [FAKE_THOROUGH_MODEL] * 2
    assert [record.tier for record in meter.requests] == ["thorough", "thorough"]


async def test_unknown_tier_override_is_refused_before_any_call() -> None:
    provider = _scripted("ok")
    meter = RecordingMeter()

    with pytest.raises(ValueError):
        await _run(_executor(provider), meter, tier="fastest")

    assert provider.requests == [] and meter.records == []


# --- the meter never runs on the event loop ----------------------------------------------------


async def test_meter_calls_run_off_the_event_loop() -> None:
    loop_thread = threading.get_ident()
    meter = RecordingMeter()
    response = ScriptedResponse(data=OK_DATA, usage_snapshots=(SNAPSHOT,), tool_calls=1)  # type: ignore[call-arg]

    await _run(_web_executor(_scripted(response)), meter)

    names = {name for name, _ in meter.threads}
    assert names == {"reserve:request", "reserve:search", "complete", "snapshot"}
    on_loop = [name for name, thread in meter.threads if thread == loop_thread]
    assert on_loop == [], "blocking meter calls belong in a worker thread"


# --- provider-reported usage -------------------------------------------------------------------


async def test_snapshots_reported_during_a_call_reach_the_meter() -> None:
    later = ProviderUsageSnapshot(
        service="fake",
        window="month",
        unit="requests",
        used=8,
        limit=None,
        utilization=None,
        resets_at=None,
        observed_at=OBSERVED,
    )
    meter = RecordingMeter()
    provider = _scripted(ScriptedResponse(data=OK_DATA, usage_snapshots=(SNAPSHOT, later)))

    await _run(_executor(provider), meter)

    assert provider.requests[0].on_usage is not None
    assert meter.snapshots == [SNAPSHOT, later], "in the order they were reported"


async def test_snapshot_of_a_failed_call_still_reaches_the_meter() -> None:
    meter = RecordingMeter()
    provider = _scripted(ScriptedResponse(failure="usage_limit", usage_snapshots=(SNAPSHOT,)))

    with pytest.raises(UsageLimitReached):
        await _run(_executor(provider), meter)

    assert meter.snapshots == [SNAPSHOT]


async def test_failing_snapshot_never_changes_the_call() -> None:
    meter = RecordingMeter(snapshot_error=RuntimeError("a fictional storage failure"))
    provider = _scripted(ScriptedResponse(data=OK_DATA, usage_snapshots=(SNAPSHOT,)))

    result = await _run(_executor(provider), meter)

    assert dict(result.data) == OK_DATA
    (record,) = meter.requests
    assert (record.completed, record.success) == (True, True)


async def test_on_usage_itself_does_no_meter_work_on_the_loop() -> None:
    """``on_usage`` runs inside the provider's receive loop: it only queues."""
    seen: list[int] = []

    class Inspecting(FakeAIProvider):
        async def run(self, request: AIRequest, cancel: CancelToken) -> AIResponse:
            assert request.on_usage is not None
            request.on_usage(SNAPSHOT)
            seen.append(len(meter.snapshots))  # nothing saved synchronously, on the loop
            return await super().run(request, cancel)

    meter = RecordingMeter()
    provider = Inspecting()
    provider.script(TASK, ScriptedResponse(data=OK_DATA))

    await _run(_executor(provider), meter)

    assert seen == [0]
    assert meter.snapshots == [SNAPSHOT]


# --- web search --------------------------------------------------------------------------------


async def test_web_search_hook_reserves_search_record() -> None:
    meter = RecordingMeter()
    provider = _scripted(_with_tool_calls(2))

    result = await _run(_web_executor(provider), meter, task_id=TASK_ID)

    assert provider.requests[0].on_tool_use is not None
    assert provider.tool_calls == [(WEB_SEARCH_TOOL, True), (WEB_SEARCH_TOOL, True)]  # type: ignore[attr-defined]
    assert len(meter.requests) == 1, "the call itself is one request"
    assert [(s.task_type, s.task_id, s.tier) for s in meter.searches] == [
        (TASK, TASK_ID, result.tier),
        (TASK, TASK_ID, result.tier),
    ]
    assert [s.completed for s in meter.searches] == [True, True], "no search is left open"
    assert [s.tokens for s in meter.searches] == [None, None]
    assert meter.events[0] == "reserve:request", "the request is reserved before any search"


async def test_web_search_hook_blocks_when_over_budget() -> None:
    meter = RecordingMeter(block_searches_after=1)
    provider = _scripted(_with_tool_calls(3))

    result = await _run(_web_executor(provider), meter)

    assert provider.tool_calls == [  # type: ignore[attr-defined]
        (WEB_SEARCH_TOOL, True),
        (WEB_SEARCH_TOOL, False),
        (WEB_SEARCH_TOOL, False),
    ]
    assert len(meter.searches) == 1, "a blocked search writes no record"
    assert meter.refused == ["search", "search"]
    assert dict(result.data) == OK_DATA, "the block stops the search, not the task"
    (request,) = meter.requests
    assert (request.completed, request.success) == (True, True)


async def test_web_search_hook_fails_closed_when_reserve_breaks() -> None:
    """Any refusal blocks the search (unreadable budgets, a storage error): never allowed."""
    for error in (UsageLimitsUnreadable(detail="fictional"), RuntimeError("fictional")):
        meter = RecordingMeter(search_error=error)
        provider = _scripted(_with_tool_calls(1))

        await _run(_web_executor(provider), meter)

        assert provider.tool_calls == [(WEB_SEARCH_TOOL, False)]  # type: ignore[attr-defined]
        assert meter.searches == []


async def test_plain_task_has_no_tool_hook_and_no_searches() -> None:
    meter = RecordingMeter()
    provider = _scripted(_with_tool_calls(2))

    await _run(_executor(provider), meter)

    assert provider.requests[0].allow_web_search is False
    assert provider.requests[0].on_tool_use is None
    assert provider.tool_calls == []  # type: ignore[attr-defined]
    assert meter.searches == []


# --- the fake's tool calls ---------------------------------------------------------------------


async def test_fake_honours_on_tool_use_false() -> None:
    asked: list[str] = []

    async def first_only(tool: str) -> bool:
        asked.append(tool)
        return len(asked) == 1

    provider = FakeAIProvider()
    provider.script("fictional_task", _with_tool_calls(3))
    request = make_request(allow_web_search=True, on_tool_use=first_only)

    response = await provider.run(request, NotCancelled())

    assert asked == [WEB_SEARCH_TOOL] * 3, "each tool call asks the hook on its own"
    assert provider.tool_calls == [  # type: ignore[attr-defined]
        (WEB_SEARCH_TOOL, True),
        (WEB_SEARCH_TOOL, False),
        (WEB_SEARCH_TOOL, False),
    ]
    assert dict(response.data) == OK_DATA, "a refused tool call still ends in an answer"


async def test_fake_makes_no_tool_calls_when_web_search_is_not_allowed() -> None:
    asked: list[str] = []

    async def allow(tool: str) -> bool:
        asked.append(tool)
        return True

    provider = FakeAIProvider()
    provider.script("fictional_task", _with_tool_calls(2))

    await provider.run(make_request(allow_web_search=False, on_tool_use=allow), NotCancelled())

    assert asked == [] and provider.tool_calls == []  # type: ignore[attr-defined]


async def test_fake_refuses_tool_calls_without_a_hook() -> None:
    """No budget hook means no search: the fake never treats a missing hook as consent."""
    provider = FakeAIProvider()
    provider.script("fictional_task", _with_tool_calls(1))

    await provider.run(make_request(allow_web_search=True), NotCancelled())

    assert provider.tool_calls == [(WEB_SEARCH_TOOL, False)]  # type: ignore[attr-defined]


async def test_fake_tool_calls_are_kept_in_app_mode_without_requests() -> None:
    """``tool_calls`` holds tool names and booleans only, so app mode may keep it."""

    async def allow(tool: str) -> bool:
        return True

    provider = FakeAIProvider(app_mode=True)
    provider.script("fictional_task", _with_tool_calls(1))

    await provider.run(make_request(allow_web_search=True, on_tool_use=allow), NotCancelled())

    assert provider.requests == []
    assert provider.tool_calls == [(WEB_SEARCH_TOOL, True)]  # type: ignore[attr-defined]


def test_scripted_tool_calls_must_not_be_negative() -> None:
    assert ScriptedResponse(data=OK_DATA).tool_calls == 0  # type: ignore[attr-defined]
    with pytest.raises(ValueError):
        ScriptedResponse(data=OK_DATA, tool_calls=-1)  # type: ignore[call-arg]


# --- typing ------------------------------------------------------------------------------------

CONFORMANCE = """
from collections.abc import Mapping

from app.domain.settings import AIProviderSettings, Tier
from app.modules.ai import (
    AITaskRunner,
    ProviderUsageSnapshot,
    TaskResult,
    TokenUsage,
    UsageKind,
    UsageMeter,
)
from app.modules.ai.executor import TaskExecutor
from app.modules.ai.prompts import PromptLibrary
from app.modules.ai.providers.fake import FakeAIProvider, ScriptedResponse


class Memory:
    def reserve(
        self, *, tier: str, task_type: str, task_id: str | None, kind: UsageKind
    ) -> str:
        return "record-1"

    def complete(self, record_id: str, *, success: bool, tokens: TokenUsage | None) -> None:
        return None

    def snapshot(self, snapshot: ProviderUsageSnapshot) -> None:
        return None


meter: UsageMeter = Memory()
scripted = ScriptedResponse(data={"answer": "fictional"}, tool_calls=2)
calls: list[tuple[str, bool]] = FakeAIProvider().tool_calls


def build(library: PromptLibrary, defaults: Mapping[Tier, str]) -> AITaskRunner:
    return TaskExecutor(
        provider=FakeAIProvider(),
        prompts=library,
        settings=AIProviderSettings,
        provider_defaults=defaults,
    )


async def use(runner: AITaskRunner) -> TaskResult:
    class Token:
        cancelled = False

    await runner.run_task("example_task", {"x": 1}, Token(), meter=meter)
    fast: Tier = "fast"
    return await runner.run_task(
        "example_task", {"x": 1}, Token(), meter=meter, tier=fast, task_id="task-1"
    )
"""


def test_usage_meter_and_metered_runner_type_check(tmp_path: Path) -> None:
    sample = tmp_path / "conformance.py"
    sample.write_text(CONFORMANCE, encoding="utf-8")
    # Fixed argv to the venv's own interpreter; no shell, no untrusted input.
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--no-incremental",
            "--cache-dir",
            str(tmp_path / ".mypy_cache"),
            str(sample),
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "MYPYPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
