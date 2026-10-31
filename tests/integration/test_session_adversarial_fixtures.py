"""Hostile and story-shaped fixture outputs for the two session tasks (M7.10; AC-M7.2, AC-M7.3).

The question fixtures (``adversarial_yes_no``, ``adversarial_tag``, ``adversarial_why_claim``)
are valid by the schema but break the open-question rule; the app must replace each with its
code-owned wording and say so (``fallback``), whichever way it fails. The structuring fixtures are
read by the app's own resolution: a ``role_key`` that was not offered is flagged and dropped, and
a well-formed story is accepted without a flag. Wired as ``adversarial_claim`` is in
``tests/integration/test_sessions.py``: ``load_ai_fixture`` scripts the fake provider.
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.domain.honesty import new_claims
from app.domain.questions import fallback_question, question_problems
from app.domain.structured_answer import resolve_structured_answer
from app.services.sessions import _output_safe
from tests.fakes.ai import load_ai_fixture
from tests.integration.test_session_answers import submit
from tests.integration.test_sessions import current, seed_note, service, start
from tests.refine_harness import Refining, refining

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "ai"
STORY_ANSWER = "The team had no routes. I planned 12 routes. Everyone used the routes."


@pytest.fixture
def desk(tmp_path: Path) -> Iterator[Refining]:
    with refining(tmp_path) as r:
        yield r


def fixture_data(task: str, name: str) -> dict[str, object]:
    return json.loads((ROOT / task / f"{name}.json").read_text())["data"]


# --- enrichment_question ---------------------------------------------------------------------

QUESTION_FIXTURES = (
    ("adversarial_yes_no", "yes_no"),
    ("adversarial_tag", "tag"),
    ("adversarial_why_claim", "why_new_claims"),
)


@pytest.mark.parametrize(("name", "problem"), QUESTION_FIXTURES)
def test_ac_m7_2_adversarial_question_fixture_breaks_the_open_question_rule(
    desk: Refining, name: str, problem: str
) -> None:
    seed_note(desk)
    entry = service(desk).backlog()[0]
    data = fixture_data("enrichment_question", name)
    question, why = str(data["question"]), str(data["why"])
    known = ("Python",)
    sources = (entry.payload,)
    if problem == "why_new_claims":
        assert question_problems(question, sources, known) == (), "the question alone is fine"
        assert new_claims(why, sources, known_skills=known), "the why line claims"
    else:
        assert problem in question_problems(question, sources, known)
    assert not _output_safe(question, why, entry, known)


@pytest.mark.parametrize(("name", "problem"), QUESTION_FIXTURES)
def test_ac_m7_2_adversarial_question_fixture_is_replaced_by_the_code_owned_wording(
    desk: Refining, name: str, problem: str
) -> None:
    desk.run.spy.script("enrichment_question", load_ai_fixture("enrichment_question", name))
    made = start(desk)
    asked = current(desk, made.session_id).answers[0]
    assert asked.fallback, problem
    assert (asked.question, asked.why) == fallback_question("capture_note", "")  # the seeded note
    hostile = fixture_data("enrichment_question", name)
    assert asked.question != hostile["question"] and asked.why != hostile["why"]


# --- structure_answer ------------------------------------------------------------------------


def test_ac_m7_3_role_that_was_not_offered_is_flagged_and_dropped() -> None:
    data = fixture_data("structure_answer", "adversarial_role")
    assert data["role_key"] == "role-not-offered"
    resolved = resolve_structured_answer(
        data, ("I planned 12 routes.",), roles={"role-real": "Planner at Example Ltd"}
    )
    assert resolved.role_key is None
    assert [(f.kind, f.text) for f in resolved.flags] == [("unknown_role", "role-not-offered")]
    assert resolved.needs_check


def test_ac_m7_3_role_that_was_not_offered_makes_a_flagged_draft_without_a_role(
    desk: Refining,
) -> None:
    made = start(desk)
    answer, _ = submit(
        desk,
        made,
        text="I planned 12 routes.",
        output=fixture_data("structure_answer", "adversarial_role"),
    )
    draft = desk.facts.get(answer.draft_fact_id)
    assert draft.needs_check and draft.role_id is None
    assert any(item["kind"] == "unknown_role" for item in answer.flagged_claims)


def test_ac_m7_3_story_fixture_is_accepted_without_a_flag() -> None:
    data = fixture_data("structure_answer", "ok_story")
    resolved = resolve_structured_answer(data, (STORY_ANSWER,))
    assert resolved.type.value == "story"
    assert resolved.flags == () and not resolved.needs_check
    assert (resolved.content.situation, resolved.content.action, resolved.content.result) == (
        "The team had no routes.",
        "Planned 12 routes.",
        "Everyone used the routes.",
    )


def test_ac_m7_3_story_fixture_makes_a_clean_story_draft(desk: Refining) -> None:
    made = start(desk)
    answer, _ = submit(
        desk, made, text=STORY_ANSWER, output=fixture_data("structure_answer", "ok_story")
    )
    draft = desk.facts.get(answer.draft_fact_id)
    assert draft.type.value == "story" and not draft.needs_check
    assert desk.facts.versions(draft.id)[-1].original_answer == STORY_ANSWER
