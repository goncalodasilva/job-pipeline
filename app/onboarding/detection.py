"""Which screen the app opens at start-up (detailed design "When it opens"; AC-M1.1; ADR-049).

A pure function over facts gathered at start-up, so every case is a table-driven unit test.
The four cases, in order: no bootstrap config (first run); a bootstrap config whose data
folder has no database (moved or deleted); unfinished required steps (resume); complete
setup with a broken provider (a banner, not the wizard).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

StepStatus = Literal["pending", "done", "skipped"]
DecisionKind = Literal["guide", "locate_or_fresh", "app", "app_with_banner"]


@dataclass(frozen=True)
class StepInfo:
    id: str
    required: bool


@dataclass(frozen=True)
class StartupFacts:
    bootstrap_exists: bool  # config.toml present
    data_dir_explicit: bool  # folder given by --data-dir, JOBPIPELINE_DATA_DIR or --demo
    demo: bool
    database_exists: bool  # a workspace is open
    steps: tuple[StepInfo, ...]  # registered steps in order; the last is the Done step
    statuses: Mapping[str, StepStatus] = field(default_factory=dict)  # missing id: pending
    provider_healthy: bool = True
    force_setup: bool = False  # --setup
    provider_step: str = "provider"
    done_step: str = "done"  # "done" once the user has finished the guide


@dataclass(frozen=True)
class StartupDecision:
    kind: DecisionKind
    step: str | None = None  # guide: step to open; app_with_banner: the first banner's step
    reopened: bool = False  # guide opened by --setup
    banners: tuple[str, ...] = ()  # app_with_banner: every step a banner links to, in order


def first_unfinished_required(
    steps: Sequence[StepInfo], statuses: Mapping[str, StepStatus]
) -> str | None:
    """The first required step not done; skipping doesn't finish a required step."""
    for step in steps:
        if step.required and statuses.get(step.id, "pending") != "done":
            return step.id
    return None


def detect(facts: StartupFacts) -> StartupDecision:
    if not facts.steps:
        raise ValueError("No setup steps are registered.")
    first_step = facts.steps[0].id
    # Demo mode never writes config.toml, so its presence or absence means nothing there.
    if not facts.demo and not facts.bootstrap_exists:
        return StartupDecision("guide", first_step)
    if not facts.database_exists:
        if facts.data_dir_explicit or facts.demo:
            return StartupDecision("guide", first_step)
        return StartupDecision("locate_or_fresh")
    finished_once = facts.statuses.get(facts.done_step) == "done"
    # --setup reopens a finished guide; an unfinished one simply resumes (ADR-050).
    if facts.force_setup and finished_once:
        return StartupDecision("guide", first_step, reopened=True)
    unfinished = first_unfinished_required(facts.steps, facts.statuses)
    if unfinished is not None and not finished_once:
        return StartupDecision("guide", unfinished)
    if not finished_once:
        return StartupDecision("guide", facts.done_step)
    # Finished once: problems become banners, never the wizard. A broken provider first,
    # then a required step added since (ADR-050).
    banners = tuple(
        step
        for step, applies in (
            (facts.provider_step, not facts.provider_healthy),
            (unfinished, unfinished is not None),
        )
        if applies and step is not None
    )
    if banners:
        return StartupDecision("app_with_banner", banners[0], banners=banners)
    return StartupDecision("app")
