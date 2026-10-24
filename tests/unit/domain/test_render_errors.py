"""Errors a renderer raises (M6.2; spec 2.11): stable kind, fixed blame-free message, and a
``detail`` that never reaches the user."""

import pytest

import app.domain.errors as errors
from app.domain.errors import (
    AppError,
    RenderFailed,
    RenderTimedOut,
    RenderTooLarge,
    message_for_kind,
)

RENDER_ERRORS = (RenderFailed, RenderTimedOut, RenderTooLarge)


@pytest.mark.parametrize("cls", RENDER_ERRORS)
def test_render_error_has_stable_kind_and_friendly_message(cls: type[AppError]) -> None:
    assert issubclass(cls, AppError) and cls is not AppError
    assert "kind" in cls.__dict__, "set kind explicitly so a class rename can't orphan stored rows"
    assert cls.kind == cls.__name__
    message = cls().user_message
    assert message != AppError.message
    assert message.endswith(".") and 20 <= len(message) <= 160
    assert message_for_kind(cls.kind) == message
    assert getattr(errors, cls.__name__) is cls


@pytest.mark.parametrize("cls", RENDER_ERRORS)
def test_render_error_detail_never_reaches_the_user(cls: type[AppError]) -> None:
    error = cls(detail="TypstError at line 3")

    assert "TypstError" not in error.user_message
    assert error.user_message == cls().user_message
    assert str(error) == "TypstError at line 3", "the detail is for logs"


def test_render_failed_says_what_could_not_be_made() -> None:
    assert RenderFailed().user_message == "The document couldn't be turned into a PDF."
