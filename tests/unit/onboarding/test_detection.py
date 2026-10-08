"""Start-up detection for the setup guide (M1.6, AC-M1.1, AC-M1.6, AC-M1.7).

``detect`` is a pure function from ``StartupFacts`` to ``StartupDecision``. It encodes the four
start-up cases of the detailed design ("Today and the setup guide"), the onboarding-step skill's
rule that a required step added after completion shows a banner instead of the wizard, and
ADR-044 items 3 to 5 (deferred workspace, explicit folder without bootstrap is a first run,
demo never writes the bootstrap, ``--setup`` reopens without resetting).

Critical module (spec 4.3): these tests aim at full branch coverage of ``detection.py``.
"""

import dataclasses
from collections.abc import Mapping
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.onboarding.detection import (
    StartupDecision,
    StartupFacts,
    StepInfo,
    StepStatus,
    detect,
    first_unfinished_required,
)

# The M1 setup guide: steps 1 to 6 of the detailed design, in order.
M1_STEPS: tuple[StepInfo, ...] = (
    StepInfo(id="welcome", required=True),
    StepInfo(id="provider", required=True),
    StepInfo(id="web_search", required=False),
    StepInfo(id="import_cv", required=False),
    StepInfo(id="preferences", required=True),
    StepInfo(id="done", required=False),
)
STEP_IDS: tuple[str, ...] = tuple(step.id for step in M1_STEPS)
REQUIRED_IDS: tuple[str, ...] = tuple(step.id for step in M1_STEPS if step.required)

ALL_REQUIRED_DONE: dict[str, StepStatus] = {
    "welcome": "done",
    "provider": "done",
    "preferences": "done",
}
COMPLETE: dict[str, StepStatus] = {**ALL_REQUIRED_DONE, "done": "done"}


def facts(**overrides: Any) -> StartupFacts:
    """An ordinary returning user: bootstrap config present, folder from it, database open."""
    values: dict[str, Any] = {
        "bootstrap_exists": True,
        "data_dir_explicit": False,
        "demo": False,
        "database_exists": True,
        "steps": M1_STEPS,
        "statuses": {},
    }
    values.update(overrides)
    return StartupFacts(**values)


# --- AC-M1.1: the four start-up cases -------------------------------------------------------


@pytest.mark.parametrize(
    ("startup", "expected"),
    [
        pytest.param(
            facts(bootstrap_exists=False, database_exists=False),
            StartupDecision(kind="guide", step="welcome"),
            id="no_bootstrap",
        ),
        pytest.param(
            facts(database_exists=False, statuses=COMPLETE),
            StartupDecision(kind="locate_or_fresh", step=None),
            id="bootstrap_no_db",
        ),
        pytest.param(
            facts(statuses={"welcome": "done"}),
            StartupDecision(kind="guide", step="provider"),
            id="incomplete",
        ),
        pytest.param(
            facts(statuses=COMPLETE, provider_healthy=False),
            StartupDecision(kind="app_with_banner", step="provider", banners=("provider",)),
            id="complete_provider_broken",
        ),
    ],
)
def test_ac_m1_1_detect_four_cases(startup: StartupFacts, expected: StartupDecision) -> None:
    assert detect(startup) == expected


def test_ac_m1_1_no_bootstrap_is_first_run_whatever_else_is_true() -> None:
    startup = facts(
        bootstrap_exists=False,
        database_exists=True,
        statuses=COMPLETE,
        provider_healthy=False,
        force_setup=True,
    )
    assert detect(startup) == StartupDecision(kind="guide", step="welcome", reopened=False)


def test_ac_m1_1_complete_provider_broken_banner_uses_provider_step_name() -> None:
    steps = (
        StepInfo(id="welcome", required=True),
        StepInfo(id="ai", required=True),
        StepInfo(id="finish", required=False),
    )
    startup = facts(
        steps=steps,
        statuses={"welcome": "done", "ai": "done", "finish": "done"},
        provider_healthy=False,
        provider_step="ai",
        done_step="finish",
    )
    assert detect(startup) == StartupDecision(kind="app_with_banner", step="ai", banners=("ai",))


def test_detect_complete_and_healthy_opens_app() -> None:
    decision = detect(facts(statuses=COMPLETE))
    assert decision == StartupDecision(kind="app", step=None, reopened=False)


# --- AC-M1.6: resume at the first unfinished required step ----------------------------------


@pytest.mark.parametrize(
    ("statuses", "expected_step"),
    [
        pytest.param({}, "welcome", id="nothing_saved"),
        pytest.param({"welcome": "done"}, "provider", id="after_welcome"),
        pytest.param(
            {"welcome": "done", "provider": "done", "web_search": "done"},
            "preferences",
            id="optional_done_required_pending",
        ),
        pytest.param(
            {"welcome": "done", "provider": "pending", "preferences": "done"},
            "provider",
            id="earlier_required_pending_wins",
        ),
        pytest.param(
            {"provider": "done", "preferences": "done"}, "welcome", id="first_step_pending"
        ),
    ],
)
def test_detect_resume_step_is_first_unfinished_required(
    statuses: Mapping[str, StepStatus], expected_step: str
) -> None:
    assert detect(facts(statuses=statuses)) == StartupDecision(kind="guide", step=expected_step)


def test_detect_skipped_optional_steps_do_not_block() -> None:
    skipped: dict[str, StepStatus] = {"web_search": "skipped", "import_cv": "skipped"}
    pending: dict[str, StepStatus] = {"web_search": "pending", "import_cv": "pending"}
    # Mid-setup: the optional steps are passed over on resume.
    assert detect(facts(statuses={"welcome": "done", "provider": "done", **skipped})) == (
        StartupDecision(kind="guide", step="preferences")
    )
    # Complete setup: skipped or pending optional steps still open the app.
    assert detect(facts(statuses={**COMPLETE, **skipped})) == StartupDecision(kind="app")
    assert detect(facts(statuses={**COMPLETE, **pending})) == StartupDecision(kind="app")
    # Required steps done, optional skipped: finish at the Done step, not at an optional step.
    assert detect(facts(statuses={**ALL_REQUIRED_DONE, **skipped})) == (
        StartupDecision(kind="guide", step="done")
    )


def test_detect_all_required_done_finishes_at_done_step() -> None:
    assert detect(facts(statuses=ALL_REQUIRED_DONE)) == StartupDecision(kind="guide", step="done")
    # Same when the Done step is explicitly pending, and regardless of provider health.
    pending_done: dict[str, StepStatus] = {**ALL_REQUIRED_DONE, "done": "pending"}
    assert detect(facts(statuses=pending_done, provider_healthy=False)) == (
        StartupDecision(kind="guide", step="done")
    )


def test_skipped_required_step_is_still_unfinished() -> None:
    statuses: dict[str, StepStatus] = {
        "welcome": "done",
        "provider": "skipped",
        "preferences": "done",
    }
    assert first_unfinished_required(M1_STEPS, statuses) == "provider"
    assert detect(facts(statuses=statuses)) == StartupDecision(kind="guide", step="provider")


def test_first_unfinished_required_none_when_all_done() -> None:
    assert first_unfinished_required(M1_STEPS, ALL_REQUIRED_DONE) is None
    assert first_unfinished_required(M1_STEPS, COMPLETE) is None
    assert first_unfinished_required((), {}) is None
    optional_only = (StepInfo(id="extra", required=False), StepInfo(id="done", required=False))
    assert first_unfinished_required(optional_only, {}) is None
    # A missing id counts as pending.
    assert first_unfinished_required(M1_STEPS, {}) == "welcome"


def test_unknown_status_ids_are_ignored() -> None:
    # Rows for steps no longer registered (or from a newer version) change nothing.
    unknown: dict[str, StepStatus] = {"retired_step": "pending", "future_step": "skipped"}
    assert first_unfinished_required(M1_STEPS, {**ALL_REQUIRED_DONE, **unknown}) is None
    assert detect(facts(statuses={**COMPLETE, **unknown})) == StartupDecision(kind="app")
    assert detect(facts(statuses={"welcome": "done", **unknown})) == (
        StartupDecision(kind="guide", step="provider")
    )


# --- AC-M1.7 and ADR-044 item 5: --setup reopens the guide without resetting -----------------


def test_detect_force_setup_opens_guide_without_resetting() -> None:
    snapshot = dict(COMPLETE)
    startup = facts(statuses=COMPLETE, force_setup=True, provider_healthy=False)
    decision = detect(startup)
    assert decision == StartupDecision(kind="guide", step="welcome", reopened=True)
    # The facts are untouched; the route-level guarantee that nothing is reset is M1.20's.
    assert dict(startup.statuses) == snapshot


@pytest.mark.parametrize(
    ("statuses", "expected_step"),
    [
        pytest.param({"welcome": "done", "provider": "done"}, "preferences", id="incomplete"),
        pytest.param(
            {**ALL_REQUIRED_DONE, "web_search": "skipped"}, "done", id="done_step_pending"
        ),
    ],
)
def test_detect_force_setup_on_unfinished_guide_resumes(
    statuses: Mapping[str, StepStatus], expected_step: str
) -> None:
    # Never finished: --setup has nothing to reopen, the guide simply resumes (ADR-050).
    assert detect(facts(statuses=statuses, force_setup=True)) == (
        StartupDecision(kind="guide", step=expected_step)
    )


def test_detect_force_setup_ignored_without_database() -> None:
    # No bootstrap: first run, not a reopening.
    assert detect(facts(bootstrap_exists=False, database_exists=False, force_setup=True)) == (
        StartupDecision(kind="guide", step="welcome", reopened=False)
    )
    # Bootstrap folder missing: still the locate-or-start-fresh choice.
    assert detect(facts(database_exists=False, force_setup=True)) == (
        StartupDecision(kind="locate_or_fresh")
    )
    # Explicit folder without a database: the guide from the start, not reopened.
    assert detect(facts(database_exists=False, data_dir_explicit=True, force_setup=True)) == (
        StartupDecision(kind="guide", step="welcome", reopened=False)
    )


# --- Required step added after completion: banner, not wizard (onboarding-step skill) --------


@pytest.mark.parametrize("provider_healthy", [True, False], ids=["provider_ok", "provider_broken"])
def test_detect_new_required_step_after_completion_shows_banner(provider_healthy: bool) -> None:
    steps = (
        *M1_STEPS[:-1],
        StepInfo(id="new_required", required=True),
        M1_STEPS[-1],
    )
    startup = facts(steps=steps, statuses=COMPLETE, provider_healthy=provider_healthy)
    # A broken provider comes first; the new step keeps its own banner (ADR-050).
    banners = ("new_required",) if provider_healthy else ("provider", "new_required")
    assert detect(startup) == StartupDecision(
        kind="app_with_banner", step=banners[0], banners=banners
    )


def test_detect_unfinished_required_before_completion_is_the_wizard() -> None:
    # The Done step "skipped" or "pending" is not a finished guide: resume, no banner.
    done_statuses: tuple[StepStatus, ...] = ("pending", "skipped")
    for done_status in done_statuses:
        statuses: dict[str, StepStatus] = {"welcome": "done", "done": done_status}
        assert detect(facts(statuses=statuses)) == StartupDecision(kind="guide", step="provider")


# --- ADR-044 item 4: explicit folder and demo ---------------------------------------------------


@pytest.mark.parametrize("database_exists", [False, True], ids=["no_db", "db"])
def test_detect_explicit_data_dir_without_bootstrap_is_first_run(database_exists: bool) -> None:
    startup = facts(
        bootstrap_exists=False,
        data_dir_explicit=True,
        database_exists=database_exists,
        statuses=COMPLETE if database_exists else {},
    )
    assert detect(startup) == StartupDecision(kind="guide", step="welcome")


def test_detect_explicit_folder_without_database_starts_guide() -> None:
    # Bootstrap exists but the folder was given by --data-dir or the environment: a new
    # workspace in that folder, so the guide starts at Welcome instead of "locate or start fresh".
    startup = facts(data_dir_explicit=True, database_exists=False, statuses=COMPLETE)
    assert detect(startup) == StartupDecision(kind="guide", step="welcome")


@pytest.mark.parametrize("bootstrap_exists", [False, True], ids=["no_bootstrap", "bootstrap"])
def test_detect_demo_mode_skips_bootstrap_cases(bootstrap_exists: bool) -> None:
    def demo(**overrides: Any) -> StartupFacts:
        return facts(
            demo=True, data_dir_explicit=True, bootstrap_exists=bootstrap_exists, **overrides
        )

    # No database yet: the guide inside the demo folder, never "locate or start fresh".
    assert detect(demo(database_exists=False)) == StartupDecision(kind="guide", step="welcome")
    # Database present: decided from the steps, as for any workspace.
    assert detect(demo(statuses={"welcome": "done"})) == StartupDecision(
        kind="guide", step="provider"
    )
    assert detect(demo(statuses=COMPLETE)) == StartupDecision(kind="app")
    assert detect(demo(statuses=COMPLETE, provider_healthy=False)) == (
        StartupDecision(kind="app_with_banner", step="provider", banners=("provider",))
    )
    assert detect(demo(statuses=COMPLETE, force_setup=True)) == (
        StartupDecision(kind="guide", step="welcome", reopened=True)
    )


def test_detect_demo_without_explicit_flag_still_skips_bootstrap_cases() -> None:
    # Demo alone (whatever data_dir_explicit says) never yields the bootstrap cases.
    startup = facts(
        demo=True, data_dir_explicit=False, bootstrap_exists=False, database_exists=False
    )
    assert detect(startup) == StartupDecision(kind="guide", step="welcome")
    startup = facts(
        demo=True, data_dir_explicit=False, bootstrap_exists=True, database_exists=False
    )
    assert detect(startup) == StartupDecision(kind="guide", step="welcome")


# --- Value objects ------------------------------------------------------------------------------


def test_decisions_are_frozen() -> None:
    decision = detect(facts(statuses=COMPLETE))
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.kind = "guide"  # type: ignore[misc]
    startup = facts()
    with pytest.raises(dataclasses.FrozenInstanceError):
        startup.database_exists = False  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        M1_STEPS[0].required = False  # type: ignore[misc]
    defaults = StartupDecision(kind="app")
    assert (defaults.step, defaults.reopened) == (None, False)
    bare = StartupFacts(
        bootstrap_exists=True,
        data_dir_explicit=False,
        demo=False,
        database_exists=True,
        steps=M1_STEPS,
    )
    assert dict(bare.statuses) == {}
    assert (bare.provider_healthy, bare.force_setup) == (True, False)
    assert (bare.provider_step, bare.done_step) == ("provider", "done")


# --- Property ----------------------------------------------------------------------------------

_status = st.sampled_from(("pending", "done", "skipped"))
_statuses = st.dictionaries(
    keys=st.sampled_from((*STEP_IDS, "retired_step")), values=_status, max_size=len(STEP_IDS) + 1
)


@given(
    bootstrap_exists=st.booleans(),
    data_dir_explicit=st.booleans(),
    demo=st.booleans(),
    database_exists=st.booleans(),
    statuses=_statuses,
    provider_healthy=st.booleans(),
    force_setup=st.booleans(),
)
def test_detect_always_returns_a_registered_step_or_none(
    bootstrap_exists: bool,
    data_dir_explicit: bool,
    demo: bool,
    database_exists: bool,
    statuses: dict[str, StepStatus],
    provider_healthy: bool,
    force_setup: bool,
) -> None:
    startup = StartupFacts(
        bootstrap_exists=bootstrap_exists,
        data_dir_explicit=data_dir_explicit,
        demo=demo,
        database_exists=database_exists,
        steps=M1_STEPS,
        statuses=statuses,
        provider_healthy=provider_healthy,
        force_setup=force_setup,
    )
    decision = detect(startup)

    assert decision.step is None or decision.step in STEP_IDS
    assert decision.kind in ("guide", "locate_or_fresh", "app", "app_with_banner")
    if decision.kind == "guide":
        assert decision.step is not None
    if decision.kind in ("app", "locate_or_fresh"):
        assert decision.step is None
    if decision.kind == "app_with_banner":
        assert decision.step is not None
        assert decision.banners and decision.banners[0] == decision.step
        assert set(decision.banners) <= set(STEP_IDS)
    else:
        assert decision.banners == ()
    if decision.reopened:
        assert decision.kind == "guide" and force_setup and database_exists
        assert statuses.get("done") == "done"

    unfinished = first_unfinished_required(M1_STEPS, statuses)
    assert unfinished is None or unfinished in REQUIRED_IDS
    if unfinished is not None:
        assert statuses.get(unfinished, "pending") != "done"
        earlier = REQUIRED_IDS[: REQUIRED_IDS.index(unfinished)]
        assert all(statuses.get(step_id) == "done" for step_id in earlier)
    else:
        assert all(statuses.get(step_id) == "done" for step_id in REQUIRED_IDS)

    expect_app = (
        unfinished is None
        and statuses.get("done") == "done"
        and provider_healthy
        and database_exists
        and (demo or bootstrap_exists)
        and not force_setup
    )
    assert (decision.kind == "app") == expect_app


def test_detect_requires_registered_steps() -> None:
    with pytest.raises(ValueError):
        detect(facts(steps=()))
