"""The salary card, built in code (M8.2 part B; ADR-124 item 8; spec 3.1). Pure.

No AI request and no model wording: at most two plain sentences from the person's saved minimum
and the job's stated pay. The minimum has no period, so none is ever written for it; the card
never compares the minimum with the posting (their periods may differ) and never calls the pay
fair, high or low. A currency is the job's own text, kept only when it is a three-letter code or
a currency symbol; a period is spelled only when the job states exactly one of the known ones.
"""

import math
import re
import unicodedata
from typing import Final

from app.domain.settings import Salary

_CODE: Final = re.compile(r"[A-Za-z]{3}")
_PERIODS: Final = {
    "year": "per year",
    "annual": "per year",
    "annually": "per year",
    "yearly": "per year",
    "annum": "per year",
    "month": "per month",
    "week": "per week",
    "day": "per day",
    "hour": "per hour",
}


def _currency(value: str | None) -> str:
    text = (value or "").strip()
    if _CODE.fullmatch(text):
        return text
    if 1 <= len(text) <= 2 and all(unicodedata.category(char) == "Sc" for char in text):
        return text
    return ""


def _period(value: str | None) -> str:
    return _PERIODS.get((value or "").strip().lower(), "")


def _amount(value: float | int | None) -> float | None:
    """The number when it is a finite, non-negative amount; else None."""
    if value is None or isinstance(value, bool):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _money(value: float) -> str:
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.2f}"


def build_salary_card(
    *,
    minimum: Salary | None,
    low: float | None,
    high: float | None,
    currency: str | None,
    period: str | None,
) -> tuple[str, ...]:
    """One string per card unit: the minimum, then the posting's stated pay. An empty tuple
    means there is nothing to build from."""
    lines: list[str] = []
    if minimum is not None:
        lines.append(f"My minimum is {minimum.amount:,} {minimum.currency}.")
    first, second = _amount(low), _amount(high)
    if first is not None and second is not None:
        first, second = min(first, second), max(first, second)
        stated = _money(first) if first == second else f"{_money(first)} to {_money(second)}"
    elif first is not None:
        stated = f"from {_money(first)}"
    elif second is not None:
        stated = f"up to {_money(second)}"
    else:
        return tuple(lines)
    tail = " ".join(part for part in (_currency(currency), _period(period)) if part)
    lines.append(f"The posting states {stated}{' ' + tail if tail else ''}.")
    return tuple(lines)


__all__ = ["build_salary_card"]
