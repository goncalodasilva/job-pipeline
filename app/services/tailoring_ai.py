"""Typed wrappers for the tailoring AI tasks ``gap_check`` and ``propose_angles`` (M6.5;
add-ai-task step 4; spec 3.6; ADR-106 items 3 and 11).

Each wrapper maps domain inputs to the prompt's declared inputs, calls the injected
``AITaskRunner`` once (one user-started request; the caller owns the budget gate, the task
and the meter), and resolves the answer with the pure rules of ``app/domain/gap_check.py``
and ``app/domain/angles.py``. Fact context is built here, and only here, from stored
``FactRecord`` rows through ``facts_for_ai()``: confirmed, current and not "never use".
Personal details and the photo are never inputs, so no request can hold them.

The callers (a later step) read the job's current summary and the fact rows, run a wrapper
in a background task, and write the output with task success. ``fact_refs`` are the fact
versions that were sent, to store beside the result.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.domain.angles import AnglesResult, resolve_angles
from app.domain.fact_selection import AIFact, FactRecord, facts_for_ai, prompt_fact_block
from app.domain.fit import Requirement
from app.domain.gap_check import GapCheckResult, resolve_gap_check
from app.domain.settings import Tier
from app.modules.ai import AITaskRunner, CancelToken, TaskResult, TokenUsage, UsageMeter

GAP_CHECK: Final = "gap_check"
PROPOSE_ANGLES: Final = "propose_angles"
MAX_EARLIER_ANGLES: Final = 10


@dataclass(frozen=True)
class TailoringJob:
    """The job as the tailoring tasks read it: its id, the posting snapshot and the
    requirements of its current summary, each with its id."""

    job_id: str
    posting: str
    requirements: tuple[Requirement, ...]


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


def _job_context(job: TailoringJob) -> str:
    return json.dumps(
        {
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
