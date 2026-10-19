"""Skill names as compared and stored (ADR-089 item 11, ADR-090 item 9). Pure."""

import unicodedata
from collections.abc import Iterable

# Control, format and surrogate characters can't be seen in a name (a zero-width space, a
# soft hyphen, NUL), so they never tell two skills apart and are never stored.
_LEFT_OUT = frozenset({"Cc", "Cf", "Cs"})


def _words(name: str) -> str:
    """``name`` single-spaced and trimmed, without the characters nobody can see. Whitespace
    is split on first: a tab or a line break is a control character and still parts words."""
    pieces = (
        "".join(char for char in piece if unicodedata.category(char) not in _LEFT_OUT)
        for piece in name.split()
    )
    return " ".join(piece for piece in pieces if piece)


def _fold(name: str) -> str:
    return _words(unicodedata.normalize("NFKC", name).casefold())


def normalize_skill(name: str) -> str:
    """What ``skills.normalized_name`` holds: compatibility forms folded (NFKC), case folded,
    control, format and surrogate characters dropped, whitespace runs as one space and the
    ends trimmed. Punctuation counts ("C", "C++" and "C#" are three skills). A blank name
    gives "". Folding repeats until nothing changes, because case folding and dropping a
    character can leave text that NFKC folds again."""
    folded = _fold(name)
    while (again := _fold(folded)) != folded:
        folded = again
    return folded


def display_skill(name: str) -> str:
    """A skill name or alias as shown: the user's spelling, trimmed and single-spaced, without
    control, format and surrogate characters."""
    return _words(name)


def skill_matches(candidate: str, name: str, aliases: Iterable[str]) -> bool:
    """Whether ``candidate`` means the skill called ``name`` with those other spellings.
    A blank candidate matches nothing."""
    wanted = normalize_skill(candidate)
    if not wanted:
        return False
    return wanted == normalize_skill(name) or any(
        wanted == normalize_skill(alias) for alias in aliases
    )
