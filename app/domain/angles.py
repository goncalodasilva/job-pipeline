"""Angle proposals (M6.5; AC-M6.1 feedback flags; spec 3.1 item 5, 3.6). Pure.

``resolve_angles`` reads the model's two or three angles. A reused angle id counts only when
it was one of the ids sent in that request; any other id is ignored (``None``), not an error.
The description and rationale of each angle are compared with only the facts it cites by the
honesty checker (``check_units``): anything they say that those facts do not, a citation to a
fact that was not sent, or no citation at all, is listed in ``claims`` with the note
``NOT_IN_FACT_BANK``. A flagged angle is shown to the user, who picks, edits or writes one;
nothing is applied here.

The angle ``name`` is deliberately left out of ``check_units``: a short capitalised label
("Reliable planner") reads to the checker like an invented proper name and would be flagged
every time. That is acceptable because the name makes no claim of its own: it is shown beside
the description and the cited facts, which are checked, and the user edits or replaces it
before anything is used.

Two or three angles are required: the schema refuses any other count, and this module refuses
it too (``InvalidModelOutput``), so a caller that skipped the schema cannot store four.
"""

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from app.domain.errors import InvalidModelOutput
from app.domain.fact_selection import AIFact
from app.domain.gap_check import NOT_IN_FACT_BANK
from app.domain.honesty import GeneratedUnit, HonestyFlag, check_units

MIN_ANGLES: Final = 2
MAX_ANGLES: Final = 3


@dataclass(frozen=True)
class ProposedAngle:
    name: str
    description: str
    rationale: str
    fact_ids: tuple[str, ...]
    reuse_angle_id: str | None
    flags: tuple[HonestyFlag, ...]

    @property
    def claims(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(flag.claim for flag in self.flags))

    @property
    def note(self) -> str | None:
        return NOT_IN_FACT_BANK if self.flags else None

    def to_data(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "rationale": self.rationale,
            "fact_ids": list(self.fact_ids),
            "reuse_angle_id": self.reuse_angle_id,
            "claims": list(self.claims),
        }


@dataclass(frozen=True)
class AnglesResult:
    angles: tuple[ProposedAngle, ...]

    def to_data(self) -> dict[str, Any]:
        return {"angles": [angle.to_data() for angle in self.angles]}


def resolve_angles(
    output: Mapping[str, Any],
    sent_facts: Iterable[AIFact],
    known_angle_ids: Collection[str],
    *,
    known_skills: Iterable[str] = (),
) -> AnglesResult:
    """``known_angle_ids`` are the earlier angle ids that were sent in the request."""
    raw = output.get("angles")
    if not isinstance(raw, list | tuple) or not MIN_ANGLES <= len(raw) <= MAX_ANGLES:
        raise InvalidModelOutput(detail="angles: expected two or three angles")
    sent, skills = tuple(sent_facts), tuple(known_skills)
    parsed = [_parse(item) for item in raw]
    units = [
        GeneratedUnit(
            f"angle-{index}",
            "content",
            "angle",
            None,
            f"{angle.description}\n{angle.rationale}",
            angle.fact_ids,
        )
        for index, angle in enumerate(parsed)
    ]
    flags = check_units(units, sent, known_skills=skills)
    known = frozenset(known_angle_ids)
    return AnglesResult(
        tuple(
            ProposedAngle(
                angle.name,
                angle.description,
                angle.rationale,
                angle.fact_ids,
                angle.reuse_angle_id if angle.reuse_angle_id in known else None,
                tuple(flag for flag in flags if flag.unit_id == f"angle-{index}"),
            )
            for index, angle in enumerate(parsed)
        )
    )


@dataclass(frozen=True)
class _Parsed:
    name: str
    description: str
    rationale: str
    fact_ids: tuple[str, ...]
    reuse_angle_id: str | None


def _text(item: Mapping[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidModelOutput(detail=f"angles: {key} must be text")
    return value.strip()


def _parse(item: object) -> _Parsed:
    if not isinstance(item, Mapping):
        raise InvalidModelOutput(detail="angles: an angle must be an object")
    refs = item.get("fact_ids", ())
    if not isinstance(refs, list | tuple) or not all(isinstance(ref, str) for ref in refs):
        raise InvalidModelOutput(detail="angles: fact_ids must be a list of ids")
    reuse = item.get("reuse_angle_id")
    return _Parsed(
        _text(item, "name"),
        _text(item, "description"),
        _text(item, "rationale"),
        tuple(dict.fromkeys(refs)),
        reuse.strip() if isinstance(reuse, str) and reuse.strip() else None,
    )


__all__ = ["MAX_ANGLES", "MIN_ANGLES", "AnglesResult", "ProposedAngle", "resolve_angles"]
