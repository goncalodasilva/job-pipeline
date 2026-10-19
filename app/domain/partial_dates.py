"""Partial ISO dates of roles and facts: a year, a month or a day (migration 0003). Pure."""

import calendar
import re
from datetime import date
from typing import Final

from app.domain.errors import FactInputInvalid

# ASCII digits only; the schema's GLOB checks this shape and nothing more.
_SHAPE: Final = re.compile(r"([0-9]{4})(?:-([0-9]{2})(?:-([0-9]{2}))?)?")
DATE_MESSAGE: Final = "Write the date as 2024, 2024-03 or 2024-03-15."
END_BEFORE_START: Final = "The end date is before the start date."


def is_partial_date(text: str) -> bool:
    """Whether ``text`` is exactly ``YYYY``, ``YYYY-MM`` or ``YYYY-MM-DD`` naming a real year,
    month or day."""
    match = _SHAPE.fullmatch(text)
    if match is None:
        return False
    year, month, day = (int(part) if part else 1 for part in match.groups())
    try:
        date(year, month, day)
    except ValueError:
        return False
    return True


def checked_partial_date(field: str, text: str | None) -> str | None:
    """A date field as stored: ``None`` when left empty, else the trimmed text. Raises
    ``FactInputInvalid`` naming ``field`` when it isn't a real partial date."""
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    if not is_partial_date(cleaned):
        raise FactInputInvalid(field, DATE_MESSAGE)
    return cleaned


def _parts(text: str) -> tuple[int, int | None, int | None]:
    match = _SHAPE.fullmatch(text)
    if match is None:
        raise ValueError("not a partial date")
    month, day = (int(part) if part else None for part in match.groups()[1:])
    return int(match.group(1)), month, day


def earliest_day(text: str) -> date:
    """The first day a partial date can mean: 2024 is 1 January 2024."""
    year, month, day = _parts(text)
    return date(year, month or 1, day or 1)


def latest_day(text: str) -> date:
    """The last day a partial date can mean: 2024 is 31 December 2024."""
    year, month, day = _parts(text)
    month = month or 12
    return date(year, month, day or calendar.monthrange(year, month)[1])


def ends_before_start(start: str | None, end: str | None) -> bool:
    """Whether a period's end is certainly before its start: every day ``end`` can mean is
    before every day ``start`` can mean. A less precise date that may name the same period
    (2024-05 to 2024) is not. Both are checked partial dates, or None."""
    if not start or not end:
        return False
    return latest_day(end) < earliest_day(start)
