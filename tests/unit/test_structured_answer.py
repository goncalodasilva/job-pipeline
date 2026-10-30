"""M7.5 pure honesty resolution; fictional answers and labels only."""

import pytest

from app.domain.fact_selection import FactRecord, facts_for_ai
from app.domain.facts import FactContent, FactStatus, Privacy
from app.domain.structured_answer import resolve_structured_answer


def payload(**changes):
    return {
        "type": "fact",
        "text": "Planned 12 routes with Python.",
        "situation": None,
        "action": None,
        "result": None,
        "role_key": None,
        "skills": ["Python"],
        "missing": [],
        "added_details": [],
        **changes,
    }


def test_resolve_structured_answer_uses_only_approved_sources():
    result = resolve_structured_answer(
        payload(), ("I planned 12 routes with Python.",), known_skills=("Python",)
    )
    assert not result.needs_check and result.skills == ("Python",)
    flagged = resolve_structured_answer(
        payload(text="Planned 950 routes."), ("I planned 12 routes.",)
    )
    assert flagged.needs_check and any("950" in f.text for f in flagged.flags)


def test_resolve_structured_answer_drops_unnamed_skill_and_clears_unknown_role():
    result = resolve_structured_answer(
        payload(role_key="invented"), ("I planned 12 routes.",), known_skills=("Python",)
    )
    assert result.role_key is None and result.skills == () and result.needs_check
    assert any(f.kind == "unknown_role" for f in result.flags)


def test_resolve_structured_answer_chosen_role_only():
    result = resolve_structured_answer(
        payload(text="Worked at Globex Corporation.", role_key="r1", skills=[]),
        ("I worked there.",),
        roles={"r1": "Planner at Globex Corporation", "r2": "Planner at Lakeshore Bank"},
    )
    assert not result.needs_check and result.role_key == "r1"
    flagged = resolve_structured_answer(
        payload(text="Worked at Lakeshore Bank.", role_key="r1", skills=[]),
        ("I worked there.",),
        roles={"r1": "Planner at Globex Corporation", "r2": "Planner at Lakeshore Bank"},
    )
    assert flagged.needs_check


def test_resolve_structured_answer_generalized_target_hides_names():
    (target,) = facts_for_ai(
        (
            FactRecord(
                1,
                1,
                True,
                FactStatus.CONFIRMED,
                Privacy.GENERALIZE,
                FactContent(text="Planned 12 routes at Globex Corporation."),
            ),
        )
    )
    result = resolve_structured_answer(
        payload(text="Planned 12 routes at Globex Corporation.", skills=[]),
        ("I planned routes.",),
        target=target,
    )
    assert result.needs_check


@pytest.mark.parametrize("answer_skill,output_skill", [("C++", "C"), ("C#", "C"), ("C", "C++")])
@pytest.mark.parametrize("returned_tags", [True, False])
def test_skill_punctuation_does_not_invent_a_different_skill(
    answer_skill, output_skill, returned_tags
):
    resolved = resolve_structured_answer(
        payload(
            text=f"Used {output_skill} to plan routes.",
            skills=[output_skill] if returned_tags else [],
        ),
        (f"I used {answer_skill} to plan routes.",),
        known_skills=("C", "C++", "C#"),
    )
    assert resolved.skills == ()
    assert resolved.needs_check and any(
        f.kind == "skill" and f.text == output_skill for f in resolved.flags
    )


@pytest.mark.parametrize("skill", ["C", "C++", "C#", "\uff30\uff59\uff54\uff48\uff4f\uff4e"])
def test_skill_punctuation_positive_control(skill):
    resolved = resolve_structured_answer(
        payload(text=f"Used {skill} to plan routes.", skills=[skill]),
        (f"I used {skill} to plan routes.",),
        known_skills=("C", "C++", "C#", "Python"),
    )
    assert resolved.skills == (skill,) and not resolved.needs_check
