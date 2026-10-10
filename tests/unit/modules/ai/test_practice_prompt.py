"""The practice prompt, its schema and its fixtures (M2.17; ADR-076 item 2; add-ai-task step 7).

The practice task is a fake-only vehicle for the task runner: it generates nothing that
represents the user, so it carries no fact ids. These tests pin the rendered text (a golden
copy, changed only together with the prompt's ``version``), keep the schema strict, tie the
fixtures to the schema and to the fake's script, and keep the task out of ``TASK_SPECS``.
"""

import json
from pathlib import Path
from typing import Any

import jsonschema

from app.modules.ai.prompts import UNTRUSTED_NOTICE, PromptLibrary, render_prompt
from app.modules.ai.providers.fake import PRACTICE_TASK, practice_responses
from app.modules.ai.tasks import PRACTICE_SPEC, TASK_SPECS
from tests.fakes.ai import load_ai_fixture

REPO_ROOT = Path(__file__).resolve().parents[4]
REPO_PROMPTS = REPO_ROOT / "prompts"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "ai" / PRACTICE_TASK

GOLDEN_VERSION = "1"
GOLDEN_SYSTEM = (
    "You take part in a practice run that checks an app's background task system. "
    "There is nothing\n"
    "to analyze and nothing to write about. Respond with JSON only, matching the provided schema."
)
GOLDEN_USER = (
    "This is step 2 of 3 of the practice run. Confirm the step as JSON matching\n"
    'the schema, with "done" set to true.'
)
GOLDEN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["done"],
    "properties": {"done": {"type": "boolean"}},
}


def _fixture(name: str) -> dict[str, Any]:
    raw: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return raw


def test_practice_prompt_golden_render() -> None:
    template = PromptLibrary(REPO_PROMPTS).load(PRACTICE_TASK)

    rendered = render_prompt(template, {"step": 2, "steps": 3})

    assert rendered.prompt_id == PRACTICE_TASK
    assert rendered.prompt_version == GOLDEN_VERSION, "bump the golden copy with the version"
    assert rendered.system.strip() == GOLDEN_SYSTEM
    assert rendered.user.strip() == GOLDEN_USER


def test_practice_prompt_has_number_inputs_only_and_nothing_untrusted() -> None:
    template = PromptLibrary(REPO_PROMPTS).load(PRACTICE_TASK)

    assert template.id == PRACTICE_TASK
    assert template.default_tier == "fast"
    assert template.inputs == ("step", "steps")
    assert template.untrusted_inputs == ()
    rendered = render_prompt(template, {"step": 1, "steps": 2})
    assert UNTRUSTED_NOTICE not in rendered.system
    assert "untrusted_document" not in rendered.system + rendered.user


def test_practice_schema_is_strict_and_minimal() -> None:
    schema = dict(PromptLibrary(REPO_PROMPTS).load(PRACTICE_TASK).output_schema)

    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema == GOLDEN_SCHEMA
    validator = jsonschema.Draft202012Validator(schema)
    assert not validator.is_valid({})
    assert not validator.is_valid({"done": "true"})
    assert not validator.is_valid({"done": True, "text": "fictional"})


def test_practice_fixtures_match_the_schema_and_the_fakes_script() -> None:
    schema = dict(PromptLibrary(REPO_PROMPTS).load(PRACTICE_TASK).output_schema)
    ok = load_ai_fixture(PRACTICE_TASK, "ok")
    malformed = load_ai_fixture(PRACTICE_TASK, "malformed")

    assert sorted(path.name for path in FIXTURES.iterdir()) == ["malformed.json", "ok.json"]
    assert set(_fixture("ok")) == {"data"}
    assert set(_fixture("malformed")) == {"raw_text"}
    jsonschema.Draft202012Validator(schema).validate(dict(ok.data or {}))
    assert malformed.data is None and malformed.raw_text is not None
    try:
        json.loads(malformed.raw_text)
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("malformed.json must not hold JSON text")
    # The fake's built-in script answers exactly what the fixtures hold, so they can't drift.
    (scripted_ok,) = practice_responses("ok", delay_seconds=0.0)
    bad, bad_retry, then_ok = practice_responses("invalid_output", delay_seconds=0.0)
    assert scripted_ok == ok
    assert bad == bad_retry == malformed
    assert then_ok == ok


def test_practice_spec_stays_out_of_the_real_task_list_and_has_no_tools() -> None:
    assert PRACTICE_SPEC.name == PRACTICE_TASK
    assert PRACTICE_SPEC.name not in TASK_SPECS, "wiring adds it only for an allowed fake"
    assert PRACTICE_SPEC.kind != "generation", "generation tasks must cite fact ids"
    assert PRACTICE_SPEC.allow_web_search is False
