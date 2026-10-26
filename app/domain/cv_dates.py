"""Role dates as a CV prints them (ADR-106 item 3). Pure: the template's closed ``date_format``
decides the shape; code, never the model, writes a role header's dates."""

from typing import Final

from app.domain.cv_templates import DateFormat
from app.domain.partial_dates import is_partial_date

_MONTHS: Final = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def format_partial_date(text: str | None, date_format: DateFormat) -> str | None:
    """A role date (``2019``, ``2019-03`` or ``2019-03-15``) in the template's format; a day is
    shown as its month, a year alone as the year in every format. None for no (or an unreadable)
    date."""
    if text is None or not is_partial_date(text):
        return None
    parts = text.split("-")
    year = parts[0]
    if len(parts) == 1 or date_format is DateFormat.YEAR:
        return year
    month = int(parts[1])
    if date_format is DateFormat.MONTH_YEAR:
        return f"{_MONTHS[month - 1]} {year}"
    if date_format is DateFormat.NUMERIC:
        return f"{month:02d}/{year}"
    return f"{year}-{month:02d}"
