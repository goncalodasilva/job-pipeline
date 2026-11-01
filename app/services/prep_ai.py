"""Typed question-task wrappers. All source wording is one untrusted context block."""

import json
from collections.abc import Sequence
from dataclasses import dataclass

from app.domain.fit import Requirement
from app.domain.question_bank import QuestionDraft, resolve_interview_questions, resolve_web_items
from app.domain.settings import Tier
from app.modules.ai import AITaskRunner, CancelToken, TaskResult, TokenUsage, UsageMeter

# Already-prepared questions sent so the model does not repeat them; bounded for cost.
_EXISTING_MAX = 20
_EXISTING_CHARS = 500


@dataclass(frozen=True)
class InterviewContext:
    application_id: str
    job_summary: str
    requirements: tuple[Requirement, ...]
    cv_text: str
    existing_questions: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompanyContext:
    application_id: str
    company: str
    title: str
    industry: str


@dataclass(frozen=True)
class QuestionOutput:
    questions: tuple[QuestionDraft, ...]
    prompt_id: str
    prompt_version: str
    model: str
    tier: Tier
    attempts: int
    usage: tuple[TokenUsage | None, ...]


def _output(result: TaskResult, questions: tuple[QuestionDraft, ...]) -> QuestionOutput:
    return QuestionOutput(
        questions,
        result.prompt_id,
        result.prompt_version,
        result.model,
        result.tier,
        result.attempts,
        result.usage,
    )


async def interview_questions(
    runner: AITaskRunner,
    context: InterviewContext,
    cancel: CancelToken,
    *,
    meter: UsageMeter,
    count: int = 10,
    ask_count: int = 3,
    known_skills: Sequence[str] = (),
    tier: Tier | None = None,
    task_id: str | None = None,
) -> QuestionOutput | None:
    if not context.requirements:
        return None
    count, ask_count = max(1, min(count, 10)), max(0, min(ask_count, 3))
    body: dict[str, object] = {
        "job_summary": context.job_summary,
        "uncovered_requirements": [
            {"id": r.id, "kind": r.kind, "text": r.text} for r in context.requirements
        ],
        "cv_sent": context.cv_text or None,
    }
    existing = [q[:_EXISTING_CHARS] for q in context.existing_questions if q][:_EXISTING_MAX]
    if existing:
        body["existing_questions"] = existing
    payload = json.dumps(body, ensure_ascii=False)
    result = await runner.run_task(
        "interview_questions",
        {
            "context": payload,
            "application_id": context.application_id,
            "count": count,
            "ask_count": ask_count,
        },
        cancel,
        meter=meter,
        tier=tier,
        task_id=task_id,
    )
    questions = resolve_interview_questions(
        result.data,
        context.requirements,
        job_text=context.job_summary + "\n" + "\n".join(r.text for r in context.requirements),
        cv_text=context.cv_text,
        known_skills=known_skills,
        count=count,
        ask_count=ask_count,
    )
    return _output(result, questions)


async def interview_web_questions(
    runner: AITaskRunner,
    context: CompanyContext,
    cancel: CancelToken,
    *,
    meter: UsageMeter,
    tier: Tier | None = None,
    task_id: str | None = None,
) -> QuestionOutput:
    payload = json.dumps(
        {"company": context.company, "title": context.title, "industry": context.industry},
        ensure_ascii=False,
    )
    result = await runner.run_task(
        "interview_web_questions",
        {
            "context": payload,
            "application_id": context.application_id,
        },
        cancel,
        meter=meter,
        tier=tier,
        task_id=task_id,
    )
    return _output(result, resolve_web_items(result.data, result.searched_urls))
