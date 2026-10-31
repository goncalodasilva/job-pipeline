"""Settings > Sessions: frequency, default length, the "Ask me later" default and the pause
switch, as one versioned ``sessions`` setting (M7.8; AC-M7.6; ADR-117 item 8, ADR-119).

Values arrive as the form's text. The version is checked first: a stale version raises
``VersionConflict`` whatever the values are, so a refused value never re-renders a form that
carries a version newer than the one the user saw. A refused value is then named by its field
with fixed copy and nothing is stored.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from app.domain.clock import Clock
from app.domain.errors import SessionNotFound, VersionConflict
from app.domain.settings import SESSIONS, SessionSettings
from app.modules.storage import Database
from app.services.settings import SettingsService, Versioned

FREQUENCIES: Final = ("never", "weekly", "twice_weekly", "after_application")
LENGTHS: Final = ("3", "5")
ASK_LATER: Final = ("next_session", "week", "month")
CHOICES: Final[Mapping[str, tuple[str, ...]]] = {
    "frequency": FREQUENCIES,
    "length": LENGTHS,
    "ask_later_default": ASK_LATER,
}
FIELD_ERRORS: Final[Mapping[str, str]] = {
    "frequency": "Choose never, weekly, twice a week or after each application.",
    "length": "Choose 3 or 5 questions.",
    "ask_later_default": "Choose next session, in a week or in a month.",
}


@dataclass(frozen=True)
class SessionSettingsResult:
    saved: Versioned[SessionSettings] | None = None
    errors: Mapping[str, str] | None = None  # message per refused field, in form order


class SessionPreferences:
    def __init__(self, database: Callable[[], Database | None], clock: Clock) -> None:
        self._database = database
        self._clock = clock

    def _settings(self) -> SettingsService:
        db = self._database()
        if db is None:
            raise SessionNotFound()
        return SettingsService(db, self._clock)

    def load(self) -> Versioned[SessionSettings]:
        """The stored value, or the defaults at version 0. Writes nothing."""
        return self._settings().load(SESSIONS)

    def save(
        self, values: Mapping[str, str], *, paused: bool, expected_version: int
    ) -> SessionSettingsResult:
        if self.load().version != expected_version:
            raise VersionConflict(detail=f"setting {SESSIONS.key} changed elsewhere")
        errors = {
            name: message
            for name, message in FIELD_ERRORS.items()
            if values.get(name) not in CHOICES[name]
        }
        if errors:
            return SessionSettingsResult(errors=errors)
        value = SessionSettings.model_validate(
            {
                "frequency": values["frequency"],
                "length": int(values["length"]),
                "ask_later_default": values["ask_later_default"],
                "paused": paused,
            }
        )
        return SessionSettingsResult(
            saved=self._settings().save(SESSIONS, value, expected_version=expected_version)
        )
