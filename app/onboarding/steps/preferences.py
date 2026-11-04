"""Step 5: job preferences (M1.19; AC-M1.6).

The form has four lists typed one item per line (target roles, locations, must-haves,
deal-breakers), work mode checkboxes, an optional minimum salary per year with its currency and
the CV country. Every field is checked here so each problem gets its own plain message (never
pydantic's text); a valid submit saves ``JobPreferences`` with the stored version (retrying once
if it changed elsewhere) and marks the step done through the injected ``mark_done``, which also
refreshes the setup decision. The text is the user's own and is stored as plain data.

The token-free page of the step is a shell (heading, intro, a loading placeholder): the form,
filled from the stored preferences, is the protected fragment ``GET /setup/preferences/form``
(app/api/setup.py; spec 2.9 rule 13), whose context is ``context()``.

The settings service is injected by wiring as a getter, since it needs the workspace's
database; without one the step asks the user to finish step 1 first.
"""

import logging
import re
from collections.abc import Callable, Mapping

from pydantic import ValidationError

from app.domain.errors import VersionConflict
from app.domain.locales import COUNTRIES, CURRENCIES
from app.domain.settings import JOB_PREFERENCES, JobPreferences, Salary, WorkMode
from app.onboarding.registry import Form, StepResult
from app.services.settings import SettingsService

_log = logging.getLogger(__name__)

LIST_FIELDS = ("target_roles", "locations", "must_haves", "deal_breakers")
MAX_ITEMS = 20
MAX_ITEM_LENGTH = 200
MAX_SALARY = 10**9
# Form value -> setting value, in the order the checkboxes are shown.
WORK_MODES: Mapping[str, WorkMode] = {"remote": "remote", "hybrid": "hybrid", "on_site": "on_site"}
WORK_MODE_LABELS: Mapping[str, str] = {"remote": "Remote", "hybrid": "Hybrid", "on_site": "On site"}
COUNTRY_CODES = frozenset(code for code, _ in COUNTRIES)
CURRENCY_CODES = frozenset(code for code, _ in CURRENCIES)

MSG_STEP_1_FIRST = "Finish step 1 first: choose where the app keeps your data."
MSG_SAVE_FAILED = "Your preferences couldn't be saved. Try again."
MSG_NOT_MOVED_ON = "Your preferences were saved, but the app couldn't move on. Try again."
MSG_ROLES_EMPTY = "Enter at least one role you're looking for."
MSG_TOO_MANY = f"Enter at most {MAX_ITEMS} items, one per line."
MSG_TOO_LONG = f"Keep each line to {MAX_ITEM_LENGTH} characters or fewer."
MSG_CONTROL = "Remove tabs and other special characters from these lines."
MSG_WORK_MODE = "Choose from remote, hybrid and on site."
MSG_AMOUNT = "Enter the yearly amount as a whole number, such as 45000 or 45,000."
MSG_AMOUNT_RANGE = "Enter an amount no higher than 1,000,000,000."
MSG_CURRENCY_MISSING = "Choose the currency of your minimum salary."
MSG_CURRENCY_UNKNOWN = "Choose a currency from the list."
MSG_COUNTRY_MISSING = "Choose the country your CV is for."
MSG_COUNTRY_UNKNOWN = "Choose a country from the list."

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SEPARATORS = re.compile("[ ,\u00a0\u202f]")  # space, comma, no-break spaces
_DIGITS = re.compile(r"[0-9]+")
_MAX_AMOUNT_DIGITS = 12  # longer is out of range anyway; never parse huge numbers


class PreferencesStep:
    # Shown back after a refused submit (autoescaped); work modes hold several values.
    echo_fields: tuple[str, ...] = (*LIST_FIELDS, "salary_amount", "salary_currency", "cv_country")
    echo_multi_fields: tuple[str, ...] = ("work_modes",)

    def __init__(
        self,
        settings: Callable[[], SettingsService | None],
        *,
        mark_done: Callable[[], None],
    ) -> None:
        self._settings = settings
        self._mark_done = mark_done  # blocking: marks the step done, refreshes the decision

    def context(self) -> Mapping[str, object]:
        common: dict[str, object] = {
            "countries": COUNTRIES,
            "currencies": CURRENCIES,
            "work_mode_choices": tuple(WORK_MODE_LABELS.items()),
        }
        settings = self._settings()
        if settings is None:
            return {**common, "workspace_open": False}
        return {**common, "workspace_open": True, **_prefill(settings.load(JOB_PREFERENCES).value)}

    def shell_context(self) -> Mapping[str, object]:
        """What the token-free page needs (TD-342; spec 2.9 rule 13): whether there is a
        workspace to keep preferences in. Nothing is read from the database."""
        return {"workspace_open": self._settings() is not None}

    def submit(self, form: Form) -> StepResult:
        settings = self._settings()
        if settings is None:
            return StepResult(ok=False, errors={"form": MSG_STEP_1_FIRST})
        try:
            parsed = _parse(form)
        except ValidationError as error:  # checked above; pydantic's text repeats the values
            _log.warning("Job preferences were refused (%s).", type(error).__name__)
            return StepResult(ok=False, errors={"form": MSG_SAVE_FAILED})
        if not isinstance(parsed, JobPreferences):
            return StepResult(ok=False, errors=parsed)
        if not _save(settings, parsed):
            return StepResult(ok=False, errors={"form": MSG_SAVE_FAILED})
        try:
            self._mark_done()
        except Exception as error:  # sqlite messages can hold paths: log the type only
            _log.warning("Preferences step couldn't be marked done (%s).", type(error).__name__)
            return StepResult(ok=False, errors={"form": MSG_NOT_MOVED_ON})
        return StepResult(ok=True)


def _prefill(saved: JobPreferences | None) -> dict[str, object]:
    """The form's values from the stored preferences (empty before the first save)."""
    if saved is None:
        return {
            **{name: "" for name in LIST_FIELDS},
            "work_modes": [],
            "salary_amount": "",
            "salary_currency": "",
            "cv_country": "",
        }
    salary = saved.minimum_salary
    return {
        **{name: "\n".join(getattr(saved, name)) for name in LIST_FIELDS},
        "work_modes": list(saved.work_modes),
        "salary_amount": str(salary.amount) if salary is not None else "",
        "salary_currency": salary.currency if salary is not None else "",
        "cv_country": saved.cv_country,
    }


def _save(settings: SettingsService, preferences: JobPreferences) -> bool:
    """Save with the stored version; retry once when it changed elsewhere meanwhile."""
    for attempt in range(2):
        try:
            version = settings.load(JOB_PREFERENCES).version
            settings.save(JOB_PREFERENCES, preferences, expected_version=version)
            return True
        except VersionConflict:
            if attempt:
                break
            _log.info("Job preferences changed elsewhere; retrying once.")
        except Exception as error:  # sqlite messages can hold paths: log the type only
            _log.warning("Job preferences couldn't be saved (%s).", type(error).__name__)
            return False
    _log.warning("Job preferences couldn't be saved (VersionConflict).")
    return False


# --- parsing ---------------------------------------------------------------------------------


def _parse(form: Form) -> JobPreferences | dict[str, str]:
    """The preferences, or a plain message per refused field."""
    errors: dict[str, str] = {}
    lists: dict[str, tuple[str, ...]] = {}
    for name in LIST_FIELDS:
        items = _lines(_first(form, name))
        problem = _list_problem(items)
        if problem is None and name == "target_roles" and not items:
            problem = MSG_ROLES_EMPTY
        if problem is not None:
            errors[name] = problem
        lists[name] = items

    modes: list[WorkMode] = []
    for value in form.get("work_modes", ()):
        mode = WORK_MODES.get(value)
        if mode is None:
            errors["work_modes"] = MSG_WORK_MODE
        elif mode not in modes:
            modes.append(mode)

    amount: int | None = None
    parsed_amount = _amount(_first(form, "salary_amount"))
    if isinstance(parsed_amount, str):
        errors["salary_amount"] = parsed_amount
    else:
        amount = parsed_amount
    currency = (_first(form, "salary_currency") or "").strip()
    if currency and currency not in CURRENCY_CODES:
        errors["salary_currency"] = MSG_CURRENCY_UNKNOWN
    elif not currency and parsed_amount is not None:
        errors["salary_currency"] = MSG_CURRENCY_MISSING

    country = (_first(form, "cv_country") or "").strip()
    if not country:
        errors["cv_country"] = MSG_COUNTRY_MISSING
    elif country not in COUNTRY_CODES:
        errors["cv_country"] = MSG_COUNTRY_UNKNOWN

    if errors:
        return errors
    return JobPreferences(
        target_roles=lists["target_roles"],
        locations=lists["locations"],
        work_modes=tuple(modes),
        minimum_salary=Salary(amount=amount, currency=currency) if amount is not None else None,
        must_haves=lists["must_haves"],
        deal_breakers=lists["deal_breakers"],
        cv_country=country,
    )


def _lines(text: str | None) -> tuple[str, ...]:
    """One item per line: carriage returns ignored, items trimmed, blank lines dropped."""
    stripped = (line.strip() for line in (text or "").replace("\r", "").split("\n"))
    return tuple(line for line in stripped if line)


def _list_problem(items: tuple[str, ...]) -> str | None:
    if len(items) > MAX_ITEMS:
        return MSG_TOO_MANY
    if any(len(item) > MAX_ITEM_LENGTH for item in items):
        return MSG_TOO_LONG
    if any(_CONTROL.search(item) for item in items):
        return MSG_CONTROL
    return None


def _amount(text: str | None) -> int | str | None:
    """The whole yearly amount, None when empty, or the message explaining the problem."""
    digits = _SEPARATORS.sub("", (text or "").strip())
    if not digits:
        return None
    if not _DIGITS.fullmatch(digits):
        return MSG_AMOUNT
    if len(digits) > _MAX_AMOUNT_DIGITS or int(digits) > MAX_SALARY:
        return MSG_AMOUNT_RANGE
    return int(digits)


def _first(form: Form, name: str) -> str | None:
    values = form.get(name)
    return values[0] if values else None
