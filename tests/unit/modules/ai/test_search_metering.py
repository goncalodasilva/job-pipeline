"""Per-task search limits and accounting for searches already performed."""

import asyncio

from app.modules.ai import WEB_SEARCH_TOOL
from app.modules.ai.metering import CallMeter
from tests.fakes.usage import RecordingMeter


async def test_task_search_cap_is_enforced_even_for_concurrent_hooks() -> None:
    meter = RecordingMeter()
    call = CallMeter(
        meter, tier="thorough", task_type="fictional_web", task_id=None, max_searches=5
    )
    await call.reserve()
    allowed = await asyncio.gather(*(call.on_tool_use(WEB_SEARCH_TOOL) for _ in range(12)))
    assert allowed.count(True) == 5
    await call.finish(success=True, tokens=None)
    assert len(meter.searches) == 5
    assert all(r.completed for r in meter.records)


async def test_reported_search_count_above_hook_count_reserved_after_call() -> None:
    meter = RecordingMeter(block_searches_after=1)
    call = CallMeter(
        meter, tier="thorough", task_type="fictional_web", task_id="T1", max_searches=5
    )
    await call.reserve()
    assert await call.on_tool_use(WEB_SEARCH_TOOL)
    await call.finish(success=True, tokens=None, reported_searches=3)
    assert len(meter.searches) == 3
    assert all(r.completed and r.success for r in meter.records)
    assert meter.refused == []


async def test_reported_count_does_not_double_count_hooks_or_cross_retry() -> None:
    meter = RecordingMeter()
    call = CallMeter(
        meter, tier="thorough", task_type="fictional_web", task_id=None, max_searches=1
    )
    for _ in range(2):
        await call.reserve()
        assert await call.on_tool_use(WEB_SEARCH_TOOL)
        await call.finish(success=True, tokens=None, reported_searches=1)
    assert len(meter.requests) == 2 and len(meter.searches) == 2


async def test_task_spec_search_limit_is_passed_to_executor_meter() -> None:
    from app.modules.ai.tasks import TaskSpec
    from tests.unit.modules.ai.test_executor_usage import (
        TASK,
        _executor,
        _run,
        _scripted,
        _with_tool_calls,
    )

    meter = RecordingMeter()
    provider = _scripted(_with_tool_calls(8))
    executor = _executor(
        provider,
        specs={TASK: TaskSpec(TASK, kind="summary", allow_web_search=True, max_searches=5)},
    )
    await _run(executor, meter)
    assert len(meter.searches) == 5
    assert provider.requests[0].max_searches == 5
    assert [allowed for _, allowed in provider.tool_calls] == [True] * 5 + [False] * 3
