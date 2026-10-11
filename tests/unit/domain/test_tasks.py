"""Task states, dedupe keys, task-type specs and the cancel flag (M2.6; AC-M2.1; spec 5 M2
reliability rules 1, 2, 6; ADR-076 item 11).

``app.domain.tasks`` is pure: the task states with their active and terminal sets, the only
transitions a conditional update may make, the states a retry may start from, the dedupe key
built from a task type and its target, a validated per-type spec (timeout and label), and a
``CancelFlag`` that satisfies the AI module's ``CancelToken`` protocol by shape (``app.tasks``
can't import ``app.modules``, ADR-034).

Imports are inside the tests so each one fails on its own while the module is missing. Task
types and targets are fictional.
"""

import dataclasses
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

STATUS_VALUES = ("queued", "running", "succeeded", "failed", "cancelled", "interrupted")

# The full transition table (spec 5 M2 rule 1). Everything not listed is refused; leaving a
# terminal state is only possible through retry (RETRY_FROM), never through can_transition.
EXPECTED_TRANSITIONS = {
    "queued": {"running", "cancelled"},
    "running": {"succeeded", "failed", "cancelled", "interrupted"},
}


# --- statuses -----------------------------------------------------------------


def test_task_status_values_match_the_schema() -> None:
    from app.domain.tasks import TaskStatus

    # The values are the strings 0001's CHECK on tasks.status accepts.
    assert {status.value for status in TaskStatus} == set(STATUS_VALUES)
    for value in STATUS_VALUES:
        assert TaskStatus(value).value == value
        # Stored and compared as plain text.
        assert TaskStatus(value) == value
    with pytest.raises(ValueError):
        TaskStatus("paused")


def test_active_and_terminal_sets_partition_the_statuses() -> None:
    from app.domain.tasks import ACTIVE, TERMINAL, TaskStatus

    assert {s.value for s in ACTIVE} == {"queued", "running"}
    assert {s.value for s in TERMINAL} == {"succeeded", "failed", "cancelled", "interrupted"}
    assert ACTIVE.isdisjoint(TERMINAL)
    assert set(ACTIVE) | set(TERMINAL) == set(TaskStatus)
    # Shared module constants must not be mutable.
    assert isinstance(ACTIVE, frozenset)
    assert isinstance(TERMINAL, frozenset)


# --- transitions ---------------------------------------------------------------


def test_allowed_transitions_table() -> None:
    from app.domain.tasks import ALLOWED_TRANSITIONS, TaskStatus, can_transition

    # The table itself: entries with no targets (terminal states) may be present or absent.
    declared = {
        source.value: {target.value for target in targets}
        for source, targets in ALLOWED_TRANSITIONS.items()
        if targets
    }
    assert declared == EXPECTED_TRANSITIONS

    # The full 6 x 6 matrix through can_transition, including self-transitions.
    for source in TaskStatus:
        for target in TaskStatus:
            expected = target.value in EXPECTED_TRANSITIONS.get(source.value, set())
            assert can_transition(source, target) is expected, (source.value, target.value)


def test_terminal_states_leave_only_by_retry() -> None:
    from app.domain.tasks import RETRY_FROM, TERMINAL, TaskStatus, can_transition

    for terminal in TERMINAL:
        for target in TaskStatus:
            assert can_transition(terminal, target) is False, (terminal.value, target.value)

    # Retry (ADR-076 item 11) starts from failed, interrupted or cancelled; never succeeded.
    assert {s.value for s in RETRY_FROM} == {"failed", "interrupted", "cancelled"}
    assert RETRY_FROM <= TERMINAL
    assert TaskStatus("succeeded") not in RETRY_FROM
    assert isinstance(RETRY_FROM, frozenset)
    # Retry is the only way back to queued: no ordinary transition reaches it.
    for source in TaskStatus:
        assert can_transition(source, TaskStatus("queued")) is False


# --- dedupe keys ---------------------------------------------------------------


def test_dedupe_key_requires_type_and_target() -> None:
    from app.domain.tasks import dedupe_key

    assert dedupe_key("practice", "run-1") == "practice:run-1"
    # Targets may be compound (background-task skill: tailor_cv:application:42). The type can't
    # hold ':' (below), so the first ':' always separates type from target.
    assert dedupe_key("tailor_cv", "application:42") == "tailor_cv:application:42"

    for task_type, target in [
        ("", "run-1"),
        ("practice", ""),
        ("", ""),
        ("   ", "run-1"),
        ("practice", " \t\n"),
    ]:
        with pytest.raises(ValueError):
            dedupe_key(task_type, target)


def test_dedupe_key_refuses_separator_in_task_type() -> None:
    from app.domain.tasks import dedupe_key

    # "a:b" + "c" and "a" + "b:c" would otherwise share a key.
    with pytest.raises(ValueError):
        dedupe_key("tailor_cv:application", "42")


def test_dedupe_key_distinct_inputs_give_distinct_keys() -> None:
    from app.domain.tasks import dedupe_key

    keys = {
        dedupe_key("practice", "run-1"),
        dedupe_key("practice", "run-2"),
        dedupe_key("tailor_cv", "run-1"),
        dedupe_key("tailor_cv", "application:42"),
        dedupe_key("tailor_cv", "application:4"),
    }
    assert len(keys) == 5


# --- task type specs -----------------------------------------------------------


def test_task_type_spec_holds_timeout_and_label() -> None:
    from app.domain.tasks import TaskTypeSpec

    spec = TaskTypeSpec(timeout=timedelta(minutes=5), label="Practice task")
    assert spec.timeout == timedelta(minutes=5)
    assert spec.label == "Practice task"
    assert spec == TaskTypeSpec(timeout=timedelta(minutes=5), label="Practice task")


def test_task_type_spec_is_frozen() -> None:
    from app.domain.tasks import TaskTypeSpec

    spec = TaskTypeSpec(timeout=timedelta(seconds=90), label="Practice task")
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.label = "Changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "timeout", [timedelta(0), timedelta(seconds=-1), timedelta(microseconds=-1)]
)
def test_task_type_spec_refuses_non_positive_timeout(timeout: timedelta) -> None:
    from app.domain.tasks import TaskTypeSpec

    with pytest.raises(ValueError):
        TaskTypeSpec(timeout=timeout, label="Practice task")


@pytest.mark.parametrize("label", ["", "   ", "\t\n"])
def test_task_type_spec_refuses_blank_label(label: str) -> None:
    from app.domain.tasks import TaskTypeSpec

    with pytest.raises(ValueError):
        TaskTypeSpec(timeout=timedelta(minutes=1), label=label)


def test_task_type_spec_refuses_timeout_that_is_not_a_timedelta() -> None:
    from app.domain.tasks import TaskTypeSpec

    with pytest.raises((TypeError, ValueError)):
        TaskTypeSpec(timeout=300, label="Practice task")  # type: ignore[arg-type]


# --- cancel flag ---------------------------------------------------------------


def test_cancel_flag_starts_clear_and_stays_set() -> None:
    from app.domain.tasks import CancelFlag

    flag = CancelFlag()
    assert flag.cancelled is False
    flag.cancel()
    assert flag.cancelled is True
    flag.cancel()  # idempotent
    assert flag.cancelled is True
    # Each task gets its own flag.
    assert CancelFlag().cancelled is False


def test_cancel_flag_is_read_only_from_outside() -> None:
    from app.domain.tasks import CancelFlag

    flag = CancelFlag()
    # Only cancel() sets it, and nothing clears it.
    with pytest.raises(AttributeError):
        flag.cancelled = False  # type: ignore[misc]


CONFORMANCE = """\
from app.domain.tasks import CancelFlag
from app.modules.ai import CancelToken

flag = CancelFlag()
token: CancelToken = flag
seen: bool = token.cancelled
flag.cancel()
"""


def test_cancel_flag_satisfies_cancel_token_protocol(tmp_path: Path) -> None:
    sample = tmp_path / "conformance.py"
    sample.write_text(CONFORMANCE, encoding="utf-8")
    # Fixed argv to the venv's own interpreter; no shell, no untrusted input.
    result = subprocess.run(  # noqa: S603
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
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("label", [None, 42, b"Practice"])
def test_task_type_spec_refuses_label_that_is_not_a_str(label: object) -> None:
    from app.domain.tasks import TaskTypeSpec

    with pytest.raises((TypeError, ValueError)):
        TaskTypeSpec(timeout=timedelta(minutes=1), label=label)  # type: ignore[arg-type]
