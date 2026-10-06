"""The setup guide's steps, in order (detailed design "Module boundaries"; ADR-018).

Each step is a frozen definition (id, title, required flag, order, fragment template
rendered by ``app/api``) plus a ``StepHandler`` that adds its behaviour in its own task.
Handlers need services, so ``app/wiring.py`` builds them and ``require_handlers`` checks there
is exactly one per registered step. A handler calls services only, and marks its step done
itself (through ``SetupProgress``) when the step is really finished, which may be later than
a valid submit (the provider step is done only after "Connected"). Completeness is that stored
"done" status (ADR-052). Adding or reordering steps touches only this module and wiring.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from app.domain.errors import UnknownSetupStep
from app.onboarding.detection import StepInfo

_STEP_ID = re.compile(r"^[a-z][a-z0-9_]*$")
# Fixed ``POST /setup/<name>`` routes (app/api/setup.py): a step with one of these ids would
# have its submit swallowed by the fixed route, so the registry refuses them.
RESERVED_STEP_IDS = frozenset({"leave"})


@dataclass(frozen=True)
class StepDefinition:
    id: str
    title: str
    required: bool
    order: int
    template: str


@dataclass(frozen=True)
class StepResult:
    ok: bool
    errors: Mapping[str, str] = field(default_factory=dict)  # field name -> plain message
    # Saved, but the step isn't finished: the guide stays on it and announces ``message``.
    stay: bool = False
    message: str | None = None
    # Finished and leaving the guide: a local path from the handler's fixed table (M1.19).
    redirect: str | None = None


# Submitted form fields; a field may repeat (lists such as target roles).
Form = Mapping[str, Sequence[str]]


@dataclass(frozen=True)
class ConnectionCheck:
    """A step's connection test (M1.18): ``errors`` when the save before it was refused (no
    test ran); else ``ok`` and the plain ``message`` shown and announced."""

    ok: bool
    message: str = ""
    errors: Mapping[str, str] = field(default_factory=dict)


class ConnectionTester(Protocol):
    """A step that can test its connection inside a POST (the provider step)."""

    async def test(self, form: Form) -> ConnectionCheck: ...


class StepHandler(Protocol):
    def context(self) -> Mapping[str, object]: ...

    def submit(self, form: Form) -> StepResult:
        """Validate and save; ``ok`` means saved, not necessarily that the step is done."""
        ...


class StepRegistry:
    def __init__(self, definitions: Iterable[StepDefinition]) -> None:
        steps = tuple(sorted(definitions, key=lambda step: step.order))
        ids = [step.id for step in steps]
        orders = [step.order for step in steps]
        if not steps:
            raise ValueError("No setup steps.")
        if bad := [step_id for step_id in ids if not _STEP_ID.fullmatch(step_id)]:
            raise ValueError(f"Invalid step ids: {bad}")
        if reserved := [step_id for step_id in ids if step_id in RESERVED_STEP_IDS]:
            raise ValueError(f"Reserved step ids: {reserved}")
        if len(set(ids)) != len(ids):
            raise ValueError("Step ids must be unique.")
        if len(set(orders)) != len(orders):
            raise ValueError("Step orders must be unique.")
        self._steps = steps
        self._by_id = {step.id: step for step in steps}

    @property
    def steps(self) -> tuple[StepDefinition, ...]:
        return self._steps

    @property
    def first(self) -> StepDefinition:
        return self._steps[0]

    @property
    def last(self) -> StepDefinition:
        return self._steps[-1]

    def get(self, step_id: str) -> StepDefinition:
        try:
            return self._by_id[step_id]
        except KeyError:
            raise UnknownSetupStep(detail="unknown step id") from None

    def infos(self) -> tuple[StepInfo, ...]:
        return tuple(StepInfo(step.id, step.required) for step in self._steps)


WELCOME = StepDefinition(
    "welcome", "Welcome and data folder", True, 10, "fragments/setup/_welcome.html"
)
PROVIDER = StepDefinition("provider", "AI provider", True, 20, "fragments/setup/_provider.html")
# Orders 30 and 40 are kept for web search (M2) and CV import (M3).
PREFERENCES = StepDefinition(
    "preferences", "Job preferences", True, 50, "fragments/setup/_preferences.html"
)
DONE = StepDefinition("done", "Done", False, 60, "fragments/setup/_done.html")


def m1_registry() -> StepRegistry:
    return StepRegistry((WELCOME, PROVIDER, PREFERENCES, DONE))


def require_handlers(
    registry: StepRegistry, handlers: Mapping[str, StepHandler]
) -> Mapping[str, StepHandler]:
    """The handlers, checked to cover exactly the registered steps."""
    registered = {step.id for step in registry.steps}
    if set(handlers) != registered:
        missing = sorted(registered - set(handlers))
        extra = sorted(set(handlers) - registered)
        raise ValueError(f"Step handlers don't match the steps (missing {missing}, extra {extra}).")
    return handlers
