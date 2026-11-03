"""The skill names a ``structure_answer`` request offers come only from usable facts (spec 3.1
item 1; ADR-089 item 6; M7.5 and M8.3 part A).

The session's answer request and the debrief's paragraph request both send the person's skills as
a vocabulary the model may tag an answer with. A skill that only a never-use fact carries (or that
only a draft carries, or that no fact carries at all) would tell the model that the person has the
skill, and so would name a fact the request is forbidden to hold. Only a skill carried by a
confirmed, current fact that is not marked never-use is offered. The check on what comes back
still knows every skill (``resolve_structured_answer``), so a skill the model names without the
person's words is flagged, as before.

Fictional data, the fake provider behind a spy, no network. The names are letters only.
"""

from pathlib import Path
from typing import Any

import pytest

from app.domain.facts import FactStatus
from app.modules.ai.providers.fake import ScriptedResponse
from tests.debrief_pages import PARAGRAPH_ONE
from tests.fakes.ai import load_ai_fixture
from tests.integration.test_debrief import Desk
from tests.integration.test_session_answers import payload, submit
from tests.integration.test_sessions import start
from tests.refine_harness import Refining, refining

pytestmark = pytest.mark.integration

HIDDEN_SKILL = "hiddenskillquokka"
DRAFT_SKILL = "draftskillnumbat"
VOCABULARY_SKILL = "vocabularyskillbilby"
KEPT_SKILL = "keptskillwombat"
HIDDEN_ROLE_WORD = "Hiddenemployerquokka"
NOT_OFFERED = (HIDDEN_SKILL, DRAFT_SKILL, VOCABULARY_SKILL, HIDDEN_ROLE_WORD)


def seed_skill_bank(r: Refining) -> None:
    """A never-use fact with a skill and a role, a draft with a skill, a skill no fact carries
    and a confirmed fact with a skill (the control)."""
    from app.services.facts import NewRole

    r.run.app.state.skills.add(VOCABULARY_SKILL)
    r.typed(
        "Confidential fictional audit statement.",
        privacy="never_use",
        skills=(HIDDEN_SKILL,),
        new_role=NewRole(employer=HIDDEN_ROLE_WORD, title="Hidden Titlequokka"),
    )
    r.drafts("An unconfirmed draft statement.", skills=(DRAFT_SKILL,))
    kept = r.typed("Wrote a fictional scheduling tool for the depot.", skills=(KEPT_SKILL,))
    assert kept.status == FactStatus.CONFIRMED


def structure_requests(r: Refining) -> list[Any]:
    found = [q for q in r.run.spy.requests if q.task == "structure_answer"]
    assert found, "a structure_answer request was made"
    return found


def assert_only_usable_skills(r: Refining) -> None:
    for request in r.run.spy.requests:  # every request, whatever its task
        text = "\n".join((request.system, request.user)).lower()
        held = [word for word in NOT_OFFERED if word.lower() in text]
        assert not held, f"{request.task} request holds {held}"
    offered = "\n".join(q.user for q in structure_requests(r)).lower()
    assert KEPT_SKILL in offered, "a skill of a usable fact is offered (the control)"


def test_session_answer_request_offers_only_skills_of_usable_facts(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        seed_skill_bank(r)
        made = start(r)
        submit(r, made, output=payload())
        assert_only_usable_skills(r)


def test_session_answer_still_flags_a_known_skill_without_a_usable_fact(tmp_path: Path) -> None:
    """The offer is narrowed; the check on the answer is not. A skill that is in the person's list
    but that no usable fact carries, named by the model without the person's words, is flagged."""
    with refining(tmp_path) as r:
        seed_skill_bank(r)
        made = start(r)
        answer, _ = submit(
            r,
            made,
            output=payload(text=f"Planned 12 routes with {VOCABULARY_SKILL}.", skills=[]),
        )
        assert r.facts.get(answer.draft_fact_id).needs_check
        assert any(item["kind"] == "skill" for item in answer.flagged_claims)


def test_debrief_paragraph_request_offers_only_skills_of_usable_facts(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        d = Desk(r)
        seed_skill_bank(r)
        r.run.spy.script("structure_answer", load_ai_fixture("structure_answer", "ok"))
        d.save(experiences=PARAGRAPH_ONE)
        d.drafted()
        assert_only_usable_skills(r)


def test_debrief_draft_still_flags_a_known_skill_without_a_usable_fact(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        d = Desk(r)
        seed_skill_bank(r)
        r.run.spy.script(
            "structure_answer",
            ScriptedResponse(
                data=payload(text=f"Planned 12 routes with {VOCABULARY_SKILL}.", skills=[])
            ),
        )
        d.save(experiences=PARAGRAPH_ONE)
        d.drafted()
        [draft] = d.debrief_facts()
        assert draft["needs_check"] == 1
