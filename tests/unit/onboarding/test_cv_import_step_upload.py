"""Step 4's upload once the CV is stored (M3.7 review): nothing after the store stops the
guide, a failure to mark the step keeps the upload and a retry reuses it, and a pasted text's
refusal belongs to the text field. Fakes stand in for the import service and the budget gate;
no I/O; every value is fictional.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from app.domain.errors import CvTextTooLong, ProviderNotConfigured, UnreadableDocument
from app.onboarding.steps.cv_import import (
    MSG_KEPT,
    MSG_SAVED_NOT_MOVED_ON,
    MSG_STARTED,
    CvImportStep,
)
from app.services.budget_gate import BlockedAction, BudgetGate, GateAnswer, Start
from app.services.cv_import import CvImportService, StartPlan

CV = b"Alex Example, fictional route planner at Example Freight Lda."
OTHER_CV = b"Sam Example, fictional archivist at Example Library."


@dataclass
class Listed:
    id: str
    state: str


@dataclass
class FakeImports:
    """The parts of ``CvImportService`` the step uses."""

    refuse: Exception | None = None
    plan_error: Exception | None = None
    start_error: Exception | None = None
    stored: list[bytes] = field(default_factory=list)
    started: list[str] = field(default_factory=list)

    def store(self, data: bytes) -> str:
        if self.refuse is not None:
            raise self.refuse
        self.stored.append(data)
        return f"imp-{len(self.stored)}"

    def imports(self) -> tuple[Listed, ...]:
        return tuple(Listed(f"imp-{n}", "uploaded") for n in range(1, len(self.stored) + 1))

    def plan_start(self, import_id: str) -> StartPlan:
        if self.plan_error is not None:
            raise self.plan_error
        return StartPlan("fake", "thorough")

    def start(self, import_id: str) -> None:
        if self.start_error is not None:
            raise self.start_error
        self.started.append(import_id)


@dataclass
class FakeGate:
    answer: GateAnswer = field(default_factory=Start)

    def check(self, service: str, *, tier: str, confirmation: object = None) -> GateAnswer:
        return self.answer


@dataclass
class Marks:
    fail: int = 0  # how many calls fail before one goes through
    done: int = 0

    def __call__(self) -> None:
        if self.fail:
            self.fail -= 1
            raise RuntimeError("fictional failure at /Users/fictional/secret-path")
        self.done += 1


def step(imports: FakeImports, gate: FakeGate | None = None, marks: Marks | None = None) -> Any:
    return CvImportStep(
        cast("CvImportService", imports),
        cast("BudgetGate", gate or FakeGate()),
        workspace_open=lambda: True,
        mark_done=marks or Marks(),
        skip=lambda: None,
    )


def test_upload_starts_and_moves_on() -> None:
    imports = FakeImports()
    result = step(imports).upload(CV)
    assert (result.ok, result.message) == (True, MSG_STARTED)
    assert imports.started == ["imp-1"]


@pytest.mark.parametrize(
    "hold",
    ["blocked", "no_provider", "start_fails"],
)
def test_start_held_back_keeps_the_upload_and_moves_on(
    hold: str, caplog: pytest.LogCaptureFixture
) -> None:
    imports = FakeImports()
    gate = FakeGate()
    if hold == "blocked":
        gate.answer = BlockedAction("A fictional block.")
    elif hold == "no_provider":
        imports.plan_error = ProviderNotConfigured()
    else:
        imports.start_error = RuntimeError("fictional failure at /Users/fictional/secret-path")
    marks = Marks()

    with caplog.at_level(logging.WARNING):
        result = step(imports, gate, marks).upload(CV)

    assert (result.ok, result.message) == (True, MSG_KEPT)
    assert imports.stored == [CV] and imports.started == [], "kept, not started"
    assert marks.done == 1, "the step is done once the CV is stored"
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "secret-path" not in logged
    if hold == "start_fails":
        assert "RuntimeError" in logged


def test_mark_failure_keeps_the_upload_and_a_retry_reuses_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    imports = FakeImports()
    marks = Marks(fail=1)
    handler = step(imports, marks=marks)

    with caplog.at_level(logging.WARNING):
        failed = handler.upload(CV)

    assert failed.ok is False and failed.errors == {"form": MSG_SAVED_NOT_MOVED_ON}
    assert imports.stored == [CV] and imports.started == []
    assert "secret-path" not in caplog.text

    again = handler.upload(CV)

    assert (again.ok, again.message) == (True, MSG_STARTED)
    assert imports.stored == [CV], "the same CV isn't stored twice"
    assert imports.started == ["imp-1"]

    other = handler.upload(OTHER_CV)
    assert other.ok and imports.stored == [CV, OTHER_CV], "another CV is stored as usual"


def test_a_different_cv_after_a_mark_failure_is_stored() -> None:
    imports = FakeImports()
    handler = step(imports, marks=Marks(fail=1))
    handler.upload(CV)
    result = handler.upload(OTHER_CV)
    assert result.ok and imports.stored == [CV, OTHER_CV]


@pytest.mark.parametrize("pasted", [False, True], ids=["file", "pasted"])
def test_refusal_belongs_to_the_field_it_came_from(pasted: bool) -> None:
    for error in (CvTextTooLong(), UnreadableDocument()):
        imports = FakeImports(refuse=error)
        marks = Marks()
        result = step(imports, marks=marks).upload(CV, pasted=pasted)
        assert result.ok is False
        assert result.errors == {("text" if pasted else "file"): error.user_message}
        assert marks.done == 0 and imports.started == []
