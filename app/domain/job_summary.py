"""Conservative checks on untrusted job summaries; never changes triage or fit."""

import re
from collections.abc import Iterator, Mapping
from decimal import Decimal
from typing import Any, Final

from app.domain.cv_checks import normalise
from app.domain.errors import InvalidModelOutput
from app.domain.honesty import Quantity, extract_quantities
from app.domain.partial_dates import is_partial_date

_UNSAFE: Final = re.compile(
    r"https?://|www\.|ignore\s+(?:all\s+)?(?:previous\s+)?instructions"
    r"|reveal\s+(?:all\s+)?facts|system\s+prompt",
    re.IGNORECASE,
)


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


def _quantity_key(q: Quantity) -> tuple[object, ...]:
    return q.kind, q.value, q.currency, q.form


def _period_stated(period: object, span: str) -> bool:
    if period is None:
        return True
    aliases = {
        "year": ("year", "yearly", "annual", "annually", "annum"),
        "month": ("month", "monthly"),
        "hour": ("hour", "hourly", "hr"),
        "week": ("week", "weekly"),
        "day": ("day", "daily"),
    }
    key = str(period).casefold()
    group = next((values for values in aliases.values() if key in values), (key,))
    return any(re.search(r"\b" + re.escape(word) + r"\b", span, re.IGNORECASE) for word in group)


def _experience_values(quote: object, spans: list[str]) -> set[Decimal]:
    if not isinstance(quote, str) or not quote.strip():
        return set()
    quote = normalise(quote).casefold()
    contexts = [normalise(span).casefold() for span in spans if quote in normalise(span).casefold()]
    # A source quote must describe required experience, rather than company age or a benefit.
    excluded = (
        r"\b(?:company|organization|business|founded|established|benefits?|leave|holidays?"
        r"|vacation|vesting|anniversary|ago)\b"
    )
    if not contexts or all(re.search(excluded, span) for span in contexts):
        return set()
    if not re.search(r"\b(?:experience|years?\s+(?:of|in))\b", quote):
        return set()
    quantities = [q for q in extract_quantities(quote) if q.kind == "number" and q.form is None]
    numbers = "(?:" + "|".join(re.escape(q.text) for q in quantities) + ")"
    return {
        q.value
        for q in quantities
        if re.search(
            r"(?<!\w)"
            + re.escape(q.text)
            + r"\s*\+?\s*(?:(?:-|\u2013|to)\s*"
            + numbers
            + r"\s*)?(?:years?|yrs?)\b",
            quote,
        )
    }


def checked_summary(data: Mapping[str, Any], posting: str) -> dict[str, Any]:
    # Inspect actual leaves, not JSON escape sequences: embedded whitespace stays whitespace.
    if any(_UNSAFE.search(normalise(text)) for text in _strings(data)):
        raise InvalidModelOutput()
    checked = dict(data)
    quantities = extract_quantities(posting)
    source = {_quantity_key(q) for q in quantities}
    prose = [str(data["summary"]), *(item["text"] for item in data["requirements"])]
    if any(_quantity_key(q) not in source for text in prose for q in extract_quantities(text)):
        raise InvalidModelOutput()
    spans = re.split(r"(?<=[.!?])\s+|\n", posting)
    for group in ("experience", "salary"):
        low, high = checked[group + "_min"], checked[group + "_max"]
        if group == "experience":
            values = _experience_values(checked["experience_text"], spans)
            valid = all(value is None or Decimal(str(value)) in values for value in (low, high))
        else:
            relevant = [
                span
                for span in spans
                if re.search(
                    r"\b(?:salary|pay|wages?|compensation|sal[aá]rio|salario|sueldo|gehalt)\b",
                    span,
                    re.I,
                )
            ]
            currency = checked["salary_currency"]
            currency = (
                {"€": "EUR", "$": "USD", "£": "GBP", "¥": "JPY"}.get(
                    str(currency), str(currency).upper()
                )
                if currency
                else None
            )
            valid = any(
                all(
                    value is None
                    or Decimal(str(value))
                    in {
                        q.value
                        for q in extract_quantities(span)
                        if q.kind in {"money", "number"}
                        and (currency is None or q.currency == currency)
                    }
                    for value in (low, high)
                )
                and _period_stated(checked["salary_period"], span)
                for span in relevant
            )
        if low is not None and high is not None and low > high:
            valid = False
        if not valid or (low is None and high is None):
            checked[group + "_min"] = checked[group + "_max"] = None
            extras = (
                ("experience_text",)
                if group == "experience"
                else ("salary_currency", "salary_period")
            )
            for extra in extras:
                checked[extra] = None
    posted = checked["posted_date"]
    if posted is not None and (
        len(posted) != 10 or not is_partial_date(posted) or posted not in posting
    ):
        checked["posted_date"] = None
    return checked
