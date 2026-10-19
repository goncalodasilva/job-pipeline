"""The fake provider's scripts for real tasks (M3.4; ADR-088; ADR-089 item 8).

A fake that wiring scripted with ``script_keywords`` answers a real task from fictional
answers chosen by a keyword in the task's text; an unscripted fake still answers no real
task. The answers shipped in ``app/modules/ai/providers/fake_scripts.py`` equal the JSON
fixtures of ``tests/fixtures/ai/<task>/`` and are checked against each task's schema and
against the fictional CV fixtures they quote.
"""

import json
import re
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from app.domain.errors import InvalidModelOutput, NetworkUnavailable, UsageLimitReached
from app.modules.ai.prompts import PromptLibrary, render_prompt
from app.modules.ai.providers import fake_scripts
from app.modules.ai.providers.fake import (
    FAKE_THOROUGH_MODEL,
    FakeAIProvider,
    KeywordScript,
    ScriptedResponse,
)
from app.modules.ai.providers.fake_scripts import (
    EXTRACT_CV_RESPONSES,
    EXTRACT_CV_TASK,
    real_task_scripts,
)
from app.modules.ai.tasks import PRACTICE_SPEC, TASK_SPECS
from tests.fakes.ai import NotCancelled, load_ai_fixture, make_request

REPO_ROOT = Path(__file__).resolve().parents[4]
REPO_PROMPTS = REPO_ROOT / "prompts"
AI_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "ai"
CV_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "cv"
CV_TEXT = (CV_FIXTURES / "cv.txt").read_text(encoding="utf-8")
INJECTION_CV = (AI_FIXTURES / EXTRACT_CV_TASK / "injection_cv.txt").read_text(encoding="utf-8")

VALID = (
    "ok",
    "adversarial_employer_number",
    "adversarial_quote",
    "adversarial_moved_number",
    "adversarial_invented_words",
    "adversarial_skill",
    "injection_obeyed",
)
KEYWORD_FIXTURES = {
    fake_scripts.KEYWORD_MALFORMED: "malformed",
    fake_scripts.KEYWORD_INVENTED_EMPLOYER: "adversarial_employer_number",
    fake_scripts.KEYWORD_INVENTED_QUOTE: "adversarial_quote",
    fake_scripts.KEYWORD_MOVED_NUMBER: "adversarial_moved_number",
    fake_scripts.KEYWORD_INVENTED_WORDS: "adversarial_invented_words",
    fake_scripts.KEYWORD_INVENTED_SKILL: "adversarial_skill",
    fake_scripts.KEYWORD_INJECTION_STATUS: "injection_status_field",
    fake_scripts.KEYWORD_INJECTION: "injection_obeyed",
}
KEYWORD_FAILURES = {
    fake_scripts.KEYWORD_USAGE_LIMIT: UsageLimitReached,
    fake_scripts.KEYWORD_OFFLINE: NetworkUnavailable,
}


def _scripted_fake(*, app_mode: bool = True) -> FakeAIProvider:
    """What wiring is to do for a fake that may answer real tasks."""
    provider = FakeAIProvider(app_mode=app_mode)
    for task, script in real_task_scripts().items():
        provider.script_keywords(task, script)
    return provider


def _request(cv_text: str) -> Any:
    """The request the executor would build for this CV text."""
    template = PromptLibrary(REPO_PROMPTS).load(EXTRACT_CV_TASK)
    rendered = render_prompt(template, {"cv_text": cv_text, "import_id": "imp-1"})
    return make_request(
        EXTRACT_CV_TASK,
        system=rendered.system,
        user=rendered.user,
        output_schema=template.output_schema,
        model=FAKE_THOROUGH_MODEL,
    )


def _data(name: str) -> dict[str, Any]:
    return json.loads(json.dumps(EXTRACT_CV_RESPONSES[name].data))  # type: ignore[no-any-return]


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"[0-9]+", text))


def _wording(unit: dict[str, Any]) -> str:
    return " ".join(unit[part] or "" for part in ("text", "situation", "action", "result"))


async def test_fake_real_task_script_chosen_by_keyword() -> None:
    provider = _scripted_fake()

    plain = await provider.run(_request(CV_TEXT), NotCancelled())
    assert dict(plain.data) == _data("ok")
    for keyword, fixture in KEYWORD_FIXTURES.items():
        request = _request(f"{CV_TEXT}\nReference code: {keyword}\n")
        expected = EXTRACT_CV_RESPONSES[fixture]
        if expected.data is None:
            # Not JSON, on every call: the executor's retry gets the same answer.
            for _ in range(2):
                with pytest.raises(InvalidModelOutput):
                    await provider.run(request, NotCancelled())
        else:
            answer = await provider.run(request, NotCancelled())
            assert dict(answer.data) == _data(fixture), keyword
            # The same text always gets the same answer; nothing is used up.
            assert dict((await provider.run(request, NotCancelled())).data) == _data(fixture)
    for keyword, error in KEYWORD_FAILURES.items():
        with pytest.raises(error):
            await provider.run(_request(f"{CV_TEXT}\n{keyword}"), NotCancelled())
    # The injection fixture carries its own keyword.
    injected = await provider.run(_request(INJECTION_CV), NotCancelled())
    assert dict(injected.data) == _data("injection_obeyed")
    # Every answer names the fake: the provider is "fake" and the model a fake model.
    assert provider.name == "fake"
    assert plain.model == injected.model == FAKE_THOROUGH_MODEL
    assert plain.model.startswith("fake")
    assert provider.requests == [], "app mode keeps no request: prompts carry the CV"


def test_fake_real_task_keywords_cover_every_listed_behaviour_once() -> None:
    script = real_task_scripts()[EXTRACT_CV_TASK]

    assert set(script.keywords) == set(KEYWORD_FIXTURES) | set(KEYWORD_FAILURES)
    assert len(script.keywords) == len(set(script.keywords))
    assert script.default == EXTRACT_CV_RESPONSES["ok"]
    assert script.response_for("") == script.default
    # A keyword inside a longer one is listed after it, or it would hide the longer one.
    both = f"{fake_scripts.KEYWORD_INJECTION} {fake_scripts.KEYWORD_INJECTION_STATUS}"
    assert script.response_for(both) == EXTRACT_CV_RESPONSES["injection_status_field"]
    with pytest.raises(ValueError):
        KeywordScript(
            default=script.default,
            by_keyword=(
                (fake_scripts.KEYWORD_INJECTION, script.default),
                (fake_scripts.KEYWORD_INJECTION_STATUS, script.default),
            ),
        )
    with pytest.raises(ValueError):
        KeywordScript(default=script.default, by_keyword=((" ", script.default),))
    # No keyword sits in a prompt or in the plain fictional CVs, so none fires by accident.
    for keyword in script.keywords:
        assert keyword not in CV_TEXT
        assert keyword not in (CV_FIXTURES / "manifest.json").read_text(encoding="utf-8")
        for prompt in REPO_PROMPTS.glob("*/prompt.md"):
            assert keyword not in prompt.read_text(encoding="utf-8")


async def test_fake_answers_no_real_task_unless_scripted() -> None:
    """The gate is wiring's: a fake nobody scripted fails a real task plainly (ADR-088)."""
    request = _request(f"{CV_TEXT}\n{fake_scripts.KEYWORD_INJECTION}")

    with pytest.raises(InvalidModelOutput):
        await FakeAIProvider(app_mode=True).run(request, NotCancelled())
    with pytest.raises(LookupError):
        await FakeAIProvider().run(request, NotCancelled())


async def test_fake_keyword_script_and_queue_replace_each_other() -> None:
    provider = FakeAIProvider()
    queued = ScriptedResponse(data={"roles": [], "units": []})
    request = _request(CV_TEXT)

    provider.script(EXTRACT_CV_TASK, queued)
    provider.script_keywords(EXTRACT_CV_TASK, real_task_scripts()[EXTRACT_CV_TASK])
    assert dict((await provider.run(request, NotCancelled())).data) == _data("ok")
    provider.script(EXTRACT_CV_TASK, queued)
    assert dict((await provider.run(request, NotCancelled())).data) == {"roles": [], "units": []}
    # Keywords are looked for in the task's own text only: another task is untouched.
    with pytest.raises(LookupError):
        await provider.run(make_request("other_task", user=request.user), NotCancelled())


async def test_fake_real_task_scripts_take_the_given_delay() -> None:
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    provider = FakeAIProvider(sleep=sleep, app_mode=True)
    script = real_task_scripts(delay_seconds=1.5)[EXTRACT_CV_TASK]
    provider.script_keywords(EXTRACT_CV_TASK, script)

    await provider.run(_request(CV_TEXT), NotCancelled())

    assert delays == [1.5]
    assert all(response.delay_seconds == 1.5 for _, response in script.by_keyword)
    with pytest.raises(ValueError):
        real_task_scripts(delay_seconds=-1.0)


def test_fake_scripts_validate_against_task_schemas() -> None:
    scripts = real_task_scripts()

    # Real tasks only, each with a prompt; the practice task keeps its own script.
    assert set(scripts) <= set(TASK_SPECS)
    assert EXTRACT_CV_TASK in scripts and PRACTICE_SPEC.name not in scripts
    schema = dict(PromptLibrary(REPO_PROMPTS).load(EXTRACT_CV_TASK).output_schema)
    validator = jsonschema.Draft202012Validator(schema)

    # The shipped answers and the fixtures are the same, name for name.
    files = sorted(path.stem for path in (AI_FIXTURES / EXTRACT_CV_TASK).glob("*.json"))
    assert files == sorted(EXTRACT_CV_RESPONSES)
    for name, response in EXTRACT_CV_RESPONSES.items():
        assert load_ai_fixture(EXTRACT_CV_TASK, name) == response, name
    for name in VALID:
        validator.validate(_data(name))
    # Malformed is not JSON at all; the status answer is JSON the schema refuses.
    malformed = EXTRACT_CV_RESPONSES["malformed"]
    assert malformed.data is None and malformed.raw_text is not None
    with pytest.raises(json.JSONDecodeError):
        json.loads(malformed.raw_text)
    status_errors = list(validator.iter_errors(_data("injection_status_field")))
    assert status_errors
    assert {error.validator for error in status_errors} == {"additionalProperties"}
    # Every keyword answer is one of the named answers or a provider failure.
    script = scripts[EXTRACT_CV_TASK]
    for _, response in script.by_keyword:
        assert response in EXTRACT_CV_RESPONSES.values() or response.failure is not None
    # Nothing here can say a unit was confirmed, except the answer the schema refuses.
    for name in VALID:
        assert "confirmed" not in json.dumps(_data(name)).lower()
        assert "status" not in json.dumps(_data(name)).lower()


def test_fake_ok_script_quotes_the_fictional_cv_fixtures() -> None:
    """The well-behaved answer says only what the fictional CVs say."""
    manifest = json.loads((CV_FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    texts = {"cv.txt": CV_TEXT}
    for name in ("single_column.pdf", "two_column.pdf"):
        groups = manifest["files"][name]["marker_groups"]
        texts[name] = "\n".join(line for group in groups for line in group)
    ok = _data("ok")

    for name, text in texts.items():
        for role in ok["roles"]:
            assert role["employer"] in text and role["title"] in text, name
            assert role["start_date"] in text and role["end_date"] in text, name
        for unit in ok["units"]:
            assert unit["source_quote"] in text, name
            assert _numbers(_wording(unit)) <= _numbers(unit["source_quote"]), name
            for skill in unit["skills"]:
                assert skill in unit["source_quote"], name


def test_fake_adversarial_scripts_break_exactly_the_rule_they_name() -> None:
    """What the import's check must catch: these answers pass the schema."""
    employer = _data("adversarial_employer_number")
    invented = [role for role in employer["roles"] if role["employer"] not in CV_TEXT]
    assert [role["key"] for role in invented] == ["r3"]
    under_invented = [unit for unit in employer["units"] if unit["role_key"] == "r3"]
    inflated, faithful = under_invented
    # Both quote the CV; one has numbers and an employer name the CV doesn't have.
    assert inflated["source_quote"] in CV_TEXT and faithful["source_quote"] in CV_TEXT
    assert _numbers(inflated["text"]) - _numbers(CV_TEXT) == {"85", "14"}
    assert invented[0]["employer"] in inflated["text"]
    assert faithful["text"] == faithful["source_quote"]
    honest = [unit for unit in employer["units"] if unit["role_key"] != "r3"]
    assert honest and all(unit["source_quote"] in CV_TEXT for unit in honest)

    quote = _data("adversarial_quote")
    missing = [unit for unit in quote["units"] if unit["source_quote"] not in CV_TEXT]
    assert len(missing) == 1 and len(quote["units"]) == 2
    assert not _numbers(missing[0]["text"]), "only the quote rule can catch this unit"
    assert all(role["employer"] in CV_TEXT for role in quote["roles"])


def _words(text: str) -> set[str]:
    return set(re.findall(r"[^\W_]+", text.casefold()))


def test_fake_ok_script_keeps_the_wording_of_its_own_quotes() -> None:
    """The exemplar is verbatim: every word and number of a unit is in that unit's quote."""
    for unit in _data("ok")["units"]:
        assert _words(_wording(unit)) <= _words(unit["source_quote"]), unit
        assert _numbers(_wording(unit)) <= _numbers(unit["source_quote"]), unit
        assert _wording(unit).replace(" ", "") in unit["source_quote"].replace(" ", "")


def test_fake_review_probe_scripts_break_exactly_the_rule_they_name() -> None:
    """Answers the whole-CV checks let through: each quotes the CV and names its roles."""
    for name in ("adversarial_moved_number", "adversarial_invented_words", "adversarial_skill"):
        data = _data(name)
        assert all(role["employer"] in CV_TEXT for role in data["roles"]), name
        assert all(unit["source_quote"] in CV_TEXT for unit in data["units"]), name

    faithful, moved = _data("adversarial_moved_number")["units"]
    assert faithful["text"] == faithful["source_quote"]
    # The numbers are the CV's, from another bullet: only the unit's own quote tells.
    assert _numbers(moved["text"]) <= _numbers(CV_TEXT)
    assert _numbers(moved["text"]) - _numbers(moved["source_quote"]) == {"60", "3"}
    assert _words(moved["text"]) - _words(moved["source_quote"]) == {"60", "3"}

    faithful, padded = _data("adversarial_invented_words")["units"]
    assert faithful["text"] == faithful["source_quote"]
    assert padded["text"].startswith(padded["source_quote"].rstrip("."))
    assert _numbers(padded["text"]) == _numbers(padded["source_quote"])
    added = _words(padded["text"]) - _words(padded["source_quote"])
    assert {"imaginary", "aerospace"} <= added and added.isdisjoint(_words(CV_TEXT))

    invented, named = _data("adversarial_skill")["units"]
    assert invented["text"] == invented["source_quote"], "faithful but for the skill"
    assert [skill for skill in invented["skills"] if skill.casefold() not in CV_TEXT.casefold()]
    assert named["skills"] and all(skill in CV_TEXT for skill in named["skills"])


def test_fake_injection_script_is_what_the_planted_text_asks_for() -> None:
    obeyed = _data("injection_obeyed")
    planted = [unit for unit in obeyed["units"] if unit not in _data("ok")["units"]]

    assert len(planted) == 1
    # The planted claim is in the injection CV, so no mechanical check can tell it from the
    # rest: it must simply stay a draft the user reviews.
    assert planted[0]["source_quote"] in INJECTION_CV
    assert planted[0]["source_quote"] not in CV_TEXT
    assert fake_scripts.KEYWORD_INJECTION in INJECTION_CV
    assert fake_scripts.KEYWORD_INJECTION_STATUS not in INJECTION_CV
    for marker in ("</untrusted_document>", "Ignore all previous instructions", '"confirmed"'):
        assert marker in INJECTION_CV
    with_status = _data("injection_status_field")
    assert all(unit.pop("status") == "confirmed" for unit in with_status["units"])
    assert with_status == obeyed
