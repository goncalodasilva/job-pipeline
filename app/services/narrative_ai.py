"""Typed wrapper for the ``draft_narrative`` task (M8.2 part B; add-ai-task step 4; spec 3.6;
ADR-124 items 7 and 8).

One user-started request for one narrative card. The job (title, company, posting and
requirements), the confirmed facts (only through ``facts_for_ai``), the chosen angle, the
company's saved notes and the latest saved company brief (both only for ``why_this_company``)
are each one untrusted block. The card kind is the only trusted input, checked here against the
fixed set. Personal details and anything about pay are never inputs, so no request can hold
them. The answer is resolved by the pure rules of ``app/domain/narrative.py`` before anything is
stored or shown. The caller owns the budget gate, the task and the meter.
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from app.domain.company_brief import BriefSection, brief_prompt_text
from app.domain.fact_selection import FactRecord, facts_for_ai, prompt_fact_block
from app.domain.narrative import CARD_KINDS, resolve_narrative_units
from app.domain.settings import Tier
from app.domain.tailored_cv import TailoredCv
from app.modules.ai import AITaskRunner, CancelToken, UsageMeter
from app.services.tailoring_ai import ChosenAngle, TailoringJob, TaskOutput

DRAFT_NARRATIVE: Final = "draft_narrative"
COMPANY_CARD: Final = "why_this_company"
MAX_NOTES_CHARS: Final = 4000
MAX_BRIEF_CHARS: Final = 4000


@dataclass(frozen=True, kw_only=True)
class NarrativeOutput(TaskOutput):
    """``result`` holds the resolved units and their honesty flags (never an approval)."""

    result: TailoredCv


def job_context(job: TailoringJob) -> str:
    """The job as one JSON text: title, company, posting and requirements (untrusted)."""
    return json.dumps(
        {
            "title": job.title,
            "company": job.company,
            "posting": job.posting,
            "requirements": [
                {"requirement_id": item.id, "kind": item.kind, "text": item.text}
                for item in job.requirements
            ],
        },
        ensure_ascii=False,
    )


async def run_draft_narrative(
    runner: AITaskRunner,
    application_id: str,
    card_kind: str,
    job: TailoringJob,
    records: Sequence[FactRecord],
    angle: ChosenAngle | None,
    company_notes: str,
    cancel: CancelToken,
    *,
    meter: UsageMeter,
    tier: Tier | None = None,
    task_id: str | None = None,
    known_skills: Sequence[str] = (),
    new_id: Callable[[], str] | None = None,
    brief: Sequence[BriefSection] = (),
) -> NarrativeOutput:
    """One card request. ``records`` are all stored fact rows; only the usable ones are sent.
    ``company_notes`` and ``brief`` (the latest saved company brief, cut at 4,000 characters from
    the end) reach the request only for ``why_this_company``. Raises ``ValueError``
    for a kind that is not a written card or without usable facts, ``InvalidModelOutput`` after
    the one retry, provider errors as they come."""
    if card_kind not in CARD_KINDS:
        raise ValueError("A narrative request needs one of the written card kinds.")
    facts = facts_for_ai(records)
    if not facts:
        raise ValueError("A narrative request needs at least one usable confirmed fact.")
    notes = company_notes[:MAX_NOTES_CHARS] if card_kind == COMPANY_CARD else ""
    result = await runner.run_task(
        DRAFT_NARRATIVE,
        {
            "card_kind": card_kind,
            "job_context": job_context(job),
            "facts": prompt_fact_block(facts),
            "angle": ""
            if angle is None
            else json.dumps(
                {"name": angle.name, "description": angle.description}, ensure_ascii=False
            ),
            "company_notes": notes,
            "brief": brief_prompt_text(brief, MAX_BRIEF_CHARS) if card_kind == COMPANY_CARD else "",
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
