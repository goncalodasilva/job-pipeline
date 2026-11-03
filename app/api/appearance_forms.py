"""What the Settings > Appearance template is given (M9.2 part B; ADR-131 item 6; ADR-125 item 5).

Kept apart from the route (``app/api/settings_appearance.py``) so the area page
(``app/api/settings_areas.py``) can show the section without importing it, as for Sessions. A
read: it writes nothing, and with nothing stored it shows the defaults at version 0.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final

from fastapi import Request

from app.domain.errors import VersionConflict
from app.services.appearance import DENSITIES, THEMES, AppearancePreferences

SAVE_PATH: Final = "/settings/appearance"  # also the area page: GET shows, POST saves
AREA_PATH: Final = SAVE_PATH
SAVED: Final = "Appearance saved."
NOT_SAVED: Final = "Appearance not saved."
CHANGED_EVENT: Final = "appearance:changed"  # app.js applies it to <html> without a reload
THEME_LABELS: Final = {"system": "System", "light": "Light", "dark": "Dark"}
DENSITY_LABELS: Final = {"comfortable": "Comfortable", "compact": "Compact"}
FIELD_ORDER: Final = ("theme", "density")
_NOTHING: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True)
class AppearanceState:
    """What just happened in the section; the default is a plain page load."""

    errors: Mapping[str, str] = field(default_factory=lambda: _NOTHING)  # message per field
    typed: Mapping[str, str] = field(default_factory=lambda: _NOTHING)  # accepted values sent
    saved: bool = False  # done: the line shows and the heading takes focus
    conflict: bool = False  # a stale version: the reload prompt shows and takes focus


def appearance_context(request: Request, state: AppearanceState | None) -> dict[str, Any]:
    """The ``appearance_form`` template value. Blocking (it reads the database)."""
    state = state or AppearanceState()
    preferences: AppearancePreferences = request.app.state.appearance
    stored = preferences.load()
    shown: dict[str, str] = {"theme": stored.value.theme, "density": stored.value.density}
    if state.errors:  # the accepted fields keep what was sent; a refused one is never shown back
        shown.update({k: v for k, v in state.typed.items() if k not in state.errors})
    return {
        "version": stored.version,
        "shown": shown,
        "themes": [(value, THEME_LABELS[value]) for value in THEMES],
        "densities": [(value, DENSITY_LABELS[value]) for value in DENSITIES],
        "save_url": SAVE_PATH,
        "errors": dict(state.errors),
        "first_invalid": next((n for n in FIELD_ORDER if n in state.errors), ""),
        "saved": SAVED if state.saved else "",
        "conflict": state.conflict,
        "conflict_message": VersionConflict.message,
        # Something in this answer takes focus, so the page heading does not.
        "attention": bool(state.errors or state.saved or state.conflict),
    }
