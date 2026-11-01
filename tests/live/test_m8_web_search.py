"""User-run M8.1 hook spike: one real search with fictional, public-only inputs.

Opt in with JOBPIPELINE_LIVE_TESTS=1 and -m live. Never run by the agent or CI.
"""

import os
from pathlib import Path

import pytest

from app.cli import SDK_SKIP_VERSION_CHECK
from app.domain.redaction import Redactor
from app.domain.settings import AIProviderSettings
from app.modules.ai import AIRequest
from app.modules.ai.metering import CallMeter
from app.wiring import clear_ai_temp
from tests.fakes.ai import NotCancelled
from tests.fakes.usage import RecordingMeter
from tests.live.support import TOKEN_VARIABLE, live_wiring, read_credential, without_error_details

pytestmark = pytest.mark.live


async def test_m8_web_search_hook_counts_real_searches(
    live_tmp: Path, live_log_redactor: Redactor, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential = read_credential(os.environ, TOKEN_VARIABLE)
    if credential is None:
        pytest.fail(f"Set {TOKEN_VARIABLE} to run this live test.", pytrace=False)
    monkeypatch.setenv(SDK_SKIP_VERSION_CHECK, "1")
    wiring, _ = live_wiring("subscription_token", credential, live_log_redactor)
    clear_ai_temp(live_tmp)
    provider = wiring.ai_provider(
        AIProviderSettings(kind="claude_subscription_token"), data_dir=live_tmp
    )
    meter = RecordingMeter()
    call = CallMeter(
        meter, tier="thorough", task_type="interview_web_questions", task_id=None, max_searches=1
    )
    request = AIRequest(
        task="interview_web_questions",
        prompt_id="m8_hook_spike",
        prompt_version="1",
        model="sonnet",
        system="Use WebSearch exactly once, then return the requested JSON.",
        user=(
            "Find the official Python programming language website using one web search. "
            "Return ok true."
        ),
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
        },
        allow_web_search=True,
        max_searches=1,
        on_tool_use=call.on_tool_use,
        on_search_usage=call.on_search_usage,
    )
    await call.reserve()
    try:
        response = await without_error_details(provider.run(request, NotCancelled()))
    except BaseException:
        await call.finish(success=False, tokens=None)
        raise
    await call.finish(success=True, tokens=response.usage, reported_searches=response.search_count)
    assert len(meter.searches) >= 1, "The real CLI must dispatch the budget hook."
    assert len(meter.searches) == max(1, response.search_count)
    assert response.searched_urls, "The real CLI must dispatch searched sources."
    assert response.data.get("ok") is True
    assert all(record.completed for record in meter.records)
