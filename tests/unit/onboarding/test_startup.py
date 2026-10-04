"""The cached start-up decision behind the setup redirect (M1.9; AC-M1.2; ADR-050, ADR-052).

``OnboardingState`` holds one ``StartupDecision``. It is computed from the start-up inputs
with no database, and recomputed by ``refresh`` at start-up and after state-changing requests
(never on GET), so the redirect middleware reads a cached value and does no I/O per request.
Progress is an in-memory stand-in with the public methods of ``SetupStateService``.
"""

from typing import cast

import pytest

from app.onboarding.detection import StartupDecision, StepStatus
from app.onboarding.progress import SetupProgress
from app.onboarding.registry import m1_registry
from app.onboarding.startup import OnboardingState, StartupInputs
from app.services.setup_state import SetupStateService

ALL_DONE: dict[str, StepStatus] = {
    "welcome": "done",
    "provider": "done",
    "preferences": "done",
    "done": "done",
}


class InMemorySetupState:
    def __init__(self, statuses: dict[str, StepStatus] | None = None) -> None:
        self.stored: dict[str, StepStatus] = dict(statuses or {})

    def statuses(self) -> dict[str, StepStatus]:
        return dict(self.stored)

    def mark_done(self, step_id: str) -> None:
        self.stored[step_id] = "done"

    def mark_skipped(self, step_id: str) -> None:
        if self.stored.get(step_id) != "done":
            self.stored[step_id] = "skipped"


def progress(statuses: dict[str, StepStatus] | None = None) -> SetupProgress:
    return SetupProgress(m1_registry(), cast("SetupStateService", InMemorySetupState(statuses)))


def inputs(
    *,
    bootstrap_exists: bool = True,
    data_dir_explicit: bool = False,
    demo: bool = False,
    force_setup: bool = False,
) -> StartupInputs:
    return StartupInputs(
        bootstrap_exists=bootstrap_exists,
        data_dir_explicit=data_dir_explicit,
        demo=demo,
        force_setup=force_setup,
    )


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (inputs(bootstrap_exists=False), StartupDecision("guide", "welcome")),
        (inputs(bootstrap_exists=True), StartupDecision("locate_or_fresh")),
        (
            inputs(bootstrap_exists=True, data_dir_explicit=True),
            StartupDecision("guide", "welcome"),
        ),
        (
            inputs(bootstrap_exists=False, data_dir_explicit=True, demo=True),
            StartupDecision("guide", "welcome"),
        ),
    ],
    ids=["first_run", "bootstrap_no_db", "explicit_no_db", "demo_no_db"],
)
def test_onboarding_state_initial_decision_without_database(
    given: StartupInputs, expected: StartupDecision
) -> None:
    state = OnboardingState(m1_registry(), given)

    assert state.decision == expected


def test_startup_inputs_are_frozen() -> None:
    given = inputs()
    with pytest.raises(AttributeError):
        given.demo = True  # type: ignore[misc]


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ({}, StartupDecision("guide", "welcome")),
        ({"welcome": "done"}, StartupDecision("guide", "provider")),
        ({"welcome": "done", "provider": "done"}, StartupDecision("guide", "preferences")),
        (
            {"welcome": "done", "provider": "done", "preferences": "done"},
            StartupDecision("guide", "done"),
        ),
        (ALL_DONE, StartupDecision("app")),
    ],
    ids=["nothing_done", "welcome_done", "provider_done", "required_done", "all_done"],
)
def test_refresh_with_progress_uses_statuses(
    statuses: dict[str, StepStatus], expected: StartupDecision
) -> None:
    state = OnboardingState(m1_registry(), inputs())
    assert state.decision.kind == "locate_or_fresh"

    returned = state.refresh(progress(statuses))

    assert returned == expected
    assert state.decision == expected, "refresh replaces the cached decision"


def test_refresh_reads_progress_each_time() -> None:
    stored = InMemorySetupState()
    live = SetupProgress(m1_registry(), cast("SetupStateService", stored))
    state = OnboardingState(m1_registry(), inputs())

    assert state.refresh(live) == StartupDecision("guide", "welcome")
    stored.mark_done("welcome")
    assert state.decision == StartupDecision("guide", "welcome"), "cached until refreshed"
    assert state.refresh(live) == StartupDecision("guide", "provider")


def test_refresh_without_workspace_returns_to_no_database_decision() -> None:
    state = OnboardingState(m1_registry(), inputs())
    state.refresh(progress(ALL_DONE))
    assert state.decision.kind == "app"

    assert state.refresh(None) == StartupDecision("locate_or_fresh")
    assert state.decision == StartupDecision("locate_or_fresh")


def test_refresh_passes_provider_health() -> None:
    state = OnboardingState(m1_registry(), inputs())

    broken = state.refresh(progress(ALL_DONE), provider_healthy=False)

    assert broken == StartupDecision("app_with_banner", "provider", banners=("provider",))
    assert state.decision == broken
    assert state.refresh(progress(ALL_DONE), provider_healthy=True) == StartupDecision("app")
    # While the guide is unfinished, a broken provider changes nothing: the guide resumes.
    assert state.refresh(progress({"welcome": "done"}), provider_healthy=False) == (
        StartupDecision("guide", "provider")
    )


def test_refresh_honours_force_setup_after_completion() -> None:
    state = OnboardingState(m1_registry(), inputs(force_setup=True))

    assert state.refresh(progress(ALL_DONE)) == StartupDecision("guide", "welcome", reopened=True)


def test_refresh_updates_bootstrap_presence() -> None:
    """Step 1 writes ``config.toml`` after opening the workspace, then refreshes (M1.16)."""
    state = OnboardingState(m1_registry(), inputs(bootstrap_exists=False))
    welcome_done = progress({"welcome": "done"})

    assert state.refresh(welcome_done) == StartupDecision("guide", "welcome"), (
        "without config.toml the guide stays at its first step"
    )
    assert state.refresh(welcome_done, bootstrap_exists=True) == StartupDecision(
        "guide", "provider"
    )
    assert state.refresh(welcome_done) == StartupDecision("guide", "provider"), (
        "None keeps the stored bootstrap presence"
    )
    assert state.refresh(welcome_done, bootstrap_exists=False) == StartupDecision(
        "guide", "welcome"
    )


def test_refresh_keeps_provider_health_when_not_given() -> None:
    """A refresh after a POST that didn't check the provider keeps the last known health."""
    state = OnboardingState(m1_registry(), inputs())
    assert state.refresh(progress(ALL_DONE)) == StartupDecision("app"), "healthy at first"

    state.refresh(progress(ALL_DONE), provider_healthy=False)

    assert state.refresh(progress(ALL_DONE)) == StartupDecision(
        "app_with_banner", "provider", banners=("provider",)
    )
