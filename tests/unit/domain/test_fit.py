"""M4.5 band thresholds, source references and comparable salary flags."""

import pytest

from app.domain.fit import Requirement, evaluate_matches, salary_below_minimum
from app.domain.settings import Salary


@pytest.mark.parametrize(
    "must_met,key_met,key_total,expected",
    [
        (True, 3, 4, "strong"),
        (True, 2, 4, "possible"),
        (True, 1, 4, "stretch"),
        (False, 4, 4, "stretch"),
        (True, 0, 0, "strong"),
        (True, 2, 3, "possible"),
        (True, 3, 3, "strong"),
        (True, 1, 2, "possible"),
        (True, 0, 1, "stretch"),
    ],
)
def test_ac_m4_3_band_follows_definitions(must_met, key_met, key_total, expected):
    requirements = [Requirement("must", "must_have", "A mandatory requirement")]
    requirements += [Requirement(f"key-{n}", "key", "A key requirement") for n in range(key_total)]
    reported = [{"requirement_id": "must", "met": must_met, "fact_ids": ["F1@1"]}]
    reported += [
        {"requirement_id": f"key-{n}", "met": n < key_met, "fact_ids": ["F1@1"]}
        for n in range(key_total)
    ]
    assert evaluate_matches(requirements, reported, {"F1@1"}).band == expected


@pytest.mark.parametrize("refs", [[], ["F999@1"], ["F1@1", "F999@1"], ["F1@2"]])
def test_ac_m4_3_unsent_or_uncited_match_counts_as_missing(refs):
    result = evaluate_matches(
        [Requirement("must", "must_have", "A mandatory requirement")],
        [{"requirement_id": "must", "met": True, "fact_ids": refs}],
        {"F1@1"},
    )
    assert result.band == "stretch"
    assert not result.matches[0].met and result.matches[0].fact_ids == ()


def test_duplicate_or_missing_requirement_cannot_raise_band():
    requirement = Requirement("must", "must_have", "A mandatory requirement")
    report = {"requirement_id": "must", "met": True, "fact_ids": ["F1@1"]}
    assert evaluate_matches([requirement], [], {"F1@1"}).band == "stretch"
    assert evaluate_matches([requirement], [report, report], {"F1@1"}).band == "stretch"
    assert (
        evaluate_matches([requirement], [dict(report, requirement_id="invented")], {"F1@1"}).band
        == "stretch"
    )


@pytest.mark.parametrize(
    "low,high,currency,period,expected",
    [
        (55000, None, "EUR", "year", True),
        (55000, None, "€", "year", True),
        (55000, None, "eur", "year", True),
        (55000, 65000, "EUR", "year", False),
        (55000, 59000, "EUR", "annual", True),
        (60000, None, "EUR", "year", False),
        (55000, None, "USD", "year", False),
        (4500, None, "EUR", "month", False),
        (55000, None, "EUR", None, False),
        (None, None, "EUR", "year", False),
    ],
)
def test_salary_below_minimum_flagged_same_currency_and_period_only(
    low, high, currency, period, expected
):
    assert (
        salary_below_minimum(low, high, currency, period, Salary(amount=60000, currency="EUR"))
        is expected
    )
