"""Contacts and interactions (M5.6): the checks for what is typed, and the wording shown.

Pure: no I/O and no clock. Names, notes and email text come from the user and are untrusted
data (spec 3.3); this module only bounds and shapes them, and the templates escape them. An
email needs only a plausible shape (nothing is delivered or looked up) and becomes a link only
when it has one.
"""

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final

CONTACT_FIELDS: Final = ("name", "role", "email", "phone", "relationship", "notes")
LIMITS: Final = {
    "name": 200,
    "role": 200,
    "email": 254,
    "phone": 50,
    "relationship": 200,
    "notes": 10000,
}
CHANNELS: Final = {
    "email": "Email",
    "call": "Call",
    "video": "Video call",
    "in_person": "In person",
    "message": "Message",
    "other": "Other",
}
INTERACTION_FIELDS: Final = ("date", "channel", "note", "contact_id")
NOTE_LIMIT: Final = 10000
NOT_THIS_COMPANY: Final = "Choose one of this company's contacts."
_JOINERS: Final = "‌‍"  # zero-width (non-)joiner: needed to spell some scripts
# Letters and digits in any script; the local part also allows . + ' and - (no ? & # % / that
# could turn a mailto link into more than an address).
_LOCAL: Final = re.compile(r"[\w+'\-]+(?:\.[\w+'\-]+)*")
_LABEL: Final = re.compile(r"[^\W_](?:[\w\-]*[^\W_])?")


class ContactInvalid(Exception):
    """One or more fields to correct, each with its words."""

    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = errors
        super().__init__("Check the highlighted fields.")


def _clean(value: str, *, multiline: bool) -> bool:
    """No control, format or invisible characters (bidi controls, zero-width space, BOM, line
    and paragraph separators), except the joiners some scripts need (and newlines in notes)."""
    if multiline:
        value = value.replace("\n", "").replace("\t", "")
    return not any(
        c not in _JOINERS
        and (unicodedata.category(c)[0] == "C" or unicodedata.category(c) in ("Zl", "Zp"))
        for c in value
    )


def valid_email_shape(value: str) -> bool:
    """Whether ``value`` looks like ``name@domain.tld``: one ``@``, a local part and a domain of
    at least two dot-separated labels, no spaces or markup. Nothing more is promised."""
    if not value or len(value) > LIMITS["email"] or value.count("@") != 1:
        return False
    local, domain = value.split("@")
    labels = domain.split(".")
    return (
        _LOCAL.fullmatch(local) is not None
        and len(labels) >= 2
        and all(_LABEL.fullmatch(label) is not None for label in labels)
    )


def mailto_link(email: str) -> str | None:
    """The ``mailto:`` link for ``email``, or None unless it has a valid shape."""
    return f"mailto:{email}" if valid_email_shape(email) else None


def checked_contact(values: Mapping[str, str]) -> dict[str, str]:
    """The six contact fields, trimmed (a missing one is empty). Raises ``ContactInvalid`` with
    an error per field to correct; the name is required and an email, when given, needs a
    valid shape."""
    clean: dict[str, str] = {}
    errors: dict[str, str] = {}
    for field in CONTACT_FIELDS:
        multiline = field == "notes"
        value = values.get(field, "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if len(value) > LIMITS[field]:
            errors[field] = f"Use {LIMITS[field]:,} characters or fewer."
        elif not _clean(value, multiline=multiline):
            errors[field] = "Remove invisible or control characters."
        elif field == "name" and not value:
            errors[field] = "Enter the contact's name."
        elif field == "email" and value and not valid_email_shape(value):
            errors[field] = "Enter an address like name@example.com, or leave it empty."
        else:
            clean[field] = value
    if errors:
        raise ContactInvalid(errors)
    return clean


@dataclass(frozen=True)
class InteractionInput:
    day: date
    channel: str
    note: str


def checked_interaction(values: Mapping[str, str]) -> InteractionInput:
    """The interaction's date, channel and note. Raises ``ContactInvalid`` with an error per
    field (the contact is checked by the caller, which knows the company's contacts)."""
    errors: dict[str, str] = {}
    day = date.min
    raw = values.get("date", "")
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw):
            raise ValueError
        day = date.fromisoformat(raw)
    except ValueError:
        errors["date"] = "Choose a valid date."
    channel = values.get("channel", "")
    if channel not in CHANNELS:
        errors["channel"] = "Choose how you were in touch."
    note = values.get("note", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(note) > NOTE_LIMIT:
        errors["note"] = f"Use {NOTE_LIMIT:,} characters or fewer."
    elif not _clean(note, multiline=True):
        errors["note"] = "Remove invisible or control characters."
    if errors:
        raise ContactInvalid(errors)
    return InteractionInput(day, channel, note)


def interaction_instant(day: date) -> datetime:
    """The instant stored for an interaction on ``day``: noon UTC, so the calendar day stays the
    one typed in nearly every time zone. It is shown by its stored date, never converted."""
    return datetime(day.year, day.month, day.day, 12, tzinfo=UTC)
