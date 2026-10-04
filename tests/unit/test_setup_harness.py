"""The completed-setup test harness (M1.9; ``tests/setup_harness.py``).

Tests of the app's pages start from a finished setup guide; this checks the harness really
produces one: a bootstrap config naming the data folder, a migrated database, and every
registered step stored as done. Folders live under ``tmp_path``; every value is fictional.
"""

from datetime import UTC, datetime
from pathlib import Path

from app.config import DATABASE_FILE, load_bootstrap
from app.modules.storage import Database
from app.onboarding.registry import m1_registry
from app.services.setup_state import SetupStateService
from tests.fakes.clock import FrozenClock
from tests.setup_harness import complete_setup, seed_workspace

START = datetime(2026, 2, 7, 10, 30, 0, tzinfo=UTC)


def test_complete_setup_creates_bootstrap_database_and_done_steps(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "cfg"

    complete_setup(data_dir, config_dir, clock=FrozenClock(START))

    bootstrap = load_bootstrap(config_dir)
    assert bootstrap is not None
    assert bootstrap.data_dir == data_dir
    assert (data_dir / DATABASE_FILE).is_file()
    db = Database.open(data_dir / DATABASE_FILE)
    try:
        state = SetupStateService(db, FrozenClock(START))
        assert state.statuses() == {step.id: "done" for step in m1_registry().steps}
        assert set(state.statuses()) >= {"welcome", "provider", "preferences", "done"}
        assert state.completed_at("welcome") == START
    finally:
        db.close()
    assert not (data_dir / "logs").exists(), "the harness attaches no logging"


def test_seed_workspace_marks_only_given_steps(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"

    seed_workspace(data_dir, done=["welcome"], clock=FrozenClock(START))

    db = Database.open(data_dir / DATABASE_FILE)
    try:
        assert SetupStateService(db, FrozenClock(START)).statuses() == {"welcome": "done"}
    finally:
        db.close()
    assert not (tmp_path / "cfg").exists(), "seeding writes no bootstrap config"
