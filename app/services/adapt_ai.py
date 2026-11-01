"""Typed wrapper for the ``adapt_answer`` task (M8.2 part C; add-ai-task step 4; spec 3.6;
ADR-124 items 7 and 8).

One user-started request that rewrites the person's earlier practice answer to a bank question
(from another application) for this job. The old answer, the question, the confirmed facts (only
through ``facts_for_ai``), the chosen angle and the target job (title, company, posting and
requirements) are each one untrusted block; the old answer proves nothing and is never a source
of names. Personal details and anything about pay are never inputs. The answer is resolved by
the pure rules of ``app/domain/narrative.py`` against the facts SENT, with the target job's
title and company as the only name sources. The caller owns the budget gate, the task and the
meter.
"""

import json
from collections.abc import Callable, Sequence
from typing import Final

from app.domain.fact_selection import FactRecord, facts_for_ai, prompt_fact_block
from app.domain.narrative import resolve_narrative_units
from app.domain.settings import Tier
from app.modules.ai import AITaskRunner, CancelToken, UsageMeter
from app.services.narrative_ai import NarrativeOutput, job_context
from app.services.tailoring_ai import ChosenAngle, TailoringJob

ADAPT_ANSWER: Final = "adapt_answer"


async def run_adapt_answer(
    runner: AITaskRunner,
    application_id: str,
    old_answer: str,
    question: str,
    job: TailoringJob,
    records: Sequence[FactRecord],
    angle: ChosenAngle | None,
    cancel: CancelToken,
    *,
    meter: UsageMeter,
    tier: Tier | None = None,
    task_id: str | None = None,
    known_skills: Sequence[str] = (),
    new_id: Callable[[], str] | None = None,
) -> NarrativeOutput:
    """One adaptation request. ``records`` are all stored fact rows; only the usable ones are
    sent. Raises ``ValueError`` without usable facts or without an old answer,
    ``InvalidModelOutput`` after the one retry (an answer with no paragraph included), provider
    errors as they come."""
    facts = facts_for_ai(records)
    if not facts:
        raise ValueError("An adaptation request needs at least one usable confirmed fact.")
    if not old_answer.strip():
        raise ValueError("An adaptation request needs an earlier answer.")
    result = await runner.run_task(
        ADAPT_ANSWER,
        {
            "old_answer": old_answer,
            "question": question,
            "facts": prompt_fact_block(facts),
            "angle": ""
            if angle is None
            else json.dumps(
                {"name": angle.name, "description": angle.description}, ensure_ascii=False
            ),
            "job_context": job_context(job),
            "application_id": application_id,
        },
        cancel,
        meter=meter,
        tier=tier,
        task_id=task_id,
    )
    resolved = resolve_narrative_units(
        result.data,
        facts,
        name_sources=[name for name in (job.title, job.company) if name],
        known_skills=known_skills,
        new_id=new_id,
    )
    return NarrativeOutput(
        result=resolved,
        prompt_id=result.prompt_id,
        prompt_version=result.prompt_version,
        model=result.model,
        tier=result.tier,
        attempts=result.attempts,
        usage=result.usage,
        fact_refs=tuple(str(fact.ref) for fact in facts),
    )
