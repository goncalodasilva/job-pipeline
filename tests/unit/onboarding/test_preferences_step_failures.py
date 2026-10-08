"""Step 5 of the setup guide: storage failures and salary separators (M1.19 review).

``PreferencesStep`` (see ``test_preferences_step.py`` for the interface) saves with the stored
version and retries once on ``VersionConflict``. Any failure answers a plain ``form`` message;
logs name the error type only, never the values typed or the error's text. When the save
committed but ``mark_done`` fails, the message says the preferences were saved. No-break spaces
(U+00A0, U+202F) are accepted as thousands separators, like spaces and commas.

Settings live in a real SQLite database under ``tmp_path``; every value is fictional.
"""

import logging
import sqlite3
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.domain.errors import VersionConflict
from app.domain.settings import JOB_PREFERENCES, JobPreferences, Salary
from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from app.onboarding.steps import preferences as preferences_module
from app.onboarding.steps.preferences import (
    MSG_NOT_MOVED_ON,
    MSG_SAVE_FAILED,
    PreferencesStep,
)
from app.services.settings import SettingsService
from tests.fakes.clock import FrozenClock

START = datetime(2026, 1, 3, 9, 0, 0, tzinfo=UTC)
FICTIONAL_ROLE = "Fictional Archivist"
PRIVATE_PATH = "/Users/fictional-person/private/app.db"
NBSP = chr(0xA0)  # no-break space
NNBSP = chr(0x202F)  # narrow no-break space
OTHER = JobPreferences(target_roles=("Fictional Cartographer",), cv_country="DE")


class Marks:
    def __init__(self, error: Exception | None = None) -> None:
        self.count = 0
        self._error = error

    def __call__(self) -> None:
        self.count += 1
        if self._error is not None:
            raise self._error


class FlakySettings(SettingsService):
    """Saving raises the given errors in turn, then saves for real."""

    def __init__(self, db: Database, errors: Sequence[Exception]) -> None:
        super().__init__(db, FrozenClock(START))
        self._errors = list(errors)
        self.saves = 0

    def save(self, spec, value, *, expected_version):  # type: ignore[no-untyped-def,override]
        self.saves += 1
        if self._errors:
            error = self._errors.pop(0)
            if isinstance(error, VersionConflict):
                # Someone else saved meanwhile, so the next load sees a newer version.
                version = super().load(JOB_PREFERENCES).version
                super().save(JOB_PREFERENCES, OTHER, expected_version=version)
            raise error
        return super().save(spec, value, expected_version=expected_version)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database.open(tmp_path / "data" / "app.db")
    apply_migrations(database, MIGRATIONS_DIR, tmp_path / "backups", FrozenClock(START))
    try:
        yield database
    finally:
        database.close()


def valid_form(**overrides: Sequence[str]) -> dict[str, Sequence[str]]:
    form: dict[str, Sequence[str]] = {
        "target_roles": [FICTIONAL_ROLE],
        "salary_amount": ["45000"],
        "salary_currency": ["EUR"],
        "cv_country": ["PT"],
    }
    form.update(overrides)
    return form


def stored(db: Database) -> JobPreferences | None:
    return SettingsService(db, FrozenClock(START)).load(JOB_PREFERENCES).value


def assert_log_names_type_only(caplog: pytest.LogCaptureFixture, type_name: str) -> None:
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert type_name in text, text
    for value in (FICTIONAL_ROLE, PRIVATE_PATH, "45000", "EUR", "PT"):
        assert value not in text, value
    assert all(record.exc_info is None for record in caplog.records), "no tracebacks"


def test_save_failure_answers_plain_message_and_logs_type_only(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    settings = FlakySettings(db, [sqlite3.OperationalError(f"disk I/O error: {PRIVATE_PATH}")])
    marks = Marks()
    with caplog.at_level(logging.INFO):
        result = PreferencesStep(lambda: settings, mark_done=marks).submit(valid_form())
    assert result.ok is False
    assert dict(result.errors) == {"form": MSG_SAVE_FAILED}
    assert marks.count == 0
    assert stored(db) is None
    assert_log_names_type_only(caplog, "OperationalError")


def test_generic_exception_answers_plain_message_and_logs_type_only(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    settings = FlakySettings(db, [RuntimeError(f"{FICTIONAL_ROLE} at {PRIVATE_PATH}")])
    marks = Marks()
    with caplog.at_level(logging.INFO):
        result = PreferencesStep(lambda: settings, mark_done=marks).submit(valid_form())
    assert dict(result.errors) == {"form": MSG_SAVE_FAILED}
    assert marks.count == 0
    assert stored(db) is None
    assert_log_names_type_only(caplog, "RuntimeError")


def test_version_conflict_once_is_retried_and_saved(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    settings = FlakySettings(db, [VersionConflict(detail="setting changed elsewhere")])
    marks = Marks()
    with caplog.at_level(logging.INFO):
        result = PreferencesStep(lambda: settings, mark_done=marks).submit(valid_form())
    assert result.ok is True, dict(result.errors)
    assert settings.saves == 2
    assert marks.count == 1
    assert stored(db) == JobPreferences(
        target_roles=(FICTIONAL_ROLE,),
        minimum_salary=Salary(amount=45000, currency="EUR"),
        cv_country="PT",
    )
    assert_log_names_type_only(caplog, "retrying once")


def test_version_conflict_twice_gives_up(db: Database, caplog: pytest.LogCaptureFixture) -> None:
    conflicts = [VersionConflict(detail="changed"), VersionConflict(detail="changed again")]
    settings = FlakySettings(db, conflicts)
    marks = Marks()
    with caplog.at_level(logging.INFO):
        result = PreferencesStep(lambda: settings, mark_done=marks).submit(valid_form())
    assert dict(result.errors) == {"form": MSG_SAVE_FAILED}
    assert settings.saves == 2
    assert marks.count == 0
    assert stored(db) == OTHER, "the other save is kept; this one was not written"
    assert_log_names_type_only(caplog, "VersionConflict")


def test_mark_done_failure_says_the_preferences_were_saved(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    settings = SettingsService(db, FrozenClock(START))
    marks = Marks(sqlite3.OperationalError(f"database is locked: {PRIVATE_PATH}"))
    with caplog.at_level(logging.INFO):
        result = PreferencesStep(lambda: settings, mark_done=marks).submit(valid_form())
    assert result.ok is False
    assert dict(result.errors) == {"form": MSG_NOT_MOVED_ON}
    assert "saved" in MSG_NOT_MOVED_ON and "couldn't be saved" not in MSG_NOT_MOVED_ON
    assert marks.count == 1
    assert stored(db) is not None, "the save committed"
    assert_log_names_type_only(caplog, "OperationalError")


def test_model_validation_error_answers_plain_message_and_logs_type_only(
    db: Database, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Should building the model ever refuse what the step accepted, pydantic's text (which
    repeats the values) never reaches the logs or the page."""

    def refuse(**values: object) -> JobPreferences:
        return JobPreferences.model_validate({**values, "cv_country": f"{FICTIONAL_ROLE}"})

    monkeypatch.setattr(preferences_module, "JobPreferences", refuse)
    settings = SettingsService(db, FrozenClock(START))
    marks = Marks()
    with caplog.at_level(logging.INFO):
        result = PreferencesStep(lambda: settings, mark_done=marks).submit(valid_form())
    assert dict(result.errors) == {"form": MSG_SAVE_FAILED}
    assert marks.count == 0
    assert stored(db) is None
    assert_log_names_type_only(caplog, "ValidationError")


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        pytest.param(f"45{NBSP}000", 45000, id="no-break-space"),
        pytest.param(f"1{NNBSP}250{NNBSP}000", 1250000, id="narrow-no-break-space"),
        pytest.param(f"{NBSP}45 000,{NNBSP}", 45000, id="mixed"),
    ],
)
def test_salary_amount_accepts_no_break_spaces(db: Database, amount: str, expected: int) -> None:
    settings = SettingsService(db, FrozenClock(START))
    result = PreferencesStep(lambda: settings, mark_done=Marks()).submit(
        valid_form(salary_amount=[amount])
    )
    assert result.ok is True, dict(result.errors)
    saved = stored(db)
    assert saved is not None
    assert saved.minimum_salary == Salary(amount=expected, currency="EUR")
