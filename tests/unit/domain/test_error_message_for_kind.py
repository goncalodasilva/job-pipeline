"""``message_for_kind``: the user-facing message for an error kind stored in ``tasks.error``
(M2.19; spec 2.11). A pure lookup over the ``AppError`` classes: it never raises, never shows
what was stored, and falls back to the generic message for anything it doesn't know.

The new name is imported inside the tests.
"""

import pytest

import app.domain.errors as errors
from app.domain.errors import AppError, BudgetBlocked, TierCapReached

GENERIC = AppError.message
MSG_BUDGET = (
    "A monthly usage limit was reached. Open Settings > Usage budgets to see when it resets."
)


def _own_classes() -> list[type[AppError]]:
    found = [
        value
        for value in vars(errors).values()
        if isinstance(value, type) and issubclass(value, AppError) and value is not AppError
    ]
    assert len(found) > 15
    return found


FIXED = [cls for cls in _own_classes() if not issubclass(cls, BudgetBlocked)]


@pytest.mark.parametrize("cls", FIXED, ids=[cls.__name__ for cls in FIXED])
def test_message_for_kind_is_the_class_message(cls: type[AppError]) -> None:
    from app.domain.errors import message_for_kind

    assert message_for_kind(cls.kind) == cls.message == cls().user_message


@pytest.mark.parametrize("cls", [BudgetBlocked, TierCapReached])
def test_message_for_kind_budget_kinds_need_no_stored_details(cls: type[AppError]) -> None:
    """The service, tier and reset date aren't stored with the kind, so these read as one
    fixed sentence that points at the place showing them."""
    from app.domain.errors import message_for_kind

    assert message_for_kind(cls.kind) == MSG_BUDGET


@pytest.mark.parametrize(
    "stored",
    [
        None,
        "",
        " ",
        "AppError",
        "FictionalKindFromTheFuture",
        "usagelimitreached",
        " UsageLimitReached",
        "UsageLimitReached\n",
        "message",
        "__class__",
        '<script>alert("fictional")</script>',
        "x" * 10_000,
    ],
)
def test_message_for_kind_unknown_is_generic(stored: str | None) -> None:
    from app.domain.errors import message_for_kind

    assert message_for_kind(stored) == GENERIC
