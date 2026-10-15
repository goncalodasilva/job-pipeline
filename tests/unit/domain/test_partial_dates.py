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


# --- M3.6: the days a partial date can mean, and an end that is before the start ----------------


@pytest.mark.parametrize(
    ("text", "first", "last"),
    [
        ("2024", date(2024, 1, 1), date(2024, 12, 31)),
        ("2024-02", date(2024, 2, 1), date(2024, 2, 29)),
        ("2023-02", date(2023, 2, 1), date(2023, 2, 28)),
        ("2024-04", date(2024, 4, 1), date(2024, 4, 30)),
        ("2024-03-15", date(2024, 3, 15), date(2024, 3, 15)),
    ],
)
def test_earliest_and_latest_day_of_a_partial_date(text: str, first: date, last: date) -> None:
    from app.domain.partial_dates import earliest_day, latest_day

    assert earliest_day(text) == first
    assert latest_day(text) == last


@pytest.mark.parametrize("text", ["", "soon", "2024-3", "24"])
def test_earliest_and_latest_day_refuse_what_is_not_a_partial_date(text: str) -> None:
    from app.domain.partial_dates import earliest_day, latest_day

    for day_of in (earliest_day, latest_day):
        with pytest.raises(ValueError, match="partial date"):
            day_of(text)


@pytest.mark.parametrize(
    ("start", "end", "before"),
    [
        pytest.param("2024-05", "2024", False, id="may-be-the-same-year"),
        pytest.param("2024", "2023", True, id="the-year-before"),
        pytest.param("2024-03", "2024-02-29", True, id="the-day-before-the-month"),
        pytest.param("2025", "2024-12-31", True, id="the-day-before-the-year"),
        pytest.param("2024", "2024", False, id="equal-years"),
        pytest.param("2024-03", "2024-03", False, id="equal-months"),
        pytest.param("2024-03-15", "2024-03-15", False, id="equal-days"),
        pytest.param("2024-03-15", "2024-03-14", True, id="one-day-before"),
        pytest.param("2022", "2024-06", False, id="in-order"),
        pytest.param(None, "2020", False, id="no-start"),
        pytest.param("2020", None, False, id="no-end"),
        pytest.param(None, None, False, id="neither"),
        pytest.param("", "2020", False, id="blank-start"),
    ],
)
def test_ends_before_start(start: str | None, end: str | None, before: bool) -> None:
    from app.domain.partial_dates import ends_before_start

    assert ends_before_start(start, end) is before


def test_date_message_shows_each_accepted_shape() -> None:
    from app.domain.errors import FactInputInvalid
    from app.domain.partial_dates import DATE_MESSAGE, checked_partial_date

    assert DATE_MESSAGE == "Write the date as 2024, 2024-03 or 2024-03-15."
    with pytest.raises(FactInputInvalid) as refused:
        checked_partial_date("end_date", "March 2024")
    assert (refused.value.field, refused.value.user_message) == ("end_date", DATE_MESSAGE)
