"""Resolution of a tailored CV (M6.7; AC-M6.1, AC-M6.2; spec 3.1 items 2-4, 3.6; ADR-106 items
8 and 12). Pure: no I/O, clock or ids of its own.

``resolve_cv_units`` turns the model's ``tailor_cv`` answer into ``DocumentUnit`` rows (origin
``ai``) and lists the honesty flags of ``check_units``. It never decides approval: a flag is for
the user to resolve (edit, remove or confirm), and ``approval_blockers`` is read when the user
approves. The answer is validated against the schema before it gets here; this module still
treats every value defensively and never raises on a shape it does not expect.

What code does, so the model cannot:

- units beyond ``MAX_CV_UNITS`` and units whose section is not one of the approved template's
  sections are dropped, and so are units with unusable text (blank, over ``MAX_UNIT_TEXT``
  characters, a control or bidi character); each is listed in ``dropped``, never silently lost;
- headings, role headers and the contact block come from code, so a model structure unit that
  only repeats a template heading is dropped (reason ``heading``); any other structure unit is
  kept as a structure unit and ``check_units`` flags it ``unsupported`` (uncited content);
- a role label that is not the role of any fact sent is not kept on the unit (so no invented
  role header can reach the document) and is flagged ``unsupported`` with the label as the claim.
  A role label that is one of the sent facts' roles but not the role of a cited fact is flagged
  by ``check_units`` (``needs_check`` on the cited id).
"""

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from app.domain.cv_templates import TemplateSpec
from app.domain.documents import DocumentUnit, UnitOrigin
from app.domain.fact_selection import AIFact
from app.domain.honesty import GeneratedUnit, HonestyFlag, UnitKind, check_units
from app.domain.settings import has_control_characters

MAX_CV_UNITS: Final = 80
MAX_UNIT_TEXT: Final = 400
MAX_FACT_IDS: Final = 20
MAX_ADDED_DETAILS: Final = 10
MAX_DETAIL_CHARS: Final = 200
_KINDS: Final = frozenset({"content", "structure"})
_FACT_ID: Final = re.compile(r"F[0-9]+@[0-9]+")
# Invisible characters that change nothing on screen but can hide a word from a comparison:
# zero-width space, non-joiner and joiner, word joiner, byte-order mark and the tag block.
_INVISIBLE: Final = re.compile("[\u200b-\u200d\u2060\ufeff\U000e0000-\U000e007f]")
_FACT_IDS_MESSAGE: Final = f"Give at most {MAX_FACT_IDS} fact references, each written like F12@3."
_DETAILS_MESSAGE: Final = (
    f"Give at most {MAX_ADDED_DETAILS} details, each one line of 1 to {MAX_DETAIL_CHARS} "
    "characters."
)

DropReason = Literal["too_many", "too_long", "unknown_section", "heading", "invalid"]


@dataclass(frozen=True)
class DroppedUnit:
    """A unit of the model's answer that code did not keep. ``index`` is its position in the
    answer; ``section`` and ``text`` are what it said (empty when it said none)."""

    index: int
    reason: DropReason
    section: str
    text: str


@dataclass(frozen=True)
class TailoredCv:
    """``units`` in the model's order, all origin ``ai`` and not removed. ``flags`` come in unit
    order, a role flag before the checker's flags of its unit; each flag's ``unit_id`` is a
    unit here. ``dropped`` lists what was not kept. Nothing here is an approval."""

    units: tuple[DocumentUnit, ...]
    flags: tuple[HonestyFlag, ...]
    dropped: tuple[DroppedUnit, ...]


@dataclass(frozen=True)
class _Accepted:
    index: int
    kind: UnitKind
    section: str
    role: str | None
    text: str
    fact_ids: tuple[str, ...]
    added_details: tuple[str, ...]


def strings_of(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list | tuple) or not all(isinstance(item, str) for item in value):
        return None
    return tuple(dict.fromkeys(value))


def said_of(raw: object, key: str) -> str:
    value = raw.get(key) if isinstance(raw, Mapping) else None
    return value if isinstance(value, str) else ""


def without_invisible(value: str) -> str:
    return _INVISIBLE.sub("", value)


def _usable_text(value: str) -> bool:
    return bool(value.strip()) and len(value) <= MAX_UNIT_TEXT and not has_control_characters(value)


def _usable_detail(value: str) -> bool:
    return len(value) <= MAX_DETAIL_CHARS and not has_control_characters(value)


def check_fact_ids(ids: Iterable[str]) -> tuple[str, ...]:
    """The fact references a user supplied, each once and in order. Each must be written
    ``F<number>@<version>``; at most ``MAX_FACT_IDS``. Anything else raises ``ValueError`` with a
    fixed message (it never echoes the input)."""
    if isinstance(ids, str):
        raise ValueError(_FACT_IDS_MESSAGE)
    values = tuple(ids)
    if not all(isinstance(item, str) and _FACT_ID.fullmatch(item) for item in values):
        raise ValueError(_FACT_IDS_MESSAGE)
    unique = tuple(dict.fromkeys(values))
    if len(unique) > MAX_FACT_IDS:
        raise ValueError(_FACT_IDS_MESSAGE)
    return unique


def check_added_details(items: Iterable[str]) -> tuple[str, ...]:
    """The added details a user supplied, stripped, in order: at most ``MAX_ADDED_DETAILS``,
    each non-empty after stripping, at most ``MAX_DETAIL_CHARS`` characters, with no control,
    bidi or invisible character. Anything else raises ``ValueError`` with a fixed message."""
    if isinstance(items, str):
        raise ValueError(_DETAILS_MESSAGE)
    values = tuple(items)
    if len(values) > MAX_ADDED_DETAILS or not all(isinstance(item, str) for item in values):
        raise ValueError(_DETAILS_MESSAGE)
    stripped = tuple(item.strip() for item in values)
    for item in stripped:
        if not item or len(item) > MAX_DETAIL_CHARS:
            raise ValueError(_DETAILS_MESSAGE)
        if has_control_characters(item) or _INVISIBLE.search(item):
            raise ValueError(_DETAILS_MESSAGE)
    return stripped


def _accept(
    index: int, raw: object, sections: frozenset[str], headings: frozenset[str]
) -> _Accepted | DroppedUnit:
    def drop(reason: DropReason) -> DroppedUnit:
        return DroppedUnit(index, reason, said_of(raw, "section"), said_of(raw, "text"))

    if not isinstance(raw, Mapping):
        return drop("invalid")
    kind, section, text, role = (
        raw.get("kind"),
        raw.get("section"),
        raw.get("text"),
        raw.get("role"),
    )
    fact_ids, details = strings_of(raw.get("fact_ids")), strings_of(raw.get("added_details", ()))
    if kind not in _KINDS or not isinstance(section, str) or not isinstance(text, str):
        return drop("invalid")
    if fact_ids is None or details is None or not (role is None or isinstance(role, str)):
        return drop("invalid")
    if not _usable_text(text) or not all(_usable_detail(item) for item in details):
        return drop("invalid")
    text = without_invisible(text)
    details = tuple(dict.fromkeys(d for d in map(without_invisible, details) if d.strip()))
    if not text.strip():
        return drop("invalid")
    if section not in sections:
        return drop("unknown_section")
    if kind == "structure":
        if text in headings:
            return drop("heading")
        return _Accepted(index, "structure", section, None, text, (), ())
    return _Accepted(
        index, "content", section, (role or "").strip() or None, text, fact_ids, details
    )


def _default_ids() -> Callable[[], str]:
    counter = iter(range(1, MAX_CV_UNITS + 1))
    return lambda: f"cv-{next(counter)}"


def resolve_cv_units(
    output: Mapping[str, Any],
    sent_facts: Iterable[AIFact],
    template: TemplateSpec,
    *,
    known_skills: Iterable[str] = (),
    new_id: Callable[[], str] | None = None,
) -> TailoredCv:
    """The tailored CV of one ``tailor_cv`` answer. ``sent_facts`` are the facts the request
    held (from ``facts_for_ai``); ``template`` is the approved template that was sent. ``new_id``
    makes the id of each kept unit (called once per kept unit, in order); the default numbers
    them ``cv-1``, ``cv-2`` and so on."""
    sent = tuple(sent_facts)
    sections = frozenset(section.id.value for section in template.sections)
    headings = frozenset(template.headings)
    roles = frozenset(fact.role for fact in sent if fact.role)
    raw_units = output.get("units") if isinstance(output, Mapping) else None
    entries = list(raw_units) if isinstance(raw_units, list | tuple) else []
    make_id = new_id or _default_ids()

    accepted: list[_Accepted] = []
    dropped: list[DroppedUnit] = []
    for index, raw in enumerate(entries):
        if index >= MAX_CV_UNITS:
            dropped.append(
                DroppedUnit(index, "too_many", said_of(raw, "section"), said_of(raw, "text"))
            )
            continue
        item = _accept(index, raw, sections, headings)
        if isinstance(item, DroppedUnit):
            dropped.append(item)
        else:
            accepted.append(item)

    units: list[DocumentUnit] = []
    role_flags: dict[str, HonestyFlag] = {}
    generated: list[GeneratedUnit] = []
    for item in accepted:
        unit_id = make_id()
        role = item.role
        if role is not None and role not in roles:
            role_flags[unit_id] = HonestyFlag("unsupported", role, unit_id)
            role = None
        units.append(
            DocumentUnit(
                unit_id,
                item.kind,
                item.section,
                role,
                item.text,
                UnitOrigin.AI,
                item.fact_ids,
                item.added_details,
            )
        )
        generated.append(
            GeneratedUnit(
                unit_id, item.kind, item.section, role, item.text, item.fact_ids, item.added_details
            )
        )
    checked = check_units(
        generated, sent, template_headings=template.headings, known_skills=known_skills
    )
    return TailoredCv(
        units=tuple(units),
        flags=in_unit_order(units, role_flags, checked),
        dropped=tuple(dropped),
    )


def in_unit_order(
    units: Sequence[DocumentUnit],
    role_flags: Mapping[str, HonestyFlag],
    checked: Sequence[HonestyFlag],
) -> tuple[HonestyFlag, ...]:
    flags: list[HonestyFlag] = []
    for unit in units:
        if unit.unit_id in role_flags:
            flags.append(role_flags[unit.unit_id])
        flags.extend(flag for flag in checked if flag.unit_id == unit.unit_id)
    return tuple(flags)


__all__ = [
    "MAX_ADDED_DETAILS",
    "MAX_CV_UNITS",
    "MAX_DETAIL_CHARS",
    "MAX_FACT_IDS",
    "MAX_UNIT_TEXT",
    "DropReason",
    "DroppedUnit",
    "TailoredCv",
    "check_added_details",
    "check_fact_ids",
    "resolve_cv_units",
]
