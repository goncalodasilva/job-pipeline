"""Errors for unreadable documents and refused uploads (M3.1; AC-M3.7; spec 2.11; ADR-089
items 2 and 11).

Each is an ``AppError`` in ``app/domain/errors.py`` with a stable ``kind`` and a plain,
blame-free message that tells the user what to do next. The new names are imported inside the
tests.
"""

import pytest

import app.domain.errors as errors
from app.domain.errors import AppError, message_for_kind

NEW_ERRORS = (
    "UnsupportedDocumentType",
    "UnreadableDocument",
    "EncryptedDocument",
    "NoTextFound",
    "ParseTimedOut",
    "UploadTooLarge",
    "UploadNotAccepted",
)
# Words that belong in logs, not in front of the user.
TECHNICAL_WORDS = ("exception", "traceback", "pypdf", "docx", "zip", "parse", "process", "bytes")


def _error(name: str) -> type[AppError]:
    assert hasattr(errors, name), f"app.domain.errors.{name} doesn't exist yet"
    cls: type[AppError] = getattr(errors, name)
    return cls


@pytest.mark.parametrize("name", NEW_ERRORS)
def test_ac_m3_7_document_error_has_stable_kind_and_friendly_message(name: str) -> None:
    cls = _error(name)

    assert issubclass(cls, AppError)
    assert "kind" in cls.__dict__, "set kind explicitly so a class rename can't orphan stored rows"
    assert cls.kind == name
    message = cls().user_message
    assert message != AppError.message
    assert message.endswith(".") and 20 <= len(message) <= 160
    for word in TECHNICAL_WORDS:
        assert word not in message.lower(), (name, word)
    assert message_for_kind(name) == message


@pytest.mark.parametrize("name", NEW_ERRORS)
def test_ac_m3_7_document_error_detail_never_reaches_the_user(name: str) -> None:
    error = _error(name)(detail="PdfReadError at byte 88")

    assert "PdfReadError" not in error.user_message
    assert error.user_message == _error(name)().user_message


def test_ac_m3_7_scanned_pdf_message_says_paste_the_text() -> None:
    # ADR-089 item 11: no OCR; a scanned PDF gets "paste the text instead".
    assert "paste the text" in _error("NoTextFound")().user_message.lower()


def test_ac_m3_7_unsupported_type_message_names_what_is_accepted() -> None:
    message = _error("UnsupportedDocumentType")().user_message

    assert "PDF" in message and "Word" in message
    assert "paste the text" in message.lower()


def test_ac_m3_7_unreadable_and_timed_out_messages_offer_pasting() -> None:
    for name in ("UnreadableDocument", "ParseTimedOut"):
        assert "paste the text" in _error(name)().user_message.lower(), name


def test_encrypted_document_is_an_unreadable_document_with_its_own_message() -> None:
    encrypted = _error("EncryptedDocument")
    unreadable = _error("UnreadableDocument")

    assert issubclass(encrypted, unreadable)
    assert "password" in encrypted().user_message.lower()
    assert encrypted().user_message != unreadable().user_message


def test_ac_m3_7_upload_too_large_message_names_the_limit() -> None:
    assert "10 MB" in _error("UploadTooLarge")().user_message


def test_upload_not_accepted_message_says_what_to_send() -> None:
    message = _error("UploadNotAccepted")().user_message.lower()

    assert "one file" in message
    assert "paste the text" in message


def test_document_errors_are_distinct_kinds() -> None:
    classes = [_error(name) for name in NEW_ERRORS]

    assert len({cls.kind for cls in classes}) == len(classes)
    assert len({cls().user_message for cls in classes}) == len(classes)
