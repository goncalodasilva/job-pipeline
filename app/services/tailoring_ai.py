"""Typed wrappers for the tailoring AI tasks ``gap_check``, ``propose_angles`` (M6.5) and
``tailor_cv`` (M6.7; add-ai-task step 4; spec 3.6; ADR-106 items 3 and 11).

Each wrapper maps domain inputs to the prompt's declared inputs, calls the injected
``AITaskRunner`` once (one user-started request; the caller owns the budget gate, the task
and the meter), and resolves the answer with the pure rules of ``app/domain/gap_check.py``,
``app/domain/angles.py`` and ``app/domain/tailored_cv.py``. Fact context is built here, and
only here, from stored ``FactRecord`` rows through ``facts_for_ai()``: confirmed, current and
not "never use".
Personal details and the photo are never inputs, so no request can hold them.

The callers (a later step) read the job's current summary and the fact rows, run a wrapper
in a background task, and write the output with task success. ``fact_refs`` are the fact
versions that were sent, to store beside the result.
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.domain.angles import AnglesResult, resolve_angles
from app.domain.cover_letter import LENGTHS, TONES, resolve_letter_paragraphs
from app.domain.cv_templates import TemplateSpec
from app.domain.documents import DocumentUnit
from app.domain.fact_selection import AIFact, FactRecord, facts_for_ai, prompt_fact_block
from app.domain.facts import FactRef
from app.domain.fit import Requirement
from app.domain.gap_check import GapCheckResult, resolve_gap_check
from app.domain.settings import Tier
from app.domain.tailored_cv import TailoredCv, resolve_cv_units
from app.modules.ai import AITaskRunner, CancelToken, TaskResult, TokenUsage, UsageMeter

GAP_CHECK: Final = "gap_check"
PROPOSE_ANGLES: Final = "propose_angles"
TAILOR_CV: Final = "tailor_cv"
DRAFT_COVER_LETTER: Final = "draft_cover_letter"
MAX_EARLIER_ANGLES: Final = 10
MAX_NOTE_CHARS: Final = 500
MAX_KEPT_UNITS: Final = 80


@dataclass(frozen=True)
class TailoringJob:
    """The job as the tailoring tasks read it: its id, the posting snapshot and the
    requirements of its current summary, each with its id."""

    job_id: str
    posting: str
    requirements: tuple[Requirement, ...]
    title: str = ""  # the job's title and company: sent to a cover letter request only
    company: str = ""


@dataclass(frozen=True)
class EarlierAngle:
    """An angle the user chose in another application (``id`` is its stored id)."""

    id: str
    name: str
    description: str


@dataclass(frozen=True, kw_only=True)
class TaskOutput:
    """What every stored AI output keeps (spec 3.6), plus what budgets and the page need.
    ``fact_refs`` are the fact versions sent, as F<number>@<version>."""

    prompt_id: str
    prompt_version: str
    model: str
    tier: Tier
    attempts: int
    usage: tuple[TokenUsage | None, ...]
    fact_refs: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class GapCheckOutput(TaskOutput):
    result: GapCheckResult


@dataclass(frozen=True, kw_only=True)
class AnglesOutput(TaskOutput):
    result: AnglesResult
    sent_angle_ids: tuple[str, ...]  # the earlier angle ids that were sent


@dataclass(frozen=True)
class ChosenAngle:
    """The angle chosen for the application (its name and description; its id stays with the
    caller)."""

    name: str
    description: str


@dataclass(frozen=True, kw_only=True)
class TailorCvOutput(TaskOutput):
    """``result`` holds the resolved units and their honesty flags (never an approval).
    ``withheld_unit_ids``: kept units not sent because they cite a fact that may not reach a
    request now (never-use, not confirmed, "generalize names", not a fact id, or a superseded
    version) or because more than ``MAX_KEPT_UNITS`` were kept (the earliest are withheld); the
    caller still keeps them in the document."""

    result: TailoredCv
    withheld_unit_ids: tuple[str, ...]


def _job_context(job: TailoringJob, *, names: bool = False) -> str:
    named = {"title": job.title, "company": job.company} if names else {}
    return json.dumps(
        {
            **named,
            "posting": job.posting,
            "requirements": [
                {"requirement_id": item.id, "kind": item.kind, "text": item.text}
                for item in job.requirements
            ],
        },
        ensure_ascii=False,
    )


def _provenance(result: TaskResult, facts: Sequence[AIFact]) -> dict[str, Any]:
    return {
        "prompt_id": result.prompt_id,
        "prompt_version": result.prompt_version,
        "model": result.model,
        "tier": result.tier,
        "attempts": result.attempts,
        "usage": result.usage,
        "fact_refs": tuple(str(fact.ref) for fact in facts),
    }


def _check_ready(job: TailoringJob, usable: int) -> None:
    if not job.requirements:
        raise ValueError("A tailoring request needs the job's requirements.")
    if not usable:
        raise ValueError("A tailoring request needs at least one usable confirmed fact.")


async def run_gap_check(
    runner: AITaskRunner,
    job: TailoringJob,
    records: Sequence[FactRecord],
    cancel: CancelToken,
    *,
    meter: UsageMeter,
    tier: Tier | None = None,
    task_id: str | None = None,
    known_skills: Sequence[str] = (),
) -> GapCheckOutput:
    """One gap check request. ``records`` are all stored fact rows; only the usable ones are
    sent. Raises ``ValueError`` without requirements or usable facts (the caller says what to
    do first), ``InvalidModelOutput`` after the one retry, provider errors as they come."""
    facts = facts_for_ai(records)
    _check_ready(job, len(facts))
    result = await runner.run_task(
        GAP_CHECK,
        {
            "job_context": _job_context(job),
            "facts": prompt_fact_block(facts),
            "job_id": job.job_id,
        },
        cancel,
        meter=meter,
        tier=tier,
        task_id=task_id,
    )
    resolved = resolve_gap_check(result.data, job.requirements, facts, known_skills=known_skills)
    return GapCheckOutput(result=resolved, **_provenance(result, facts))


async def run_propose_angles(
    runner: AITaskRunner,
    job: TailoringJob,
    records: Sequence[FactRecord],
    earlier_angles: Sequence[EarlierAngle],
    cancel: CancelToken,
    *,
    meter: UsageMeter,
    tier: Tier | None = None,
    task_id: str | None = None,
    known_skills: Sequence[str] = (),
) -> AnglesOutput:
    """One angle proposal request. Only the first ``MAX_EARLIER_ANGLES`` earlier angles are
    sent (the caller chooses which come first); a reused id counts only if it was sent."""
    facts = facts_for_ai(records)
    _check_ready(job, len(facts))
    sent_angles = tuple(earlier_angles[:MAX_EARLIER_ANGLES])
    result = await runner.run_task(
        PROPOSE_ANGLES,
        {
            "job_context": _job_context(job),
            "facts": prompt_fact_block(facts),
            "other_angles": json.dumps(
                [
                    {"angle_id": item.id, "name": item.name, "description": item.description}
                    for item in sent_angles
                ],
                ensure_ascii=False,
            ),
            "job_id": job.job_id,
        },
        cancel,
        meter=meter,
        tier=tier,
        task_id=task_id,
    )
    sent_ids = tuple(item.id for item in sent_angles)
    resolved = resolve_angles(result.data, facts, sent_ids, known_skills=known_skills)
    return AnglesOutput(result=resolved, sent_angle_ids=sent_ids, **_provenance(result, facts))


def _template_context(template: TemplateSpec) -> str:
    return json.dumps(
        {
            "sections": [
                {"section_id": section.id.value, "heading": heading}
                for section, heading in zip(template.sections, template.headings, strict=True)
            ],
            "max_pages": template.max_pages,
        },
        ensure_ascii=False,
    )


def _may_send(unit: DocumentUnit, current: dict[int, AIFact]) -> bool:
    """Whether every fact the unit cites may reach a request now: it is one of ``facts_for_ai``
    (so not never-use, archived, unconfirmed or missing), the cited version is not an older one
    than the current version (superseded wording), and the fact does not ask for its names to
    be withheld (the user's text may still hold them). An id that is no fact reference fails."""
    for fact_id in unit.fact_ids:
        try:
            cited = FactRef.parse(fact_id)
        except ValueError:
            return False
        fact = current.get(cited.number)
        if fact is None or fact.generalize or cited.version < fact.ref.version:
            return False
    return True


def _kept_for_ai(
    units: Sequence[DocumentUnit], facts: Sequence[AIFact]
) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    """The kept units a request may hold, and the ids of those withheld (they stay in the
    document). Withheld: a unit citing a fact that may not reach a request now (never-use,
    archived, missing, no fact id at all), a fact marked "generalize names", or a version older
    than the fact's current one (superseded). At most ``MAX_KEPT_UNITS`` of the rest are sent,
    the last ones in document order (the newest additions); earlier ones are withheld too."""
    current = {fact.ref.number: fact for fact in facts}
    sendable: list[DocumentUnit] = []
    withheld: set[str] = set()
    for unit in units:
        if unit.removed:
            continue
        if _may_send(unit, current):
            sendable.append(unit)
        else:
            withheld.add(unit.unit_id)
    overflow = max(0, len(sendable) - MAX_KEPT_UNITS)
    withheld.update(unit.unit_id for unit in sendable[:overflow])
    sent = [
        {
            "section": unit.section,
            "role": unit.role,
            "text": unit.text,
            "fact_ids": list(unit.fact_ids),
        }
        for unit in sendable[overflow:]
    ]
    return sent, tuple(unit.unit_id for unit in units if unit.unit_id in withheld)


async def run_tailor_cv(
    runner: AITaskRunner,
    job: TailoringJob,
    records: Sequence[FactRecord],
    angle: ChosenAngle | None,
    template: TemplateSpec,
    note: str,
    keep_units: Sequence[DocumentUnit],
    cancel: CancelToken,
    *,
    meter: UsageMeter,
    tier: Tier | None = None,
    task_id: str | None = None,
    known_skills: Sequence[str] = (),
    new_id: Callable[[], str] | None = None,
) -> TailorCvOutput:
    """One CV generation request for the approved ``template``. ``records`` are all stored fact
    rows; only the usable ones are sent. ``keep_units`` are the user's edits and additions
    (removed ones are not sent). ``note`` is the user's note for this version, at most
    ``MAX_NOTE_CHARS`` after trimming. Raises ``ValueError`` without usable facts or with a
    longer note, ``InvalidModelOutput`` after the one retry, provider errors as they come.
    ``new_id`` makes the id of each resulting unit (see ``resolve_cv_units``)."""
    facts = facts_for_ai(records)
    if not facts:
        raise ValueError("A tailoring request needs at least one usable confirmed fact.")
    cleaned = note.strip()
    if len(cleaned) > MAX_NOTE_CHARS:
        raise ValueError(f"The note can be at most {MAX_NOTE_CHARS} characters.")
    kept, withheld = _kept_for_ai(keep_units, facts)
    result = await runner.run_task(
        TAILOR_CV,
        {
            "job_context": _job_context(job),
            "facts": prompt_fact_block(facts),
            "angle": json.dumps(
                None if angle is None else {"name": angle.name, "description": angle.description},
                ensure_ascii=False,
            ),
            "template": _template_context(template),
            "note": json.dumps({"note": cleaned}, ensure_ascii=False),
            "keep_units": json.dumps(kept, ensure_ascii=False),
            "job_id": job.job_id,
        },
        cancel,
        meter=meter,
        tier=tier,
        task_id=task_id,
    )
    resolved = resolve_cv_units(
        result.data, facts, template, known_skills=known_skills, new_id=new_id
    )
    return TailorCvOutput(result=resolved, withheld_unit_ids=withheld, **_provenance(result, facts))


async def run_draft_cover_letter(
    runner: AITaskRunner,
    job: TailoringJob,
    records: Sequence[FactRecord],
    angle: ChosenAngle | None,
    tone: str,
    length: str,
    note: str,
    keep_units: Sequence[DocumentUnit],
    cancel: CancelToken,
    *,
    meter: UsageMeter,
    tier: Tier | None = None,
    task_id: str | None = None,
    known_skills: Sequence[str] = (),
    new_id: Callable[[], str] | None = None,
) -> TailorCvOutput:
    """One cover letter request. The job's title and company go to the model and may support
    names in the paragraphs (and nothing else). ``tone`` and ``length`` are words the caller has
    already checked against their sets. The greeting, opening line, closing and name are built
    by code and never sent. Otherwise like ``run_tailor_cv``; ``result`` holds the paragraphs as
    units."""
    if tone not in TONES or length not in LENGTHS:
        raise ValueError("A letter request needs a tone and a length from the fixed sets.")
    facts = facts_for_ai(records)
    if not facts:
        raise ValueError("A tailoring request needs at least one usable confirmed fact.")
    cleaned = note.strip()
    if len(cleaned) > MAX_NOTE_CHARS:
        raise ValueError(f"The note can be at most {MAX_NOTE_CHARS} characters.")
    kept, withheld = _kept_for_ai(keep_units, facts)
    result = await runner.run_task(
        DRAFT_COVER_LETTER,
        {
            "job_context": _job_context(job, names=True),
            "facts": prompt_fact_block(facts),
            "angle": json.dumps(
                None if angle is None else {"name": angle.name, "description": angle.description},
                ensure_ascii=False,
            ),
            "tone": tone,
            "length": length,
            "note": json.dumps({"note": cleaned}, ensure_ascii=False),
            "keep_units": json.dumps(
                [{"text": unit["text"], "fact_ids": unit["fact_ids"]} for unit in kept],
                ensure_ascii=False,
            ),
            "job_id": job.job_id,
        },
        cancel,
        meter=meter,
        tier=tier,
        task_id=task_id,
    )
    resolved = resolve_letter_paragraphs(
        result.data,
        facts,
        name_sources=[name for name in (job.title, job.company) if name],
        known_skills=known_skills,
        new_id=new_id,
    )
    return TailorCvOutput(result=resolved, withheld_unit_ids=withheld, **_provenance(result, facts))
