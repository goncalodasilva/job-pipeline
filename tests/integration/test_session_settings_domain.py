"""M7.2 sessions settings use existing versioned persistence, without a schema change."""

import pytest

from app.domain.errors import VersionConflict
from app.domain.settings import SESSIONS, SessionSettings
from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from app.services.settings import SettingsService
from tests.fakes.clock import FrozenClock
from tests.integration.test_schema_0004 import START

pytestmark = pytest.mark.integration


@pytest.fixture
def db(tmp_path):
    with Database.open(tmp_path / "fictional.db") as database:
        apply_migrations(database, MIGRATIONS_DIR, tmp_path / "backups", FrozenClock(START))
        yield database


def test_session_settings_saved_with_optimistic_version(db):
    service = SettingsService(db, FrozenClock(START))
    defaults = service.load(SESSIONS)
    assert defaults.value == SessionSettings() and defaults.version == 0
    chosen = SessionSettings(frequency="twice_weekly", length=5, paused=True)
    stored = service.save(SESSIONS, chosen, expected_version=0)
    assert stored.version == 1 and service.load(SESSIONS) == stored
    with pytest.raises(VersionConflict):
        service.save(SESSIONS, SessionSettings(), expected_version=0)
    changed = service.save(SESSIONS, SessionSettings(), expected_version=1)
    assert changed.version == 2
    with pytest.raises(VersionConflict):
        service.save(SESSIONS, chosen, expected_version=1)
