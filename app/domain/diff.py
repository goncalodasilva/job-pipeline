"""Word-level diff between two wordings, for the fact history and Refine with AI (AC-M3.3).

Pure. The segments keep every character of both texts: the equal and added ones joined give
the new text, the equal and removed ones the old text. The text is returned as it came, never
as markup; templates escape it.
"""

import difflib
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class DiffOp(StrEnum):
    EQUAL = "equal"
    ADDED = "added"
    REMOVED = "removed"


@dataclass(frozen=True)
class DiffSegment:
    op: DiffOp
    text: str


# A run of whitespace or a run of anything else, so the tokens rebuild the text exactly and a
# word is never split.
_TOKEN: Final = re.compile(r"\s+|\S+")


def word_diff(old: str, new: str) -> tuple[DiffSegment, ...]:
    """The changes from ``old`` to ``new`` as whole words and the spacing between them."""
    before: list[str] = _TOKEN.findall(old)
    after: list[str] = _TOKEN.findall(new)
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    segments: list[DiffSegment] = []
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            segments.append(DiffSegment(DiffOp.EQUAL, "".join(before[old_start:old_end])))
            continue
        if old_end > old_start:
            segments.append(DiffSegment(DiffOp.REMOVED, "".join(before[old_start:old_end])))
        if new_end > new_start:
            segments.append(DiffSegment(DiffOp.ADDED, "".join(after[new_start:new_end])))
    return tuple(segments)
