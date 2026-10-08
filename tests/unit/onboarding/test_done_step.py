"""Step 6 of the setup guide: finishing (M1.19; AC-M1.7).

The handler lives in ``app/onboarding/steps/done.py``. Like ``ProviderStep`` it gets its
collaborators as callables::

    DoneStep(
        registry: StepRegistry,
        *,
        statuses: Callable[[], Mapping[str, StepStatus]],  # cached statuses, no I/O
        mark_done: Callable[[], None],  # marks "done" done and refreshes the decision
    )

``submit(form)`` finishes the guide. The form's ``then`` chooses where the user goes next, from
a fixed table: ``add_job`` goes to ``/inbox``, anything else (``today``, missing or unknown) to
``/``. The target never comes from the request text. A finished guide answers
``StepResult(ok=True, redirect=<path>)``: ``StepResult`` gains an optional ``redirect: str |
None = None`` field, which the route turns into a 303 (or ``HX-Redirect`` for htmx).

Finishing is refused while a required step (other than Done itself) is not done: ``ok=False``
with a ``form`` error naming the unfinished steps by title, and ``mark_done`` is not called.
Optional steps that are pending or skipped never block finishing.
"""

from collections.abc import Mapping

import pytest

from app.onboarding.detection import StepStatus
from app.onboarding.registry import (
    DONE,
    PREFERENCES,
    PROVIDER,
    WELCOME,
    StepDefinition,
    StepRegistry,
    m1_registry,
)
from app.onboarding.steps.done import DoneStep

SAMPLE_OPTIONAL = StepDefinition(
    "sample_optional", "Sample optional step", False, 45, "fragments/setup/_done.html"
)
ALL_REQUIRED_DONE: Mapping[str, StepStatus] = {
    "welcome": "done",
    "provider": "done",
    "preferences": "done",
}


class Marks:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> None:
        self.count += 1


def build(
    statuses: Mapping[str, StepStatus], registry: StepRegistry | None = None
) -> tuple[DoneStep, Marks]:
    marks = Marks()
    step = DoneStep(
        registry if registry is not None else m1_registry(),
        statuses=lambda: statuses,
        mark_done=marks,
    )
    return step, marks


@pytest.mark.parametrize(
    ("then", "target"),
    [
        pytest.param(["add_job"], "/inbox", id="add_job"),
        pytest.param(["today"], "/", id="today"),
        pytest.param(None, "/", id="missing"),
        pytest.param([""], "/", id="empty"),
        pytest.param(["board"], "/", id="unknown"),
        pytest.param(["/board"], "/", id="path"),
        pytest.param(["https://evil.example/"], "/", id="absolute_url"),
        pytest.param(["//evil.example"], "/", id="scheme_relative"),
        pytest.param(["ADD_JOB"], "/", id="case"),
    ],
)
def test_finishing_marks_done_and_redirects_from_a_fixed_table(
    then: list[str] | None, target: str
) -> None:
    step, marks = build(ALL_REQUIRED_DONE)
    result = step.submit({} if then is None else {"then": then})
    assert result.ok is True, dict(result.errors)
    assert result.redirect == target
    assert result.stay is False
    assert marks.count == 1


def test_finishing_refused_while_a_required_step_is_not_done() -> None:
    step, marks = build({"welcome": "done", "provider": "done"})
    result = step.submit({"then": ["add_job"]})
    assert result.ok is False
    assert set(result.errors) == {"form"}
    message = result.errors["form"]
    assert PREFERENCES.title in message
    assert WELCOME.title not in message, "only unfinished steps are named"
    assert PROVIDER.title not in message
    assert result.redirect is None
    assert marks.count == 0


def test_finishing_refused_names_every_unfinished_required_step() -> None:
    step, marks = build({"welcome": "done"})
    result = step.submit({"then": ["today"]})
    assert result.ok is False
    assert PROVIDER.title in result.errors["form"]
    assert PREFERENCES.title in result.errors["form"]
    assert marks.count == 0


def test_skipped_counts_as_unfinished_for_a_required_step() -> None:
    step, marks = build({"welcome": "done", "provider": "done", "preferences": "skipped"})
    assert step.submit({"then": ["today"]}).ok is False
    assert marks.count == 0


@pytest.mark.parametrize("optional", ["pending", "skipped", None])
def test_optional_steps_never_block_finishing(optional: StepStatus | None) -> None:
    registry = StepRegistry((WELCOME, PROVIDER, SAMPLE_OPTIONAL, PREFERENCES, DONE))
    statuses: dict[str, StepStatus] = dict(ALL_REQUIRED_DONE)
    if optional is not None:
        statuses[SAMPLE_OPTIONAL.id] = optional
    step, marks = build(statuses, registry)
    result = step.submit({"then": ["add_job"]})
    assert result.ok is True, dict(result.errors)
    assert result.redirect == "/inbox"
    assert marks.count == 1


def test_finishing_again_is_allowed() -> None:
    """Reopened with --setup, the user finishes again; marking done is monotonic."""
    step, marks = build({**ALL_REQUIRED_DONE, "done": "done"})
    assert step.submit({"then": ["today"]}).ok is True
    assert marks.count == 1
