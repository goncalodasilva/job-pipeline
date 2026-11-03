"""The welcome step's two restore entry points (M9.1 part D2; AC-M9.3; ADR-050, ADR-131).

``open_for_restore`` opens the folder setup's "Restore from an export" goes into, the way "Start
fresh" does; ``finish_restore`` names the folder in ``config.toml`` after the restore, under the
rule for finishing the step, and never marks the step done. Collaborators are the recording
doubles of ``test_welcome_step.Harness``. Every value is fictional.
"""

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.domain.errors import ConfigError
from app.onboarding.steps.welcome import (
    MSG_EMPTY,
    MSG_OPEN_FAILED,
    MSG_RELATIVE,
)
from tests.unit.onboarding.test_welcome_step import Harness


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    made = Harness(tmp_path)
    yield made
    made.close()


def done_steps(harness: Harness) -> set[str]:
    progress = harness.progress()
    assert progress is not None
    return {step for step, status in progress.statuses().items() if status == "done"}


# --- open_for_restore -------------------------------------------------------------------------


def test_open_for_restore_opens_a_missing_folder_without_marking_the_step_done(
    harness: Harness, tmp_path: Path
) -> None:
    chosen = tmp_path / "chosen"

    result = harness.step.open_for_restore(str(chosen))

    assert result.ok and result.errors == {}
    assert harness.opened == [chosen] and harness.folder == chosen
    assert harness.writes == [], "config.toml is written after the restore, not before"
    assert done_steps(harness) == set()


def test_open_for_restore_uses_the_folder_messages_of_start_fresh(
    harness: Harness, tmp_path: Path
) -> None:
    from app.onboarding.steps.welcome import MSG_ALREADY_WORKSPACE_RESTORE, MSG_HAS_DATA_RESTORE

    foreign = tmp_path / "holiday-photos"
    foreign.mkdir()
    (foreign / "notes.txt").write_text("fictional notes\n", encoding="utf-8")
    seeded = Harness(tmp_path / "seeded")
    try:
        seeded.open_workspace(tmp_path / "seeded" / "workspace")
    finally:
        seeded.close()

    assert harness.step.open_for_restore("").errors == {"folder": MSG_EMPTY}
    assert harness.step.open_for_restore(None).errors == {"folder": MSG_EMPTY}
    assert harness.step.open_for_restore("relative/data").errors == {"folder": MSG_RELATIVE}
    assert harness.step.open_for_restore(str(foreign)).errors == {"folder": MSG_HAS_DATA_RESTORE}
    assert harness.step.open_for_restore(str(tmp_path / "seeded" / "workspace")).errors == {
        "folder": MSG_ALREADY_WORKSPACE_RESTORE
    }
    assert harness.opened == [] and harness.folder is None


def test_restore_words_say_restore_not_start_fresh(harness: Harness) -> None:
    from app.onboarding.steps import welcome

    assert "restoring" in welcome.MSG_HAS_DATA_RESTORE
    assert "starting fresh" not in welcome.MSG_HAS_DATA_RESTORE
    assert "Locate the folder" not in welcome.MSG_ALREADY_WORKSPACE_RESTORE
    assert "start fresh" not in welcome.MSG_ALREADY_WORKSPACE_RESTORE


def test_open_for_restore_uses_an_open_workspace_as_it_is(harness: Harness, tmp_path: Path) -> None:
    harness.open_workspace(tmp_path / "already")
    harness.opened.clear()

    result = harness.step.open_for_restore(str(tmp_path / "elsewhere"))

    assert result.ok
    assert harness.opened == [] and harness.folder == tmp_path / "already"
    assert not (tmp_path / "elsewhere").exists()


def test_open_for_restore_with_a_fixed_folder_and_no_workspace_opens_nothing(
    tmp_path: Path,
) -> None:
    fixed = Harness(tmp_path, folder_fixed=True)
    try:
        result = fixed.step.open_for_restore(str(tmp_path / "chosen"))

        assert not result.ok and result.errors == {"form": MSG_OPEN_FAILED}
        assert fixed.opened == [] and not (tmp_path / "chosen").exists()
    finally:
        fixed.close()


def test_open_for_restore_failure_is_plain_and_opens_nothing_else(
    harness: Harness, tmp_path: Path
) -> None:
    harness.open_error = RuntimeError(f"fictional failure at {tmp_path}")

    result = harness.step.open_for_restore(str(tmp_path / "chosen"))

    assert result.errors == {"folder": MSG_OPEN_FAILED}
    assert harness.folder is None and harness.writes == []


def test_a_retry_after_a_refused_attempt_uses_the_opened_folder_and_may_overwrite_the_config(
    tmp_path: Path,
) -> None:
    """A refused file leaves the folder open. The next attempt reuses it (nothing opens again) and
    the config.toml it then writes replaces an older one: this step chose the folder (locate)."""
    older = tmp_path / "older-location"
    made = Harness(tmp_path, bootstrap=older)
    chosen = tmp_path / "chosen"
    try:
        assert made.step.open_for_restore(str(chosen)).ok  # attempt 1: the file is refused later
        assert made.step.open_for_restore(str(tmp_path / "other")).ok  # attempt 2: same folder
        assert made.opened == [chosen]

        assert made.step.finish_restore() is True

        assert made.writes == [chosen] and made.bootstrap == chosen, "overwritten by design"
    finally:
        made.close()


# --- finish_restore ---------------------------------------------------------------------------


def test_finish_restore_writes_the_config_when_none_exists_and_never_marks_done(
    harness: Harness, tmp_path: Path
) -> None:
    assert harness.step.open_for_restore(str(tmp_path / "chosen")).ok

    assert harness.step.finish_restore() is True

    assert harness.writes == [tmp_path / "chosen"]
    assert done_steps(harness) == set(), "the restored setup state decides, not this step"


def test_finish_restore_never_replaces_a_config_for_a_folder_it_did_not_open(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "existing-config-folder"
    made = Harness(tmp_path, folder_fixed=True, bootstrap=existing)
    try:
        made.open_workspace(tmp_path / "explicit")  # --data-dir: opened at start-up
        assert made.step.open_for_restore(None).ok

        assert made.step.finish_restore() is True

        assert made.writes == [] and made.bootstrap == existing
    finally:
        made.close()


def test_finish_restore_writes_for_an_explicit_folder_when_no_config_exists(
    tmp_path: Path,
) -> None:
    made = Harness(tmp_path, folder_fixed=True)
    try:
        made.open_workspace(tmp_path / "explicit")

        assert made.step.finish_restore() is True

        assert made.writes == [tmp_path / "explicit"]
    finally:
        made.close()


def test_finish_restore_never_writes_in_demo_mode(tmp_path: Path) -> None:
    demo = Harness(tmp_path, folder_fixed=True, demo=True)
    try:
        demo.open_workspace(tmp_path / "demo-data")
        assert demo.step.open_for_restore("ignored").ok

        assert demo.step.finish_restore() is True

        assert demo.writes == [] and demo.bootstrap is None
    finally:
        demo.close()
    # Even a folder this step opened is not named in demo mode.
    opened = Harness(tmp_path / "second", demo=True)
    try:
        assert opened.step.open_for_restore(str(tmp_path / "second" / "chosen")).ok
        assert opened.step.finish_restore() is True
        assert opened.writes == []
    finally:
        opened.close()


def test_finish_restore_without_a_workspace_does_nothing(harness: Harness) -> None:
    assert harness.step.finish_restore() is False
    assert harness.writes == []


@pytest.mark.parametrize(
    "failure", [OSError("disk"), ConfigError(detail="fictional config problem")]
)
def test_finish_restore_reports_a_config_write_failure(
    harness: Harness,
    tmp_path: Path,
    failure: BaseException,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert harness.step.open_for_restore(str(tmp_path / "chosen")).ok
    harness.write_error = failure

    with caplog.at_level(logging.DEBUG):
        assert harness.step.finish_restore() is False

    assert harness.bootstrap is None
    assert done_steps(harness) == set()
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert type(failure).__name__ in messages
    assert str(tmp_path) not in messages


def test_finish_restore_reports_a_refresh_failure_and_logs_only_the_type(
    harness: Harness, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    assert harness.step.open_for_restore(str(tmp_path / "chosen")).ok

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError(f"fictional failure at {tmp_path}")

    harness.onboarding.refresh = broken  # type: ignore[method-assign]
    with caplog.at_level(logging.DEBUG):
        assert harness.step.finish_restore() is False

    assert harness.writes == [tmp_path / "chosen"], "the config was written before the refresh"
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "RuntimeError" in messages and str(tmp_path) not in messages


# --- what the step shows after a restore attempt opened its folder ----------------------------


def test_context_tells_the_template_a_restore_attempt_opened_the_folder(
    harness: Harness, tmp_path: Path
) -> None:
    assert harness.step.context()["opened_for_restore"] is False
    assert harness.step.open_for_restore(str(tmp_path / "chosen")).ok

    shown = harness.step.context()

    assert shown["folder_fixed"] is True and shown["opened_for_restore"] is True
    assert shown["explicit_folder"] is False


def test_context_after_continuing_is_no_longer_a_restore_attempt(
    harness: Harness, tmp_path: Path
) -> None:
    assert harness.step.open_for_restore(str(tmp_path / "chosen")).ok

    assert harness.step.submit({"folder": [str(tmp_path / "chosen")]}).ok

    assert harness.step.context()["opened_for_restore"] is False


def test_context_for_a_folder_that_was_never_opened_by_a_restore(
    tmp_path: Path,
) -> None:
    made = Harness(tmp_path, folder_fixed=True)
    try:
        made.open_workspace(tmp_path / "explicit")

        assert made.step.context()["opened_for_restore"] is False
    finally:
        made.close()
