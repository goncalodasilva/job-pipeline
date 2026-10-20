"""Validation for local corrections. Values are data, never prompt instructions."""

import math
import unicodedata
from collections.abc import Mapping
from datetime import date
from typing import Any

from app.domain.jobs import JOB_FIELDS

EDIT_FIELDS = (*JOB_FIELDS, "notes")


class JobEditInvalid(Exception):
    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = errors
        super().__init__("Check the highlighted fields.")


def checked_edits(values: Mapping[str, str], current: Mapping[str, Any]) -> dict[str, Any]:
    """Validate only posted fields, including their range against a retained peer."""
    clean: dict[str, Any] = {}
    errors = {}
    for field in EDIT_FIELDS:
        if field not in values:
            continue
        value = values[field].replace("\r\n", "\n").replace("\r", "\n").strip()
        limit = 10000 if field == "notes" else 2000 if field == "locations" else 500
        if len(value) > limit:
            errors[field] = f"Use {limit:,} characters or fewer."
        elif any(unicodedata.category(c).startswith("C") and c not in "\n\t" for c in value):
            errors[field] = "Remove invisible or control characters."
        elif field == "title" and not value:
            errors[field] = "Enter a job title."
        elif field in ("salary_min", "salary_max", "experience_min", "experience_max"):
            try:
                number = (
                    (int(value) if field.startswith("experience") else float(value))
                    if value
                    else None
                )
                if number is not None and (
                    number < 0 or number > 1_000_000_000 or not math.isfinite(number)
                ):
                    raise ValueError
                clean[field] = number
            except ValueError:
                errors[field] = (
                    "Enter a non-negative whole number."
                    if field.startswith("experience")
                    else "Enter a non-negative amount up to 1,000,000,000."
                )
        elif field == "posted_date" and value:
            try:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError
                clean[field] = value
            except ValueError:
                errors[field] = "Enter a date as YYYY-MM-DD."
        elif field == "locations":
            clean[field] = list(
                dict.fromkeys(line.strip() for line in value.splitlines() if line.strip())
            )
        else:
            clean[field] = value or None
    for group in ("salary", "experience"):
        low, high = group + "_min", group + "_max"
        a, b = clean.get(low, current[low]), clean.get(high, current[high])
        if a is not None and b is not None and a > b:
            field = high if high in values else low
            errors[field] = "The minimum must not exceed the maximum."
    if errors:
        raise JobEditInvalid(errors)
    return clean
