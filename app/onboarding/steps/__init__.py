"""Step handlers of the setup guide (ADR-052). Wiring (``app/main.py``) builds them."""

from collections.abc import Mapping

from app.onboarding.registry import Form, StepResult

NOT_AVAILABLE = "This step can't be saved yet."


class PendingStep:
    """Stands in for a step whose form arrives in a later task (M1.17 to M1.19)."""

    echo_fields: tuple[str, ...] = ()

    def context(self) -> Mapping[str, object]:
        return {}

    def submit(self, form: Form) -> StepResult:
        return StepResult(ok=False, errors={"form": NOT_AVAILABLE})
