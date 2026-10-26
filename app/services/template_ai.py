"""Typed wrapper for the AI task ``draft_cv_template`` (M6.6; add-ai-task step 4; spec 3.6;
ADR-106 items 3, 6 and 12).

The request holds the country code and name, the style choices and an optional short note
(untrusted, in its own delimited block), and nothing else: no fact text, no personal detail
and no job text are inputs, so no request can hold them. The caller (a later step) owns the
budget gate, the task and the meter; this calls the injected ``AITaskRunner`` once and
resolves the answer with ``resolve_template_draft``, which pins the person's page and photo
choices over whatever the model returned. The result is a draft: only the person's approval
makes a template usable.
"""

import re
from dataclasses import dataclass
from typing import Final

from app.domain.settings import Tier, has_control_characters
from app.domain.template_draft import TemplateDraft, TemplateStyle, resolve_template_draft
from app.modules.ai import AITaskRunner, CancelToken, TokenUsage, UsageMeter

DRAFT_CV_TEMPLATE: Final = "draft_cv_template"
MAX_NOTE_CHARS: Final = 300
MAX_COUNTRY_NAME: Final = 80
_CODE: Final = re.compile(r"[A-Z]{2}")


@dataclass(frozen=True, kw_only=True)
class TemplateDraftOutput:
    """What every stored AI output keeps (spec 3.6), plus what budgets need."""

    result: TemplateDraft
    prompt_id: str
    prompt_version: str
    model: str
    tier: Tier
    attempts: int
    usage: tuple[TokenUsage | None, ...]


def template_dedupe_key(country_code: str) -> str:
    """The task's dedupe key: one draft at a time per country."""
    _check_code(country_code)
    return f"{DRAFT_CV_TEMPLATE}:{country_code}"


def _check_code(country_code: str) -> None:
    if not isinstance(country_code, str) or not _CODE.fullmatch(country_code):
        raise ValueError("The country code must be two capital letters.")


def _check_country(code: str, name: str) -> None:
    _check_code(code)
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name) > MAX_COUNTRY_NAME
        or has_control_characters(name)
    ):
        raise ValueError("The country name must be one short line of text.")


def _clean_note(note: str | None) -> str:
    text = (note or "").strip()
    if len(text) > MAX_NOTE_CHARS:
        raise ValueError(f"The note is at most {MAX_NOTE_CHARS} characters.")
    return text


async def run_draft_cv_template(
    runner: AITaskRunner,
    country_code: str,
    country_name: str,
    style: TemplateStyle,
    note: str | None,
    cancel: CancelToken,
    *,
    meter: UsageMeter,
    tier: Tier | None = None,
    task_id: str | None = None,
) -> TemplateDraftOutput:
    """One template draft request. Raises ``ValueError`` (before any request) for a country
    that is not a code and a short name, a style that is not a ``TemplateStyle`` or a note over
    ``MAX_NOTE_CHARS``; ``InvalidModelOutput`` after the one retry or when the closed spec
    refuses the answer; provider errors as they come."""
    _check_country(country_code, country_name)
    # country_name must stay a value from the fixed country list: the prompt renders it
    # outside the untrusted block (as JSON via tojson), so free text must never reach it.
    if not isinstance(style, TemplateStyle):
        raise ValueError("The style must be a TemplateStyle.")
    text = _clean_note(note)
    result = await runner.run_task(
        DRAFT_CV_TEMPLATE,
        {
            "country_code": country_code,
            "country_name": country_name.strip(),
            "style": style.to_data(),
            "note": text,
        },
        cancel,
        meter=meter,
        tier=tier,
        task_id=task_id,
    )
    return TemplateDraftOutput(
        result=resolve_template_draft(result.data, style),
        prompt_id=result.prompt_id,
        prompt_version=result.prompt_version,
        model=result.model,
        tier=result.tier,
        attempts=result.attempts,
        usage=result.usage,
    )
