"""Edge cases of the honesty check (M3.8; spec 3.1 item 5; ADR-089 item 5): number formats,
multipliers and currencies, name runs at sentence starts, and skills written with letters
that change length when folded. Pure; every value is fictional."""

from decimal import Decimal

import pytest

from app.domain.honesty import Claim, extract_quantities, new_claims


@pytest.mark.parametrize(
    ("text", "kind", "value"),
    [
        ("A 3,5 day backlog", "number", Decimal("3.5")),
        ("Release 1.2.3 shipped", "number", Decimal(123)),
        ("Served 2 million customers", "number", Decimal(2_000_000)),
        ("A €2m budget", "money", Decimal(2_000_000)),
        ("A $1bn fund", "money", Decimal(1_000_000_000)),
        ("Saved 3,000 EUR", "money", Decimal(3000)),
        ("Saved EUR 3,000", "money", Decimal(3000)),
        ("Saved 3000€", "money", Decimal(3000)),
        ("Up 35 per cent", "percent", Decimal(35)),
        ("Hired 12 thousand seasonal staff", "number", Decimal(12_000)),
        ("Planned 2,000 routes", "number", Decimal(2000)),
    ],
)
def test_quantity_formats(text: str, kind: str, value: Decimal) -> None:
    ((found_kind, found_value),) = {(q.kind, q.value) for q in extract_quantities(text)}

    assert (found_kind, found_value) == (kind, value)


def test_letters_after_a_number_are_not_a_multiplier() -> None:
    assert {(q.kind, q.value) for q in extract_quantities("Ran 5km daily")} == {
        ("number", Decimal(5))
    }


def test_a_million_more_is_flagged() -> None:
    found = new_claims("Managed 5 million people.", ["Managed 5 people."])

    assert found == (Claim("number", "5 million"),)


def test_money_and_percent_are_not_plain_numbers() -> None:
    found = new_claims("Saved €35 and 35% of the time.", ["Saved 35 hours."])

    assert set(found) == {Claim("money", "€35"), Claim("percent", "35%")}


def test_a_name_at_a_sentence_start_is_still_a_name_when_it_runs_on() -> None:
    found = new_claims("Globex Corporation hired the team. Cut costs.", ["Cut costs."])

    assert found == (Claim("name", "Globex Corporation"),)


def test_a_known_name_after_a_sentence_start_word_is_not_flagged() -> None:
    sources = ["Operations Analyst at Example Widgets Ltd"]

    assert new_claims("Across Example Widgets Ltd, cut costs.", sources) == ()


def test_i_and_punctuation_split_names() -> None:
    found = new_claims("Then I joined Globex, Initech and Umbrella.", ["Then joined."])

    assert [claim.text for claim in found] == ["Globex", "Initech", "Umbrella"]


def test_a_skill_named_like_a_name_is_left_to_the_skill_check() -> None:
    found = new_claims(
        "Built the report in Power BI.", ["Built the report."], known_skills=["Power BI"]
    )

    assert found == (Claim("skill", "Power BI"),)


def test_skill_text_falls_back_to_its_name_when_folding_changes_lengths() -> None:
    found = new_claims("Built Straße reports with sql.", ["Built reports."], known_skills=["SQL"])

    assert Claim("skill", "SQL") in found
    assert new_claims("Built reports with sql.", ["Built reports."], known_skills=[" "]) == ()


def test_blank_sources_are_skipped() -> None:
    assert new_claims("Cut costs by 35%.", [None, "", "Cut costs by 35%."]) == ()
