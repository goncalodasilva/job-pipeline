"""Typed wrapper for the ``company_brief`` task (M8.3 part B; add-ai-task step 4; spec 3.6;
ADR-124 items 2 to 4).

One user-started request. The company's own stored name, website and industry are the only
inputs and travel as one untrusted block; no fact, job text, CV text or personal detail is sent.
The answer goes through ``resolve_brief`` (searched urls only) before anything is stored or shown.
The caller owns the budget gate, the task and the meter.
"""

import json
from dataclasses import dataclass

from app.domain.company_brief import BriefSection, resolve_brief
from app.domain.settings import Tier
from app.modules.ai import AITaskRunner, CancelToken, TokenUsage, UsageMeter

COMPANY_BRIEF = "company_brief"
# The stored fields are untrusted and may be long; only this much of each is sent.
MAX_NAME = 200
MAX_WEBSITE = 500
MAX_INDUSTRY = 100


@dataclass(frozen=True)
class BriefContext:
    company_id: str
    company: str
    website: str
    industry: str


@dataclass(frozen=True)
class BriefOutput:
    sections: tuple[BriefSection, ...]
    sources: tuple[str, ...]
    prompt_id: str
    prompt_version: str
    model: str
    tier: Tier
    attempts: int
    usage: tuple[TokenUsage | None, ...]


async def company_brief(
    runner: AITaskRunner,
    context: BriefContext,
    cancel: CancelToken,
    *,
    meter: UsageMeter,
    tier: Tier | None = None,
    task_id: str | None = None,
) -> BriefOutput:
    payload = json.dumps(
        {
            "company": context.company[:MAX_NAME],
            "website": context.website[:MAX_WEBSITE] or None,
            "industry": context.industry[:MAX_INDUSTRY] or None,
        },
        ensure_ascii=False,
    )
    result = await runner.run_task(
        COMPANY_BRIEF,
        {"context": payload, "company_id": context.company_id},
        cancel,
        meter=meter,
        tier=tier,
        task_id=task_id,
    )
    brief = resolve_brief(result.data, result.searched_urls)
    return BriefOutput(
        brief.sections,
        brief.sources,
        result.prompt_id,
        result.prompt_version,
        result.model,
        result.tier,
        result.attempts,
        result.usage,
    )
