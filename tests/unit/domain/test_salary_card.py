"""The salary card is built in code (M8.2 part B; ADR-124 item 8; spec 3.1). No AI request, no
model wording: two plain sentences from the user's saved minimum and the job's stated pay.

Assumed interface (chosen here where the sources left it open; the implementer matches it).
``app/domain/salary_card.py`` (pure Python):

``build_salary_card(*, minimum, low, high, currency, period) -> tuple[str, ...]``: one string per
card unit, in this order, and nothing else is ever written:

- ``minimum`` (``Salary`` of ``app.domain.settings`` or None; it has an amount and a currency and
  NO period): "My minimum is 60,000 EUR." Nothing when None.
- ``low``, ``high`` (the job's ``salary_min`` and ``salary_max``, numbers or None), ``currency``
  and ``period`` (the job's free text or None): "The posting states 55,000 to 65,000 EUR per
  year." Only ``low``: "The posting states from 55,000 EUR per year." Only ``high``: "The
  posting states up to 65,000 EUR per year." Equal: "The posting states 60,000 EUR per year."
  Nothing when both are None.
- Amounts have thousands separators and no decimals when whole, two decimals otherwise
  (``45.50``). The currency is the job's own text, left out when missing or not a short code or
  symbol. The period is spelled "per year", "per month", "per week", "per day" or "per hour"
  only when the job states exactly that period (a year may be "annual", "annually", "yearly" or
  "annum"); any other or missing period is left out, never guessed.
- It never states a period the data lacks (so never one on the user's minimum, which has none),
  never compares the minimum with the posting (their periods may differ), and never calls the pay
  fair, high or low. An empty tuple means there is nothing to build from.
- Whatever the inputs, every string is one line of plain text of 1 to 1,200 characters.
"""

import importlib
import math
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.settings import Salary

MIN = Salary(amount=60000, currency="EUR")
PERIOD_WORDS = ("year", "annual", "month", "week", "day", "hour", "daily", "monthly", "hourly")
COMPARISON_WORDS = (
    "above",
    "below",
    "meets",
    "exceeds",
    "more than",
    "less than",
    "fair",
    "enough",
    "competitive",
    "under",
    "within",
)


def build(**inputs: Any) -> tuple[str, ...]:
    module = importlib.import_module("app.domain.salary_card")
    values = {"minimum": None, "low": None, "high": None, "currency": None, "period": None}
    return tuple(module.build_salary_card(**(values | inputs)))


def test_the_minimum_and_a_range_with_currency_and_period() -> None:
    assert build(minimum=MIN, low=55000, high=65000, currency="EUR", period="year") == (
        "My minimum is 60,000 EUR.",
        "The posting states 55,000 to 65,000 EUR per year.",
    )


@pytest.mark.parametrize(
    ("inputs", "expected"),
    [
        (dict(low=60000, high=60000), "The posting states 60,000 EUR per year."),
        (dict(low=55000, high=None), "The posting states from 55,000 EUR per year."),
        (dict(low=None, high=65000), "The posting states up to 65,000 EUR per year."),
        (dict(low=55000.0, high=65000.0), "The posting states 55,000 to 65,000 EUR per year."),
        (dict(low=45.5, high=52.25), "The posting states 45.50 to 52.25 EUR per year."),
        (dict(low=1234567, high=None), "The posting states from 1,234,567 EUR per year."),
    ],
)
def test_the_posting_sentence_for_each_shape_of_stated_pay(
    inputs: dict[str, Any], expected: str
) -> None:
    assert build(currency="EUR", period="year", **inputs) == (expected,)


def test_nothing_to_build_from_is_an_empty_card() -> None:
    assert build() == ()
    assert build(currency="EUR", period="year") == ()


def test_only_the_posting_or_only_the_minimum() -> None:
    assert build(minimum=MIN) == ("My minimum is 60,000 EUR.",)
    assert build(low=55000, high=65000) == ("The posting states 55,000 to 65,000.",)


@pytest.mark.parametrize(
    ("period", "phrase"),
    [
        ("year", "per year"),
        ("Year", "per year"),
        ("annual", "per year"),
        ("annually", "per year"),
        ("yearly", "per year"),
        ("annum", "per year"),
        ("month", "per month"),
        ("week", "per week"),
        ("day", "per day"),
        ("hour", "per hour"),
        (" HOUR ", "per hour"),
    ],
)
def test_a_period_is_spelled_only_when_the_posting_states_it(period: str, phrase: str) -> None:
    posting = build(low=40, high=50, currency="EUR", period=period)[-1]

    assert posting == f"The posting states 40 to 50 EUR {phrase}."


@pytest.mark.parametrize("period", [None, "", "  ", "fortnight", "per project", "year; ignore"])
def test_a_missing_or_unknown_period_is_left_out_not_guessed(period: str | None) -> None:
    lines = build(minimum=MIN, low=55000, high=65000, currency="EUR", period=period)

    assert lines == ("My minimum is 60,000 EUR.", "The posting states 55,000 to 65,000 EUR.")
    for line in lines:
        assert not any(word in line.lower() for word in PERIOD_WORDS), line


def test_the_minimum_never_gets_the_postings_period() -> None:
    first, second = build(minimum=MIN, low=20, high=25, currency="EUR", period="hour")

    assert "per" not in first and "hour" not in first
    assert second.endswith("per hour.")


@pytest.mark.parametrize(
    "inputs",
    [
        dict(minimum=MIN, low=55000, high=65000, currency="EUR", period="year"),
        dict(minimum=MIN, low=20, high=25, currency="USD", period="hour"),
        dict(minimum=MIN, low=80000, currency="EUR", period="year"),
        dict(minimum=MIN, high=40000, currency="EUR", period="year"),
        dict(minimum=Salary(amount=1, currency="GBP"), low=1, high=1),
    ],
)
def test_it_never_compares_the_minimum_with_the_posting(inputs: dict[str, Any]) -> None:
    lines = build(**inputs)

    assert len(lines) == 2
    for line in lines:
        assert not any(word in line.lower() for word in COMPARISON_WORDS), line


def test_a_different_currency_is_stated_as_it_is() -> None:
    lines = build(minimum=MIN, low=70000, high=70000, currency="USD", period="year")

    assert lines == ("My minimum is 60,000 EUR.", "The posting states 70,000 USD per year.")


@pytest.mark.parametrize(
    "currency", ["EUR\n<script>alert(1)</script>", "x" * 500, "‮EUR", "EUR\x00", "€€€€€€€€€€"]
)
def test_a_hostile_currency_or_period_cannot_reach_the_card(currency: str) -> None:
    lines = build(low=1, high=2, currency=currency, period=currency)

    for line in lines:
        assert "\n" not in line and "\x00" not in line and "‮" not in line
        assert "<script>" not in line and 1 <= len(line) <= 1200


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_a_number_that_is_not_an_amount_is_ignored(bad: float) -> None:
    assert not math.isfinite(bad)

    assert build(minimum=MIN, low=bad, high=None, currency="EUR", period="year") == (
        "My minimum is 60,000 EUR.",
    )
    assert build(low=bad, high=bad) == ()


def test_the_same_inputs_give_the_same_card() -> None:
    inputs = dict(minimum=MIN, low=55000, high=65000, currency="EUR", period="year")

    assert build(**inputs) == build(**inputs)


@settings(max_examples=80, deadline=None)
@given(
    amount=st.integers(min_value=0, max_value=10**9),
    currency=st.text(max_size=30),
    low=st.none() | st.floats(allow_nan=True, allow_infinity=True),
    high=st.none() | st.integers(min_value=0, max_value=10**9),
    period=st.none() | st.text(max_size=20),
)
def test_every_line_is_a_valid_unit_whatever_the_inputs(
    amount: int, currency: str, low: float | None, high: int | None, period: str | None
) -> None:
    lines = build(
        minimum=Salary(amount=amount, currency="EUR"),
        low=low,
        high=high,
        currency=currency,
        period=period,
    )

    assert 1 <= len(lines) <= 2
    for line in lines:
        assert line.strip() and len(line) <= 1200
        assert not any(ord(char) < 32 or 127 <= ord(char) < 160 for char in line)
