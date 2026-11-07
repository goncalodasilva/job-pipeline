"""The one place that knows each AI task's default tier (M9.2 part C; ADR-089 item 9; TD-165).

``app.domain.tiers.DEFAULT_TIERS`` maps every task of ``TASK_SPECS`` to its default tier, and
``task_tier(task, settings)`` is the tier a task runs on: the user's override in
``AIProviderSettings.task_tiers``, else the default. Task names are the real ones because the
table is about them; the settings are fictional.
"""

import pytest

from app.domain.settings import AIProviderSettings
from app.modules.ai.tasks import PRACTICE_SPEC, TASK_SPECS

FAST_TASKS = frozenset(
    {
        "summarize_job",
        "assess_fit",
        "enrichment_question",
        "structure_answer",
        "refine_fact",
        "group_removal_reasons",
    }
)


def defaults() -> dict[str, str]:
    from app.domain.tiers import DEFAULT_TIERS

    return dict(DEFAULT_TIERS)


def test_default_tiers_cover_every_task_spec_and_nothing_else() -> None:
    assert set(defaults()) == set(TASK_SPECS), "one entry per task of TASK_SPECS"
    assert PRACTICE_SPEC.name not in defaults(), "practice is the fake-only task, not listed"


def test_default_tiers_are_the_spec_table() -> None:
    expected = {name: "fast" if name in FAST_TASKS else "thorough" for name in TASK_SPECS}
    assert defaults() == expected
    assert set(defaults().values()) == {"fast", "thorough"}


def test_default_tiers_cannot_be_changed_in_place() -> None:
    from app.domain.tiers import DEFAULT_TIERS

    with pytest.raises(TypeError):
        DEFAULT_TIERS["summarize_job"] = "thorough"  # type: ignore[index]
    assert defaults()["summarize_job"] == "fast"


def test_task_tier_is_the_default_without_an_override() -> None:
    from app.domain.tiers import task_tier

    plain = AIProviderSettings()
    for name, tier in defaults().items():
        assert task_tier(name, plain) == tier, name


def test_task_tier_follows_an_override_either_way() -> None:
    from app.domain.tiers import task_tier

    moved = AIProviderSettings(
        task_tiers={"summarize_job": "thorough", "tailor_cv": "fast", "extract_cv": "fast"}
    )
    assert task_tier("summarize_job", moved) == "thorough"
    assert task_tier("tailor_cv", moved) == "fast"
    assert task_tier("extract_cv", moved) == "fast"
    # Tasks without an override keep their default.
    assert task_tier("assess_fit", moved) == "fast"
    assert task_tier("gap_check", moved) == "thorough"


def test_task_tier_ignores_an_override_that_equals_the_default() -> None:
    from app.domain.tiers import task_tier

    same = AIProviderSettings(task_tiers={"assess_fit": "fast", "tailor_cv": "thorough"})
    assert task_tier("assess_fit", same) == "fast"
    assert task_tier("tailor_cv", same) == "thorough"


def test_task_tier_agrees_with_resolve_tier() -> None:
    from app.domain.tiers import resolve_tier, task_tier

    moved = AIProviderSettings(task_tiers={"refine_fact": "thorough", "company_brief": "fast"})
    for name, tier in defaults().items():
        assert task_tier(name, moved) == resolve_tier(name, tier, moved)  # type: ignore[arg-type]
