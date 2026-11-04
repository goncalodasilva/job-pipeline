"""The rules for the values and other spellings the Lists editor saves (M9.2 part D; ADR-131
items 6-7; ADR-064). Pure Python.

A controlled value is stored trimmed and lower-cased. Two texts mean the same value when their
``value_key`` is equal (case, accents, spaces and punctuation do not tell them apart), the same
rule "Values to map" uses. Within one field no value or other spelling may share a key with
another. Messages are fixed copy: they never contain the typed text.
"""

from collections.abc import Iterable
from typing import Final

from app.domain.errors import ControlledValueInvalid
from app.domain.job_normalization import value_key

EDITABLE_FIELDS: Final = ("industry", "seniority", "employment_type")
LISTED_FIELDS: Final = (*EDITABLE_FIELDS, "work_mode")  # work mode is shown, never edited
MAX_LENGTH: Final = 200

NEEDS_TEXT: Final = "Type a value."
NEEDS_LETTER: Final = "Use at least one letter or number."
TOO_LONG: Final = f"Use {MAX_LENGTH} characters or fewer."
VALUE_TAKEN: Final = "This list already has a value or spelling like that."
ALIAS_TAKEN: Final = "One of these spellings already belongs to another value in this list."


def split_lines(text: str) -> list[str]:
    """The non-blank lines of a text box (CR LF, LF or CR), each trimmed."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return [line.strip() for line in lines if line.strip()]


def clean_value(text: str, taken: Iterable[str]) -> str:
    """The text to store for a new value, or ``ControlledValueInvalid`` on ``value``. ``taken``
    holds the ``value_key`` of every value and spelling of the field."""
    shown = text.strip().lower()
    if not shown:
        raise ControlledValueInvalid({"value": NEEDS_TEXT})
    if len(shown) > MAX_LENGTH:
        raise ControlledValueInvalid({"value": TOO_LONG})
    key = value_key(shown)
    if not key:
        raise ControlledValueInvalid({"value": NEEDS_LETTER})
    if key in set(taken):
        raise ControlledValueInvalid({"value": VALUE_TAKEN})
    return shown


def clean_aliases(text: str, own: str, taken: Iterable[str]) -> list[str]:
    """The other spellings to store for the value ``own``: one per line, trimmed and lower-cased,
    repeats and the value's own spelling dropped, in the order typed; ``ControlledValueInvalid``
    on ``aliases`` when one is too long, has no letter or number, or already belongs to another
    value or spelling of the field (``taken`` holds those keys, not this value's own)."""
    others = set(taken)
    seen = {value_key(own)}
    kept: list[str] = []
    for line in split_lines(text):
        alias = line.lower()
        if len(alias) > MAX_LENGTH:
            raise ControlledValueInvalid({"aliases": TOO_LONG})
        key = value_key(alias)
        if not key:
            raise ControlledValueInvalid({"aliases": NEEDS_LETTER})
        if key in others:
            raise ControlledValueInvalid({"aliases": ALIAS_TAKEN})
        if key not in seen:
            seen.add(key)
            kept.append(alias)
    return kept


def skill_alias_lines(text: str) -> list[str]:
    """The lines of a skill's other-spellings box, as typed (a skill keeps the user's spelling;
    ``SkillService`` trims, single-spaces and compares them); ``ControlledValueInvalid`` on
    ``aliases`` when one is too long."""
    lines = split_lines(text)
    if any(len(line) > MAX_LENGTH for line in lines):
        raise ControlledValueInvalid({"aliases": TOO_LONG})
    return lines
