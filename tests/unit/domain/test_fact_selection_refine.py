"""The fact being refined and the parts of the fact block (M3.8; ADR-089 item 6): which fact
may be refined, how it is labelled and written, and the generalize instruction given for
it. Pure; every value is fictional."""

import pytest

from app.domain.fact_selection import (
    GENERALIZE_INSTRUCTION,
    GENERALIZE_MARK,
    NO_FACTS,
    FactRecord,
    fact_label,
    fact_text,
    facts_for_ai,
    prompt_fact_block,
    refine_target,
)
from app.domain.facts import FactContent, FactStatus, Privacy
from tests.unit.domain.test_fact_selection import rendered


def record(**fields: object) -> FactRecord:
    values: dict[str, object] = {
        "number": 4,
        "version": 2,
        "is_current": True,
        "status": FactStatus.CONFIRMED,
        "privacy": Privacy.NORMAL,
        "content": FactContent(text="Planned 60 delivery routes a week."),
    }
    values.update(fields)
    return FactRecord(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("status", [FactStatus.DRAFT, FactStatus.CONFIRMED])
def test_drafts_and_confirmed_facts_can_be_refined(status: FactStatus) -> None:
    target = refine_target(record(status=status))

    assert target is not None and str(target.ref) == "F4@2"


@pytest.mark.parametrize(
    "fields",
    [
        {"status": FactStatus.DISCARDED},
        {"status": FactStatus.ARCHIVED},
        {"privacy": Privacy.NEVER_USE},
        {"is_current": False},
    ],
)
def test_other_facts_are_never_refined(fields: dict[str, object]) -> None:
    assert refine_target(record(**fields)) is None


def test_label_text_and_instruction_for_a_generalized_target() -> None:
    target = refine_target(
        record(
            privacy=Privacy.GENERALIZE,
            role="Junior Planner at Sample Logistics SA",
            skills=("Excel", "SQL"),
            content=FactContent(situation="A <late> depot.", result="On time."),
        )
    )
    assert target is not None

    assert fact_label(target) == f"F4@2 {GENERALIZE_MARK}"
    assert fact_text(target) == (
        "Role: Junior Planner at Sample Logistics SA\nSkills: Excel, SQL\n"
        "Situation: A &lt;late> depot.\nResult: On time."
    )
    # Sent with no other fact, it still gets the instruction once, and the block says none.
    with_instruction = rendered([], also_generalize=True)
    assert with_instruction.count(GENERALIZE_INSTRUCTION) == 1
    assert with_instruction.index(GENERALIZE_INSTRUCTION) < with_instruction.index(NO_FACTS)
    without = rendered([])
    assert GENERALIZE_INSTRUCTION not in without and NO_FACTS in without
    block = prompt_fact_block([target])
    assert block.generalize is True and str(block) == block.text
    assert block.text == f"[{fact_label(target)}]\n{fact_text(target)}"


def test_the_same_version_is_sent_once() -> None:
    assert len(facts_for_ai([record(), record()])) == 1
