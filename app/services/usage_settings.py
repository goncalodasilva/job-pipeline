"""Settings > Usage budgets: what the section shows and what its forms save (M2.22; AC-M2.7,
AC-M2.10; spec 3.7; ADR-076 items 4, 5, 8; ADR-079).

One request budget per service, optional ("No limit" until one is saved), and how many
background tasks run at once. Stored budgets that can't be read are reported as such (the app
then refuses AI requests, ADR-079); saving a budget replaces them. Which services are offered
is decided here: the fake provider's only while fakes are allowed. The rules themselves stay
in the domain (``Budget`` and ``TaskRunnerSettings``); this module turns their answers into the
copy shown next to a field.
Reads write nothing. Everything here blocks (database).
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.domain.budgets import Budget, BudgetFieldError, InvalidBudget
from app.domain.settings import TASK_RUNNER, TaskRunnerSettings
from app.domain.usage import SERVICE_NAMES, ServiceId, is_service
from app.services.settings import SettingsService
from app.services.usage import UsageService

REQUIRED_FIELDS: tuple[str, ...] = (
    "allowance",
    "first_warning",
    "second_warning",
    "block_at",
    "reset_day",
)
CAP_FIELDS: tuple[str, ...] = ("fast_cap", "thorough_cap")  # empty: no cap
BUDGET_FIELDS: tuple[str, ...] = (*REQUIRED_FIELDS, *CAP_FIELDS)
CONCURRENCY_FIELD = "max_concurrent"
CONCURRENCY_CHOICES: tuple[int, ...] = (1, 2, 3)  # the range ``TaskRunnerSettings`` accepts
FAKE_SERVICE = "fake"

# ASCII digits only, short enough to be stored and shown as typed.
_DIGITS = re.compile(r"-?[0-9]{1,12}")

_WHOLE_NUMBER = "Enter a whole number, 1 or more."
_NO_NUMBER = "Enter a number."
_NOT_DIGITS = "Enter a whole number, 1 or more, using digits only (for example 1000)."
_RESET_DAY = "Enter a day from 1 to 31."
_NOTHING_ENTERED = "Enter a monthly allowance to set a budget."
_CONCURRENCY = "Choose 1, 2 or 3."
# An ordering rule: the field it compares with, and the message naming that limit.
_ORDERING: Mapping[str, tuple[str, str]] = {
    "must_exceed_first_warning": (
        "first_warning",
        "Enter a number higher than the first warning ({limit}).",
    ),
    "must_exceed_second_warning": (
        "second_warning",
        "Enter a number higher than the second warning ({limit}).",
    ),
    "must_not_exceed_allowance": (
        "allowance",
        "Enter a number no higher than the monthly allowance ({limit}).",
    ),
}


def whole_number(text: str) -> int | str | None:
    """What a number field holds: the whole number, None when nothing was typed, or the text
    as typed when it isn't a whole number (the rule that refuses it lives in the domain)."""
    typed = text.strip()
    if not typed:
        return None
    return int(typed) if _DIGITS.fullmatch(typed) else text


class UsageSettingUnavailable(LookupError):
    """A budget was sent for a service that isn't offered (unknown, or the fake provider's
    while fakes aren't allowed), or no workspace is open. Never names what was sent."""


@dataclass(frozen=True)
class BudgetView:
    service: str
    name: str
    budget: Budget | None  # None: no limit
    values: Mapping[str, str]  # the stored budget as the form shows it; "" where unset


@dataclass(frozen=True)
class UsageSettingsView:
    budgets: tuple[BudgetView, ...]
    budgets_version: int  # one version for every service's budget (ADR-079)
    # The stored budgets can't be read: AI requests are refused until one is saved (ADR-079).
    budgets_unreadable: bool
    max_concurrent: int
    runner_version: int


def _shown(budget: Budget | None) -> Mapping[str, str]:
    values = {name: getattr(budget, name, None) for name in BUDGET_FIELDS}
    return {name: "" if value is None else str(value) for name, value in values.items()}


def _message(error: BudgetFieldError, entered: Mapping[str, Any]) -> str:
    """The copy for one refused field. Only numbers that were entered are ever put in it."""
    if error.field == "reset_day":
        return _RESET_DAY
    if error.code == "must_be_integer":
        return _NO_NUMBER if entered.get(error.field) is None else _NOT_DIGITS
    compared, message = _ORDERING.get(error.code, ("", _WHOLE_NUMBER))
    limit = entered.get(compared)
    return message.format(limit=limit) if isinstance(limit, int) else _WHOLE_NUMBER


class UsageSettings:
    """``usage`` and ``settings`` give the open workspace's services, or None before one is
    open."""

    def __init__(
        self,
        usage: Callable[[], UsageService | None],
        settings: Callable[[], SettingsService | None],
        *,
        allow_fakes: bool,
    ) -> None:
        self._usage = usage
        self._settings = settings
        self._allow_fakes = allow_fakes

    def available(self) -> bool:
        """Whether a workspace is open, so budgets can be read and saved."""
        return self._usage() is not None

    def offered(self) -> tuple[ServiceId, ...]:
        """The services a budget can be set for, in display order (ADR-076 item 4)."""
        known = (s for s in SERVICE_NAMES if is_service(s))
        return tuple(s for s in known if s != FAKE_SERVICE or self._allow_fakes)

    def name(self, service: str) -> str:
        """The display name of an offered service; ``UsageSettingUnavailable`` otherwise."""
        if service not in self.offered():
            raise UsageSettingUnavailable
        return SERVICE_NAMES[service]

    def view(self) -> UsageSettingsView:
        usage, settings = self._usage(), self._settings()
        stored: Mapping[ServiceId, Budget] = {}
        budgets_version, unreadable = 0, False
        if usage is not None:
            loaded = usage.read_budgets()
            stored, budgets_version, unreadable = loaded.budgets, loaded.version, loaded.unreadable
        runner = settings.load(TASK_RUNNER) if settings is not None else None
        return UsageSettingsView(
            budgets=tuple(
                BudgetView(s, SERVICE_NAMES[s], stored.get(s), _shown(stored.get(s)))
                for s in self.offered()
            ),
            budgets_version=budgets_version,
            budgets_unreadable=unreadable,
            max_concurrent=(runner.value if runner else TASK_RUNNER.default).max_concurrent,
            runner_version=runner.version if runner else 0,
        )

    def save_budget(
        self, service: str, values: Mapping[str, Any], *, expected_version: int
    ) -> Mapping[str, str]:
        """Save the service's budget from ``values`` (one entry per ``BUDGET_FIELDS``: the
        number entered, None when nothing was, or whatever else was typed). Returns the
        message per refused field; empty when it was saved. Raises ``VersionConflict``."""
        usage = self._writable(service)
        entered: dict[str, Any] = {name: values.get(name) for name in BUDGET_FIELDS}
        if all(value is None for value in entered.values()):
            # Emptying the form doesn't remove a budget: Remove does.
            return {"allowance": _NOTHING_ENTERED}
        try:
            budget = Budget(**entered)
        except InvalidBudget as invalid:
            return {error.field: _message(error, entered) for error in invalid.errors}
        usage.save_budget(service, budget, expected_version=expected_version)
        return {}

    def remove_budget(self, service: str, *, expected_version: int) -> None:
        """Back to no limit for this service. Raises ``VersionConflict``."""
        self._writable(service).save_budget(service, None, expected_version=expected_version)

    def save_concurrency(self, value: object, *, expected_version: int) -> str | None:
        """Save how many background tasks run at once; the message when ``value`` is
        refused. The runner reads it when it starts. Raises ``VersionConflict``."""
        settings = self._settings()
        if settings is None:
            raise UsageSettingUnavailable
        try:
            chosen = TaskRunnerSettings.model_validate({CONCURRENCY_FIELD: value}, strict=True)
        except ValidationError:
            return _CONCURRENCY
        settings.save(TASK_RUNNER, chosen, expected_version=expected_version)
        return None

    def _writable(self, service: str) -> UsageService:
        usage = self._usage()
        if usage is None or service not in self.offered():
            raise UsageSettingUnavailable
        return usage
