"""Usage records when the asyncio task running ``run_task`` is cancelled (M2.16; spec 3.7).

The task runner cancels a running handler with ``asyncio.CancelledError`` (M2.11). A record
that was reserved is completed (unsuccessful) before the cancellation goes on, and so is a
web-search record: the completion is shielded, so even a second cancel can't leave a record
open. Also here: the hook refuses tools other than web search, and a failing completion
never changes the call. The meter is ``tests.fakes.usage.RecordingMeter``; nothing sleeps.
All content is fictional.
"""

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from app.domain.settings import AIProviderSettings
from app.modules.ai import AICapabilities, AIRequest, AIResponse, CancelToken, TokenUsage
from app.modules.ai.executor import TaskExecutor
from app.modules.ai.prompts import PromptLibrary
from app.modules.ai.providers.fake import FAKE_FAST_MODEL, FAKE_THOROUGH_MODEL
from app.modules.ai.tasks import TaskSpec
from tests.fakes.ai import NotCancelled
from tests.fakes.usage import RecordingMeter

REPO_ROOT = Path(__file__).resolve().parents[4]
TASK = "_test_task"
INPUTS: dict[str, object] = {
    "job_text": "Example Ltd is hiring a fictional lighthouse keeper in a fictional town.",
    "preferences": {"target_roles": ["Lighthouse Keeper"], "locations": ["Fictionville"]},
}
WAIT = 5.0  # seconds; an upper bound for a thread hand-over, never reached when green


class Hanging:
    """A provider call that never answers; optionally asks for web searches first."""

    name = "fake"
    capabilities = AICapabilities()

    def __init__(self, searches: int = 0) -> None:
        self.started = asyncio.Event()
        self.calls = 0
        self._searches = searches

    async def run(self, request: AIRequest, cancel: CancelToken) -> AIResponse:
        self.calls += 1
        for _ in range(self._searches):
            assert request.on_tool_use is not None
            assert await request.on_tool_use("web_search") is True
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover


class GatedMeter(RecordingMeter):
    """``gate`` names the method that waits in its worker thread until ``release`` is set."""

    def __init__(self, gate: str) -> None:
        super().__init__()
        self._gate = gate
        self.entered = threading.Event()
        self.release = threading.Event()

    def _wait(self, name: str) -> None:
        if name == self._gate:
            self.entered.set()
            assert self.release.wait(WAIT)

    def reserve(self, **kwargs: Any) -> str:
        record_id = super().reserve(**kwargs)
        self._wait("reserve")
        return record_id

    def complete(self, record_id: str, **kwargs: Any) -> None:
        self._wait("complete")
        super().complete(record_id, **kwargs)


def _start(provider: Any, meter: RecordingMeter, *, web: bool = False) -> asyncio.Task[Any]:
    executor = TaskExecutor(
        provider=provider,
        prompts=PromptLibrary(REPO_ROOT / "tests" / "fixtures" / "prompts"),
        settings=AIProviderSettings,
        provider_defaults={"fast": FAKE_FAST_MODEL, "thorough": FAKE_THOROUGH_MODEL},
        specs={TASK: TaskSpec(TASK, "summary", allow_web_search=web)},
    )
    return asyncio.create_task(executor.run_task(TASK, INPUTS, NotCancelled(), meter=meter))


async def test_asyncio_cancel_completes_the_record_before_propagating() -> None:
    meter = RecordingMeter()
    provider = Hanging()
    task = _start(provider, meter)
    await provider.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    (record,) = meter.requests
    assert (record.completed, record.success, record.tokens) == (True, False, None)
    assert record.completions == 1
    assert meter.events == ["reserve:request", "complete:request"]
    assert provider.calls == 1, "a cancelled call is not retried"


async def test_asyncio_cancel_completes_the_web_search_records_too() -> None:
    meter = RecordingMeter()
    provider = Hanging(searches=2)
    task = _start(provider, meter, web=True)
    await provider.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [(s.completed, s.success, s.completions) for s in meter.searches] == [
        (True, False, 1),
        (True, False, 1),
    ]
    assert [(r.completed, r.success) for r in meter.requests] == [(True, False)]


async def test_completion_is_shielded_from_a_second_cancel() -> None:
    """The completion is already running in its thread when a second cancel arrives: the
    task ends only once the record is completed."""
    meter = GatedMeter("complete")
    provider = Hanging()
    task = _start(provider, meter)
    await provider.started.wait()

    task.cancel()
    assert await asyncio.to_thread(meter.entered.wait, WAIT)
    task.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done(), "the cancellation waits for the completion"
    assert meter.requests[0].completed is False

    meter.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    (record,) = meter.requests
    assert (record.completed, record.success, record.completions) == (True, False, 1)


async def test_cancel_while_reserving_leaves_no_open_record() -> None:
    """Cancelled while the reservation is being written: the record that was written is
    completed, and the provider is never called."""
    meter = GatedMeter("reserve")
    provider = Hanging()
    task = _start(provider, meter)
    assert await asyncio.to_thread(meter.entered.wait, WAIT)

    task.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done(), "the cancellation waits for the reservation"

    meter.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    (record,) = meter.requests
    assert (record.completed, record.success) == (True, False)
    assert provider.calls == 0


async def test_hook_refuses_any_tool_but_web_search() -> None:
    """Fails closed: a tool the budgets don't know is refused and reserves nothing."""
    answers: list[bool] = []

    class AsksForAnotherTool(Hanging):
        async def run(self, request: AIRequest, cancel: CancelToken) -> AIResponse:
            assert request.on_tool_use is not None
            answers.append(await request.on_tool_use("fictional_shell"))
            return AIResponse(
                raw_text="{}", data={"answer": "A fictional answer."}, model="fake-fast", usage=None
            )

    meter = RecordingMeter()

    await _start(AsksForAnotherTool(), meter, web=True)

    assert answers == [False]
    assert meter.searches == [] and meter.refused == []


async def test_failing_completion_never_changes_the_call() -> None:
    class BrokenComplete(RecordingMeter):
        def complete(self, record_id: str, **kwargs: Any) -> None:
            raise RuntimeError("a fictional storage failure")

    class Answers(Hanging):
        async def run(self, request: AIRequest, cancel: CancelToken) -> AIResponse:
            return AIResponse(
                raw_text="{}", data={"answer": "A fictional answer."}, model="fake-fast", usage=None
            )

    meter = BrokenComplete()

    result = await _start(Answers(), meter)

    assert dict(result.data) == {"answer": "A fictional answer."}
    assert [r.completed for r in meter.requests] == [False], "left open, so still counted"


async def test_searches_beyond_the_per_call_maximum_are_refused_before_reserving() -> None:
    """A call can't run away with searches, whatever the budget says (security review)."""
    from app.modules.ai.metering import MAX_SEARCHES_PER_CALL

    answers: list[bool] = []

    class Greedy(Hanging):
        async def run(self, request: AIRequest, cancel: CancelToken) -> AIResponse:
            assert request.on_tool_use is not None
            for _ in range(MAX_SEARCHES_PER_CALL + 3):
                answers.append(await request.on_tool_use("web_search"))
            return AIResponse(
                raw_text="{}", data={"answer": "A fictional answer."}, model="fake-fast", usage=None
            )

    meter = RecordingMeter()

    await _start(Greedy(), meter, web=True)

    assert MAX_SEARCHES_PER_CALL == 20
    assert answers == [True] * MAX_SEARCHES_PER_CALL + [False] * 3
    assert len(meter.searches) == MAX_SEARCHES_PER_CALL
    reserves = [name for name, _ in meter.threads if name == "reserve:search"]
    assert len(reserves) == MAX_SEARCHES_PER_CALL, "the extra ones never reach the meter"
    assert all(search.completed for search in meter.searches)


async def test_unexpected_validation_error_still_completes_the_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provider answered, then checking the answer crashed: counted, tokens kept."""
    from app.modules.ai import executor

    tokens = TokenUsage(input_tokens=30, output_tokens=4)

    class Answers(Hanging):
        async def run(self, request: AIRequest, cancel: CancelToken) -> AIResponse:
            self.calls += 1
            return AIResponse(
                raw_text="{}", data={"answer": "fictional"}, model="fake-fast", usage=tokens
            )

    def crash(*args: object) -> object:
        raise RuntimeError("a fictional crash while validating")

    monkeypatch.setattr(executor, "_checked", crash)
    meter = RecordingMeter()
    provider = Answers()

    with pytest.raises(RuntimeError):
        await _start(provider, meter)

    (record,) = meter.requests
    assert (record.completed, record.success, record.tokens) == (True, False, tokens)
    assert record.completions == 1 and provider.calls == 1


async def test_snapshot_flood_is_capped_and_keeps_the_newest_per_window() -> None:
    """A provider can't queue snapshots without bound; the latest of each window survives."""
    from datetime import UTC, datetime

    from app.modules.ai import ProviderUsageSnapshot
    from app.modules.ai.metering import MAX_SNAPSHOTS_PER_CALL

    def snapshot(window: str, used: int) -> ProviderUsageSnapshot:
        return ProviderUsageSnapshot(
            service="fake",
            window=window,
            unit="requests",
            used=used,
            limit=None,
            utilization=None,
            resets_at=None,
            observed_at=datetime(2026, 2, 20, 15, 0, tzinfo=UTC),
        )

    lone = snapshot("five_hour", 1)  # reported once, early: the newest of its window
    flood = [snapshot("month", used) for used in range(500)]

    class Flooding(Hanging):
        async def run(self, request: AIRequest, cancel: CancelToken) -> AIResponse:
            assert request.on_usage is not None
            request.on_usage(lone)
            for item in flood:
                request.on_usage(item)
            return AIResponse(
                raw_text="{}", data={"answer": "A fictional answer."}, model="fake-fast", usage=None
            )

    meter = RecordingMeter()

    await _start(Flooding(), meter)

    assert MAX_SNAPSHOTS_PER_CALL == 50
    assert len(meter.snapshots) == MAX_SNAPSHOTS_PER_CALL
    assert meter.snapshots[-1] == flood[-1], "the last one reported is kept"
    assert meter.snapshots[0] == lone, "and the only observation of another window"
    assert meter.snapshots[1:] == flood[-49:], "in the order they were reported"
