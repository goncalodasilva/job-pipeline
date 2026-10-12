"""Partial ISO dates of roles and facts: a year, a month or a day (migration 0003). Pure."""

import re
from datetime import date
from typing import Final

from app.domain.errors import FactInputInvalid

# ASCII digits only; the schema's GLOB checks this shape and nothing more.
_SHAPE: Final = re.compile(r"([0-9]{4})(?:-([0-9]{2})(?:-([0-9]{2}))?)?")
DATE_MESSAGE: Final = "Use a year, a month or a full date, such as 2024-03."


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
