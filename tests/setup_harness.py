"""Put temporary data and config folders into a known setup state (M1.9; AC-M1.2).

While setup is incomplete every page redirects to the guide, so tests of the rest of the app
start from a completed setup: ``complete_setup`` writes ``config.toml`` naming the data
folder, creates and migrates ``app.db`` in it, and marks every registered guide step done.
``seed_workspace`` creates the database with only some steps done, for resume tests.

Only public APIs that already exist are used (``write_bootstrap``, ``open_workspace_folder``,
``SetupProgress``), and nothing is attached to logging. Always pass folders under the test's
``tmp_path``; every value written is fictional.
"""

from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from app import __version__
from app.config import write_bootstrap
from app.domain.clock import Clock
from app.main import open_workspace_folder
from app.modules.storage import MIGRATIONS_DIR
from app.onboarding.progress import SetupProgress
from app.onboarding.registry import m1_registry
from app.services.setup_state import SetupStateService
from tests.fakes.clock import FrozenClock

HARNESS_START = datetime(2026, 1, 3, 8, 0, 0, tzinfo=UTC)


def seed_workspace(data_dir: Path, *, done: Iterable[str] = (), clock: Clock | None = None) -> None:
    """Create and migrate ``data_dir/app.db`` and mark the given steps done; then close it."""
    used_clock = clock if clock is not None else FrozenClock(HARNESS_START)
    workspace = open_workspace_folder(data_dir, migrations_dir=MIGRATIONS_DIR, clock=used_clock)
    try:
        progress = SetupProgress(m1_registry(), SetupStateService(workspace.db, used_clock))
        for step_id in done:
            progress.mark_done(step_id)
    finally:
        workspace.db.close()


def complete_setup(data_dir: Path, config_dir: Path, *, clock: Clock | None = None) -> None:
    """A finished setup guide: bootstrap config, migrated database, every step done."""
    write_bootstrap(config_dir, data_dir=data_dir, version=__version__)
    seed_workspace(data_dir, done=[step.id for step in m1_registry().steps], clock=clock)
