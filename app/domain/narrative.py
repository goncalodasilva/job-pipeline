"""Resolution of a narrative card (M8.2 part B; spec 3.1 items 1 to 6, 3.6; ADR-124 items 7 and
8). Pure: no I/O, clock or ids of its own.

A card is a short answer the person gives in an interview. ``resolve_narrative_units`` turns the
model's ``draft_narrative`` answer into ``DocumentUnit`` rows (kind ``content``, section
``answer``, no role, origin ``ai``) and lists the honesty flags of ``check_units``, as
``resolve_letter_paragraphs`` does for a cover letter. Code, not the model, limits the card to
``MAX_NARRATIVE_UNITS`` units and ``MAX_NARRATIVE_CHARS`` characters in all (the first unit that
passes the total ends the card); a dropped unit is listed, never silently lost. The job's title
and company may support a capitalised name and nothing else. An empty answer is an empty card, not
an error. Nothing here approves anything.
"""

import itertools
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

CARD_KINDS: Final = (
    "tell_me_about_yourself",
    "why_this_role",
    "why_this_company",
    "why_leaving",
    "strengths",
    "weaknesses",
)
REUSABLE_KINDS: Final = frozenset(
    {"tell_me_about_yourself", "why_leaving", "strengths", "weaknesses"}
)
MAX_NARRATIVE_UNITS: Final = 6
MAX_NARRATIVE_CHARS: Final = 1200
NARRATIVE_SECTION: Final = "answer"


def _usable(value: str) -> bool:
    return (
        bool(value.strip())
        and len(value) <= MAX_NARRATIVE_CHARS
        and not has_control_characters(value)
    )


def _default_ids() -> Callable[[], str]:
    counter = itertools.count(1)
    return lambda: f"narrative-{next(counter)}"


def resolve_narrative_units(
    output: Mapping[str, Any],
    sent_facts: Iterable[AIFact],
    *,
    name_sources: Iterable[str] = (),
    known_skills: Iterable[str] = (),
    new_id: Callable[[], str] | None = None,
) -> TailoredCv:
    """The units of one ``draft_narrative`` answer. ``sent_facts`` are the facts the request held
    (from ``facts_for_ai``); ``name_sources`` are the job's title and company. ``new_id`` makes
    the id of each kept unit (once per kept unit, in order). Never raises, whatever the shape."""
    sent = tuple(sent_facts)
    raw_items = output.get("units") if isinstance(output, Mapping) else None
    entries = list(raw_items) if isinstance(raw_items, list | tuple) else []
    make_id = new_id or _default_ids()
    units: list[DocumentUnit] = []
    generated: list[GeneratedUnit] = []
    dropped: list[DroppedUnit] = []
    total = 0
    full = False
    for index, raw in enumerate(entries):
        text = said_of(raw, "text")
        ids = strings_of(raw.get("fact_ids")) if isinstance(raw, Mapping) else None
        details = strings_of(raw.get("added_details", ())) if isinstance(raw, Mapping) else None
        if index >= MAX_NARRATIVE_UNITS:
            dropped.append(DroppedUnit(index, "too_many", NARRATIVE_SECTION, text))
            continue
        if (
            ids is None
            or details is None
            or not _usable(text)
            or not all(
                len(d) <= MAX_DETAIL_CHARS and not has_control_characters(d) for d in details
            )
        ):
            dropped.append(DroppedUnit(index, "invalid", NARRATIVE_SECTION, text))
            continue
        text = without_invisible(text)
        details = tuple(dict.fromkeys(d for d in map(without_invisible, details) if d.strip()))
        if not text.strip():
            dropped.append(DroppedUnit(index, "invalid", NARRATIVE_SECTION, ""))
            continue
        if full or total + len(text) > MAX_NARRATIVE_CHARS:
            full = True  # a later, shorter paragraph would read out of order: the card ends here
            dropped.append(DroppedUnit(index, "too_long", NARRATIVE_SECTION, text))
            continue
        total += len(text)
        unit_id = make_id()
        units.append(
            DocumentUnit(
                unit_id, "content", NARRATIVE_SECTION, None, text, UnitOrigin.AI, ids, details
            )
        )
        generated.append(
            GeneratedUnit(unit_id, "content", NARRATIVE_SECTION, None, text, ids, details)
        )
    checked = check_units(
        generated, sent, known_skills=known_skills, name_sources=tuple(name_sources)
    )
    return TailoredCv(
        units=tuple(units), flags=in_unit_order(units, {}, checked), dropped=tuple(dropped)
    )


__all__ = [
    "CARD_KINDS",
    "MAX_NARRATIVE_CHARS",
    "MAX_NARRATIVE_UNITS",
    "NARRATIVE_SECTION",
    "REUSABLE_KINDS",
    "resolve_narrative_units",
]
