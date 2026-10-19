"""Setup step 3, web search: its place in the registry and in start-up detection (M2.25;
AC-M1.7; ADR-052, ADR-076 item 13; detailed design "Today and the setup guide").

The guide gains an optional step ``web_search`` ("Web search") at order 30, between the AI
provider (20) and job preferences (50); order 40 stays free for CV import (M3). Because the
step is optional, a setup finished before the step existed stays finished: the app opens with
no banner and the guide is not reopened.

The registry is read through ``m1_registry()`` (it keeps its name).

The step's handler, ``WebSearchStep``, is driven here with doubles for ``UsageSettings`` and
for the injected ``mark_done`` and ``skip``: what it announces when the guide moves on, and
what it answers when a mark fails or the workspace closes meanwhile. No I/O; every value is
fictional.
"""

import logging
import sqlite3
from collections.abc import Mapping
from typing import Any, cast

import pytest

from app.domain.budgets import Budget
from app.domain.errors import VersionConflict
from app.onboarding.detection import StartupDecision, StartupFacts, StepInfo, StepStatus, detect
from app.onboarding.progress import SetupProgress
from app.onboarding.registry import StepRegistry, StepResult, m1_registry
from app.onboarding.steps.web_search import (
    MSG_NOT_MOVED_ON,
    MSG_SAVED_NOT_MOVED_ON,
    MSG_STEP_1_FIRST,
    WebSearchStep,
)
from app.services.setup_state import SetupStateService
from app.services.usage_settings import (
    BUDGET_FIELDS,
    BudgetView,
    UsageSettings,
    UsageSettingsView,
    UsageSettingUnavailable,
)

WEB_SEARCH = "web_search"
# Every step that existed before M2.25, finished.
FINISHED_BEFORE: dict[str, StepStatus] = {
    "welcome": "done",
    "provider": "done",
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


def registered_web_search(registry: StepRegistry) -> None:
    """Fail plainly (not with a lookup error) while the step isn't registered."""
    ids = [step.id for step in registry.steps]
    assert WEB_SEARCH in ids, f"the guide has no {WEB_SEARCH} step yet: {ids}"


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


def test_registry_registers_web_search_optional_at_30() -> None:
    registry = m1_registry()
    registered_web_search(registry)

    step = registry.get(WEB_SEARCH)
    assert (step.id, step.order, step.required) == (WEB_SEARCH, 30, False)
    assert step.title == "Web search"
    assert step.template.startswith("fragments/setup/")
    assert step.template.endswith(".html")
    # Between the AI provider and CV import; "Step N of M" counts it (3 of 6).
    assert [s.id for s in registry.steps] == [
        "welcome",
        "provider",
        WEB_SEARCH,
        "cv_import",
        "preferences",
        "done",
    ]
    assert registry.steps.index(step) + 1 == 3
    assert len(registry.steps) == 6
    assert StepInfo(WEB_SEARCH, False) in registry.infos()
    assert registry.last.id == "done", "Done stays the final step"
    assert registry.get("cv_import").order == 40  # order 40 is CV import since M3.7


def test_web_search_step_can_be_skipped_and_then_done() -> None:
    """Optional: "Skip for now" is allowed, and a later save still marks it done (ADR-052)."""
    registry = m1_registry()
    registered_web_search(registry)
    state = InMemorySetupState({"welcome": "done", "provider": "done"})
    progress = SetupProgress(registry, cast("SetupStateService", state))

    progress.skip(WEB_SEARCH)
    assert progress.statuses()[WEB_SEARCH] == "skipped"
    assert progress.first_unfinished_required() == "preferences", "skipping never blocks"
    progress.mark_done(WEB_SEARCH)
    assert progress.statuses()[WEB_SEARCH] == "done"
    progress.skip(WEB_SEARCH)
    assert progress.statuses()[WEB_SEARCH] == "done", "a done step stays done"


def test_detection_new_optional_step_no_banner() -> None:
    """A setup finished before the step existed has no row for it: still finished, the app
    opens, no banner, no wizard; a broken provider is still the only banner."""
    registry = m1_registry()
    registered_web_search(registry)

    assert detect(facts(registry, FINISHED_BEFORE)) == StartupDecision("app")
    assert detect(facts(registry, FINISHED_BEFORE, provider_healthy=False)) == StartupDecision(
        "app_with_banner", "provider", banners=("provider",)
    )
    # --setup still reopens the finished guide at its first step, not at the new one.
    assert detect(facts(registry, FINISHED_BEFORE, force_setup=True)) == StartupDecision(
        "guide", "welcome", reopened=True
    )


@pytest.mark.parametrize("status", [None, "skipped", "done"], ids=["pending", "skipped", "done"])
def test_detection_web_search_never_where_the_guide_resumes(status: StepStatus | None) -> None:
    """Mid-guide, whatever the optional step's state, the guide resumes at the first
    unfinished required step (preferences), then at Done; never at web search."""
    registry = m1_registry()
    registered_web_search(registry)
    statuses: dict[str, StepStatus] = {"welcome": "done", "provider": "done"}
    if status is not None:
        statuses[WEB_SEARCH] = status

    assert detect(facts(registry, statuses)) == StartupDecision("guide", "preferences")
    statuses["preferences"] = "done"
    assert detect(facts(registry, statuses)) == StartupDecision("guide", "done")


# --- the handler, with doubles ---------------------------------------------------------------

SERVICE = "web_search"
PRIVATE_PATH = "/Users/fictional-person/private/app.db"
STORED = Budget(allowance=100, first_warning=50, second_warning=80, block_at=100, reset_day=15)
TYPED = {
    "allowance": "40",
    "first_warning": "10",
    "second_warning": "20",
    "block_at": "30",
    "reset_day": "1",
    "fast_cap": "",
    "thorough_cap": "",
}
ENTERED: dict[str, int | None] = {
    "allowance": 40,
    "first_warning": 10,
    "second_warning": 20,
    "block_at": 30,
    "reset_day": 1,
    "fast_cap": None,
    "thorough_cap": None,
}


class Marks:
    """Stands in for the injected ``mark_done`` or ``skip``; raises ``error`` when given."""

    def __init__(self, error: Exception | None = None) -> None:
        self.count = 0
        self._error = error

    def __call__(self) -> None:
        self.count += 1
        if self._error is not None:
            raise self._error


class UsageSettingsDouble:
    """The part of ``UsageSettings`` the step uses. ``save_error`` is raised by a save;
    otherwise a save is recorded and, like the real one, moves the version on."""

    def __init__(
        self,
        *,
        stored: Budget | None = None,
        unreadable: bool = False,
        save_error: Exception | None = None,
    ) -> None:
        self.stored = stored
        self.unreadable = unreadable
        self.version = 1 if stored else 0
        self.saves: list[tuple[str, dict[str, Any], int]] = []
        self._save_error = save_error

    def available(self) -> bool:
        return True

    def view(self) -> UsageSettingsView:
        held = {name: getattr(self.stored, name, None) for name in BUDGET_FIELDS}
        values = {name: "" if value is None else str(value) for name, value in held.items()}
        return UsageSettingsView(
            budgets=(BudgetView(SERVICE, "Web search", self.stored, values),),
            budgets_version=self.version,
            budgets_unreadable=self.unreadable,
            max_concurrent=1,
            runner_version=0,
        )

    def save_budget(
        self, service: str, values: Mapping[str, Any], *, expected_version: int
    ) -> Mapping[str, str]:
        if self._save_error is not None:
            raise self._save_error
        if expected_version != self.version:
            raise VersionConflict(detail="changed")
        self.saves.append((service, dict(values), expected_version))
        self.stored = Budget(**dict(values))
        self.version += 1
        return {}


def step(
    usage: UsageSettingsDouble, *, mark_done: Marks | None = None, skip: Marks | None = None
) -> WebSearchStep:
    return WebSearchStep(
        cast("UsageSettings", usage), mark_done=mark_done or Marks(), skip=skip or Marks()
    )


def form(values: Mapping[str, str] | None = None, **extra: str) -> dict[str, list[str]]:
    sent = {**dict.fromkeys(BUDGET_FIELDS, ""), **(values or {}), "version": "0", **extra}
    return {name: [value] for name, value in sent.items()}


def assert_log_names_type_only(caplog: pytest.LogCaptureFixture, type_name: str) -> None:
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert type_name in text, text
    assert PRIVATE_PATH not in text and "fictional-person" not in text, text


def test_saved_budget_moves_on_and_says_it_was_saved() -> None:
    usage, marks, skips = UsageSettingsDouble(), Marks(), Marks()

    result = step(usage, mark_done=marks, skip=skips).submit(form(TYPED, action="save"))

    assert result == StepResult(ok=True, message="Web search budget saved.")
    assert usage.saves == [(SERVICE, ENTERED, 0)]
    assert (marks.count, skips.count) == (1, 0)


def test_skip_moves_on_saves_nothing_and_says_skipped() -> None:
    usage, marks, skips = UsageSettingsDouble(), Marks(), Marks()

    result = step(usage, mark_done=marks, skip=skips).submit(form(TYPED, action="skip"))

    assert result == StepResult(ok=True, message="Web search skipped.")
    assert usage.saves == []
    assert (marks.count, skips.count) == (0, 1)


def test_empty_form_without_a_stored_budget_moves_on_with_nothing_to_say() -> None:
    usage, marks = UsageSettingsDouble(), Marks()

    result = step(usage, mark_done=marks).submit(form(action="save"))

    assert result == StepResult(ok=True)
    assert usage.saves == [] and marks.count == 1


def test_empty_form_with_a_stored_budget_says_the_budget_was_kept() -> None:
    """ADR-083: an emptied form removes nothing, and the step says so instead of going on
    silently."""
    usage, marks = UsageSettingsDouble(stored=STORED), Marks()

    result = step(usage, mark_done=marks).submit(form(action="save", version="1"))

    assert result == StepResult(ok=True, message="Your web search budget of 100 requests was kept.")
    assert usage.saves == [] and usage.stored == STORED
    assert marks.count == 1


def test_empty_form_with_unreadable_budgets_claims_nothing() -> None:
    """ADR-084: nothing is known about the stored budgets, so nothing is said about them."""
    usage, marks = UsageSettingsDouble(unreadable=True), Marks()

    result = step(usage, mark_done=marks).submit(form(action="save"))

    assert result == StepResult(ok=True)
    assert marks.count == 1


def test_mark_done_failure_after_a_save_says_the_budget_was_saved(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The budget is stored but the step couldn't be marked: the message says so (as the
    preferences step does), ``saved`` tells the route to show what is stored with its new
    version, and the same form sent again from that version goes through."""
    usage = UsageSettingsDouble()
    marks = Marks(sqlite3.OperationalError(f"database is locked: {PRIVATE_PATH}"))
    with caplog.at_level(logging.INFO):
        result = step(usage, mark_done=marks).submit(form(TYPED, action="save"))

    assert result == StepResult(ok=False, errors={"form": MSG_SAVED_NOT_MOVED_ON}, saved=True)
    assert "saved" in MSG_SAVED_NOT_MOVED_ON and "isn't saved" not in MSG_SAVED_NOT_MOVED_ON
    assert MSG_SAVED_NOT_MOVED_ON != MSG_NOT_MOVED_ON
    assert usage.saves == [(SERVICE, ENTERED, 0)], "the save went through"
    assert marks.count == 1
    assert_log_names_type_only(caplog, "OperationalError")

    # The retry, from the version the step now shows.
    shown = step(usage).context()
    assert shown["version"] == "1"
    assert {name: shown[name] for name in BUDGET_FIELDS} == TYPED
    again = step(usage).submit(form(TYPED, action="save", version=str(shown["version"])))
    assert again == StepResult(ok=True, message="Web search budget saved.")


def test_mark_done_failure_with_nothing_saved_claims_no_save(
    caplog: pytest.LogCaptureFixture,
) -> None:
    usage = UsageSettingsDouble()
    marks = Marks(sqlite3.OperationalError(f"database is locked: {PRIVATE_PATH}"))
    with caplog.at_level(logging.INFO):
        result = step(usage, mark_done=marks).submit(form(action="save"))

    assert result == StepResult(ok=False, errors={"form": MSG_NOT_MOVED_ON})
    assert "saved" not in MSG_NOT_MOVED_ON
    assert usage.saves == []
    assert_log_names_type_only(caplog, "OperationalError")


def test_skip_failure_saves_nothing_and_says_the_app_could_not_move_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    usage, marks = UsageSettingsDouble(), Marks()
    skips = Marks(sqlite3.OperationalError(f"database is locked: {PRIVATE_PATH}"))
    with caplog.at_level(logging.INFO):
        result = step(usage, mark_done=marks, skip=skips).submit(form(TYPED, action="skip"))

    assert result == StepResult(ok=False, errors={"form": MSG_NOT_MOVED_ON})
    assert usage.saves == []
    assert (marks.count, skips.count) == (0, 1)
    assert_log_names_type_only(caplog, "OperationalError")


def test_workspace_closed_during_the_save_asks_for_step_1() -> None:
    usage = UsageSettingsDouble(save_error=UsageSettingUnavailable())
    marks = Marks()

    result = step(usage, mark_done=marks).submit(form(TYPED, action="save"))

    assert result == StepResult(ok=False, errors={"form": MSG_STEP_1_FIRST})
    assert marks.count == 0, "nothing was saved, so the step isn't done"


def test_version_conflict_is_a_conflict_and_marks_nothing() -> None:
    usage, marks = UsageSettingsDouble(stored=STORED), Marks()

    result = step(usage, mark_done=marks).submit(form(TYPED, action="save", version="0"))

    assert result == StepResult(ok=False, conflict=True)
    assert usage.stored == STORED and marks.count == 0
