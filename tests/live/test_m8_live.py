"""AC-M8.1 to AC-M8.3 (live, opt-in, run by the user; never in CI): the real service accepts the six
M8 task schemas (M8.3 part C; ADR-044 item 10, ADR-124).

Run only with ``JOBPIPELINE_LIVE_TESTS=1`` and ``-m live``; the credential comes only from
``JOBPIPELINE_LIVE_CLAUDE_OAUTH_TOKEN`` (see ``tests/live/support.py``). Without it the test fails,
naming the variable, as ``test_m7_live.py`` does.

The six tasks are ``interview_questions``, ``interview_web_questions``, ``answer_feedback``,
``draft_narrative``, ``adapt_answer`` and ``company_brief``. The requests are built by the app
itself, exactly as its services build them, from fictional data (``tests/prep_live_inputs.py``
runs each flow against the fake provider in a temporary folder and keeps the request it sent).
Each request (system text, the delimited user text and the output schema) is sent once to the real
provider with the real model for its tier, with the usage and tool callbacks removed and web search
switched off, so there are six calls and zero searches (a web task is asked for its answer without
the tool; the schema is what is being checked). The real answer must satisfy the task's schema.

A call that fails does not stop the others: every failure is collected and named by its task and,
when the call raised, the exception's type (``failure_note``, as in ``test_m7_live.py``), never by
any text. The credential is never printed.
"""

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from app.cli import SDK_SKIP_VERSION_CHECK
from app.domain.redaction import Redactor
from app.domain.settings import AIProviderSettings
from app.modules.ai import AIRequest
from app.modules.ai.providers.claude_sdk import (
    CLAUDE_FAST_DEFAULT,
    CLAUDE_THOROUGH_DEFAULT,
    ClaudeSdkProvider,
)
from app.wiring import clear_ai_temp
from tests.fakes.ai import NotCancelled
from tests.live.support import TOKEN_VARIABLE, live_wiring, read_credential, without_error_details
from tests.prep_live_inputs import PREP_TASKS, PrepInputs, built_prep_inputs, live_request

pytestmark = pytest.mark.live


def real_model(request: AIRequest) -> str:
    """The real model for the tier the app asked for (the fake names its tier in the model)."""
    return CLAUDE_THOROUGH_DEFAULT if "thorough" in request.model else CLAUDE_FAST_DEFAULT


def schema_problems(request: AIRequest, data: Any) -> list[str]:
    return sorted(
        "/".join(str(part) for part in error.absolute_path) or "(root)"
        for error in Draft202012Validator(request.output_schema).iter_errors(data)
    )


def failure_note(error: AssertionError) -> str:
    """What may be said of a failed call: ``without_error_details`` already reduced an exception
    to its type; anything else a call asserts is reduced to that fact (no text of either)."""
    text = str(error)
    return text if text.startswith("the call raised ") else "the call failed an assertion"


async def test_m8_schemas_accepted_by_real_service(
    live_tmp: Path,
    live_log_redactor: Redactor,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Any,
) -> None:
    credential = read_credential(os.environ, TOKEN_VARIABLE)
    if credential is None:
        pytest.fail(
            f"{TOKEN_VARIABLE} is not set: export a token from `claude setup-token` in it "
            "to run this live test.",
            pytrace=False,
        )
    monkeypatch.setenv(SDK_SKIP_VERSION_CHECK, "1")  # as the CLI sets it (ADR-058)
    built: PrepInputs = await asyncio.to_thread(built_prep_inputs, live_tmp / "fake")
    assert tuple(r.task for r in built.requests) == PREP_TASKS

    wiring, _backend = live_wiring("subscription_token", credential, live_log_redactor)
    data_dir = live_tmp / "data"
    clear_ai_temp(data_dir)
    settings = AIProviderSettings(
        kind="claude_subscription_token", saved_sign_ins=("claude_subscription_token",)
    )
    provider = wiring.ai_provider(settings, data_dir=data_dir)
    assert isinstance(provider, ClaudeSdkProvider)

    failures: list[str] = []  # tasks whose call did not answer, with the type raised
    refused: list[str] = []  # tasks whose answer is outside the schema
    for request in built.requests:
        live = live_request(request, real_model(request))
        assert live.allow_web_search is False, request.task
        assert live.on_tool_use is None and live.on_usage is None, request.task
        try:
            response = await without_error_details(provider.run(live, NotCancelled()))
        except AssertionError as error:
            failures.append(f"{request.task} ({failure_note(error)})")
            continue
        record_property(f"{request.task}_model_reported", response.model)
        record_property(f"{request.task}_searches", response.search_count)
        if schema_problems(request, response.data):
            refused.append(request.task)

    report = [
        f"{title}: {', '.join(tasks)}"
        for title, tasks in (
            ("a call did not answer", failures),
            ("the real service answered outside the schema", refused),
        )
        if tasks
    ]
    assert not report, "\n".join(report)
