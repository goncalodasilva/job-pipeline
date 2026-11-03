"""What the Settings > Board template is given (M5.7; AC-M5.1; ADR-102 item 10).

Kept apart from the routes (``app/api/settings_board.py``) so the Settings page
(``app/api/settings.py``) can show the section without importing them, as the Usage budgets
section does. A read: it writes nothing. Status and rule values are untrusted text: the
templates escape them.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from fastapi import Request

from app.domain.applications import ApplicationPhase
from app.domain.errors import VersionConflict
from app.domain.reminders import MAX_DAYS, MIN_DAYS
from app.services.applications import ApplicationStatuses
from app.services.reminders import ReminderRules

SETTINGS_PATH = "/settings"
AREA_PATH = "/settings/lists"  # the Lists and statuses page (M9.2)
STATUSES_PATH = f"{SETTINGS_PATH}/board/statuses"
REMINDERS_PATH = f"{SETTINGS_PATH}/board/reminders"
DEFAULT_DAYS = 14  # shown for a status with no rule yet
ADD, RENAME, DELETE, RULE = "add", "rename", "delete", "rule"
_NOTHING: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True)
class BoardSettingsState:
    """What just happened in the section; the default is a plain page load."""

    scope: str = ""  # the form that was refused: ADD, RENAME, DELETE or RULE (empty when done)
    key: str = ""  # the status the form or the done line belongs to (not for a refused ADD)
    errors: Mapping[str, str] = field(default_factory=lambda: _NOTHING)  # message per field
    typed: Mapping[str, str] = field(default_factory=lambda: _NOTHING)  # shown back, escaped
    message: str = ""  # done: the line shown in the section (fixed copy and a label)
    # Done: what takes focus in the row of ``key``: "status" (the row), "name" (its name field),
    # "up" or "down" (its Move button), "rule" (its reminder row), or "statuses" (the heading).
    focus: str = ""
    conflict: bool = False  # a stale version: the reload prompt shows and takes focus


def board_context(request: Request, state: BoardSettingsState | None) -> dict[str, Any]:
    """The ``board`` template value. Blocking (it reads the database)."""
    state = state or BoardSettingsState()
    statuses: ApplicationStatuses = request.app.state.application_statuses
    saved_rules: ReminderRules = request.app.state.reminder_rules
    listed = statuses.listing()
    rules = {rule.status_key: rule for rule in saved_rules.listing()}
    first_keys: set[str] = set()
    last_keys: set[str] = set()
    for phase in ApplicationPhase:  # the ends of each phase: a Move there changes nothing
        keys = [s.key for s in listed if s.phase == phase.value]
        if keys:
            first_keys.add(keys[0])
            last_keys.add(keys[-1])
    first_invalid = ""
    if state.errors and state.scope != DELETE:
        first_invalid = next((n for n in ("label", "phase", "days") if n in state.errors), "")
    return {
        "statuses": [
            {
                "key": s.key,
                "label": s.label,
                "phase": s.phase,
                "phase_label": s.phase.capitalize(),
                "built_in": s.built_in,
                "version": s.version,
                "first": s.key in first_keys,
                "last": s.key in last_keys,
            }
            for s in listed
        ],
        "rules": [
            {
                "key": s.key,
                "label": s.label,
                "enabled": rules[s.key].enabled if s.key in rules else False,
                "days": rules[s.key].days if s.key in rules else DEFAULT_DAYS,
                "version": rules[s.key].version if s.key in rules else 0,
            }
            for s in listed
            if s.phase != ApplicationPhase.CLOSED.value
        ],
        "phases": [(phase.value, phase.value.capitalize()) for phase in ApplicationPhase],
        "statuses_url": STATUSES_PATH,
        "reminders_url": REMINDERS_PATH,
        "min_days": MIN_DAYS,
        "max_days": MAX_DAYS,
        "scope": state.scope,
        "key": state.key,
        "errors": dict(state.errors),
        "typed": dict(state.typed),
        "message": state.message,
        "focus": state.focus,
        "conflict": state.conflict,
        "conflict_message": VersionConflict.message,
        "first_invalid": first_invalid,
        # Something in this answer takes focus, so the page heading does not.
        "attention": bool(state.errors or state.message or state.conflict),
    }
