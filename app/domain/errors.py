"""Errors the user can see (spec 2.11).

``user_message`` is the plain, blame-free text shown in the interface. The optional
``detail`` is for logs only and never reaches ``user_message``. ``detail`` must not contain
secrets or personal data: log redaction (M0.10) is a backstop, not permission.
``AppError`` is a base class; raise one of its subclasses.
``kind`` is the stable name stored in the database (``tasks.error``) and mapped back to a
message. It defaults to the class name; errors that get stored set it explicitly so a class
rename can't orphan stored rows.
"""

from datetime import date, datetime
from typing import Any, ClassVar

# Fixed English month names, so the system locale never changes user-facing text (spec 3.10).
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def format_day(day: date) -> str:
    """Write a date the way the interface shows it, e.g. "1 November 2026"."""
    return f"{day.day} {_MONTHS[day.month - 1]} {day.year}"


def format_day_time(moment: datetime) -> str:
    """Write a moment the way the interface shows it, e.g. "10 October 2026, 14:05".

    The moment is written as given: the caller converts it to the local zone first."""
    return f"{format_day(moment.date())}, {moment.hour:02d}:{moment.minute:02d}"


class AppError(Exception):
    message = "Something went wrong. Try again."
    kind: ClassVar[str] = "AppError"

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "kind" not in cls.__dict__:
            cls.kind = cls.__name__

    def __init__(self, *, detail: str | None = None) -> None:
        super().__init__(detail if detail is not None else self.message)

    @property
    def user_message(self) -> str:
        return self.message


class ProviderNotInstalled(AppError):
    kind = "ProviderNotInstalled"
    message = "Claude Code wasn't found. Install it, or choose Claude via API key."


class ProviderAuthError(AppError):
    kind = "ProviderAuthError"
    message = "The token or key was rejected. Create a new one and paste it again."


class UsageLimitReached(AppError):
    kind = "UsageLimitReached"
    message = "Your Claude plan's usage limit was reached. Try later or switch provider."


class ProviderRequestRejected(AppError):
    """The provider refused the request itself (unknown model, request too large)."""

    kind = "ProviderRequestRejected"
    message = "Claude refused the request. Check the model in Settings > Providers."


class BudgetBlocked(AppError):
    kind = "BudgetBlocked"

    def __init__(self, service: str, resets_on: date, *, detail: str | None = None) -> None:
        self.service = service
        self.resets_on = resets_on
        super().__init__(detail=detail)

    @property
    def user_message(self) -> str:
        return f"Monthly limit reached for {self.service}. Resets on {format_day(self.resets_on)}."


class TierCapReached(BudgetBlocked):
    """Only a tier's cap blocks (spec 3.7). ``offer_fast`` (Thorough only) means the caller
    may offer to run once on Fast after checking Fast; it never switches by itself."""

    kind = "TierCapReached"

    def __init__(
        self, service: str, tier: str, resets_on: date, *, detail: str | None = None
    ) -> None:
        self.tier = tier
        self.offer_fast = tier == "thorough"
        super().__init__(service, resets_on, detail=detail)

    @property
    def user_message(self) -> str:
        text = (
            f"The {self.tier.capitalize()} limit for {self.service} is reached. "
            f"It resets on {format_day(self.resets_on)}"
        )
        return text + ("; you can run this once on Fast." if self.offer_fast else ".")


class UsageLimitsUnreadable(AppError):
    """The stored budgets can't be read, so no AI request runs until they are saved again."""

    kind = "UsageLimitsUnreadable"
    message = (
        "Your usage limits couldn't be read. Open Settings > Usage budgets and save them again."
    )


class BudgetNoticeRefused(AppError):
    """A budget warning was confirmed that isn't due in the current cycle."""

    kind = "BudgetNoticeRefused"
    message = "This warning can't be confirmed now. Reload the page to see your current usage."


class NetworkUnavailable(AppError):
    kind = "NetworkUnavailable"
    message = "No connection. Nothing was lost; try again."


class InvalidModelOutput(AppError):
    kind = "InvalidModelOutput"
    message = "The AI response didn't have the expected shape. Try again."


class VersionConflict(AppError):
    kind = "VersionConflict"
    message = "This item changed elsewhere. Reload to see the latest version."


class ConfigError(AppError):
    """A start-up setting is invalid. ``detail`` names the setting, never its value."""

    message = "A setting isn't valid. Check the command options, environment and settings file."


class DataFolderUnavailable(ConfigError):
    """The chosen data folder can't be created, opened or logged into (step 1; ADR-044).

    ``detail`` names the error type only, never the path.
    """

    message = "This folder can't be used. Choose another folder."


class DataFolderNotYours(ConfigError):
    """The chosen data folder belongs to another account (ADR-043)."""

    message = "This folder belongs to another user. Choose a folder you own."


class DataFolderInUse(ConfigError):
    """Another copy of the app holds the data folder's lock (ADR-072, ADR-076 item 9).

    ``detail`` never names the path.
    """

    kind = "DataFolderInUse"
    message = (
        "This folder is already open in another copy of the app. "
        "Close that copy first, or choose another folder."
    )

    def __init__(self, *, detail: str | None = "the data folder lock is held") -> None:
        super().__init__(detail=detail)


class FetchRefused(AppError):
    kind = "FetchRefused"
    message = "This address can't be fetched. Paste the job text instead."


class SecretStoreUnavailable(AppError):
    """The system keychain can't be used. ``detail`` names the error type, never a value."""

    kind = "SecretStoreUnavailable"
    message = "Your system keychain isn't available. You can use an environment variable instead."


class TaskNotRetryable(AppError):
    """Retry was asked for a task that is still queued or running, or has succeeded."""

    kind = "TaskNotRetryable"
    message = (
        "This task can't be retried. Only failed, cancelled or interrupted tasks can run again."
    )


class TaskFailed(AppError):
    """A task handler raised something other than an ``AppError``. ``detail`` names the
    exception type only, never its text."""

    kind = "TaskFailed"
    message = "This task stopped because of an unexpected problem. Try again."


class PracticeNotOffered(AppError):
    """The practice task (M2.17) was started or claimed while it isn't offered: fakes aren't
    allowed, or the saved provider isn't the fake."""

    kind = "PracticeNotOffered"
    message = "Practice tasks are only available while the fake provider is active."


class UnknownTaskType(AppError):
    """A queued task has a type no handler is registered for (for example, a newer app
    version queued it)."""

    kind = "UnknownTaskType"
    message = "This task can't run in this version of the app."


class UnknownSetupStep(AppError):
    message = "This setup step doesn't exist."


class StepNotSkippable(AppError):
    message = "This step is needed, so it can't be skipped."


BUDGET_MESSAGE = (
    "A monthly usage limit was reached. Open Settings > Usage budgets to see when it resets."
)
"""What a stored budget kind reads as: the service, tier and reset date aren't stored."""


def _own_subclasses(cls: type[AppError]) -> list[type[AppError]]:
    found: list[type[AppError]] = []
    for sub in cls.__subclasses__():
        if sub.__module__ == __name__:  # never a class defined elsewhere
            found.append(sub)
        found.extend(_own_subclasses(sub))
    return found


def message_for_kind(kind: str | None) -> str:
    """The message for an error kind stored in ``tasks.error``. An exact match on a kind of
    this module's classes only: anything else (None, other text, another letter case) reads
    as the generic message, and what was stored is never part of the answer."""
    if not isinstance(kind, str):
        return AppError.message
    for cls in _own_subclasses(AppError):
        if cls.kind == kind:
            return BUDGET_MESSAGE if issubclass(cls, BudgetBlocked) else cls.message
    return AppError.message
