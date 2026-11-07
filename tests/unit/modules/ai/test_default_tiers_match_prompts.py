"""The default tiers in code and in the prompt files stay one list (M9.2 part C; ADR-089 item 9;
TD-165; spec 3.6).

``app.domain.tiers.DEFAULT_TIERS`` is what Settings shows and what every service plans a start
with; each prompt's ``default_tier`` front matter is what the executor falls back to. They must
agree for every task of ``TASK_SPECS``, and no service may keep a default of its own.
"""

import re
from pathlib import Path

import pytest

from app.modules.ai.prompts import PromptLibrary
from app.modules.ai.tasks import TASK_SPECS

REPO_ROOT = Path(__file__).resolve().parents[4]
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


def test_service_default_tiers_match_prompt_front_matter() -> None:
    from app.domain.tiers import DEFAULT_TIERS

    library = PromptLibrary(REPO_ROOT / "prompts")
    from_prompts = {name: library.load(name).default_tier for name in TASK_SPECS}
    assert dict(DEFAULT_TIERS) == from_prompts
    # And the table is the spec's: Fast only for the routine tasks.
    assert {name for name, tier in from_prompts.items() if tier == "fast"} == FAST_TASKS


DIRECT_READ = re.compile(
    r"task_tiers\s*\.\s*get\s*\(|task_tiers\s*\[|\bEXTRACT_CV_TIER\b|\bTIER_FALLBACK\b"
)


@pytest.mark.parametrize(
    "line",
    [
        'tier = settings.task_tiers.get("x", "fast")',
        'tier = settings.task_tiers . get ("x")',
        'tier = settings.task_tiers["summarize_job"]',
        "tier = provider.task_tiers [name]",
        "tier = EXTRACT_CV_TIER",
        "tier = TIER_FALLBACK",
    ],
)
def test_the_guard_flags_every_direct_read_of_task_tiers(line: str) -> None:
    assert DIRECT_READ.search(line), line


@pytest.mark.parametrize(
    "line",
    [
        'tier = task_tier("summarize_job", settings)',
        "for name, tier in settings.task_tiers.items():",
    ],
)
def test_the_guard_leaves_the_one_reader_alone(line: str) -> None:
    assert DIRECT_READ.search(line) is None, line


@pytest.mark.parametrize("folder", ["app/services", "app/api"])
def test_services_read_task_tiers_only_through_task_tier(folder: str) -> None:
    """One place for defaults: no service or route reads ``task_tiers`` directly (``.get(...)``
    with a default of its own, or ``task_tiers[...]``), and the per-service constants
    (``EXTRACT_CV_TIER``, ``TIER_FALLBACK``) are gone."""
    offenders: list[str] = []
    pattern = DIRECT_READ
    for path in sorted((REPO_ROOT / folder).rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")
    assert offenders == [], "use app.domain.tiers.task_tier:\n" + "\n".join(offenders)
