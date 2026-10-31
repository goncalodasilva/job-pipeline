"""AC-M7.2 (live, opt-in, run by the user; never in CI): the real service accepts both M7 task
schemas, and the questions it writes for five fictional items are open (M7.10; ADR-044 item 10).

Run only with ``JOBPIPELINE_LIVE_TESTS=1`` and ``-m live``; the credential comes only from
``JOBPIPELINE_LIVE_CLAUDE_OAUTH_TOKEN`` (see ``tests/live/support.py``). Without it the test fails,
naming the variable, as ``test_claude_live.py`` and ``test_m6_live.py`` do.

The two tasks are ``enrichment_question`` and ``structure_answer``. The requests are built by the
app itself, exactly as a service builds them, from fictional data (``tests/session_live_inputs.py``
runs two sessions against the fake provider in a temporary folder and keeps the requests it
sent): five question requests, one for each of five fictional items (two capture notes, two
requirements one saved job asks for, one story with a missing result), and the one structuring
request for the one fictional answer, which is about the first capture note (a spring stocktake).
What each request holds, as built:

- the stocktake note's request carries an earlier wording (the person was "not sure" about it in
  an earlier session, 22 days before), so ``earlier_wordings`` is not empty there;
- the requests after the first carry the session so far, which holds only the canned question the
  fake asked and the person skipped. The answer is not in any later question request: once an item
  is answered it leaves the session history. The offline control asserts all of this, and that the
  five items are two capture notes, two clusters and one quality hint;
- the structuring request holds the question, the answer, the note and the known skills.

Each request (system text, the delimited user text and the output schema) is sent once to the real
provider, the structuring request first, and the answer must satisfy the schema. A call that fails
does not stop the others: every failure is collected and named by its task and item number, with
the exception type only, never its text.

Then the results are judged, which the schema cannot do (AC-M7.2). For each of the five questions,
it must pass ``question_problems`` against what the app lets it say (the item's own wording, the
facts it may use, a skill topic, the person's earlier answers), and the why line must carry no
claim of its own either: the same rule the app applies before it keeps a question, so a pass means
the app would have kept the real question and not replaced it with its code-owned fallback. For the
two requirement items, a question that takes for granted that the person has done what the job
asks ("How did you ...", "You ...") is also reported, as ``presumes`` (this heuristic is only
here; the app does not replace such a question). The structuring answer is run through the app's
own ``resolve_structured_answer`` with the answer and the note, and the draft must come out with
no flags, no skill the answer does not name, no role, and no new claim in its text (details the
model lists as ``added_details`` are shown to the person by design and not judged). One call per
request, no tools, six calls in all, no personal details.

The credential is never printed; an unexpected exception from a call is reduced to its type and
pytest's log handlers redact the credential. A failure names the task, the item number and the
problem codes (``yes_no``, ``tag``, ``form``, ``new_claims``, ``presumes``, ``flag_*``), never the
text of a question or a draft.
"""

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
from tests.fakes.ai import NotCancelled
from tests.live.support import TOKEN_VARIABLE, live_wiring, read_credential, without_error_details
from tests.session_live_inputs import (
    QUESTION_COUNT,
    BuiltInputs,
    built_inputs,
    draft_problems,
    open_question_problems,
    presumes,
)

pytestmark = pytest.mark.live

STRUCTURE = "structure_answer"
QUESTION = "enrichment_question"


def real_model(request: AIRequest) -> str:
    """The real model for the tier the app asked for (the fake names its tier in the model)."""
    return CLAUDE_THOROUGH_DEFAULT if "thorough" in request.model else CLAUDE_FAST_DEFAULT


def schema_problems(request: AIRequest, data: Any) -> list[str]:
    return sorted(
        "/".join(str(part) for part in error.absolute_path) or "(root)"
        for error in Draft202012Validator(request.output_schema).iter_errors(data)
    )


def failure_note(error: AssertionError) -> str:
    """What may be said of a failed call: ``without_error_details`` already reduced it to its
    type; anything else a call asserts is reduced to that fact."""
    text = str(error)
    return text if text.startswith("the call raised ") else "the call failed an assertion"


async def test_m7_schemas_accepted_and_questions_open(
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
    built: BuiltInputs = await _build(live_tmp / "fake")
    assert len(built.questions) == QUESTION_COUNT == len(built.allowed) == len(built.kinds)

    wiring, _backend = live_wiring("subscription_token", credential, live_log_redactor)
    data_dir = live_tmp / "data"
    clear_ai_temp(data_dir)
    settings = AIProviderSettings(
        kind="claude_subscription_token", saved_sign_ins=("claude_subscription_token",)
    )
    provider = wiring.ai_provider(settings, data_dir=data_dir)
    assert isinstance(provider, ClaudeSdkProvider)

    failures: list[str] = []  # a call that did not answer
    refused: list[str] = []  # an answer outside the schema
    replaced: list[str] = []  # a question the app would not keep
    presuming: list[str] = []  # a requirement question that assumes the person did it
    unclean: list[str] = []  # a draft that is not honest to the answer

    # The structuring call first: it is the one with the most to judge.
    structure = replace(
        built.structure, model=real_model(built.structure), on_usage=None, on_tool_use=None
    )
    assert structure.allow_web_search is False, STRUCTURE
    try:
        answer = await without_error_details(provider.run(structure, NotCancelled()))
    except AssertionError as error:
        failures.append(f"{STRUCTURE} 1: {failure_note(error)}")
    else:
        record_property("structure_answer_model_reported", answer.model)
        problems = schema_problems(built.structure, answer.data)
        if problems:
            refused.append(f"{STRUCTURE} 1: not the schema at {problems[:5]}")
        elif found := draft_problems(built, answer.data):
            unclean.append(f"{STRUCTURE} 1: the draft is not clean ({found})")

    for number, (request, allowed, kind) in enumerate(
        zip(built.questions, built.allowed, built.kinds, strict=True), 1
    ):
        live = replace(request, model=real_model(request), on_usage=None, on_tool_use=None)
        assert live.allow_web_search is False, QUESTION
        try:
            response = await without_error_details(provider.run(live, NotCancelled()))
        except AssertionError as error:
            failures.append(f"{QUESTION} {number}: {failure_note(error)}")
            continue
        record_property(f"question_{number}_model_reported", response.model)
        problems = schema_problems(request, response.data)
        if problems:
            refused.append(f"{QUESTION} {number}: not the schema at {problems[:5]}")
            continue
        question, why = str(response.data["question"]).strip(), str(response.data["why"]).strip()
        found = open_question_problems(question, why, allowed)
        if found:
            replaced.append(f"{QUESTION} {number}: the app would replace this question ({found})")
        if kind == "cluster" and presumes(question):
            presuming.append(f"{QUESTION} {number}: presumes (a requirement, not a claim)")

    report = [
        f"{title}:\n" + "\n".join(lines)
        for title, lines in (
            ("a call did not answer", failures),
            ("the real service answered outside the schema", refused),
            ("the real draft was not honest to the answer", unclean),
            ("generated questions were not open (AC-M7.2)", replaced),
            ("requirement questions presumed an answer", presuming),
        )
        if lines
    ]
    assert not report, "\n\n".join(report)


async def _build(folder: Path) -> BuiltInputs:
    import asyncio

    return await asyncio.to_thread(built_inputs, folder)
