"""Mechanical near-duplicate similarity between two wordings (M3.7; ADR-089 item 7: no AI call).

Two texts are compared as bags of words: letter case, whitespace and punctuation don't count,
and the score is the Dice coefficient of the two word multisets, ``2 * shared / (len(a) +
len(b))``. It is symmetric, between 0 and 1, and 1 only for the same words. One word more or
less in a sentence stays above ``SUGGEST_AT``; a single word inside a longer sentence doesn't.
A text with no word in it is never a duplicate of anything. Pure Python, no I/O.
"""

import re
from collections import Counter
from typing import Final

SUGGEST_AT: Final = 0.8
"""The score from which two wordings are suggested as possible duplicates."""

_WORD: Final = re.compile(r"[^\W_]+")


def words(text: str) -> Counter[str]:
    """The text's words as a multiset (letter case folded): what is compared. Make it once
    per text when one text is compared with many."""
    return Counter(_WORD.findall(text.casefold()))


def words_similarity(first: Counter[str], second: Counter[str]) -> float:
    """``similarity`` of two texts already split by ``words``."""
    total = sum(first.values()) + sum(second.values())
    if not total:
        return 0.0
    shared = sum((first & second).values())
    return 2.0 * shared / total


def similarity(a: str, b: str) -> float:
    """How alike two wordings are, from 0.0 (no word shared, or a blank text) to 1.0."""
    return words_similarity(words(a), words(b))


def is_near_duplicate(a: str, b: str) -> bool:
    """Whether two wordings are alike enough to be suggested as one fact said twice."""
    return similarity(a, b) >= SUGGEST_AT
