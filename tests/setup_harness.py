"""Put temporary data and config folders into a known setup state (M1.9; AC-M1.2).

While setup is incomplete every page redirects to the guide, so tests of the rest of the app
start from a completed setup: ``complete_setup`` writes ``config.toml`` naming the data
folder, creates and migrates ``app.db`` in it, saves an AI provider setting and marks every
registered guide step done. ``seed_workspace`` creates the database with only some steps done
(or skipped), for resume tests; it can also save job preferences and use another registry (a
test-registered optional step, M1.19).

The saved provider defaults to the fake (``HARNESS_PROVIDER``), so provider health is the same
on every operating system: it is healthy when the app allows fakes
(``JOBPIPELINE_ALLOW_FAKE_PROVIDERS=1``, set by ``tests/running_app.py``) and reported as a
broken provider (a banner, never the wizard) otherwise (M1.15; ADR-054).

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
from app.domain.settings import AI_PROVIDER, JOB_PREFERENCES, AIProviderSettings, JobPreferences
from app.main import open_workspace_folder
from app.modules.storage import MIGRATIONS_DIR
from app.onboarding.progress import SetupProgress
from app.onboarding.registry import StepRegistry, m1_registry
from app.services.settings import SettingsService
from app.services.setup_state import SetupStateService
from tests.fakes.clock import FrozenClock

HARNESS_START = datetime(2026, 1, 3, 8, 0, 0, tzinfo=UTC)
HARNESS_PROVIDER = AIProviderSettings(kind="fake")


def seed_workspace(
    data_dir: Path,
    *,
    done: Iterable[str] = (),
    clock: Clock | None = None,
    provider: AIProviderSettings | None = None,
    preferences: JobPreferences | None = None,
    skipped: Iterable[str] = (),
    registry: StepRegistry | None = None,
) -> None:
    """Create and migrate ``data_dir/app.db``, save ``provider`` and ``preferences`` (when
    given) as settings, mark the given steps done and skip the ``skipped`` (optional) steps of
    ``registry`` (default ``m1_registry()``); then close it."""
    used_clock = clock if clock is not None else FrozenClock(HARNESS_START)
    workspace = open_workspace_folder(data_dir, migrations_dir=MIGRATIONS_DIR, clock=used_clock)
    try:
        settings = SettingsService(workspace.db, used_clock)
        if provider is not None:
            version = settings.load(AI_PROVIDER).version
            settings.save(AI_PROVIDER, provider, expected_version=version)
        if preferences is not None:
            saved = settings.load(JOB_PREFERENCES).version
            settings.save(JOB_PREFERENCES, preferences, expected_version=saved)
        used_registry = registry if registry is not None else m1_registry()
        progress = SetupProgress(used_registry, SetupStateService(workspace.db, used_clock))
        for step_id in done:
            progress.mark_done(step_id)
        for step_id in skipped:
            progress.skip(step_id)
    finally:
        workspace.db.close()


def complete_setup(
    data_dir: Path,
    config_dir: Path,
    *,
    clock: Clock | None = None,
    provider: AIProviderSettings = HARNESS_PROVIDER,
) -> None:
    """A finished setup guide: bootstrap config, migrated database, a saved AI provider
    setting (the fake by default) and every step done."""
    write_bootstrap(config_dir, data_dir=data_dir, version=__version__)
    seed_workspace(
        data_dir, done=[step.id for step in m1_registry().steps], clock=clock, provider=provider
    )
