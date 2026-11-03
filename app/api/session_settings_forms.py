"""What the Settings > Sessions template is given (M7.8; AC-M7.6; ADR-119).

Kept apart from the route (``app/api/settings_sessions.py``) so the Settings page
(``app/api/settings.py``) can show the section without importing it, as for Board. A read: it
writes nothing, and with nothing stored it shows the defaults at version 0.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from fastapi import Request

from app.domain.errors import VersionConflict
from app.services.session_preferences import ASK_LATER, FREQUENCIES, LENGTHS, SessionPreferences

SAVE_PATH = "/settings/sessions"  # also the area page (M9.2): GET shows, POST saves
AREA_PATH = SAVE_PATH
SAVED = "Session settings saved."
NOT_SAVED = "Session settings not saved."
FREQUENCY_LABELS = {
    "never": "Never",
    "weekly": "Weekly",
    "twice_weekly": "Twice a week",
    "after_application": "After each application",
}
LENGTH_LABELS = {"3": "3 questions", "5": "5 questions"}
ASK_LATER_LABELS = {"next_session": "Next session", "week": "In a week", "month": "In a month"}
FIELD_ORDER = ("frequency", "length", "ask_later_default")
_NOTHING: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True)
class SessionSettingsState:
    """What just happened in the section; the default is a plain page load."""

    errors: Mapping[str, str] = field(default_factory=lambda: _NOTHING)  # message per field
    typed: Mapping[str, str] = field(default_factory=lambda: _NOTHING)  # accepted values sent
    paused: bool = False  # the switch as sent, kept when a field is refused
    saved: bool = False  # done: the line shows and the heading takes focus
    conflict: bool = False  # a stale version: the reload prompt shows and takes focus


def sessions_context(request: Request, state: SessionSettingsState | None) -> dict[str, Any]:
    """The ``sessions`` template value. Blocking (it reads the database)."""
    state = state or SessionSettingsState()
    preferences: SessionPreferences = request.app.state.session_preferences
    stored = preferences.load()
    shown: dict[str, Any] = {
        "frequency": stored.value.frequency,
        "length": str(stored.value.length),
        "ask_later_default": stored.value.ask_later_default,
        "paused": stored.value.paused,
    }
    if state.errors:  # the accepted fields keep what was sent; a refused one is never shown back
        shown.update({k: v for k, v in state.typed.items() if k not in state.errors})
        shown["paused"] = state.paused
    return {
        "version": stored.version,
        "shown": shown,
        "frequencies": [(v, FREQUENCY_LABELS[v]) for v in FREQUENCIES],
        "lengths": [(v, LENGTH_LABELS[v]) for v in LENGTHS],
        "ask_later": [(v, ASK_LATER_LABELS[v]) for v in ASK_LATER],
        "save_url": SAVE_PATH,
        "errors": dict(state.errors),
        "first_invalid": next((n for n in FIELD_ORDER if n in state.errors), ""),
        "saved": SAVED if state.saved else "",
        "conflict": state.conflict,
        "conflict_message": VersionConflict.message,
        # Something in this answer takes focus, so the page heading does not.
        "attention": bool(state.errors or state.saved or state.conflict),
    }
