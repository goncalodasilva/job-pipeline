"""AC-M6 (live, opt-in, run by the user; never in CI): the real service accepts the five M6 task
schemas (M6.12; ADR-044 item 10).

Run only with ``JOBPIPELINE_LIVE_TESTS=1`` and ``-m live``; the credential comes only from
``JOBPIPELINE_LIVE_CLAUDE_OAUTH_TOKEN`` (see ``tests/live/support.py``). Without it the test fails,
naming the variable, as ``test_claude_live.py`` does.

The five tasks are the ones M6 added to ``TASK_SPECS``: ``gap_check``, ``propose_angles``,
``draft_cv_template``, ``tailor_cv`` and ``draft_cover_letter``. (Claim resolution has no AI task:
code resolves a flagged claim from the person's choice, so it has no schema of its own.) Each
request is built by the app itself, exactly as a service builds it, from fictional facts and a
fictional job, by running the real flow against the fake provider in a temporary folder and
keeping the request it sent. That request (system text with the code-owned honesty notice, the
delimited user text and the output schema) is then sent once to the real provider, and the answer
must be accepted: no schema refusal, no invalid output, and the data must satisfy the schema. The
content of the answers is not judged here (fixtures and the checker do that); only that the real
service takes the schema and answers in its shape. One call per task, no tools, no personal
details (none are saved in this folder).

The credential is never printed; as in the other live test, an unexpected exception from a call
is reduced to its type and pytest's log handlers redact the credential.
"""

import asyncio
import os
from dataclasses import replace
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
from tests.export_pages import exporting_app
from tests.fakes.ai import NotCancelled
from tests.integration.test_m6_cross_cutting import DRIVERS
from tests.live.support import TOKEN_VARIABLE, live_wiring, read_credential, without_error_details

pytestmark = pytest.mark.live

M6_TASKS = (
    "gap_check",
    "propose_angles",
    "draft_cv_template",
    "tailor_cv",
    "draft_cover_letter",
)


def built_request(folder: Path, task: str) -> AIRequest:
    """The request the app builds for ``task`` from fictional data (the fake provider answers it
    in a temporary folder; nothing here touches the network)."""
    with exporting_app(folder) as desk:
        DRIVERS[task](desk)
        sent = [request for request in desk.spy.requests if request.task == task]
        assert sent, f"the app made no {task} request"
        return sent[0]


def _real_model(request: AIRequest) -> str:
    """The real model for the tier the app asked for (the fake names its tier in the model)."""
    return CLAUDE_THOROUGH_DEFAULT if "thorough" in request.model else CLAUDE_FAST_DEFAULT


async def test_m6_schemas_accepted_by_real_service(
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
    requests: dict[str, AIRequest] = {}
    for task in M6_TASKS:
        requests[task] = await asyncio.to_thread(built_request, live_tmp / f"fake-{task}", task)

    wiring, _backend = live_wiring("subscription_token", credential, live_log_redactor)
    data_dir = live_tmp / "data"
    clear_ai_temp(data_dir)
    settings = AIProviderSettings(
        kind="claude_subscription_token", saved_sign_ins=("claude_subscription_token",)
    )
    provider = wiring.ai_provider(settings, data_dir=data_dir)
    assert isinstance(provider, ClaudeSdkProvider)

    refused: list[str] = []
    for task, request in requests.items():
        live = replace(request, model=_real_model(request), on_usage=None, on_tool_use=None)
        assert live.allow_web_search is False, task
        response = await without_error_details(provider.run(live, NotCancelled()))
        problems = sorted(
            "/".join(str(part) for part in error.absolute_path) or "(root)"
            for error in Draft202012Validator(request.output_schema).iter_errors(response.data)
        )
        record_property(f"{task}_model_reported", response.model)
        if problems:
            refused.append(f"{task}: data does not satisfy the schema at {problems[:5]}")
    assert not refused, "the real service answered outside the schema:\n" + "\n".join(refused)
