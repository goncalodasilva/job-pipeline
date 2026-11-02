"""Pure rules for the debrief after an interview (M8.3 part A; AC-M8.5; ADR-124 item 9).

What the person writes (the questions they were asked, how it went, the experiences they
mentioned) is untrusted text. These functions only check its shape; no message here holds the
text itself, so an error can be shown or logged safely.
"""

import re
import unicodedata
from typing import Final

DEBRIEF_WINDOW_DAYS: Final = 14
MAX_DEBRIEF_TEXT_CHARS: Final = 4000
MAX_ASKED_QUESTIONS: Final = 20
MAX_QUESTION_CHARS: Final = 1000
MAX_EXPERIENCE_PARAGRAPHS: Final = 3

NEXT_STEPS: Final = ("next_round", "offer", "rejected", "other")
# The status each next step proposes (a proposal only: the person confirms it).
STATUS_BY_STEP: Final = {"next_round": "interviewing", "offer": "offer", "rejected": "rejected"}

TOO_MANY_QUESTIONS: Final = f"List at most {MAX_ASKED_QUESTIONS} questions."
QUESTION_TOO_LONG: Final = f"Keep each question under {MAX_QUESTION_CHARS + 1:,} characters."
QUESTIONS_TOO_LONG: Final = f"Keep the questions within {MAX_DEBRIEF_TEXT_CHARS:,} characters."
HIDDEN_CHARACTER: Final = "Remove the hidden or direction-changing characters from this text."
TOO_MANY_PARAGRAPHS: Final = (
    f"Use at most {MAX_EXPERIENCE_PARAGRAPHS} paragraphs, separated by a blank line."
)
EXPERIENCES_TOO_LONG: Final = f"Keep this within {MAX_DEBRIEF_TEXT_CHARS:,} characters."
HOW_TOO_LONG: Final = EXPERIENCES_TOO_LONG

_LINE_END = re.compile(r"\r\n|\r|\n")
_CLOSED = "closed"
_ENDED_ALREADY = {"next_round": frozenset({"interviewing", "offer"}), "offer": frozenset({"offer"})}


def _hidden(char: str) -> bool:
    """Control, line-separator, bidirectional-mark and other format characters (zero-width
    space and joiners, BOM, word joiner, tag characters) that must never be stored."""
    n = ord(char)
    return (
        n < 32
        or 0x7F <= n <= 0x9F
        or n in {0x061C, 0x200E, 0x200F, 0x2028, 0x2029}
        or 0x202A <= n <= 0x202E
        or 0x2066 <= n <= 0x2069
        or unicodedata.category(char) == "Cf"
    )


def _plain(text: str) -> str:
    """A tab is allowed in pasted text and kept as a space."""
    return text.replace("\t", " ")


def suggest_status(next_step: str | None, current_key: str, current_phase: str) -> str | None:
    """The status to propose after the interview, only when it is a real change the person has
    to approve; None otherwise (also for an unknown or blank step). Never the current key."""
    target = STATUS_BY_STEP.get(next_step or "")
    if target is None or current_phase == _CLOSED:
        return None
    if current_key in _ENDED_ALREADY.get(next_step or "", frozenset()):
        return None
    return None if target == current_key else target


def parse_asked_questions(text: str) -> tuple[str, ...]:
    """The questions asked, one per line, trimmed, blank lines dropped, a repeated line kept
    once. ``ValueError`` (a fixed message) for too many, too long or hidden characters."""
    text = _plain(text)
    if any(_hidden(c) for c in _LINE_END.sub("", text)):
        raise ValueError(HIDDEN_CHARACTER)
    questions = tuple(dict.fromkeys(line.strip() for line in _LINE_END.split(text) if line.strip()))
    if any(len(q) > MAX_QUESTION_CHARS for q in questions):
        raise ValueError(QUESTION_TOO_LONG)
    if len(questions) > MAX_ASKED_QUESTIONS:
        raise ValueError(TOO_MANY_QUESTIONS)
    if len("\n".join(questions)) > MAX_DEBRIEF_TEXT_CHARS:
        raise ValueError(QUESTIONS_TOO_LONG)
    return questions


def split_experiences(text: str) -> tuple[str, ...]:
    """The experiences as paragraphs split on blank lines, each trimmed. ``ValueError`` (a
    fixed message) for too many paragraphs, too long a text or hidden characters."""
    lines = _LINE_END.split(_plain(text))
    if any(_hidden(c) for line in lines for c in line):
        raise ValueError(HIDDEN_CHARACTER)
    if len("\n".join(lines)) > MAX_DEBRIEF_TEXT_CHARS:
        raise ValueError(EXPERIENCES_TOO_LONG)
    paragraphs: list[str] = []
    current: list[str] = []
    for line in [*lines, ""]:
        if line.strip():
            current.append(line)
        elif current:
            paragraphs.append("\n".join(current).strip())
            current = []
    if len(paragraphs) > MAX_EXPERIENCE_PARAGRAPHS:
        raise ValueError(TOO_MANY_PARAGRAPHS)
    return tuple(paragraphs)


def check_how_it_went(text: str) -> str:
    """The "how it went" notes as they are kept: tabs as spaces, newlines and spacing kept.
    ``ValueError`` (a fixed message) for hidden characters or more than the characters a debrief
    keeps."""
    kept = _plain(text)
    if any(_hidden(c) for c in _LINE_END.sub("", kept)):
        raise ValueError(HIDDEN_CHARACTER)
    if len(kept) > MAX_DEBRIEF_TEXT_CHARS:
        raise ValueError(HOW_TOO_LONG)
    return kept
