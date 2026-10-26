"""Edge cases and properties of ``check_units`` and ``approval_blockers`` (M6.3; AC-M6.1,
AC-M6.2; spec 3.1 items 2-4 and 6). They drive ``app/domain/honesty.py`` to 95% branch
coverage. The interface is the one assumed at the top of ``test_honesty_units.py``. Pure;
every value is fictional."""

import dataclasses
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.facts import FactStatus
from tests.unit.domain.test_honesty_units import (
    OPS_FACT,
    ROLE_A,
    ROLE_B,
    check,
    fact,
    flag_set,
    honesty,
    unit,
)

# --- Shape of the result -----------------------------------------------------------------------


def test_no_units_no_flags() -> None:
    assert check([], [fact(1, OPS_FACT)]) == ()
    assert tuple(honesty().approval_blockers([], [])) == ()


def test_a_flag_has_kind_claim_and_unit_and_is_frozen() -> None:
    (flag,) = check([unit("u7", "Cut time by 99%.", ("F1@1",))], [fact(1, OPS_FACT)])

    assert (flag.kind, flag.claim, flag.unit_id) == ("needs_check", "99%", "u7")
    assert isinstance(flag, honesty().HonestyFlag)
    with pytest.raises(dataclasses.FrozenInstanceError):
        flag.kind = "unsupported"


def test_units_and_current_facts_are_frozen() -> None:
    generated = unit("u1", "Text.", ("F1@1",))
    current = honesty().CurrentFact(1, 1, FactStatus.CONFIRMED)

    with pytest.raises(dataclasses.FrozenInstanceError):
        generated.text = "Other."
    with pytest.raises(dataclasses.FrozenInstanceError):
        current.version = 2


def test_one_unit_can_carry_several_flags() -> None:
    sent = [fact(1, "Built the weekly stock report at Example Widgets Ltd.")]
    wording = unit(
        "u1",
        "Built the weekly stock report for 40 users at Globex Corporation.",
        ("F1@1", "F5@1"),
        added_details=("won an award",),
    )

    found = check([wording], sent)

    assert flag_set(found, "unsupported") == {("F5@1", "u1")}
    assert flag_set(found, "needs_check") == {
        ("40", "u1"),
        ("Globex Corporation", "u1"),
        ("won an award", "u1"),
    }
    assert found[0].kind == "unsupported"  # citation flags come first


def test_a_claim_repeated_in_one_unit_is_flagged_once() -> None:
    found = check(
        [unit("u1", "Cut time by 99%, then by 99% again.", ("F1@1",))], [fact(1, OPS_FACT)]
    )

    assert [f.claim for f in found] == ["99%"]


def test_a_cited_id_repeated_in_one_unit_is_flagged_once() -> None:
    found = check([unit("u1", "Cut time.", ("F9@1", "F9@1"))], [fact(1, OPS_FACT)])

    assert [(f.kind, f.claim) for f in found] == [("unsupported", "F9@1")]


def test_flags_follow_the_order_of_the_units() -> None:
    sent = [fact(1, OPS_FACT)]
    units = [
        unit("a", "Cut time by 99%.", ("F1@1",)),
        unit("b", "Cut invoice processing time by 35%.", ("F1@1",)),
        unit("c", "Cut time.", ()),
        unit("d", "Cut time.", ("F3@1",)),
    ]

    found = check(units, sent)

    assert [(f.kind, f.unit_id) for f in found] == [
        ("needs_check", "a"),
        ("unsupported", "c"),
        ("unsupported", "d"),
    ]


# --- Structure units ---------------------------------------------------------------------------


def test_structure_units_with_exactly_a_template_heading_have_no_flags() -> None:
    units = [
        unit("h1", "Experience", kind="structure", section="Experience"),
        unit("h2", "Education", kind="structure", section="Education"),
    ]

    assert check(units, [], template_headings=("Experience", "Education")) == ()


def test_structure_unit_that_only_resembles_a_heading_is_unsupported() -> None:
    units = [
        unit("h1", "Experience and awards", kind="structure"),
        unit("h2", "Experience", kind="structure"),
    ]

    found = check(units, [], template_headings=("Experience",))

    assert [(f.kind, f.claim, f.unit_id) for f in found] == [
        ("unsupported", "Experience and awards", "h1")
    ]


def test_without_template_headings_every_structure_unit_is_unsupported() -> None:
    found = check([unit("h1", "Experience", kind="structure")], [])

    assert [(f.kind, f.unit_id) for f in found] == [("unsupported", "h1")]


def test_a_structure_unit_is_not_rescued_by_citing_a_fact() -> None:
    found = check(
        [unit("h1", "Cut invoice processing time by 35%", ("F1@1",), kind="structure")],
        [fact(1, OPS_FACT)],
        template_headings=("Experience",),
    )

    assert [(f.kind, f.unit_id) for f in found] == [("unsupported", "h1")]


# --- Roles, skills and context ----------------------------------------------------------------


def test_unit_outside_a_role_section_may_cite_facts_of_any_role() -> None:
    sent = [fact(1, "Built the weekly stock report.", role=ROLE_A)]

    found = check([unit("u1", "Built the weekly stock report.", ("F1@1",), role=None)], sent)

    assert found == ()


def test_unit_citing_facts_of_its_own_role_only_has_no_role_flag() -> None:
    sent = [
        fact(1, "Built the weekly stock report.", role=ROLE_A),
        fact(2, "Planned the weekly freight routes.", role=ROLE_B),
    ]

    found = check([unit("u1", "Planned the weekly freight routes.", ("F2@1",), role=ROLE_B)], sent)

    assert found == ()


def test_a_skill_a_cited_fact_lists_is_supported() -> None:
    """Lower case, so no capitalised-name rule can flag: only the skill path decides."""
    wording = unit("u1", "Built weekly stock dashboards in sql.", ("F1@1",))

    listed = [fact(1, "Built weekly stock dashboards.", skills=("sql",))]
    assert check([wording], listed, known_skills=("sql",)) == ()
    # The cited fact doesn't list the skill, so the skill is a claim the person must check.
    found = check([wording], [fact(1, "Built weekly stock dashboards.")], known_skills=("sql",))
    assert flag_set(found, "needs_check") == {("sql", "u1")}


def test_a_capitalised_skill_name_a_cited_fact_lists_is_supported() -> None:
    sent = [fact(1, "Built weekly stock dashboards.", skills=("Power BI",))]
    wording = unit("u1", "Built weekly stock dashboards in Power BI.", ("F1@1",))

    assert check([wording], sent, known_skills=("Power BI",)) == ()
    # The cited fact doesn't state the skill, so the name is a claim the person must check.
    found = check([wording], [fact(1, "Built weekly stock dashboards.")])
    assert flag_set(found, "needs_check") == {("Power BI", "u1")}


def test_a_skill_only_an_uncited_fact_lists_needs_check() -> None:
    sent = [
        fact(1, "Built weekly stock dashboards."),
        fact(2, "Tuned database queries.", skills=("SQL",)),
    ]

    found = check(
        [unit("u1", "Built weekly stock dashboards in SQL.", ("F1@1",))],
        sent,
        known_skills=("SQL",),
    )

    assert flag_set(found, "needs_check") == {("SQL", "u1")}


def test_a_name_in_an_uncited_sent_fact_still_needs_check() -> None:
    sent = [
        fact(1, "Built the weekly stock report."),
        fact(2, "Planned routes for Globex Corporation."),
    ]

    found = check(
        [unit("u1", "Built the weekly stock report for Globex Corporation.", ("F1@1",))], sent
    )

    assert flag_set(found, "needs_check") == {("Globex Corporation", "u1")}


@pytest.mark.parametrize("word", ["tripling", "doubling", "triple", "double", "quadruple"])
def test_a_multiplier_word_no_cited_fact_states_needs_check(word: str) -> None:
    sent = [fact(1, "Built the weekly stock report.")]

    found = check([unit("u1", f"Built the weekly stock report, {word} output.", ("F1@1",))], sent)

    assert flag_set(found, "needs_check") == {(word, "u1")}


def test_a_multiplier_word_a_cited_fact_states_is_supported() -> None:
    sent = [fact(1, "Built the weekly stock report, tripling output.")]

    assert (
        check([unit("u1", "Built the weekly stock report, tripling output.", ("F1@1",))], sent)
        == ()
    )


def test_a_look_alike_letter_inside_a_word_needs_check_as_that_word() -> None:
    """A Cyrillic "e" inside "ten" hides it from the number words; the word is the claim."""
    disguised = "t\u0435n"
    sent = [fact(1, "Built the weekly stock report.")]

    found = check(
        [unit("u1", f"Built the weekly stock report for {disguised} teams.", ("F1@1",))], sent
    )

    assert flag_set(found, "needs_check") == {(disguised, "u1")}


def test_a_word_wholly_in_another_script_is_not_flagged_for_mixing() -> None:
    sent = [fact(1, "Built the weekly stock report.")]

    found = check(
        [unit("u1", "Built the weekly stock report \u043e\u0442\u0447\u0435\u0442.", ("F1@1",))],
        sent,
    )

    assert found == ()


# --- approval_blockers -------------------------------------------------------------------------


def test_approval_blockers_flag_a_citation_that_is_not_a_fact_reference_or_does_not_exist() -> None:
    cf = honesty().CurrentFact
    units = [unit("u1", "Wording.", ("bogus", "F8@1")), unit("u2", "Wording.", ("F1@1",))]

    found = tuple(honesty().approval_blockers(units, [cf(1, 1, FactStatus.CONFIRMED)]))

    assert [(f.kind, f.claim, f.unit_id) for f in found] == [
        ("unsupported", "bogus", "u1"),
        ("unsupported", "F8@1", "u1"),
    ]


def test_approval_blockers_fail_closed_on_a_fact_that_is_not_confirmed() -> None:
    cf = honesty().CurrentFact
    units = [unit("u1", "Wording.", ("F1@1",))]

    found = tuple(honesty().approval_blockers(units, [cf(1, 1, FactStatus.DRAFT)]))

    assert [(f.kind, f.claim, f.unit_id) for f in found] == [("unsupported", "F1@1", "u1")]


def test_approval_blockers_flag_a_cited_version_newer_than_the_current_one() -> None:
    cf = honesty().CurrentFact
    units = [unit("u1", "Wording.", ("F1@3",))]

    found = tuple(honesty().approval_blockers(units, [cf(1, 2, FactStatus.CONFIRMED)]))

    assert [(f.kind, f.claim, f.unit_id) for f in found] == [("unsupported", "F1@3", "u1")]


def test_approval_blockers_ignore_structure_units_and_flag_a_repeated_id_once() -> None:
    cf = honesty().CurrentFact
    units = [
        unit("h1", "Experience", kind="structure"),
        unit("u1", "Wording.", ("F1@1", "F1@1")),
    ]

    found = tuple(honesty().approval_blockers(units, [cf(1, 1, FactStatus.ARCHIVED)]))

    assert [(f.kind, f.claim, f.unit_id) for f in found] == [("unsupported", "F1@1", "u1")]


def test_approval_blockers_do_not_recheck_wording() -> None:
    cf = honesty().CurrentFact
    units = [unit("u1", "Cut time by 99% at Globex Corporation.", ("F1@1",)), unit("u2", "Text.")]

    assert tuple(honesty().approval_blockers(units, [cf(1, 1, FactStatus.CONFIRMED)])) == ()


# --- Properties (spec 3.1 item 6) --------------------------------------------------------------

_BASE = "Built the weekly stock report with planning teams."
_INSERTED_QUANTITY: Any = st.one_of(
    st.integers(min_value=2, max_value=9_999_999).map(str),
    st.integers(min_value=1, max_value=999).map(lambda n: f"{n}%"),
    st.integers(min_value=2, max_value=999_999).map(lambda n: f"€{n}"),
    st.integers(min_value=1, max_value=999).map(lambda n: f"{n} percent"),
)


@settings(max_examples=200, deadline=None)
@given(quantity=_INSERTED_QUANTITY)
def test_check_units_flags_every_inserted_quantity(quantity: str) -> None:
    wording = f"Built the weekly stock report with {quantity} planning teams."

    found = check([unit("u1", wording, ("F1@1",))], [fact(1, _BASE)])

    assert ("needs_check", quantity, "u1") in {(f.kind, f.claim, f.unit_id) for f in found}


_PHRASES = [
    "Cut invoice processing time by 35%",
    "Saved €5,000 a year in supplier costs",
    "Hired fourteen analysts in 2021",
    "Worked at Example Widgets Ltd with Sample Freight Co",
    "Managed 4 warehouses and a team of 3.5 planners",
    "Doubled the weekly report throughput",
    "Built dashboards in Power BI and SQL",
    "Raised satisfaction from 80 percent to 95 percent",
    "Trained twelve hundred staff",
]


@settings(max_examples=200, deadline=None)
@given(phrases=st.lists(st.sampled_from(_PHRASES), min_size=1, max_size=5))
def test_check_units_never_flags_verbatim_cited_text(phrases: list[str]) -> None:
    text = ". ".join(phrases) + "."

    found = check(
        [unit("u1", text, ("F1@1",))],
        [fact(1, text)],
        known_skills=("Power BI", "SQL"),
    )

    assert found == ()


def test_unit_with_a_role_citing_a_fact_with_no_role_needs_check() -> None:
    """A role header over a line that cites only role-less facts (education, skills) would
    hang a real employer over words no work of that role supports."""
    sent = [
        fact(1, "Built the weekly stock report.", role=ROLE_A),
        fact(3, "Speaks English at work every day."),
        fact(4, "Holds a fictional degree.", role=""),
    ]
    hung = unit("u1", "Speaks English at work every day.", ("F3@1",), role=ROLE_A)
    mixed = unit(
        "u2", "Built the weekly stock report in English.", ("F1@1", "F3@1", "F4@1"), role=ROLE_A
    )

    found = check([hung, mixed], sent)

    assert [(f.kind, f.claim, f.unit_id) for f in found] == [
        ("needs_check", "F3@1", "u1"),
        ("needs_check", "F3@1", "u2"),
        ("needs_check", "F4@1", "u2"),
    ]


def test_unit_without_a_role_may_cite_facts_with_no_role() -> None:
    sent = [fact(3, "Speaks English at work every day.")]

    assert (
        check([unit("u1", "Speaks English at work every day.", ("F3@1",), role=None)], sent) == ()
    )
