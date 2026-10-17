"""Mechanical checks of a CV import's answer against the CV's own text (M3.4; AC-M3.1; spec
3.1; ADR-089 item 5). Pure.

An import only ever makes drafts; these checks decide which of them are marked
``needs_check``: a quote that isn't in the CV, wording that says more than its own quote (a
number or a word the quote doesn't hold), a role name, a role year or a skill the CV never
gives.

Both sides of every comparison go through ``normalise`` first, so that what the model was
shown and what the file holds are the same text: characters nobody sees are dropped,
ligatures, curly quotes and dashes are folded, ``&lt;`` is read as ``<`` and whitespace runs
are one space. Quotes and role names are then compared as written; words and skills without
regard to case.

Numbers go through ``app/domain/honesty.py``'s ``extract_quantities`` (M3.8; ADR-092 item
4), the one quantity reader of imports and refine. An import compares each quantity as
written (spacing aside), so it is at least as strict as the M3.4 helper it replaced: "1200"
is not "1,200" and "35 percent" is not "35%" here, since an import is told to copy the CV's
wording. Only the refine check treats equal values in other formats as equal.
"""

import re
import unicodedata
from collections.abc import Iterable
from typing import Final

from app.domain.honesty import extract_quantities
from app.domain.partial_dates import is_partial_date

_DIGITS: Final = re.compile(r"\d+")
# A run of letters and digits ("5k" is one word, and not "5").
_WORD: Final = re.compile(r"[^\W_]+")
_ESCAPED_LT: Final = "&lt;"
_YEAR_LENGTH: Final = 4


def _folds() -> dict[int, str]:
    """Characters written several ways, each to its plain form. NFKC leaves these alone."""
    groups = (
        ("'", (0x2018, 0x2019, 0x201A, 0x201B)),
        ('"', (0x201C, 0x201D, 0x201E, 0x201F)),
        ("-", (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015)),
        # The signs a prompt shows as "&lt;", like "<" itself (the prompt loader's LT_LIKE).
        ("<", (0x2329, 0x3008, 0x27E8, 0x2039)),
    )
    return {code: plain for plain, codes in groups for code in codes}


_FOLDS: Final = _folds()


def _quantities_as_written(text: str) -> frozenset[str]:
    """The quantities written in digits in ``text``, each as written without its spacing
    and case: "60" is not "6", "1,200" is not "1200", "35 %" is "35%". Numbers written in
    words ("fourteen") are left to ``has_words_of``, as before M3.8."""
    return frozenset(
        "".join(q.text.split()).casefold()
        for q in extract_quantities(text)
        if _DIGITS.search(q.text)
    )


def word_tokens(text: str) -> frozenset[str]:
    """The words of ``text`` (runs of letters and digits), normalised and without case."""
    return frozenset(_WORD.findall(normalise(text).casefold()))


def one_space(text: str) -> str:
    """``text`` with every run of whitespace as one space and none at the ends."""
    return " ".join(text.split())


def unescape_lt(text: str) -> str:
    """``text`` with ``&lt;`` as ``<``: a prompt shows untrusted text with that sign escaped,
    and an answer may copy the escape."""
    return text.replace(_ESCAPED_LT, "<")


def normalise(text: str) -> str:
    """``text`` as it is compared: ``&lt;`` as ``<``, no invisible (category Cf) characters
    such as a soft hyphen or a zero-width space, compatibility forms such as ligatures
    spelled out (NFKC), curly quotes straight, en and em dashes as hyphens, whitespace runs
    as one space. Case is kept."""
    visible = "".join(char for char in unescape_lt(text) if unicodedata.category(char) != "Cf")
    return one_space(unicodedata.normalize("NFKC", visible).translate(_FOLDS))


class CvText:
    """One text (a CV, or one quote from it), prepared once for the lookups made in it."""

    def __init__(self, text: str) -> None:
        self._text = normalise(text)
        self._folded = self._text.casefold()
        self._numbers = _quantities_as_written(self._text)
        self._digit_runs = frozenset(_DIGITS.findall(self._text))
        self._words = frozenset(_WORD.findall(self._folded))

    def states(self, passage: str) -> bool:
        """Whether ``passage`` is in the text as one continuous run, as written (after
        ``normalise``). A blank passage is not."""
        wanted = normalise(passage)
        return bool(wanted) and wanted in self._text

    def has_numbers_of(self, parts: Iterable[str | None]) -> bool:
        """Whether every quantity written in ``parts`` is written the same way in the text."""
        return all(
            _quantities_as_written(normalise(part)) <= self._numbers for part in parts if part
        )

    def has_words_of(self, parts: Iterable[str | None]) -> bool:
        """Whether every word of ``parts`` is a word of the text, whatever its case."""
        return all(word_tokens(part) <= self._words for part in parts if part)

    def names(self, *names: str) -> bool:
        """Whether each given name (an employer, a title) is in the text; blank ones aren't
        looked up."""
        return all(self.states(name) for name in names if name.strip())

    def mentions(self, name: str) -> bool:
        """Whether ``name`` (a skill) is in the text as whole words, whatever its case: "Go"
        is not in "Google". A blank name is not."""
        wanted = normalise(name).casefold()
        if not wanted:
            return False
        start = self._folded.find(wanted)
        while start >= 0:
            if self._stands_alone(start, start + len(wanted)):
                return True
            start = self._folded.find(wanted, start + 1)
        return False

    def has_year(self, date: str) -> bool:
        """Whether the year of a partial date ("2021", "2021-03") is written in the text, by
        itself or as a part of a dotted date ("03.2021"), never inside a longer number."""
        return date[:_YEAR_LENGTH] in self._digit_runs

    def _stands_alone(self, start: int, end: int) -> bool:
        """Whether the run from ``start`` to ``end`` doesn't cut a word at either side."""
        folded = self._folded
        cut_before = start > 0 and folded[start - 1].isalnum() and folded[start].isalnum()
        cut_after = end < len(folded) and folded[end].isalnum() and folded[end - 1].isalnum()
        return not (cut_before or cut_after)


def says_no_more_than(quote: str, parts: Iterable[str | None]) -> bool:
    """Whether a unit's wording (``parts``) holds only numbers and words of its own
    ``quote``. An import is told to keep the CV's wording, so anything added (a number from
    another line, a tool, an employer, "5k" for "5") is noticed."""
    source = CvText(quote)
    wording = tuple(parts)
    return source.has_numbers_of(wording) and source.has_words_of(wording)


def has_duplicates(keys: Iterable[str]) -> bool:
    seen: set[str] = set()
    for key in keys:
        if key in seen:
            return True
        seen.add(key)
    return False


def real_partial_date(text: str | None) -> str | None:
    """``text`` when it names a real year, month or day; None for anything else (a date the
    calendar doesn't have is dropped, not guessed)."""
    return text if text is not None and is_partial_date(text) else None
