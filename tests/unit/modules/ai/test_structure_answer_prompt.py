"""M7.5 closed prompt/schema with fictional sources."""

import json
from pathlib import Path

import jsonschema
import pytest

from app.modules.ai.prompts import PromptLibrary, render_prompt

ROOT = Path(__file__).resolve().parents[4]
GOLDEN = ROOT / "tests/fixtures/prompts/structure_answer"
INPUTS = {
    "question": "What did you do?",
    "answers": '["I planned 12 routes."]',
    "target": '{"note":"Planned garden routes","fact":"(none)"}',
    "choices": '{"roles":{},"generalized_roles":[],"skills":["Python"]}',
    "session_id": "session-1",
}


def template():
    return PromptLibrary(ROOT / "prompts").load("structure_answer")


def test_structure_answer_prompt_golden():
    output = render_prompt(template(), INPUTS)
    assert output.prompt_version == "2"
    assert output.system == (GOLDEN / "golden_system.txt").read_text()
    assert output.user == (GOLDEN / "golden_user.txt").read_text()
    assert output.user.count("<untrusted_document") == 4


@pytest.mark.parametrize(
    "change",
    [
        {"status": "confirmed"},
        {"text": None},
        {"text": " "},
        {"text": "x" * 4001},
        {"text": "A\u202eclaim"},
        {"action": "Added action"},
        {"missing": ["invented"]},
        {"skills": ["Python", "Python"]},
        {"added_details": ["x"] * 11},
        {"type": "story", "text": None},
        {"role_key": "../role"},
    ],
)
def test_structure_answer_schema_refuses_invalid(change):
    good = json.loads((ROOT / "tests/fixtures/ai/structure_answer/ok.json").read_text())["data"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**good, **change}, dict(template().output_schema))


def test_structure_answer_adversarial_fixtures_valid_and_injection_escaped():
    for path in (ROOT / "tests/fixtures/ai/structure_answer").glob("*.json"):
        if path.stem != "malformed":
            jsonschema.validate(
                json.loads(path.read_text())["data"], dict(template().output_schema)
            )
    injection = (ROOT / "tests/fixtures/ai/structure_answer/injection_answer.txt").read_text()
    rendered = render_prompt(template(), {**INPUTS, "answers": injection})
    assert rendered.user.count("<untrusted_document") == 4
    assert "&lt;/untrusted_document>" in rendered.user
    assert "Ignore all previous instructions" not in rendered.system
