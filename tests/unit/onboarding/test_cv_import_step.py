"""Setup step 4, CV import: its place in the registry and in start-up detection (M3.7;
AC-M3.8; ADR-052, ADR-089 items 3 and 4; detailed design "Today and the setup guide").

The guide gains an optional, recommended step ``cv_import`` ("Import CV") at order 40, between
web search (30) and job preferences (50). Because the step is optional, a setup finished before
the step existed stays finished: the app opens with no banner and the guide is not reopened.
No I/O; every value is fictional.
"""

from typing import cast

from app.onboarding.detection import StartupDecision, StartupFacts, StepInfo, StepStatus, detect
from app.onboarding.progress import SetupProgress
from app.onboarding.registry import StepRegistry, m1_registry
from app.services.setup_state import SetupStateService

CV_IMPORT = "cv_import"
# Every step that existed before M3.7, finished.
FINISHED_BEFORE: dict[str, StepStatus] = {
    "welcome": "done",
    "provider": "done",
    "web_search": "skipped",
    "preferences": "done",
    "done": "done",
}


class InMemorySetupState:
    """Stands in for ``SetupStateService``: the same public methods, no database."""

    def __init__(self, statuses: dict[str, StepStatus] | None = None) -> None:
        self.stored: dict[str, StepStatus] = dict(statuses or {})

    def statuses(self) -> dict[str, StepStatus]:
        return dict(self.stored)

    def mark_done(self, step_id: str) -> None:
        self.stored[step_id] = "done"

    def mark_skipped(self, step_id: str) -> None:
        if self.stored.get(step_id) != "done":
            self.stored[step_id] = "skipped"


def registered_cv_import(registry: StepRegistry) -> None:
    """Fail plainly (not with a lookup error) while the step isn't registered."""
    ids = [step.id for step in registry.steps]
    assert CV_IMPORT in ids, f"the guide has no {CV_IMPORT} step yet: {ids}"


def facts(
    registry: StepRegistry, statuses: dict[str, StepStatus], **overrides: bool
) -> StartupFacts:
    return StartupFacts(
        bootstrap_exists=True,
        data_dir_explicit=False,
        demo=False,
        database_exists=True,
        steps=registry.infos(),
        statuses=statuses,
        **overrides,
    )


def test_registry_registers_cv_import_optional_at_40() -> None:
    registry = m1_registry()
    registered_cv_import(registry)

    step = registry.get(CV_IMPORT)
    assert (step.id, step.order, step.required) == (CV_IMPORT, 40, False)
    assert step.title == "Import CV"
    assert step.template.startswith("fragments/setup/")
    assert step.template.endswith(".html")
    # Between web search and job preferences; "Step N of M" counts it (4 of 6).
    assert [s.id for s in registry.steps] == [
        "welcome",
        "provider",
        "web_search",
        CV_IMPORT,
        "preferences",
        "done",
    ]
    assert registry.steps.index(step) + 1 == 4
    assert len(registry.steps) == 6
    assert StepInfo(CV_IMPORT, False) in registry.infos()
    assert registry.last.id == "done", "Done stays the final step"


def test_cv_import_step_can_be_skipped_and_then_done() -> None:
    """Optional: "Skip for now" is allowed, and a later import still marks it done (ADR-052)."""
    registry = m1_registry()
    registered_cv_import(registry)
    state = InMemorySetupState({"welcome": "done", "provider": "done"})
    progress = SetupProgress(registry, cast("SetupStateService", state))

    progress.skip(CV_IMPORT)
    assert progress.statuses()[CV_IMPORT] == "skipped"
    assert progress.first_unfinished_required() == "preferences", "skipping never blocks"
    progress.mark_done(CV_IMPORT)
    assert progress.statuses()[CV_IMPORT] == "done"
    progress.skip(CV_IMPORT)
    assert progress.statuses()[CV_IMPORT] == "done", "a done step stays done"


def test_detection_new_optional_step_no_banner() -> None:
    """A setup finished before the step existed has no row for it: still finished, the app
    opens, no banner, no wizard; a broken provider is still the only banner."""
    registry = m1_registry()
    registered_cv_import(registry)

    assert detect(facts(registry, FINISHED_BEFORE)) == StartupDecision("app")
    assert detect(facts(registry, FINISHED_BEFORE, provider_healthy=False)) == StartupDecision(
        "app_with_banner", "provider", banners=("provider",)
    )
    # Unfinished guides resume at the first required step, never at the optional one.
    resumed = {"welcome": "done", "provider": "done", "web_search": "done"}
    assert detect(facts(registry, resumed)) == StartupDecision("guide", "preferences")
