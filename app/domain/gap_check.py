"""Gap check resolution (M6.5; AC-M6.8; spec 3.1 item 5, 3.6; ADR-106 items 8 and 11). Pure.

``resolve_gap_check`` turns the model's answer into what the user sees, with the same rules
as the fit (``app/domain/fit.py``): a verdict of met or partial counts only when it cites at
least one fact and every cited id was sent in that request; anything else is missing, with no
ids. Valid fact references prove which facts were sent, not that the match is true, so the
page shows the user's own fact wording beside each verdict (TD-241).

Code, not the model, builds the sentence about the role: when a must-have is missing it says
"Your confirmed facts don't show N of this role's must-haves: <titles>. It may not be worth
applying for." Each framing idea is compared with only the facts it cites by the honesty
checker (``check_units``); anything it says that they do not (or a citation to a fact that
was not sent, or none) is listed in ``claims`` with the note ``NOT_IN_FACT_BANK``. Nothing is
decided here: a flagged idea is shown, never hidden or applied.

The model's answer is validated against the schema before it gets here; this module still
treats every value defensively and never raises on a shape it does not expect.
"""

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from app.domain.fact_selection import AIFact
from app.domain.fit import Requirement
from app.domain.honesty import GeneratedUnit, HonestyFlag, check_units

NOT_IN_FACT_BANK: Final = "This isn't in your fact bank."
Verdict = Literal["met", "partial", "missing"]
_CITING: Final = frozenset({"met", "partial"})
_MAX_TITLE: Final = 160


@dataclass(frozen=True)
class RequirementVerdict:
    """The verdict for one requirement after the citation rule. ``downgraded``: the model said
    met or partial but its citations did not hold, so the verdict is missing."""

    requirement_id: str
    kind: str
    text: str
    verdict: Verdict
    fact_ids: tuple[str, ...]
    downgraded: bool

    def to_data(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "kind": self.kind,
            "text": self.text,
            "verdict": self.verdict,
            "fact_ids": list(self.fact_ids),
            "downgraded": self.downgraded,
        }


@dataclass(frozen=True)
class FramingIdea:
    """One honest way to present related experience. ``flags`` come from the honesty checker;
    ``claims`` are what each flag is about (the claim, or the cited id or text), shown with
    ``note``."""

    text: str
    fact_ids: tuple[str, ...]
    flags: tuple[HonestyFlag, ...]

    @property
    def claims(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(flag.claim for flag in self.flags))

    @property
    def note(self) -> str | None:
        return NOT_IN_FACT_BANK if self.flags else None

    def to_data(self) -> dict[str, Any]:
        return {"text": self.text, "fact_ids": list(self.fact_ids), "claims": list(self.claims)}


@dataclass(frozen=True)
class GapCheckResult:
    """``verdicts`` follow the job's requirements, in order. ``missing_must_haves`` are the
    must-have verdicts that are missing; ``message`` is code-built, None when there are none."""

    verdicts: tuple[RequirementVerdict, ...]
    missing_must_haves: tuple[RequirementVerdict, ...]
    message: str | None
    framing_ideas: tuple[FramingIdea, ...]

    def to_data(self) -> dict[str, Any]:
        return {
            "requirements": [verdict.to_data() for verdict in self.verdicts],
            "missing_must_haves": [item.requirement_id for item in self.missing_must_haves],
            "message": self.message,
            "framing_ideas": [idea.to_data() for idea in self.framing_ideas],
        }


def resolve_gap_check(
    output: Mapping[str, Any],
    requirements: Sequence[Requirement],
    sent_facts: Iterable[AIFact],
    *,
    known_skills: Iterable[str] = (),
) -> GapCheckResult:
    """The result of one gap check: ``requirements`` are the job's requirements as sent,
    ``sent_facts`` the facts the request held (from ``facts_for_ai``)."""
    sent = tuple(sent_facts)
    sent_refs = frozenset(str(fact.ref) for fact in sent)
    reported = _entries(output.get("requirements"))
    verdicts = tuple(_verdict(requirement, reported, sent_refs) for requirement in requirements)
    missing = tuple(
        item for item in verdicts if item.kind == "must_have" and item.verdict == "missing"
    )
    return GapCheckResult(
        verdicts=verdicts,
        missing_must_haves=missing,
        message=_message(missing),
        framing_ideas=_framing(output.get("framing_ideas"), sent, tuple(known_skills)),
    )


def _entries(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list | tuple):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _refs(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list | tuple) or not all(isinstance(ref, str) for ref in value):
        return None
    return tuple(dict.fromkeys(value))


def _verdict(
    requirement: Requirement, reported: Sequence[Mapping[str, Any]], sent: Collection[str]
) -> RequirementVerdict:
    candidates = [item for item in reported if item.get("requirement_id") == requirement.id]
    item = candidates[0] if len(candidates) == 1 else {}
    said = item.get("verdict")
    refs = _refs(item.get("fact_ids"))
    claims_support = said in _CITING
    valid = claims_support and bool(refs) and all(ref in sent for ref in refs or ())
    verdict: Verdict = said if valid else "missing"  # type: ignore[assignment]
    return RequirementVerdict(
        requirement.id,
        requirement.kind,
        requirement.text,
        verdict,
        (refs or ()) if valid else (),
        downgraded=claims_support and not valid,
    )


def _title(text: str) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= _MAX_TITLE else one_line[: _MAX_TITLE - 1].rstrip() + "…"


def _message(missing: Sequence[RequirementVerdict]) -> str | None:
    if not missing:
        return None
    titles = "; ".join(_title(item.text) for item in missing)
    return (
        f"Your confirmed facts don't show {len(missing)} of this role's must-haves: "
        f"{titles}. It may not be worth applying for."
    )


def _framing(
    value: object, sent: Sequence[AIFact], skills: Sequence[str]
) -> tuple[FramingIdea, ...]:
    ideas: list[tuple[str, tuple[str, ...]]] = []
    for item in _entries(value):
        text, refs = item.get("text"), _refs(item.get("fact_ids"))
        if isinstance(text, str) and text.strip() and refs is not None:
            ideas.append((text, refs))
    units = [
        GeneratedUnit(f"framing-{index}", "content", "framing", None, text, refs)
        for index, (text, refs) in enumerate(ideas)
    ]
    flags = check_units(units, sent, known_skills=skills)
    return tuple(
        FramingIdea(
            text,
            refs,
            tuple(flag for flag in flags if flag.unit_id == f"framing-{index}"),
        )
        for index, (text, refs) in enumerate(ideas)
    )


__all__ = [
    "NOT_IN_FACT_BANK",
    "FramingIdea",
    "GapCheckResult",
    "RequirementVerdict",
    "Verdict",
    "resolve_gap_check",
]
