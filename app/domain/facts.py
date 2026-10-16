"""Fact lifecycle, content versions and fact references (spec 3.1 item 1, 3.2; ADR-089 item 11).

Pure: no I/O and no clock. The enumerations hold the values 0003's CHECK constraints accept.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Self

from app.domain.cv_checks import word_tokens


class FactStatus(StrEnum):
    """The values 0003's CHECK on ``facts.status`` accepts."""

    DRAFT = "draft"
    CONFIRMED = "confirmed"
    DISCARDED = "discarded"
    ARCHIVED = "archived"


class FactType(StrEnum):
    FACT = "fact"
    STORY = "story"


class Privacy(StrEnum):
    """``never_use`` facts are never sent to AI; ``generalize`` ones are sent with an
    instruction to generalize names (ADR-089 item 6)."""

    NORMAL = "normal"
    NEVER_USE = "never_use"
    GENERALIZE = "generalize"


class RoleStatus(StrEnum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"


# The only status changes a fact may make: the user confirms a draft; a draft is discarded (or
# closed by a merge) and undo brings it back as a draft; a confirmed fact is archived and
# restored. Editing adds a version and is not a status change.
ALLOWED_TRANSITIONS: Final[Mapping[FactStatus, frozenset[FactStatus]]] = MappingProxyType(
    {
        FactStatus.DRAFT: frozenset({FactStatus.CONFIRMED, FactStatus.DISCARDED}),
        FactStatus.DISCARDED: frozenset({FactStatus.DRAFT}),
        FactStatus.CONFIRMED: frozenset({FactStatus.ARCHIVED}),
        FactStatus.ARCHIVED: frozenset({FactStatus.CONFIRMED}),
    }
)


def can_transition(source: FactStatus, target: FactStatus) -> bool:
    return target in ALLOWED_TRANSITIONS.get(source, frozenset())


def clean_part(text: str | None) -> str | None:
    """One part of a fact as it is stored and compared: CRLF and lone CR line endings become
    LF (a browser posts CRLF for a textarea), surrounding whitespace is dropped and a blank
    part is ``None``. Services pass what they store through this."""
    if text is None:
        return None
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return cleaned or None


@dataclass(frozen=True)
class FactContent:
    """What one fact version says: a plain text, story parts, or both."""

    text: str | None = None
    situation: str | None = None
    action: str | None = None
    result: str | None = None

    def normalized(self) -> tuple[str, str, str, str]:
        """The parts as compared between versions, each through ``clean_part``: line endings
        are LF, surrounding whitespace is dropped and a missing part equals a blank one, since
        a form posts "" for an empty field. Case, punctuation and inner spacing all count."""
        return (
            clean_part(self.text) or "",
            clean_part(self.situation) or "",
            clean_part(self.action) or "",
            clean_part(self.result) or "",
        )


def next_version(current: int, old: FactContent, new: FactContent) -> int | None:
    """The version number an edit from ``old`` to ``new`` gets, or ``None`` when nothing
    changed and no version is made (AC-M3.3). ``current`` is the fact's current version number."""
    if current < 1:
        raise ValueError("version numbers start at 1")
    if old.normalized() == new.normalized():
        return None
    return current + 1


def _words(content: FactContent) -> frozenset[str]:
    """The word tokens of every part joined (``cv_checks.word_tokens``: shared normaliser,
    without case)."""
    return word_tokens(" ".join(part for part in content.normalized() if part))


def still_reads_as(current: FactContent, earlier: FactContent) -> bool:
    """Whether the user's ``current`` wording still reads as ``earlier``, a version a provider
    made: at least half of its word tokens occur in ``earlier`` (the practice badge follows
    the current wording). Wording without a word doesn't."""
    words = _words(current)
    if not words:
        return False
    return 2 * len(words & _words(earlier)) >= len(words)


# ASCII digits only and no leading zeros, so each reference has exactly one spelling.
_FACT_REF: Final = re.compile(r"F([1-9][0-9]*)@([1-9][0-9]*)")


@dataclass(frozen=True)
class FactRef:
    """One version of one fact as cited in AI requests and answers: ``F12@3`` is fact number
    12 at content version 3 (spec 3.1 item 1)."""

    number: int
    version: int

    def __post_init__(self) -> None:
        # Exactly int: True would print as FTrue@1 and 1.5 as F1.5@1.
        if type(self.number) is not int or type(self.version) is not int:
            raise ValueError("fact numbers and version numbers are whole numbers")
        if self.number < 1 or self.version < 1:
            raise ValueError("fact numbers and version numbers start at 1")

    def __str__(self) -> str:
        return f"F{self.number}@{self.version}"

    @classmethod
    def parse(cls, text: str) -> Self:
        """Read the exact canonical form only. Cited ids are compared with the ids that were
        sent (spec 3.1 item 3), so a near miss is an unknown id, never a guess."""
        match = _FACT_REF.fullmatch(text)
        if match is None:
            raise ValueError("not a fact reference")
        return cls(int(match.group(1)), int(match.group(2)))
