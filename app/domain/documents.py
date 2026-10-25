"""Tailored documents: their statuses and the merge of a regeneration (spec 3.2; ADR-090,
ADR-106 items 5, 8 and 10).

Pure: no I/O, clock or ids. The enumerations hold the values 0006's CHECK constraints accept.
Text and fact ids are data: nothing here changes, normalises or escapes them.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal

from app.domain.facts import FactRef


class DocumentKind(StrEnum):
    CV = "cv"
    COVER_LETTER = "cover_letter"


class DocumentStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    SENT = "sent"


class UnitOrigin(StrEnum):
    AI = "ai"
    USER = "user"


# Forward only: the user approves a draft; marking the application applied sends the approved
# versions and freezes them. Editing is a new draft version, never a status change.
ALLOWED_TRANSITIONS: Final[Mapping[DocumentStatus, frozenset[DocumentStatus]]] = MappingProxyType(
    {
        DocumentStatus.DRAFT: frozenset({DocumentStatus.APPROVED}),
        DocumentStatus.APPROVED: frozenset({DocumentStatus.SENT}),
    }
)


def can_transition(source: DocumentStatus, target: DocumentStatus) -> bool:
    return target in ALLOWED_TRANSITIONS.get(source, frozenset())


@dataclass(frozen=True)
class DocumentUnit:
    """One line or paragraph of a stored document version. ``origin`` is ``USER`` for a line
    the user wrote or edited (an edited AI line keeps its ids). ``removed`` is a tombstone: the
    user removed the line; it isn't shown but is remembered so a regeneration doesn't bring it
    back. ``fact_ids`` are the ids as written, such as ``F12@3``."""

    unit_id: str
    kind: Literal["structure", "content"]
    section: str
    role: str | None
    text: str
    origin: UnitOrigin
    fact_ids: tuple[str, ...] = ()
    added_details: tuple[str, ...] = ()
    removed: bool = False


FactKey = frozenset[int | str]


def fact_key(fact_ids: Sequence[str]) -> FactKey:
    """The facts a unit cites, ignoring versions, order and repeats: ``F2@1`` and ``F2@3`` are
    the same fact. An id that is not a canonical reference stands for itself."""
    key: set[int | str] = set()
    for fact_id in fact_ids:
        try:
            key.add(FactRef.parse(fact_id).number)
        except ValueError:
            key.add(fact_id)
    return frozenset(key)


def _is_kept(unit: DocumentUnit) -> bool:
    return unit.origin == UnitOrigin.USER or unit.removed


def _surviving(kept: Sequence[DocumentUnit], new: Sequence[DocumentUnit]) -> list[DocumentUnit]:
    """The new units the kept ones don't already say."""
    blocked = {fact_key(u.fact_ids) for u in kept if u.fact_ids}
    sections = {u.section for u in kept if u.kind == "structure"}
    survivors: list[DocumentUnit] = []
    for unit in new:
        if unit.kind == "structure":
            wanted = unit.section not in sections
        else:
            wanted = not unit.fact_ids or fact_key(unit.fact_ids) not in blocked
        if wanted:
            survivors.append(unit)
    return survivors


def merge_regenerated(
    previous: Sequence[DocumentUnit], new: Sequence[DocumentUnit], replace_all: bool
) -> tuple[DocumentUnit, ...]:
    """The units of a regenerated version: every user edit, addition and removal stays where
    it stood, and the new units fill the other slots in order (the left-over ones follow the
    last slot). A new unit citing exactly a kept unit's facts is dropped. ``replace_all``
    keeps only the new units.

    "In place" means relative order, not an absolute index: a slot with no new unit left to fill
    it is closed, so when the new run is shorter than the previous one a kept unit ends at an
    earlier index (never ahead of a line that came before it)."""
    if replace_all:
        return tuple(new)
    kept = [u for u in previous if _is_kept(u)]
    survivors = iter(_surviving(kept, new))
    merged: list[DocumentUnit] = []
    for unit in previous:
        if _is_kept(unit):
            merged.append(unit)
        elif (replacement := next(survivors, None)) is not None:
            merged.append(replacement)
    merged.extend(survivors)
    return tuple(merged)
