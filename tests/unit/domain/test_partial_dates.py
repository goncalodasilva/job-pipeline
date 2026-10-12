"""Partial ISO dates for roles and facts (M3.3; migration 0003: "the domain checks they are
real dates").

``app.domain.partial_dates.is_partial_date(text) -> bool`` accepts exactly ``YYYY``, ``YYYY-MM``
and ``YYYY-MM-DD`` in ASCII digits naming a real year, month or day. The schema's GLOB only
checks the shape, so ``2024-13`` and ``2023-02-29`` reach the database unless the domain
refuses them.

Imports are inside the tests so each one fails on its own while the module is missing.
"""

from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

# "2024" in full-width digits, which str.isdigit() accepts.
FULL_WIDTH_YEAR = "".join(chr(0xFF10 + int(digit)) for digit in "2024")

VALID = [
    "2024",
    "1987",
    "2024-01",
    "2024-12",
    "2024-02-29",
    "2023-02-28",
    "2023-12-31",
    "2000-02-29",
]

INVALID = [
    pytest.param("", id="empty"),
    pytest.param("24", id="two-digit-year"),
    pytest.param("20245", id="five-digit-year"),
    pytest.param("0000", id="year-zero"),
    pytest.param("2024-00", id="month-zero"),
    pytest.param("2024-13", id="month-thirteen"),
    pytest.param("2024-1", id="one-digit-month"),
    pytest.param("2024-02-30", id="february-thirtieth"),
    pytest.param("2023-02-29", id="not-a-leap-year"),
    pytest.param("1900-02-29", id="century-not-a-leap-year"),
    pytest.param("2024-04-31", id="april-thirty-first"),
    pytest.param("2024-01-00", id="day-zero"),
    pytest.param("2024-01-32", id="day-thirty-two"),
    pytest.param("2024-1-05", id="one-digit-month-with-day"),
    pytest.param("2024-01-5", id="one-digit-day"),
    pytest.param(" 2024", id="leading-space"),
    pytest.param("2024\n", id="trailing-newline"),
    pytest.param("2024-03-05T10:00", id="date-and-time"),
    pytest.param("2024/03/05", id="slashes"),
    pytest.param("03-2024", id="month-first"),
    pytest.param("March 2024", id="words"),
    pytest.param(FULL_WIDTH_YEAR, id="full-width-digits"),
    pytest.param("2024-", id="dangling-dash"),
]


@pytest.mark.parametrize("text", VALID)
def test_real_partial_dates_accepted(text: str) -> None:
    from app.domain.partial_dates import is_partial_date

    assert is_partial_date(text) is True


@pytest.mark.parametrize("text", INVALID)
def test_impossible_or_misshapen_dates_refused(text: str) -> None:
    from app.domain.partial_dates import is_partial_date

    assert is_partial_date(text) is False


@given(st.dates(min_value=date(1, 1, 1)))
def test_every_real_date_and_its_month_and_year_are_accepted(day: date) -> None:
    from app.domain.partial_dates import is_partial_date

    full = day.isoformat()
    assert is_partial_date(full)
    assert is_partial_date(full[:7])
    assert is_partial_date(full[:4])


@given(st.text(max_size=12))
def test_accepted_text_always_parses_as_a_date(text: str) -> None:
    from app.domain.partial_dates import is_partial_date

    if is_partial_date(text):
        assert text.isascii()
        # Filling the missing parts with 01 gives a real date.
        date.fromisoformat(text + "-01-01"[len(text) - 4 :] if len(text) < 10 else text)
