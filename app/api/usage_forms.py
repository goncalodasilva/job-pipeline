"""What the Settings > Usage budgets templates are given, and how its forms are read (M2.22).

Kept apart from the routes (``app/api/settings_usage.py``) so the Settings page
(``app/api/settings.py``) can show the section without importing them. Reading a form only
turns text into numbers; every rule is the domain's, reached through ``UsageSettings``.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from fastapi import Request

from app.api.forms import first_value, whole_number
from app.domain.errors import VersionConflict
from app.onboarding.registry import Form
from app.services.usage_panel import UsagePanel
from app.services.usage_settings import (
    BUDGET_FIELDS,
    CONCURRENCY_CHOICES,
    CONCURRENCY_FIELD,
    UsageSettings,
)

BUDGETS_PATH = "/settings/usage/budgets"
CONCURRENCY_PATH = "/settings/usage/concurrency"
PANEL_PATH = "/settings/usage"  # the usage panel alone (M2.23)
# Fired after a budget was saved or removed, so the usage panel fetches itself again.
USAGE_CHANGED_EVENT = "usage:changed"
# The panel's trigger: at once after a budget change; a task change (a provider call may have
# been counted) after a second without another one, so a burst of task events is one fetch
# and the last one is never dropped. Not tasks:resync: every stream connection fires it.
PANEL_TRIGGER = f"{USAGE_CHANGED_EVENT} from:body, tasks:updated from:body delay:1s"
CONCURRENCY = "concurrency"  # ``UsageFormState.target`` for the concurrency form
_NOTHING: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True)
class UsageFormState:
    """What just happened to one of the section's forms; the default is a plain page load."""

    target: str = ""  # the service whose form was sent, or ``CONCURRENCY``
    errors: Mapping[str, str] = field(default_factory=lambda: _NOTHING)  # message per field
    typed: Mapping[str, str] = field(default_factory=lambda: _NOTHING)  # shown back, escaped
    changed: bool = False  # saved or removed: focus moves to the form's heading
    confirmed: str = ""  # with ``changed``: the line shown in the block (fixed copy)
    conflict: bool = False  # a stale version: the reload prompt shows and takes focus
    conflict_detail: str = ""  # with ``conflict``: what it means for this form (fixed copy)
    # A refused or stale form keeps the version it was sent with ("" when that wasn't a
    # number), so it keeps conflicting until the page is reloaded. None: the stored version.
    kept_version: str | None = None


def usage_settings(request: Request) -> UsageSettings:
    service: UsageSettings = request.app.state.usage_settings
    return service


def panel_context(request: Request) -> dict[str, Any]:
    """The ``usage_panel`` template value (M2.23). A read: writes nothing. Blocking."""
    panel: UsagePanel = request.app.state.usage_panel
    return {"view": panel.view(), "url": PANEL_PATH, "refresh_on": PANEL_TRIGGER}


def usage_context(request: Request, state: UsageFormState | None) -> dict[str, Any]:
    """The ``usage`` template value. A read: writes nothing."""
    state = state or UsageFormState()
    return {
        "view": usage_settings(request).view(),
        "budgets_url": BUDGETS_PATH,
        "concurrency_url": CONCURRENCY_PATH,
        "concurrency_target": CONCURRENCY,
        "concurrency_field": CONCURRENCY_FIELD,
        "concurrency_choices": CONCURRENCY_CHOICES,
        "fields": BUDGET_FIELDS,
        "target": state.target,
        "errors": dict(state.errors),
        "typed": dict(state.typed),
        "changed": state.changed,
        "confirmed": state.confirmed,
        "conflict": state.conflict,
        "conflict_detail": state.conflict_detail,
        "kept_version": state.kept_version,
        "conflict_message": VersionConflict.message,
        # The first refused field in form order takes focus.
        "first_invalid": next((name for name in BUDGET_FIELDS if name in state.errors), ""),
    }


def typed_budget(form: Form) -> dict[str, str]:
    """The budget fields as typed, to show back."""
    return {name: first_value(form, name) for name in BUDGET_FIELDS}


def entered_budget(typed: Mapping[str, str]) -> dict[str, int | str | None]:
    """The budget fields as numbers where they are numbers (see ``whole_number``)."""
    return {name: whole_number(text) for name, text in typed.items()}


def expected_version(form: Form) -> int | None:
    """The version the form was rendered with; None when it isn't a number."""
    version = whole_number(first_value(form, "version"))
    return version if isinstance(version, int) else None
