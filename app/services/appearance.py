"""Settings > Appearance: the theme and the density of the pages, as one versioned ``appearance``
setting (M9.2 part B; ADR-131 item 6; ADR-042 follow-up; TD-085 density only; ADR-125 item 5).

Values arrive as the form's text. The version is checked first: a stale version raises
``VersionConflict`` whatever the values are, so a refused value never re-renders a form that
carries a version newer than the one the user saw. A refused value is then named by its field
with fixed copy and nothing is stored; the refused text never reaches a result or a log.

``current`` is what every page puts on ``<html>``: the stored choice, or the defaults when
nothing is stored, the stored row is not valid, no workspace is open or the database can't be
read. It never raises and writes nothing.
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from app.domain.clock import Clock
from app.domain.errors import VersionConflict
from app.domain.settings import APPEARANCE, AppearanceSettings
from app.modules.storage import Database
from app.services.settings import SettingsService, Versioned

THEMES: Final = ("system", "light", "dark")
DENSITIES: Final = ("comfortable", "compact")
CHOICES: Final[Mapping[str, tuple[str, ...]]] = {"theme": THEMES, "density": DENSITIES}
FIELD_ERRORS: Final[Mapping[str, str]] = {
    "theme": "Choose System, Light or Dark.",
    "density": "Choose Comfortable or Compact.",
}

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AppearanceResult:
    saved: Versioned[AppearanceSettings] | None = None
    errors: Mapping[str, str] | None = None  # message per refused field, in form order


class AppearancePreferences:
    def __init__(self, database: Callable[[], Database | None], clock: Clock) -> None:
        self._database = database
        self._clock = clock

    def _settings(self) -> SettingsService | None:
        db = self._database()
        return SettingsService(db, self._clock) if db is not None else None

    def load(self) -> Versioned[AppearanceSettings]:
        """The stored value, or the defaults at version 0 (also without a workspace). Writes
        nothing."""
        settings = self._settings()
        if settings is None:
            return Versioned(AppearanceSettings(), 0)
        return settings.load(APPEARANCE)

    def current(self) -> AppearanceSettings:
        """The choice every page shows; the defaults when it can't be read."""
        try:
            return self.load().value
        except Exception as error:  # a page must render whatever the database says
            _log.warning("The appearance setting couldn't be read (%s).", type(error).__name__)
            return AppearanceSettings()

    def save(self, values: Mapping[str, str], *, expected_version: int) -> AppearanceResult:
        settings = self._settings()
        if settings is None or settings.load(APPEARANCE).version != expected_version:
            raise VersionConflict(detail=f"setting {APPEARANCE.key} changed elsewhere")
        errors = {
            name: message
            for name, message in FIELD_ERRORS.items()
            if values.get(name) not in CHOICES[name]
        }
        if errors:
            return AppearanceResult(errors=errors)
        value = AppearanceSettings.model_validate(
            {"theme": values["theme"], "density": values["density"]}
        )
        return AppearanceResult(
            saved=settings.save(APPEARANCE, value, expected_version=expected_version)
        )
