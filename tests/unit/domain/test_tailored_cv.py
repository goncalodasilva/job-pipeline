"""M6.7 resolution of a tailored CV: ``app/domain/tailored_cv.py`` (AC-M6.1, AC-M6.2; spec 3.1
items 2-4; ADR-106 items 8 and 12). Pure; every value is fictional.

``resolve_cv_units(output, sent_facts, template, ...)`` turns the model's units into document
units (origin ai) and lists the honesty flags. It never decides approval. The adversarial
fixtures of ``tests/fixtures/ai/tailor_cv/`` are the model answers.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from app.domain.documents import UnitOrigin
from app.domain.tailored_cv import (
    MAX_ADDED_DETAILS,
    MAX_CV_UNITS,
    MAX_DETAIL_CHARS,
    MAX_FACT_IDS,
    DroppedUnit,
    check_added_details,
    check_fact_ids,
    resolve_cv_units,
)
from tests.tailoring_ai_inputs import CV_SENT, ROLE, TEMPLATE

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "ai" / "tailor_cv"
INVENTED_ROLE = "Head of Planning at Globex Corporation"


def answer(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["data"]


def resolve(output: Any, **kwargs: Any) -> Any:
    return resolve_cv_units(output, CV_SENT, TEMPLATE, **kwargs)


def flag_tuples(result: Any) -> list[tuple[str, str]]:
    return [(flag.kind, flag.claim) for flag in result.flags]


def test_ok_answer_has_five_unflagged_ai_units_in_order() -> None:
    result = resolve(answer("ok"))

    assert result.flags == ()
    assert result.dropped == ()
    assert [unit.section for unit in result.units] == [
        "summary",
        "experience",
        "experience",
        "experience",
        "languages",
    ]
    assert {unit.origin for unit in result.units} == {UnitOrigin.AI}
    assert {unit.kind for unit in result.units} == {"content"}
    assert [unit.removed for unit in result.units] == [False] * 5
    first = result.units[0]
    assert first.fact_ids == ("F1@1", "F2@1")
    assert first.added_details == ()
    assert result.units[1].role == ROLE
    assert len({unit.unit_id for unit in result.units}) == 5


def test_unit_ids_come_from_the_given_factory_and_flags_use_them() -> None:
    made = iter(f"id-{n}" for n in range(1, 50))

    result = resolve(answer("adversarial_unknown_id"), new_id=lambda: next(made))

    assert [unit.unit_id for unit in result.units] == ["id-1", "id-2"]
    assert [flag.unit_id for flag in result.flags] == ["id-2"]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (
            "adversarial_invented_employer",
            [("needs_check", "Globex Corporation"), ("unsupported", INVENTED_ROLE)],
        ),
        ("adversarial_unknown_id", [("unsupported", "F999@1")]),
        ("adversarial_inflated_number", [("needs_check", "85%"), ("needs_check", "14")]),
        (
            "adversarial_uncited_paragraph",
            [("unsupported", "A seasoned leader known for turning around struggling teams.")],
        ),
        ("adversarial_structure_claim", [("unsupported", "Eight years of leadership at Initech")]),
        ("adversarial_wrong_role", [("needs_check", "F7@1")]),
        ("adversarial_role_on_roleless_facts", [("needs_check", "F3@1")]),
        ("adversarial_generalized_name", [("needs_check", "Lakeshore Bank")]),
        ("injection_note", [("needs_check", "50")]),
    ],
)
def test_ac_m6_1_adversarial_units_are_flagged(name: str, expected: list[tuple[str, str]]) -> None:
    result = resolve(answer(name))

    assert flag_tuples(result) == expected
    # The summary line of each fixture is clean; the flags belong to the offending units.
    flagged = {flag.unit_id for flag in result.flags}
    assert result.units[0].unit_id not in flagged
    assert flagged <= {unit.unit_id for unit in result.units}


def test_injection_posting_claim_is_flagged() -> None:
    result = resolve(answer("injection_posting"))

    claims = [claim for _kind, claim in flag_tuples(result)]
    assert "Ten years" in claims or any("Ten" in claim for claim in claims)
    assert any("Globex Corporation" in claim for claim in claims)
    assert {kind for kind, _claim in flag_tuples(result)} == {"needs_check"}


def test_an_invented_role_label_is_dropped_from_the_unit_and_flagged() -> None:
    result = resolve(answer("adversarial_invented_employer"))

    invented = result.units[2]
    assert invented.role is None  # no invented role header can reach the document
    assert invented.text == "Planned 60 delivery routes a week with a team of 3."
    assert [flag.unit_id for flag in result.flags][-1] == invented.unit_id


def test_a_structure_unit_that_is_a_template_heading_is_dropped_not_flagged() -> None:
    result = resolve(answer("adversarial_structure_claim"))

    assert [(item.reason, item.text) for item in result.dropped] == [("heading", "Work history")]
    assert result.dropped[0].index == 0
    kinds = [unit.kind for unit in result.units]
    assert kinds == ["content", "structure"]
    # The other structure unit is kept (so the user can see and remove it), cites nothing.
    assert result.units[1].fact_ids == () and result.units[1].role is None


def test_units_beyond_the_limit_are_dropped_and_listed() -> None:
    result = resolve(answer("too_many_units"))

    assert MAX_CV_UNITS == 80
    assert len(result.units) == 80
    assert result.dropped == (DroppedUnit(80, "too_many", "experience", "Planned route 80."),)


def test_a_section_outside_the_template_is_dropped_and_listed() -> None:
    data = answer("ok")
    data["units"][4]["section"] = "publications"  # a known id, but not in this template

    result = resolve(data)

    assert len(result.units) == 4
    assert [(item.index, item.reason, item.section) for item in result.dropped] == [
        (4, "unknown_section", "publications")
    ]
    assert result.flags == ()


@pytest.mark.parametrize(
    "text", ["", "   ", "x" * 401, "two\nlines", "bidi ‮ override", "tab\there"]
)
def test_a_unit_with_unusable_text_is_dropped_as_invalid(text: str) -> None:
    data = answer("ok")
    data["units"][1]["text"] = text

    result = resolve(data)

    assert len(result.units) == 4
    assert [(item.index, item.reason) for item in result.dropped] == [(1, "invalid")]


def test_the_longest_allowed_text_is_kept() -> None:
    data = answer("ok")
    data["units"][0]["text"] = "Plans routes. " * 28 + "Done."  # 397 characters
    assert len(data["units"][0]["text"]) <= 400

    assert len(resolve(data).units) == 5


def test_added_details_are_flagged_as_claims() -> None:
    data = answer("ok")
    data["units"][1]["added_details"] = ["a large depot"]

    result = resolve(data)

    assert flag_tuples(result) == [("needs_check", "a large depot")]
    assert result.units[1].added_details == ("a large depot",)


def test_repeated_fact_ids_are_listed_once() -> None:
    data = answer("ok")
    data["units"][0]["fact_ids"] = ["F1@1", "F1@1", "F2@1"]

    assert resolve(data).units[0].fact_ids == ("F1@1", "F2@1")


def test_known_skills_are_checked_against_the_cited_facts() -> None:
    data = answer("ok")
    data["units"][1]["text"] = "Planned 60 delivery routes a week using terraform."

    assert flag_tuples(resolve(data, known_skills=("Terraform",))) == [("needs_check", "terraform")]
    assert resolve(data).flags == ()


@pytest.mark.parametrize(
    "output",
    [
        {},
        {"units": "not a list"},
        {"units": [None, 3, "x", {"kind": "content"}]},
        {"units": [{"kind": "free", "section": "summary", "text": "x", "fact_ids": []}]},
        {"units": [{"kind": "content", "section": "summary", "text": "x", "fact_ids": "F1@1"}]},
        {"units": [{"kind": "content", "section": ["summary"], "text": "x", "fact_ids": []}]},
    ],
)
def test_a_shape_the_schema_would_refuse_never_raises_and_yields_no_units(output: Any) -> None:
    result = resolve(output)

    assert result.units == ()
    assert result.flags == ()


def test_the_result_has_no_status_or_approval() -> None:
    result = resolve(answer("ok"))

    for name in ("status", "approved", "approval"):
        assert not hasattr(result, name)
    assert not any(hasattr(unit, "status") for unit in result.units)


INVISIBLE = ["\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\U000e0001", "\U000e0041"]


@pytest.mark.parametrize("char", INVISIBLE)
def test_invisible_characters_are_removed_from_the_text(char: str) -> None:
    data = answer("ok")
    data["units"][1]["text"] = f"Planned 60 de{char}livery routes a week with a team of 3."

    result = resolve(data)

    assert result.units[1].text == "Planned 60 delivery routes a week with a team of 3."
    assert result.flags == ()
    assert result.dropped == ()


def test_a_text_of_only_invisible_characters_is_dropped_as_invalid() -> None:
    data = answer("ok")
    data["units"][1]["text"] = "".join(INVISIBLE)

    result = resolve(data)

    assert len(result.units) == 4
    assert [(item.index, item.reason) for item in result.dropped] == [(1, "invalid")]


@pytest.mark.parametrize("detail", ["two\nlines", "bidi \u202e text", "tab\there", "x" * 201])
def test_a_unit_with_an_unusable_added_detail_is_dropped_as_invalid(detail: str) -> None:
    data = answer("ok")
    data["units"][1]["added_details"] = ["a large depot", detail]

    result = resolve(data)

    assert len(result.units) == 4
    assert [(item.index, item.reason) for item in result.dropped] == [(1, "invalid")]


def test_invisible_characters_are_removed_from_added_details_and_empty_ones_vanish() -> None:
    data = answer("ok")
    data["units"][1]["added_details"] = ["a large\u200b depot", "\u200b\ufeff"]

    result = resolve(data)

    assert result.units[1].added_details == ("a large depot",)
    assert flag_tuples(result) == [("needs_check", "a large depot")]


def test_a_role_on_a_line_citing_only_role_less_facts_is_flagged() -> None:
    data = answer("ok")
    data["units"][1]["fact_ids"] = ["F3@1"]
    data["units"][1]["text"] = "Speaks English at work every day."

    result = resolve(data)

    assert result.units[1].role == ROLE  # the role is a real one; the line does not belong to it
    assert flag_tuples(result) == [("needs_check", "F3@1")]


# --- shared validation of user-supplied values -----------------------------------------------


def test_the_limits_of_the_shared_validators() -> None:
    assert (MAX_FACT_IDS, MAX_ADDED_DETAILS, MAX_DETAIL_CHARS) == (20, 10, 200)


def test_check_fact_ids_returns_each_valid_reference_once_in_order() -> None:
    assert check_fact_ids(["F2@1", "F1@3", "F2@1"]) == ("F2@1", "F1@3")
    assert check_fact_ids(()) == ()
    assert len(check_fact_ids([f"F{n}@1" for n in range(MAX_FACT_IDS)])) == MAX_FACT_IDS


@pytest.mark.parametrize(
    "ids",
    [
        ["F1"],
        ["f1@1"],
        ["F1@"],
        ["F@1"],
        ["F1@1\n"],
        [" F1@1"],
        ["F1@1 "],
        ["F1@1;DROP"],
        ["F1@\u0661"],
        [""],
        [3],
        [None],
        "F1@1",
        [f"F{n}@1" for n in range(MAX_FACT_IDS + 1)],
    ],
)
def test_check_fact_ids_refuses_with_a_fixed_message(ids: Any) -> None:
    with pytest.raises(ValueError) as caught:
        check_fact_ids(ids)

    message = str(caught.value)
    assert message == str(_refused(check_fact_ids, ["bad"]))
    assert "bad" not in message


def _refused(check: Any, value: Any) -> ValueError:
    with pytest.raises(ValueError) as caught:
        check(value)
    return caught.value  # type: ignore[no-any-return]


def test_check_added_details_returns_the_stripped_details_in_order() -> None:
    assert check_added_details(["  a depot ", "a sector"]) == ("a depot", "a sector")
    assert check_added_details([]) == ()
    assert len(check_added_details(["d"] * MAX_ADDED_DETAILS)) == MAX_ADDED_DETAILS
    assert check_added_details(["x" * MAX_DETAIL_CHARS]) == ("x" * MAX_DETAIL_CHARS,)


@pytest.mark.parametrize(
    "items",
    [
        [""],
        ["   "],
        ["\n"],
        ["x" * (MAX_DETAIL_CHARS + 1)],
        ["two\nlines"],
        ["tab\there"],
        ["nul\u0000"],
        ["bidi \u202e text"],
        ["mark \u200e"],
        ["zero\u200bwidth"],
        ["tag \U000e0041"],
        [3],
        [None],
        "a depot",
        ["d"] * (MAX_ADDED_DETAILS + 1),
    ],
)
def test_check_added_details_refuses_with_a_fixed_message(items: Any) -> None:
    with pytest.raises(ValueError) as caught:
        check_added_details(items)

    message = str(caught.value)
    assert message == str(_refused(check_added_details, [""]))
    assert "depot" not in message and "lines" not in message
