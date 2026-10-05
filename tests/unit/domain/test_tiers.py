"""Tier resolution (M1.12; spec 3.7; ADR-051).

Pure functions: a task's tier is its prompt's default unless the user moved it in
``AIProviderSettings.task_tiers``; a tier's model is the user's mapping, else the provider's
default. Task and model names are fictional.
"""

from app.domain.settings import AIProviderSettings, TierMap
from app.domain.tiers import resolve_model, resolve_tier

PROVIDER_DEFAULTS = {"fast": "example-fast-default", "thorough": "example-thorough-default"}


def test_resolve_tier_default_and_override() -> None:
    plain = AIProviderSettings()
    assert resolve_tier("summarize_job", "fast", plain) == "fast"
    assert resolve_tier("tailor_cv", "thorough", plain) == "thorough"

    moved = AIProviderSettings(
        task_tiers={
            "summarize_job": "thorough",
            "tailor_cv": "fast",
            "a_task_that_does_not_exist": "thorough",
        }
    )
    assert resolve_tier("summarize_job", "fast", moved) == "thorough"
    assert resolve_tier("tailor_cv", "thorough", moved) == "fast"
    # Tasks without an override keep their default; unknown override names change nothing.
    assert resolve_tier("assess_fit", "fast", moved) == "fast"
    assert resolve_tier("gap_check", "thorough", moved) == "thorough"


def test_resolve_model_mapped_and_default() -> None:
    unmapped = AIProviderSettings()
    assert resolve_model("fast", unmapped, PROVIDER_DEFAULTS) == "example-fast-default"
    assert resolve_model("thorough", unmapped, PROVIDER_DEFAULTS) == "example-thorough-default"

    fast_only = AIProviderSettings(tiers=TierMap(fast="example-fast-2"))
    assert resolve_model("fast", fast_only, PROVIDER_DEFAULTS) == "example-fast-2"
    assert resolve_model("thorough", fast_only, PROVIDER_DEFAULTS) == "example-thorough-default"

    both = AIProviderSettings(tiers=TierMap(fast="example-fast-2", thorough="example-thorough-2"))
    assert resolve_model("fast", both, PROVIDER_DEFAULTS) == "example-fast-2"
    assert resolve_model("thorough", both, PROVIDER_DEFAULTS) == "example-thorough-2"
