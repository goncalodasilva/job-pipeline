"""User-facing error classes (spec 2.11; task M0.7)."""

from datetime import date

import pytest

from app.domain import errors
from app.domain.errors import (
    AppError,
    BudgetBlocked,
    BudgetNoticeRefused,
    ConfigError,
    DataFolderInUse,
    FetchRefused,
    InvalidModelOutput,
    NetworkUnavailable,
    PracticeNotOffered,
    ProviderAuthError,
    ProviderNotInstalled,
    ProviderRequestRejected,
    SecretStoreUnavailable,
    TaskFailed,
    TaskNotRetryable,
    TierCapReached,
    UnknownTaskType,
    UsageLimitReached,
    UsageLimitsUnreadable,
    VersionConflict,
)

FIXED_MESSAGES: dict[type[AppError], str] = {
    ProviderNotInstalled: ("Claude Code wasn't found. Install it, or choose Claude via API key."),
    ProviderAuthError: ("The token or key was rejected. Create a new one and paste it again."),
    UsageLimitReached: (
        "Your Claude plan's usage limit was reached. Try later or switch provider."
    ),
    ProviderRequestRejected: (
        "Claude refused the request. Check the model in Settings > Providers."
    ),
    NetworkUnavailable: "No connection. Nothing was lost; try again.",
    InvalidModelOutput: "The AI response didn't have the expected shape. Try again.",
    VersionConflict: "This item changed elsewhere. Reload to see the latest version.",
    FetchRefused: "This address can't be fetched. Paste the job text instead.",
}

SPEC_ERROR_NAMES = (
    "ProviderNotInstalled",
    "ProviderAuthError",
    "UsageLimitReached",
    "BudgetBlocked",
    "NetworkUnavailable",
    "InvalidModelOutput",
    "VersionConflict",
    "FetchRefused",
)


def fake_key() -> str:
    # Built at runtime so no key-shaped literal exists in the repo (M0.3).
    return "sk-ant-" + "x" * 10


def build(cls: type[AppError], detail: str | None = None) -> AppError:
    if cls is BudgetBlocked:
        if detail is None:
            return BudgetBlocked("Claude", date(2026, 11, 1))
        return BudgetBlocked("Claude", date(2026, 11, 1), detail=detail)
    if detail is None:
        return cls()
    return cls(detail=detail)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("cls", "expected"),
    list(FIXED_MESSAGES.items()),
    ids=[cls.__name__ for cls in FIXED_MESSAGES],
)
def test_error_user_messages_match_spec_2_11(cls: type[AppError], expected: str) -> None:
    assert cls().user_message == expected


def test_budget_blocked_message_names_service_and_reset_date() -> None:
    error = BudgetBlocked("Claude", date(2026, 11, 1))

    assert error.user_message == ("Monthly limit reached for Claude. Resets on 1 November 2026.")


def test_budget_blocked_message_uses_given_service() -> None:
    error = BudgetBlocked(service="Web search", resets_on=date(2027, 2, 1))

    assert error.user_message == (
        "Monthly limit reached for Web search. Resets on 1 February 2027."
    )


@pytest.mark.parametrize(
    "cls",
    [*FIXED_MESSAGES, BudgetBlocked],
    ids=[*(cls.__name__ for cls in FIXED_MESSAGES), "BudgetBlocked"],
)
def test_user_message_never_includes_internal_detail(cls: type[AppError]) -> None:
    secret_like = fake_key()
    detail = f"upstream said: invalid credential {secret_like}"

    error = build(cls, detail=detail)

    assert secret_like not in error.user_message
    assert "upstream said" not in error.user_message
    assert error.user_message == build(cls).user_message
    assert str(error) == detail  # detail is for logs only


@pytest.mark.parametrize("name", SPEC_ERROR_NAMES)
def test_all_errors_derive_from_app_error(name: str) -> None:
    cls = getattr(errors, name)

    assert issubclass(cls, AppError)
    assert cls is not AppError
    assert issubclass(AppError, Exception)
    with pytest.raises(AppError):
        raise build(cls)


def test_reset_date_is_written_the_same_whatever_the_system_locale() -> None:
    # Month names come from a fixed list, not strftime, so the OS language can't change them.
    for month, name in [(1, "January"), (5, "May"), (12, "December")]:
        error = BudgetBlocked("Claude", date(2027, month, 9))
        assert error.user_message.endswith(f"Resets on 9 {name} 2027.")


def test_generic_app_error_makes_no_promise_it_cannot_keep() -> None:
    assert AppError().user_message == "Something went wrong. Try again."


# Stored in tasks.error and mapped to messages by the tasks panel (M2.7, M2.19). Changing a
# string here orphans rows already stored, so a class rename must keep its kind.
PINNED_KINDS: dict[type[AppError], str] = {
    ProviderNotInstalled: "ProviderNotInstalled",
    ProviderAuthError: "ProviderAuthError",
    UsageLimitReached: "UsageLimitReached",
    ProviderRequestRejected: "ProviderRequestRejected",
    BudgetBlocked: "BudgetBlocked",
    NetworkUnavailable: "NetworkUnavailable",
    InvalidModelOutput: "InvalidModelOutput",
    VersionConflict: "VersionConflict",
    FetchRefused: "FetchRefused",
    SecretStoreUnavailable: "SecretStoreUnavailable",
    DataFolderInUse: "DataFolderInUse",
    TaskNotRetryable: "TaskNotRetryable",
    TaskFailed: "TaskFailed",
    UnknownTaskType: "UnknownTaskType",
    TierCapReached: "TierCapReached",
    UsageLimitsUnreadable: "UsageLimitsUnreadable",
    BudgetNoticeRefused: "BudgetNoticeRefused",
    PracticeNotOffered: "PracticeNotOffered",
}


@pytest.mark.parametrize(
    ("cls", "expected"),
    list(PINNED_KINDS.items()),
    ids=[cls.__name__ for cls in PINNED_KINDS],
)
def test_error_kinds_are_pinned(cls: type[AppError], expected: str) -> None:
    assert cls.kind == expected
    # Explicit on the class itself, so renaming the class can't change the stored kind.
    assert cls.__dict__["kind"] == expected


# Shown in the tasks panel for tasks the runner ends itself (M2.8).
RUNNER_MESSAGES: dict[type[AppError], str] = {
    TaskFailed: "This task stopped because of an unexpected problem. Try again.",
    UnknownTaskType: "This task can't run in this version of the app.",
}


@pytest.mark.parametrize(
    ("cls", "expected"),
    list(RUNNER_MESSAGES.items()),
    ids=[cls.__name__ for cls in RUNNER_MESSAGES],
)
def test_runner_error_messages_are_plain_and_hide_detail(
    cls: type[AppError], expected: str
) -> None:
    assert cls().user_message == expected
    error = cls(detail="Fictional failure for Ada Example")
    assert error.user_message == expected
    assert "Ada Example" not in error.user_message


def test_error_kind_defaults_to_the_class_name_and_is_not_inherited() -> None:
    class FictionalFailure(NetworkUnavailable):
        pass

    assert AppError.kind == "AppError"
    assert ConfigError.kind == "ConfigError"
    assert FictionalFailure.kind == "FictionalFailure"
    assert FictionalFailure().kind == "FictionalFailure"


# --- usage budgets (M2.15) ----------------------------------------------------------


def test_tier_cap_reached_on_thorough_offers_fast() -> None:
    error = TierCapReached("Claude API key", "thorough", date(2026, 3, 15))

    assert (error.service, error.tier, error.resets_on) == (
        "Claude API key",
        "thorough",
        date(2026, 3, 15),
    )
    assert error.offer_fast is True
    assert error.user_message == (
        "The Thorough limit for Claude API key is reached. It resets on 15 March 2026; "
        "you can run this once on Fast."
    )


def test_tier_cap_reached_on_fast_offers_nothing() -> None:
    error = TierCapReached("Web search", "fast", date(2027, 2, 1))

    assert error.offer_fast is False
    assert error.user_message == (
        "The Fast limit for Web search is reached. It resets on 1 February 2027."
    )


def test_tier_cap_reached_is_a_budget_block_and_hides_detail() -> None:
    error = TierCapReached("Claude API key", "thorough", date(2026, 3, 15), detail="cap 3 of 3")

    assert isinstance(error, BudgetBlocked)
    assert "cap 3" not in error.user_message
    assert str(error) == "cap 3 of 3"


@pytest.mark.parametrize(
    ("cls", "expected"),
    [
        (
            UsageLimitsUnreadable,
            "Your usage limits couldn't be read. "
            "Open Settings > Usage budgets and save them again.",
        ),
        (
            BudgetNoticeRefused,
            "This warning can't be confirmed now. Reload the page to see your current usage.",
        ),
    ],
    ids=["UsageLimitsUnreadable", "BudgetNoticeRefused"],
)
def test_usage_error_messages_are_plain_and_hide_detail(cls: type[AppError], expected: str) -> None:
    assert cls().user_message == expected
    error = cls(detail="Fictional detail for Ada Example")
    assert error.user_message == expected
    assert "Ada Example" not in error.user_message


def test_format_day_time_writes_the_day_and_a_24_hour_time() -> None:
    """The moment as given (the caller converts to the local zone first): the day as
    ``format_day`` writes it, then hours and minutes, zero-padded."""
    from datetime import UTC, datetime

    from app.domain.errors import format_day_time

    assert format_day_time(datetime(2026, 10, 10, 14, 5, 59, tzinfo=UTC)) == (
        "10 October 2026, 14:05"
    )
    assert format_day_time(datetime(2026, 1, 3, 0, 0, tzinfo=UTC)) == "3 January 2026, 00:00"
