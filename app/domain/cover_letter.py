"""Resolution of a cover letter (M6.9; AC-M6.1 to AC-M6.4; spec 3.1, 3.6; ADR-106 items 5 and 8).
Pure: no I/O, clock or ids of its own.

``resolve_letter_paragraphs`` turns the model's ``draft_cover_letter`` answer into ``DocumentUnit``
rows (kind ``content``, section ``body``, no role, origin ``ai``) and lists the honesty flags of
``check_units``, as ``resolve_cv_units`` does for a CV. The greeting, the opening line, the
closing and the name are built by code and are never a unit. The job's title and company may
support a capitalised name in a letter paragraph (``name_sources``) and nothing else: quantities,
skills and every other name are still checked against the cited facts.
"""

from collections.abc import Callable, Iterable, Mapping
from typing import Any, Final

from app.domain.documents import DocumentUnit, UnitOrigin
from app.domain.fact_selection import AIFact
from app.domain.honesty import GeneratedUnit, check_units
from app.domain.settings import has_control_characters
from app.domain.tailored_cv import (
    MAX_DETAIL_CHARS,
    DroppedUnit,
    TailoredCv,
    in_unit_order,
    said_of,
    strings_of,
    without_invisible,
)

MAX_LETTER_PARAGRAPHS: Final = 8
MAX_PARAGRAPH_TEXT: Final = 1200
LETTER_SECTION: Final = "body"
TONES: Final = ("warm", "formal", "direct")
LENGTHS: Final = ("short", "medium", "long")


def _usable(value: str) -> bool:
    return (
        bool(value.strip())
        and len(value) <= MAX_PARAGRAPH_TEXT
        and not has_control_characters(value)
    )


def _default_ids() -> Callable[[], str]:
    counter = iter(range(1, MAX_LETTER_PARAGRAPHS + 1))
    return lambda: f"letter-{next(counter)}"


def resolve_letter_paragraphs(
    output: Mapping[str, Any],
    sent_facts: Iterable[AIFact],
    *,
    name_sources: Iterable[str] = (),
    known_skills: Iterable[str] = (),
    new_id: Callable[[], str] | None = None,
) -> TailoredCv:
    """The letter of one ``draft_cover_letter`` answer. ``sent_facts`` are the facts the request
    held (from ``facts_for_ai``); ``name_sources`` are the job's title and company. ``new_id``
    makes the id of each kept paragraph (once per kept paragraph, in order). Paragraphs beyond
    ``MAX_LETTER_PARAGRAPHS`` and paragraphs with unusable text are dropped and listed."""
    sent = tuple(sent_facts)
    raw_items = output.get("paragraphs") if isinstance(output, Mapping) else None
    entries = list(raw_items) if isinstance(raw_items, list | tuple) else []
    make_id = new_id or _default_ids()
    units: list[DocumentUnit] = []
    generated: list[GeneratedUnit] = []
    dropped: list[DroppedUnit] = []
    for index, raw in enumerate(entries):
        text = said_of(raw, "text")
        ids = strings_of(raw.get("fact_ids")) if isinstance(raw, Mapping) else None
        details = strings_of(raw.get("added_details", ())) if isinstance(raw, Mapping) else None
        if index >= MAX_LETTER_PARAGRAPHS:
            dropped.append(DroppedUnit(index, "too_many", LETTER_SECTION, text))
            continue
        if (
            ids is None
            or details is None
            or not _usable(text)
            or not all(
                len(d) <= MAX_DETAIL_CHARS and not has_control_characters(d) for d in details
            )
        ):
            dropped.append(DroppedUnit(index, "invalid", LETTER_SECTION, text))
            continue
        text = without_invisible(text)
        details = tuple(dict.fromkeys(d for d in map(without_invisible, details) if d.strip()))
        if not text.strip():
            dropped.append(DroppedUnit(index, "invalid", LETTER_SECTION, ""))
            continue
        unit_id = make_id()
        units.append(
            DocumentUnit(
                unit_id, "content", LETTER_SECTION, None, text, UnitOrigin.AI, ids, details
            )
        )
        generated.append(
            GeneratedUnit(unit_id, "content", LETTER_SECTION, None, text, ids, details)
        )
    checked = check_units(
        generated, sent, known_skills=known_skills, name_sources=tuple(name_sources)
    )
    return TailoredCv(
        units=tuple(units), flags=in_unit_order(units, {}, checked), dropped=tuple(dropped)
    )


__all__ = [
    "LENGTHS",
    "LETTER_SECTION",
    "MAX_LETTER_PARAGRAPHS",
    "MAX_PARAGRAPH_TEXT",
    "TONES",
    "resolve_letter_paragraphs",
]
