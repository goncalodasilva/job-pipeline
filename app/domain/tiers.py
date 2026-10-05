"""Which model an AI task uses (spec 3.7): task → tier → that tier's model."""

from collections.abc import Mapping

from app.domain.settings import AIProviderSettings, Tier


def resolve_tier(task: str, default_tier: Tier, settings: AIProviderSettings) -> Tier:
    """The user's per-task override, else the prompt's default tier."""
    return settings.task_tiers.get(task, default_tier)


def resolve_model(
    tier: Tier, settings: AIProviderSettings, provider_defaults: Mapping[Tier, str]
) -> str:
    """The model mapped to the tier in settings, else the provider's default for it."""
    mapped = settings.tiers.fast if tier == "fast" else settings.tiers.thorough
    return mapped if mapped is not None else provider_defaults[tier]
