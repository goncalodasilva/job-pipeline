"""One ``structure_answer`` request per experience paragraph of a debrief (M8.3 part A).

No new AI task and no new prompt: the session's ``structure_answer`` is reused as it is. Each
request holds the one paragraph (the person's own words, an untrusted block), a question the
code owns, no target fact and no roles: nothing else of the debrief (not what was asked, not
how it went) and none of the fact bank is sent. The result is resolved against the paragraph
by ``resolve_structured_answer``, so a claim the paragraph does not state is flagged.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from app.domain.fact_selection import FactPromptBlock, prompt_fact_block
from app.domain.settings import Tier
from app.domain.structured_answer import resolve_structured_answer
from app.modules.ai import AITaskRunner, CancelToken, UsageMeter
from app.services.facts import DraftInput

STRUCTURE_ANSWER: Final = "structure_answer"
# Code-owned context for the model; it is never evidence of anything the person did.
QUESTION: Final = "Describe an experience from your work that you would like to keep."


@dataclass(frozen=True, kw_only=True)
class ParagraphDraft:
    draft: DraftInput
    model: str
    prompt_id: str
    prompt_version: str


async def structure_paragraph(
    runner: AITaskRunner,
    paragraph: str,
    *,
    debrief_id: str,
    known_skills: Sequence[str],
    cancel: CancelToken,
    meter: UsageMeter,
    tier: Tier | None,
    task_id: str,
) -> ParagraphDraft:
    """The draft fact for one paragraph. ``InvalidModelOutput`` after the one retry, provider
    errors as they come."""
    skills = tuple(known_skills)
    result = await runner.run_task(
        STRUCTURE_ANSWER,
        {
            "question": QUESTION,
            "answers": json.dumps([paragraph], ensure_ascii=False),
            "target": FactPromptBlock(
                json.dumps({"note": None, "target": str(prompt_fact_block(()))}), False
            ),
            "choices": json.dumps(
                {"roles": {}, "generalized_roles": [], "skills": skills}, ensure_ascii=False
            ),
            "session_id": debrief_id,
        },
        cancel,
        meter=meter,
        tier=tier,
        task_id=task_id,
    )
    resolved = resolve_structured_answer(result.data, (paragraph,), known_skills=skills)
    draft = DraftInput(
        type=resolved.type,
        text=resolved.content.text,
        situation=resolved.content.situation,
        action=resolved.content.action,
        result=resolved.content.result,
        skills=resolved.skills,
        original_answer=paragraph,
        needs_check=resolved.needs_check,
    )
    return ParagraphDraft(
        draft=draft,
        model=result.model,
        prompt_id=result.prompt_id,
        prompt_version=result.prompt_version,
    )
