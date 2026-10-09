"""Errors the user can see (spec 2.11).

``user_message`` is the plain, blame-free text shown in the interface. The optional
``detail`` is for logs only and never reaches ``user_message``. ``detail`` must not contain
secrets or personal data: log redaction (M0.10) is a backstop, not permission.
``AppError`` is a base class; raise one of its subclasses.
``kind`` is the stable name stored in the database (``tasks.error``) and mapped back to a
message. It defaults to the class name; errors that get stored set it explicitly so a class
rename can't orphan stored rows.
"""

from datetime import date
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


class UnknownSetupStep(AppError):
    message = "This setup step doesn't exist."


class StepNotSkippable(AppError):
    message = "This step is needed, so it can't be skipped."
