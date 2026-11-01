"""Typed wrapper for the ``answer_feedback`` task (M8.2; add-ai-task step 4; spec 3.6).

One user-started request: the interview question, the practice answer, the confirmed facts
(only through ``facts_for_ai``) and the CV text sent are each one untrusted block. The model's
output is resolved by the pure rules of ``app/domain/answer_feedback.py`` before anything is
stored or shown. The caller owns the budget gate, the task and the meter.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from app.domain.answer_feedback import Feedback, resolve_feedback
from app.domain.fact_selection import FactRecord, facts_for_ai, prompt_fact_block
from app.domain.settings import Tier
from app.modules.ai import AITaskRunner, CancelToken, TokenUsage, UsageMeter

ANSWER_FEEDBACK: Final = "answer_feedback"


@dataclass(frozen=True)
class AnswerContext:
    application_id: str
    question: str
    answer: str
    cv_sent: str  # "" when no CV was sent


@dataclass(frozen=True, kw_only=True)
class FeedbackOutput:
    feedback: Feedback
    fact_refs: tuple[str, ...]  # the sent fact versions the suggestions point at
    prompt_id: str
    prompt_version: str
    model: str
    tier: Tier
    attempts: int
    usage: tuple[TokenUsage | None, ...]


async def answer_feedback(
    runner: AITaskRunner,
    context: AnswerContext,
    records: Sequence[FactRecord],
    cancel: CancelToken,
    *,
    meter: UsageMeter,
    tier: Tier | None = None,
    task_id: str | None = None,
    known_skills: Sequence[str] = (),
) -> FeedbackOutput:
    """One feedback request. ``records`` are all stored fact rows; only the usable ones are
    sent, and only they support a claim. ``InvalidModelOutput`` after the one retry, provider
    errors as they come."""
    facts = facts_for_ai(records)
    result = await runner.run_task(
        ANSWER_FEEDBACK,
        {
            "question": context.question,
            "answer": context.answer,
            "facts": prompt_fact_block(facts),
            "cv_sent": context.cv_sent,
            "application_id": context.application_id,
        },
        cancel,
        meter=meter,
        tier=tier,
        task_id=task_id,
    )
    feedback = resolve_feedback(
        result.data, context.answer, facts, cv_text=context.cv_sent, known_skills=known_skills
    )
    return FeedbackOutput(
        feedback=feedback,
        fact_refs=feedback.fact_refs,
        prompt_id=result.prompt_id,
        prompt_version=result.prompt_version,
        model=result.model,
        tier=result.tier,
        attempts=result.attempts,
        usage=result.usage,
    )
