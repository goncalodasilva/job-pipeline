"""Which model an AI task uses (spec 3.7): task → tier → that tier's model."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from app.domain.settings import AIProviderSettings, Tier

# The one place that knows each task's default tier (spec 3.6; ADR-089 item 9; TD-165). Services
# and Settings read it through ``task_tier``; the prompt files' ``default_tier`` front matter must
# agree (tests/unit/modules/ai/test_default_tiers_match_prompts.py). One entry per task of
# ``TASK_SPECS``; the fake-only practice task is not listed.
DEFAULT_TIERS: Final[Mapping[str, Tier]] = MappingProxyType(
    {
        "extract_cv": "thorough",
        "refine_fact": "fast",
        "summarize_job": "fast",
        "assess_fit": "fast",
        "gap_check": "thorough",
        "propose_angles": "thorough",
        "draft_cv_template": "thorough",
        "tailor_cv": "thorough",
        "draft_cover_letter": "thorough",
        "enrichment_question": "fast",
        "structure_answer": "fast",
        "interview_questions": "thorough",
        "interview_web_questions": "thorough",
        "answer_feedback": "thorough",
        "draft_narrative": "thorough",
        "adapt_answer": "thorough",
        "company_brief": "thorough",
        "group_removal_reasons": "fast",
    }
)


def resolve_tier(task: str, default_tier: Tier, settings: AIProviderSettings) -> Tier:
    """The user's per-task override, else the prompt's default tier."""
    return settings.task_tiers.get(task, default_tier)


def task_tier(task: str, settings: AIProviderSettings) -> Tier:
    """The tier ``task`` runs on: the user's override, else its default in ``DEFAULT_TIERS``.
    A name the table does not list runs on Thorough."""
    return resolve_tier(task, DEFAULT_TIERS.get(task, "thorough"), settings)


def resolve_model(
    tier: Tier, settings: AIProviderSettings, provider_defaults: Mapping[Tier, str]
) -> str:
    """The model mapped to the tier in settings, else the provider's default for it."""
    mapped = settings.tiers.fast if tier == "fast" else settings.tiers.thorough
    return mapped if mapped is not None else provider_defaults[tier]
