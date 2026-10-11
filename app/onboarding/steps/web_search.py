"""Step 3: web search and its optional budget (M2.25; AC-M1.7; ADR-064, ADR-081 to ADR-084).

Optional. The step explains what web search is and offers the Web search service's request
budget, with the same fields and rules as Settings > Usage budgets (the rules are the
domain's, reached through ``UsageSettings``). The service is fixed here: nothing the form
sends chooses it.

- "Skip for now" marks the step skipped and saves nothing, whatever was typed.
- Continuing with every field empty marks the step done and saves nothing: no budget when
  none is stored, and a stored budget is never removed by an emptied form (ADR-083), which
  the step then says ("... was kept").
- A filled form is saved with the version it was made from; a refused budget returns a
  message per field, and a version that changed elsewhere returns ``conflict`` (ADR-081).
- When the budget was saved but the step couldn't be marked done, the result says so and is
  ``saved``, so the form shown again holds the stored version and can be sent again.

A result that moves on carries what happened as ``message``, announced before the next step.

``mark_done`` and ``skip`` are injected by wiring; both store the status and refresh the
setup decision. Without a workspace the step asks the user to finish step 1 first.
"""

import logging
from collections.abc import Callable, Mapping

from app.domain.errors import VersionConflict
from app.domain.usage import WEB_SEARCH_SERVICE
from app.onboarding.forms import first_value
from app.onboarding.registry import Form, StepResult
from app.services.usage_settings import (
    BUDGET_FIELDS,
    BudgetView,
    UsageSettings,
    UsageSettingUnavailable,
    whole_number,
)

_log = logging.getLogger(__name__)

SKIP = "skip"
MSG_STEP_1_FIRST = "Finish step 1 first: choose where the app keeps your data."
MSG_NOT_MOVED_ON = "The app couldn't move on. Try again."
MSG_SAVED_NOT_MOVED_ON = "Your budget was saved, but the app couldn't move on. Try again."
MSG_SAVED = "Web search budget saved."
MSG_SKIPPED = "Web search skipped."
MSG_KEPT = "Your web search budget of {allowance} requests was kept."


class WebSearchStep:
    # Shown back after a refused or stale submit (autoescaped). The version is kept as it
    # was sent, so a form that is behind keeps conflicting until the step is loaded again.
    echo_fields: tuple[str, ...] = (*BUDGET_FIELDS, "version")

    def __init__(
        self,
        usage_settings: UsageSettings,
        *,
        mark_done: Callable[[], None],
        skip: Callable[[], None],
    ) -> None:
        self._usage_settings = usage_settings
        self._mark_done = mark_done  # blocking: marks the step done, refreshes the decision
        self._skip = skip  # blocking: marks the step skipped, refreshes the decision

    def context(self) -> Mapping[str, object]:
        """A read: writes nothing."""
        common: dict[str, object] = {
            "fields": BUDGET_FIELDS,
            "conflict_message": VersionConflict.message,
        }
        if not self._usage_settings.available():
            return {**common, "workspace_open": False}
        view = self._usage_settings.view()
        stored = _web_search(view.budgets)
        return {
            **common,
            "workspace_open": True,
            "version": str(view.budgets_version),
            "budgets_unreadable": view.budgets_unreadable,
            "allowance_set": stored.budget.allowance if stored and stored.budget else None,
            **(stored.values if stored else dict.fromkeys(BUDGET_FIELDS, "")),
        }

    def submit(self, form: Form) -> StepResult:
        if not self._usage_settings.available():
            return StepResult(ok=False, errors={"form": MSG_STEP_1_FIRST})
        if first_value(form, "action") == SKIP:
            return self._finish(self._skip, MSG_SKIPPED)
        entered = {name: whole_number(first_value(form, name)) for name in BUDGET_FIELDS}
        if all(value is None for value in entered.values()):
            return self._finish(self._mark_done, self._kept())
        refused = self._save(entered, whole_number(first_value(form, "version")))
        if refused is not None:
            return refused
        return self._finish(self._mark_done, MSG_SAVED, saved=True)

    def _kept(self) -> str | None:
        """What to say when an emptied form leaves a stored budget in place (ADR-083); None
        when there is none, or when the stored budgets can't be read (ADR-084)."""
        stored = _web_search(self._usage_settings.view().budgets)
        if stored is None or stored.budget is None:
            return None
        return MSG_KEPT.format(allowance=stored.budget.allowance)

    def _save(self, entered: Mapping[str, object], version: object) -> StepResult | None:
        """Save the budget; the refusal when it wasn't saved."""
        if not isinstance(version, int):  # not a number: no page of this app sent it
            return StepResult(ok=False, conflict=True)
        try:
            errors = self._usage_settings.save_budget(
                WEB_SEARCH_SERVICE, entered, expected_version=version
            )
        except VersionConflict:
            return StepResult(ok=False, conflict=True)
        except UsageSettingUnavailable:  # the workspace closed meanwhile
            return StepResult(ok=False, errors={"form": MSG_STEP_1_FIRST})
        return StepResult(ok=False, errors=errors) if errors else None

    def _finish(
        self, record: Callable[[], None], message: str | None, *, saved: bool = False
    ) -> StepResult:
        """Mark the step and move on, announcing ``message``; ``saved`` when a budget was
        just stored, which a failure to mark must not hide."""
        try:
            record()
        except Exception as error:  # sqlite messages can hold paths: log the type only
            _log.warning("Web search step couldn't be recorded (%s).", type(error).__name__)
            said = MSG_SAVED_NOT_MOVED_ON if saved else MSG_NOT_MOVED_ON
            return StepResult(ok=False, errors={"form": said}, saved=saved)
        return StepResult(ok=True, message=message)


def _web_search(budgets: tuple[BudgetView, ...]) -> BudgetView | None:
    return next((item for item in budgets if item.service == WEB_SEARCH_SERVICE), None)
