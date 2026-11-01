"""M8.1: real SDK hook dispatch over the offline replay transport."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from app.modules.ai import WEB_SEARCH_TOOL
from app.modules.ai.providers import claude_sdk
from app.modules.ai.providers.claude_sdk import ClaudeSdkProvider
from app.modules.secrets.providers.memory import MemorySecretStore
from tests.fakes.ai import NotCancelled, make_request
from tests.fakes.claude_query import replaying, success_frames


def hook(event: str, *, tool: str = "WebSearch", response: object = None) -> dict:
    return {"__hook_event__": event, "tool_name": tool, "tool_response": response}


def provider(query, tmp_path: Path) -> ClaudeSdkProvider:
    return ClaudeSdkProvider(
        secrets=MemorySecretStore(),
        mode="existing_login",
        client_factory=query,
        cli_path=lambda: tmp_path / "fictional-cli",
        environ={},
        temp_root=tmp_path,
    )


def decisions(query) -> list[str]:
    return [
        f["response"]["response"]["hookSpecificOutput"]["permissionDecision"]
        for f in query.last.transport.written
        if f.get("type") == "control_response"
        and "hookSpecificOutput" in f.get("response", {}).get("response", {})
    ]


async def test_claude_web_search_asks_hook_before_each_search(tmp_path: Path) -> None:
    asked = []

    async def allow(tool):
        asked.append(tool)
        return True

    query = replaying([hook("PreToolUse"), hook("PreToolUse"), *success_frames({"answer": "ok"})])
    await provider(query, tmp_path).run(
        make_request(allow_web_search=True, on_tool_use=allow), NotCancelled()
    )
    assert asked == [WEB_SEARCH_TOOL, WEB_SEARCH_TOOL]
    assert decisions(query) == ["allow", "allow"]
    assert query.last.options.max_turns == 7


async def test_claude_web_search_denied_when_hook_refuses(tmp_path: Path) -> None:
    async def refuse(tool):
        return False

    query = replaying([hook("PreToolUse"), *success_frames({"answer": "ok"})])
    await provider(query, tmp_path).run(
        make_request(allow_web_search=True, on_tool_use=refuse), NotCancelled()
    )
    assert decisions(query) == ["deny"]


@pytest.mark.parametrize("failure", ["hook_error", "hook_timeout", "other_tool", "missing_hook"])
async def test_claude_hook_fails_closed(tmp_path: Path, monkeypatch, failure: str) -> None:
    monkeypatch.setattr(claude_sdk, "HOOK_TIMEOUT_SECONDS", 0.01, raising=False)

    async def callback(tool):
        if failure == "hook_error":
            raise RuntimeError("fictional private text")
        if failure == "hook_timeout":
            await asyncio.Event().wait()
        return True

    query = replaying(
        [
            hook("PreToolUse", tool="Read" if failure == "other_tool" else "WebSearch"),
            *success_frames({"answer": "ok"}),
        ]
    )
    await provider(query, tmp_path).run(
        make_request(
            allow_web_search=True,
            on_tool_use=None if failure == "missing_hook" else callback,
        ),
        NotCancelled(),
    )
    assert decisions(query) == ["deny"]


async def test_claude_plain_requests_register_no_hooks_no_tools_two_turns(tmp_path: Path) -> None:
    query = replaying(success_frames({"answer": "ok"}))
    await provider(query, tmp_path).run(make_request(), NotCancelled())
    options = query.last.options
    assert options.tools == [] and options.allowed_tools == []
    assert options.hooks is None and options.max_turns == 2


async def test_claude_web_options_turns_cover_search_cap(tmp_path: Path) -> None:
    query = replaying(success_frames({"answer": "ok"}))
    request = replace(make_request(allow_web_search=True), max_searches=3)
    await provider(query, tmp_path).run(request, NotCancelled())
    assert query.last.options.max_turns == 5


async def test_claude_collects_searched_urls_bounded_http_only(tmp_path: Path) -> None:
    urls = ["https://example.test/" + str(i) for i in range(60)]
    response = {
        "results": [
            {"url": u}
            for u in [
                "file:///private",
                "javascript:alert(1)",
                "https://example.test/" + "a" * 2000,
                *urls,
            ]
        ]
    }
    query = replaying(
        [
            hook("PreToolUse"),
            hook("PostToolUse", response=response),
            *success_frames(
                {"answer": "ok"}, usage={"server_tool_use": {"web_search_requests": 2}}
            ),
        ]
    )

    async def allow(tool):
        return True

    result = await provider(query, tmp_path).run(
        make_request(allow_web_search=True, on_tool_use=allow), NotCancelled()
    )
    assert result.searched_urls == tuple(urls[:50])
    assert result.search_count == 2


async def test_unapproved_post_tool_result_cannot_supply_sources(tmp_path: Path) -> None:
    query = replaying(
        [
            hook("PostToolUse", response={"url": "https://example.test/invented"}),
            *success_frames({"answer": "ok"}),
        ]
    )
    result = await provider(query, tmp_path).run(
        make_request(allow_web_search=True), NotCancelled()
    )
    assert result.searched_urls == ()


@pytest.mark.parametrize("output,error", [([], False), ({"answer": "ok"}, True)])
async def test_reported_search_usage_survives_invalid_and_error_results(
    tmp_path: Path, output, error
) -> None:
    from app.domain.errors import InvalidModelOutput
    from app.modules.ai.metering import CallMeter
    from tests.fakes.usage import RecordingMeter

    frames = success_frames(output, usage={"server_tool_use": {"web_search_requests": 3}})
    if error:
        frames[-1]["is_error"] = True
        frames[-1]["subtype"] = "error_max_turns"
    query = replaying([hook("PreToolUse"), *frames])
    meter = RecordingMeter()
    call = CallMeter(meter, tier="thorough", task_type="fictional_web", task_id=None)
    for _ in range(2):
        await call.reserve()
        request = replace(
            make_request(allow_web_search=True, on_tool_use=call.on_tool_use),
            on_search_usage=call.on_search_usage,
        )
        with pytest.raises(InvalidModelOutput):
            await provider(query, tmp_path).run(request, NotCancelled())
        await call.finish(success=False, tokens=None)
    assert len(meter.searches) == 6
    assert all(r.completed and not r.success for r in meter.records)


async def test_reported_usage_callback_and_response_count_do_not_double_count(
    tmp_path: Path,
) -> None:
    from app.modules.ai.metering import CallMeter
    from tests.fakes.usage import RecordingMeter

    query = replaying(
        success_frames({"answer": "ok"}, usage={"server_tool_use": {"web_search_requests": 3}})
    )
    meter = RecordingMeter()
    call = CallMeter(meter, tier="thorough", task_type="fictional_web", task_id=None)
    await call.reserve()
    request = replace(make_request(allow_web_search=True), on_search_usage=call.on_search_usage)
    response = await provider(query, tmp_path).run(request, NotCancelled())
    await call.finish(success=True, tokens=None, reported_searches=response.search_count)
    assert len(meter.searches) == 3


@pytest.mark.parametrize("output,error", [([], False), ({"answer": "ok"}, True)])
async def test_executor_counts_searches_on_both_failed_attempts(
    tmp_path: Path, output, error
) -> None:
    from app.domain.errors import InvalidModelOutput
    from tests.fakes.usage import RecordingMeter
    from tests.unit.modules.ai.test_executor_usage import _run, _web_executor

    frames = success_frames(output, usage={"server_tool_use": {"web_search_requests": 3}})
    if error:
        frames[-1]["is_error"] = True
        frames[-1]["subtype"] = "error_max_turns"
    query = replaying([hook("PreToolUse"), *frames])
    meter = RecordingMeter()
    with pytest.raises(InvalidModelOutput):
        await _run(_web_executor(provider(query, tmp_path)), meter)
    assert len(query.calls) == 2
    assert len(meter.requests) == 2 and len(meter.searches) == 6
    assert all(r.completed and not r.success for r in meter.records)


async def test_successful_result_urls_reach_task_result(tmp_path: Path) -> None:
    from tests.fakes.usage import RecordingMeter
    from tests.unit.modules.ai.test_executor_usage import _run, _web_executor

    url = "https://example.test/fictional-interviews"
    # The nested shape comes from the bundled CLI WebSearch tool's declared output.
    search_response = {
        "query": "fictional interviews",
        "results": [
            {
                "tool_use_id": "fictional-search",
                "content": [{"title": "Fictional interviews", "url": url}],
            }
        ],
        "durationSeconds": 0,
        "searchCount": 1,
    }
    query = replaying(
        [
            hook("PreToolUse"),
            hook("PostToolUse", response=search_response),
            *success_frames({"answer": "ok"}),
        ]
    )
    result = await _run(_web_executor(provider(query, tmp_path)), RecordingMeter())
    assert result.searched_urls == (url,)


async def test_internal_structured_output_allowed_without_search_charge(tmp_path: Path) -> None:
    asked = []

    async def refuse_search(tool):
        asked.append(tool)
        return False

    query = replaying(
        [
            hook("PreToolUse", tool="StructuredOutput"),
            hook("PreToolUse", tool="WebSearch"),
            hook("PreToolUse", tool="Read"),
            hook("PreToolUse", tool="StructuredOutputExtra"),
            *success_frames({"answer": "ok"}),
        ]
    )
    await provider(query, tmp_path).run(
        make_request(allow_web_search=True, on_tool_use=refuse_search), NotCancelled()
    )
    assert decisions(query) == ["allow", "deny", "deny", "deny"]
    assert asked == [WEB_SEARCH_TOOL]
