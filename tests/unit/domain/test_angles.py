"""M6.5 angle resolution: ``app/domain/angles.py`` (AC-M6.1 feedback flags; spec 3.1 item 5;
ADR-106 item 11). Pure; every value is fictional.

``resolve_angles(output, sent_facts, known_angle_ids)``: two or three angles; a reused angle id
that was not sent is ignored (``None``), never an error; wording that says something the cited
facts do not is flagged with the code-owned "This isn't in your fact bank." note.
"""

from typing import Any

import pytest

from app.domain.errors import InvalidModelOutput
from app.domain.gap_check import NOT_IN_FACT_BANK
from tests.tailoring_ai_inputs import EARLIER_ANGLE_ID, SENT


def angle(
    name: str = "Reliable planner",
    description: str = "Lead with the weekly route planning.",
    rationale: str = "The role is about route planning.",
    fact_ids: tuple[str, ...] = ("F1@1",),
    reuse: str | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": name,
        "description": description,
        "rationale": rationale,
        "fact_ids": list(fact_ids),
    }
    if reuse is not None:
        data["reuse_angle_id"] = reuse
    return data


def resolve(angles: list[dict[str, Any]], known: tuple[str, ...] = (EARLIER_ANGLE_ID,)) -> Any:
    from app.domain.angles import resolve_angles

    return resolve_angles({"angles": angles}, SENT, known)


def test_two_clean_angles_keep_order_and_have_no_flags() -> None:
    result = resolve([angle(), angle(name="Cost cutter", fact_ids=("F2@1", "F2@1"))])

    first, second = result.angles
    assert (first.name, first.fact_ids, first.flags, first.note) == (
        "Reliable planner",
        ("F1@1",),
        (),
        None,
    )
    assert second.fact_ids == ("F2@1",)
    assert first.reuse_angle_id is None


def test_a_known_reuse_id_is_kept() -> None:
    result = resolve([angle(reuse=EARLIER_ANGLE_ID), angle(name="Other")])

    assert result.angles[0].reuse_angle_id == EARLIER_ANGLE_ID
    assert result.angles[1].reuse_angle_id is None


def test_angle_rationale_claim_flagged() -> None:
    result = resolve(
        [
            angle(),
            angle(
                name="Scale",
                description="Lead with the warehouse work.",
                rationale="Cut processing time by 85% across 14 warehouses at Globex Co.",
                fact_ids=("F2@1",),
            ),
        ]
    )

    plain, flagged = result.angles
    assert plain.note is None
    assert flagged.note == NOT_IN_FACT_BANK
    assert any("85" in claim for claim in flagged.claims)
    assert any("14" in claim for claim in flagged.claims)


def test_a_claim_in_the_description_is_flagged_too() -> None:
    result = resolve([angle(), angle(name="Two", description="Stress the 12 years of SAP use.")])

    assert result.angles[1].note == NOT_IN_FACT_BANK
    assert any("12" in claim for claim in result.angles[1].claims)


def test_an_angle_citing_an_unsent_fact_or_nothing_is_flagged_not_dropped() -> None:
    result = resolve(
        [angle(fact_ids=("F999@1",)), angle(name="Two", fact_ids=()), angle(name="Three")]
    )

    assert len(result.angles) == 3
    assert result.angles[0].claims == ("F999@1",)
    assert result.angles[0].note == NOT_IN_FACT_BANK
    assert result.angles[1].note == NOT_IN_FACT_BANK
    assert result.angles[2].note is None


def test_angles_four_or_unknown_reuse_refused_or_ignored() -> None:
    # An unknown (or never-sent) reuse id is ignored, not an error.
    result = resolve([angle(reuse="angle-not-sent"), angle(name="Two", reuse="  ")])
    assert [item.reuse_angle_id for item in result.angles] == [None, None]

    # Four, one and none are refused here as the schema refuses them.
    for count in (0, 1, 4):
        with pytest.raises(InvalidModelOutput):
            resolve([angle(name=f"Angle {n}") for n in range(count)])


@pytest.mark.parametrize(
    "garbage",
    [{}, {"angles": "x"}, {"angles": [None, None]}, {"angles": [angle(), "x"]}],
)
def test_malformed_shapes_are_refused(garbage: Any) -> None:
    from app.domain.angles import resolve_angles

    with pytest.raises(InvalidModelOutput):
        resolve_angles(garbage, SENT, ())


def test_result_is_plain_data_for_storage() -> None:
    result = resolve([angle(reuse=EARLIER_ANGLE_ID), angle(name="Two")])

    data = result.to_data()

    assert data["angles"][0] == {
        "name": "Reliable planner",
        "description": "Lead with the weekly route planning.",
        "rationale": "The role is about route planning.",
        "fact_ids": ["F1@1"],
        "reuse_angle_id": EARLIER_ANGLE_ID,
        "claims": [],
    }
