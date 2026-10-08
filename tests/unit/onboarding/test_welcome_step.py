"""The welcome step handler's failure paths (M1.16; ADR-049 item 4).

The integration tests in tests/integration/test_setup_welcome.py drive the real app; these
inject failing collaborators to show every failure maps to plain copy, logs only the error type,
and leaves the step unfinished so the user can retry. Folders live under ``tmp_path``.
"""

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from app.onboarding.progress import SetupProgress
from app.onboarding.registry import m1_registry
from app.onboarding.startup import OnboardingState, StartupInputs
from app.onboarding.steps import NOT_AVAILABLE, PendingStep
from app.onboarding.steps.welcome import (
    MSG_ALREADY_WORKSPACE,
    MSG_BOOTSTRAP_FAILED,
    MSG_OPEN_FAILED,
    MSG_RESERVED,
    WelcomeSettings,
    WelcomeStep,
)
from app.services.setup_state import SetupStateService
from tests.fakes.clock import FrozenClock

START = datetime(2026, 1, 3, 9, 0, 0, tzinfo=UTC)


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        refused: tuple[Path, ...] = (),
        folder_fixed: bool = False,
        bootstrap: Path | None = None,
    ) -> None:
        self.registry = m1_registry()
        self.onboarding = OnboardingState(
            self.registry,
            StartupInputs(
                bootstrap_exists=False, data_dir_explicit=False, demo=False, force_setup=False
            ),
        )
        self.clock = FrozenClock(START)
        self.db: Database | None = None
        self.folder: Path | None = None
        self.bootstrap: Path | None = bootstrap
        self.open_error: BaseException | None = None
        self.write_error: BaseException | None = None
        self.step = WelcomeStep(
            WelcomeSettings(
                suggested_folder=tmp_path / "data",
                folder_fixed=folder_fixed,
                demo=False,
                refused_folders=refused,
                config_folder=tmp_path / "cfg",
            ),
            onboarding=self.onboarding,
            workspace_folder=lambda: self.folder,
            open_workspace=self.open_workspace,
            bootstrap_exists=lambda: self.bootstrap is not None,
            write_bootstrap=self.write_bootstrap,
            progress=self.progress,
        )

    def open_workspace(self, folder: Path) -> None:
        if self.open_error is not None:
            raise self.open_error
        folder.mkdir(parents=True, exist_ok=True)
        self.db = Database.open(folder / "app.db")
        apply_migrations(self.db, MIGRATIONS_DIR, folder / "backups", self.clock)
        self.folder = folder

    def write_bootstrap(self, folder: Path) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.bootstrap = folder

    def progress(self) -> SetupProgress | None:
        if self.db is None:
            return None
        return SetupProgress(self.registry, SetupStateService(self.db, self.clock))

    def close(self) -> None:
        if self.db is not None:
            self.db.close()


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    made = Harness(tmp_path)
    yield made
    made.close()


def test_unexpected_open_failure_is_plain_and_logs_only_the_type(
    harness: Harness, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    harness.open_error = RuntimeError(f"fictional failure at {tmp_path / 'chosen'}")
    with caplog.at_level(logging.DEBUG):
        result = harness.step.submit({"folder": [str(tmp_path / "chosen")]})

    assert not result.ok
    assert result.errors == {"folder": MSG_OPEN_FAILED}
    assert harness.bootstrap is None
    messages = [record.getMessage() for record in caplog.records]
    assert any("RuntimeError" in message for message in messages)
    assert all(str(tmp_path) not in message for message in messages)


def test_interrupts_are_not_swallowed(harness: Harness, tmp_path: Path) -> None:
    harness.open_error = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        harness.step.submit({"folder": [str(tmp_path / "chosen")]})


def test_bootstrap_failure_keeps_step_unfinished_and_retry_writes_it(
    harness: Harness, tmp_path: Path
) -> None:
    chosen = tmp_path / "chosen"
    harness.write_error = OSError("fictional disk full")

    failed = harness.step.submit({"folder": [str(chosen)]})

    assert failed.errors == {"form": MSG_BOOTSTRAP_FAILED}
    assert harness.folder == chosen, "the workspace stays open"
    progress = harness.progress()
    assert progress is not None and progress.statuses() == {}
    assert harness.step.context()["folder_fixed"] is True

    harness.write_error = None
    retried = harness.step.submit({"folder": [str(tmp_path / "ignored")]})

    assert retried.ok
    assert harness.bootstrap == chosen
    assert progress.statuses() == {"welcome": "done"}
    assert harness.onboarding.decision.step == "provider"
    assert not (tmp_path / "ignored").exists()


def test_inspection_failure_is_plain(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(folder: Path, *, ignored: frozenset[str] = frozenset()) -> str:
        raise PermissionError("fictional")

    monkeypatch.setattr("app.onboarding.steps.welcome.inspect_data_folder", refuse)

    result = harness.step.submit({"folder": [str(tmp_path / "chosen")]})

    assert result.errors == {"folder": MSG_OPEN_FAILED}
    assert harness.folder is None


def test_folder_inside_a_refused_folder_is_refused(tmp_path: Path) -> None:
    reserved = tmp_path / "demo-data"
    made = Harness(tmp_path, refused=(reserved,))
    try:
        for folder in (reserved, reserved / "inside"):
            result = made.step.submit({"folder": [str(folder)]})
            assert result.errors == {"folder": MSG_RESERVED}, folder
        assert not reserved.exists()
    finally:
        made.close()


def test_pending_step_saves_nothing() -> None:
    step = PendingStep()
    assert step.context() == {}
    result = step.submit({"anything": ["fictional"]})
    assert not result.ok
    assert result.errors == {"form": NOT_AVAILABLE}


def test_locate_retry_after_failed_save_rewrites_existing_bootstrap(tmp_path: Path) -> None:
    moved, found = tmp_path / "moved", tmp_path / "found"
    made = Harness(tmp_path, bootstrap=moved)  # config.toml names a folder that has gone
    try:
        made.write_error = OSError("fictional read-only folder")
        failed = made.step.submit({"folder": [str(found)], "action": ["fresh"]})
        assert failed.errors == {"form": MSG_BOOTSTRAP_FAILED}

        made.write_error = None
        retried = made.step.submit({"folder": [str(tmp_path / "ignored")]})

        assert retried.ok
        assert made.bootstrap == found, "config.toml names the folder step 1 opened"
    finally:
        made.close()


def test_open_folder_from_elsewhere_never_replaces_bootstrap(tmp_path: Path) -> None:
    made = Harness(tmp_path, bootstrap=tmp_path / "configured")
    try:
        made.open_workspace(tmp_path / "explicit")  # as start-up does for --data-dir
        made.write_error = AssertionError("config.toml must not be written")
        result = made.step.submit({})
        assert result.ok
        assert made.bootstrap == tmp_path / "configured"
    finally:
        made.close()


def test_fixed_folder_without_workspace_ignores_the_form(tmp_path: Path) -> None:
    made = Harness(tmp_path, folder_fixed=True)
    try:
        result = made.step.submit({"folder": [str(tmp_path / "from-form")]})
        assert not result.ok
        assert result.errors == {"form": MSG_OPEN_FAILED}
        assert not (tmp_path / "from-form").exists()
    finally:
        made.close()


def test_symlink_loop_is_a_plain_field_error(tmp_path: Path) -> None:
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    made = Harness(tmp_path, refused=(tmp_path / "reserved",))
    try:
        result = made.step.submit({"folder": [str(loop / "inside")]})
        assert not result.ok
        assert set(result.errors) == {"folder"}
        assert made.folder is None
    finally:
        made.close()


def test_start_fresh_on_existing_workspace_points_to_locate(
    harness: Harness, tmp_path: Path
) -> None:
    existing = tmp_path / "existing"
    other = Harness(tmp_path)
    other.open_workspace(existing)
    other.close()

    result = harness.step.submit({"folder": [str(existing)], "action": ["fresh"]})

    assert result.errors == {"folder": MSG_ALREADY_WORKSPACE}
    assert harness.folder is None


def test_config_toml_does_not_count_when_config_folder_is_the_data_folder(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "config.toml").write_text('data_dir = "/fictional"\n', encoding="utf-8")
    made = Harness(tmp_path)
    made.step = WelcomeStep(
        WelcomeSettings(
            suggested_folder=shared,
            folder_fixed=False,
            demo=False,
            refused_folders=(),
            config_folder=shared,
        ),
        onboarding=made.onboarding,
        workspace_folder=lambda: made.folder,
        open_workspace=made.open_workspace,
        bootstrap_exists=lambda: made.bootstrap is not None,
        write_bootstrap=made.write_bootstrap,
        progress=made.progress,
    )
    try:
        result = made.step.submit({"folder": [str(shared)], "action": ["fresh"]})
        assert result.ok, result.errors
        assert made.bootstrap == shared
    finally:
        made.close()
