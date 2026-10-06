"""Step 6: Done (M1.19; AC-M1.7).

Lists every other step with its state in words and a link back to it, without personal
details. Finishing marks the step done through the injected ``mark_done`` (which also refreshes
the setup decision) and leaves the guide for a page chosen from a fixed table, never from the
request text. It is refused while a required step is not done; optional steps, pending or
skipped, never block it. Finishing again (after ``--setup``) is allowed.
"""

import logging
from collections.abc import Callable, Mapping

from app.onboarding.detection import StepStatus
from app.onboarding.registry import Form, StepDefinition, StepRegistry, StepResult

_log = logging.getLogger(__name__)

# Where "then" leads; anything else goes to Today.
TARGETS: Mapping[str, str] = {"add_job": "/inbox", "today": "/"}
DEFAULT_TARGET = "/"
STATUS_TEXT: Mapping[str, str] = {"done": "Done", "skipped": "Skipped"}
NOT_DONE_TEXT = "Not done yet"  # a required step
OPTIONAL_TEXT = "Optional, not set up"  # an optional step neither done nor skipped
MSG_SAVE_FAILED = "The app couldn't record that setup is finished. Try again."


class DoneStep:
    echo_fields: tuple[str, ...] = ()

    def __init__(
        self,
        registry: StepRegistry,
        *,
        statuses: Callable[[], Mapping[str, StepStatus]],
        mark_done: Callable[[], None],
    ) -> None:
        self._registry = registry
        self._statuses = statuses  # no I/O: the cached statuses
        self._mark_done = mark_done  # blocking: marks the step done, refreshes the decision

    def context(self) -> Mapping[str, object]:
        statuses = self._statuses()
        rows = [
            {"id": step.id, "title": step.title, "status_text": _status_text(step, statuses)}
            for step in self._others()
        ]
        unfinished = [{"id": step.id, "title": step.title} for step in self._unfinished(statuses)]
        return {"rows": rows, "unfinished": unfinished}

    def submit(self, form: Form) -> StepResult:
        unfinished = self._unfinished(self._statuses())
        if unfinished:
            titles = _join([step.title for step in unfinished])
            return StepResult(ok=False, errors={"form": f"Finish {titles} first."})
        try:
            self._mark_done()
        except Exception as error:  # sqlite messages can hold paths: log the type only
            _log.warning("Done step couldn't be marked done (%s).", type(error).__name__)
            return StepResult(ok=False, errors={"form": MSG_SAVE_FAILED})
        then = form.get("then") or ()
        target = TARGETS.get(then[0], DEFAULT_TARGET) if then else DEFAULT_TARGET
        return StepResult(ok=True, redirect=target)

    def _others(self) -> tuple[StepDefinition, ...]:
        last = self._registry.last
        return tuple(step for step in self._registry.steps if step != last)

    def _unfinished(self, statuses: Mapping[str, StepStatus]) -> list[StepDefinition]:
        return [
            step for step in self._others() if step.required and statuses.get(step.id) != "done"
        ]


def _status_text(step: StepDefinition, statuses: Mapping[str, StepStatus]) -> str:
    status = statuses.get(step.id, "pending")
    if status in STATUS_TEXT:
        return STATUS_TEXT[status]
    return NOT_DONE_TEXT if step.required else OPTIONAL_TEXT


def _join(titles: list[str]) -> str:
    if len(titles) == 1:
        return titles[0]
    return ", ".join(titles[:-1]) + " and " + titles[-1]
