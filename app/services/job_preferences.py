"""Settings > Job preferences: a read of the stored preferences (M9.2 part A; ADR-131 item 7).

The preferences are written by the setup guide's preferences step; this only reads them, so the
Settings area can show them. A read: it writes nothing, and with nothing stored (or no open
workspace) it returns None.
"""

from collections.abc import Callable

from app.domain.clock import Clock
from app.domain.settings import JOB_PREFERENCES, JobPreferences
from app.modules.storage import Database
from app.services.settings import SettingsService


class JobPreferencesView:
    def __init__(self, database: Callable[[], Database | None], clock: Clock) -> None:
        self._database = database
        self._clock = clock

    def load(self) -> JobPreferences | None:
        """The stored preferences, or None. Blocking (reads the database)."""
        db = self._database()
        if db is None:
            return None
        return SettingsService(db, self._clock).load(JOB_PREFERENCES).value
