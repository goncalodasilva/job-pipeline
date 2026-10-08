"""Setup progress for the registered steps (AC-M1.6): the rules on top of stored state."""

from app.domain.errors import StepNotSkippable
from app.onboarding.detection import StepStatus, first_unfinished_required
from app.onboarding.registry import StepRegistry
from app.services.setup_state import SetupStateService


class SetupProgress:
    def __init__(self, registry: StepRegistry, state: SetupStateService) -> None:
        self._registry = registry
        self._state = state

    def statuses(self) -> dict[str, StepStatus]:
        """Statuses of registered steps; rows for steps no longer registered are ignored."""
        stored = self._state.statuses()
        return {step.id: stored[step.id] for step in self._registry.steps if step.id in stored}

    def mark_done(self, step_id: str) -> None:
        self._registry.get(step_id)  # UnknownSetupStep for an unknown id
        self._state.mark_done(step_id)

    def skip(self, step_id: str) -> None:
        """Only optional steps; the final Done step is finished, never skipped."""
        step = self._registry.get(step_id)
        if step.required or step == self._registry.last:
            raise StepNotSkippable(detail="required or final step")
        self._state.mark_skipped(step_id)

    def first_unfinished_required(self) -> str | None:
        return first_unfinished_required(self._registry.infos(), self.statuses())
