"""Setup guide step registry and the Step protocol (M1.8; AC-M1.6 part; spec 2.3).

The registry holds the step definitions (id, title, required, order, template) in order,
refuses ambiguous definitions, and feeds start-up detection through ``infos()``. M1 registers
steps 1, 2, 5 and 6 of the detailed design; orders 30 and 40 stay free for web search (M2)
and CV import (M3). Step behaviour is a structural ``StepHandler`` protocol.
"""

import dataclasses
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import pytest

from app.domain.errors import StepNotSkippable, UnknownSetupStep
from app.onboarding.detection import StepInfo, StepStatus, first_unfinished_required
from app.onboarding.progress import SetupProgress
from app.onboarding.registry import (
    DONE,
    PREFERENCES,
    PROVIDER,
    RESERVED_STEP_IDS,
    WELCOME,
    StepDefinition,
    StepRegistry,
    StepResult,
    m1_registry,
    require_handlers,
)
from app.services.setup_state import SetupStateService

REPO_ROOT = Path(__file__).resolve().parents[3]


def step(step_id: str, order: int, *, required: bool = True) -> StepDefinition:
    return StepDefinition(
        id=step_id,
        title=f"Fictional {step_id}",
        required=required,
        order=order,
        template=f"fragments/setup/_{step_id}.html",
    )


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


def progress_over(state: InMemorySetupState, registry: StepRegistry) -> SetupProgress:
    return SetupProgress(registry, cast("SetupStateService", state))


# --- ordering and validation ------------------------------------------------------


def test_registry_orders_steps_by_order() -> None:
    registry = StepRegistry([step("gamma", 30), step("alpha", 10), step("beta", 20)])

    assert [s.id for s in registry.steps] == ["alpha", "beta", "gamma"]
    assert isinstance(registry.steps, tuple)
    assert registry.first.id == "alpha"
    assert registry.get("beta").order == 20
    assert [i.id for i in registry.infos()] == ["alpha", "beta", "gamma"]


def test_registry_get_unknown_raises_key_error() -> None:
    registry = StepRegistry([step("alpha", 10)])

    with pytest.raises(UnknownSetupStep):
        registry.get("missing")


def test_registry_rejects_duplicate_ids() -> None:
    with pytest.raises(ValueError):
        StepRegistry([step("alpha", 10), step("alpha", 20)])


def test_registry_rejects_duplicate_orders() -> None:
    with pytest.raises(ValueError):
        StepRegistry([step("alpha", 10), step("beta", 10)])


@pytest.mark.parametrize(
    "bad_id",
    ["", "Welcome", "1step", "_hidden", "has-dash", "has space", "dots.in.id", "../up", "é"],
)
def test_registry_rejects_bad_ids(bad_id: str) -> None:
    with pytest.raises(ValueError):
        StepRegistry([step("alpha", 10), dataclasses.replace(step("alpha", 20), id=bad_id)])


@pytest.mark.parametrize("reserved", sorted(RESERVED_STEP_IDS))
def test_registry_rejects_reserved_ids(reserved: str) -> None:
    """``POST /setup/leave`` is a fixed route: a step named ``leave`` could never be saved."""
    assert "leave" in RESERVED_STEP_IDS
    with pytest.raises(ValueError, match="Reserved"):
        StepRegistry([step("alpha", 10), step(reserved, 20)])


def test_m1_registry_uses_no_reserved_ids() -> None:
    assert not {step.id for step in m1_registry().steps} & RESERVED_STEP_IDS


def test_definitions_are_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        WELCOME.required = False  # type: ignore[misc]
    result = StepResult(ok=True)
    assert result.errors == {}
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.ok = False  # type: ignore[misc]


# --- the M1 registry ---------------------------------------------------------------


def test_m1_registers_steps_1_2_5_6() -> None:
    registry = m1_registry()

    assert [(s.id, s.order, s.required) for s in registry.steps] == [
        ("welcome", 10, True),
        ("provider", 20, True),
        ("preferences", 50, True),
        ("done", 60, False),
    ]
    assert registry.steps == (WELCOME, PROVIDER, PREFERENCES, DONE)
    assert WELCOME.title == "Welcome and data folder"
    assert PROVIDER.title == "AI provider"
    assert PREFERENCES.title == "Job preferences"
    assert DONE.title == "Done"
    for definition in registry.steps:
        assert definition.title.strip()
        assert re.fullmatch(r"[a-z][a-z0-9_]*", definition.id)
        assert definition.template.startswith("fragments/setup/")
        assert definition.template.endswith(".html")
    ids = {s.id for s in registry.steps}
    assert "web_search" not in ids
    assert "import_cv" not in ids
    assert registry.steps[-1].id == "done"
    assert registry.first is WELCOME
    assert registry.infos() == (
        StepInfo("welcome", True),
        StepInfo("provider", True),
        StepInfo("preferences", True),
        StepInfo("done", False),
    )


def test_m1_registry_returns_fresh_equal_registries() -> None:
    assert m1_registry().steps == m1_registry().steps


# --- first unfinished required step -------------------------------------------------


def test_first_unfinished_required_step() -> None:
    registry = m1_registry()
    state = InMemorySetupState()
    progress = progress_over(state, registry)

    assert progress.first_unfinished_required() == "welcome"
    progress.mark_done("welcome")
    assert progress.first_unfinished_required() == "provider"
    progress.mark_done("provider")
    assert progress.first_unfinished_required() == "preferences"
    progress.mark_done("preferences")
    assert progress.first_unfinished_required() is None
    assert state.stored == {"welcome": "done", "provider": "done", "preferences": "done"}

    # Agrees with detection over the registry's infos.
    assert first_unfinished_required(registry.infos(), {"welcome": "done"}) == "provider"


def test_first_unfinished_required_ignores_unregistered_rows() -> None:
    state = InMemorySetupState({"welcome": "done", "retired_step": "pending"})
    progress = progress_over(state, m1_registry())

    assert progress.statuses() == {"welcome": "done"}
    assert progress.first_unfinished_required() == "provider"


def test_progress_skip_required_step_raises_and_stores_nothing() -> None:
    state = InMemorySetupState()
    progress = progress_over(state, m1_registry())

    with pytest.raises(StepNotSkippable):
        progress.skip("provider")
    with pytest.raises(UnknownSetupStep):
        progress.skip("not_registered")
    with pytest.raises(UnknownSetupStep):
        progress.mark_done("not_registered")
    assert state.stored == {}


# --- Step protocol -------------------------------------------------------------------

CONFORMANCE = """\
from collections.abc import Mapping, Sequence

from app.onboarding.registry import StepHandler, StepResult


class FictionalStep:
    def context(self) -> Mapping[str, object]:
        return {"heading": "Fictional"}

    def submit(self, form: Mapping[str, Sequence[str]]) -> StepResult:
        if not form.get("name"):
            return StepResult(ok=False, errors={"name": "Enter a name."})
        return StepResult(ok=True)


handler: StepHandler = FictionalStep()
"""

NON_CONFORMING = """\
from collections.abc import Mapping

from app.onboarding.registry import StepHandler


class MissingSubmit:
    def context(self) -> Mapping[str, object]:
        return {}


handler: StepHandler = MissingSubmit()
"""


def run_mypy(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    sample = tmp_path / "conformance.py"
    sample.write_text(source, encoding="utf-8")
    # Fixed argv to the venv's own interpreter; no shell, no untrusted input.
    return subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--no-incremental",
            "--cache-dir",
            str(tmp_path / ".mypy_cache"),
            str(sample),
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "MYPYPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )


def test_step_handler_protocol_is_structural(tmp_path: Path) -> None:
    (tmp_path / "ok").mkdir()
    (tmp_path / "bad").mkdir()

    accepted = run_mypy(tmp_path / "ok", CONFORMANCE)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr

    rejected = run_mypy(tmp_path / "bad", NON_CONFORMING)
    assert rejected.returncode != 0
    assert "StepHandler" in rejected.stdout


def test_registry_get_raises_plain_error_for_unknown_steps() -> None:
    with pytest.raises(UnknownSetupStep) as raised:
        m1_registry().get("../etc")
    assert raised.value.user_message == "This setup step doesn't exist."


def test_final_done_step_cannot_be_skipped() -> None:
    progress = SetupProgress(m1_registry(), cast("SetupStateService", InMemorySetupState()))
    with pytest.raises(StepNotSkippable):
        progress.skip("done")


def test_require_handlers_matches_registered_steps() -> None:
    registry = m1_registry()

    class Handler:
        def context(self) -> Mapping[str, object]:
            return {}

        def submit(self, form: Mapping[str, Sequence[str]]) -> StepResult:
            return StepResult(ok=True)

    handlers = {step.id: Handler() for step in registry.steps}
    assert require_handlers(registry, handlers) is handlers
    with pytest.raises(ValueError):
        require_handlers(registry, {k: v for k, v in handlers.items() if k != "provider"})
    with pytest.raises(ValueError):
        require_handlers(registry, {**handlers, "fictional_extra": Handler()})


def test_m1_registry_matches_detection_step_names() -> None:
    from app.onboarding.detection import StartupFacts

    registry = m1_registry()
    facts = StartupFacts(
        bootstrap_exists=True, data_dir_explicit=False, demo=False, database_exists=True, steps=()
    )
    assert registry.last.id == facts.done_step
    assert registry.get(facts.provider_step) == PROVIDER
