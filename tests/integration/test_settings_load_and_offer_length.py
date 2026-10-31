"""M7.9 lookups: ``SettingsService.load`` takes no write lock and writes nothing, and
``session_offer_length`` falls back to the default length without raising and reads the setting
once per request.

A real migrated SQLite database in a temp folder, the frozen clock, no network; every value is
fictional.
"""

import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.api.templating import session_offer_length
from app.domain.settings import SESSIONS, SessionSettings
from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from app.services.session_preferences import SessionPreferences
from app.services.settings import SettingsService
from tests.fakes.clock import FrozenClock

pytestmark = pytest.mark.integration

WRITES = ("BEGIN IMMEDIATE", "INSERT", "UPDATE", "DELETE")


@pytest.fixture
def workspace(tmp_path):
    clock = FrozenClock(datetime(2026, 10, 20, 21, 59, tzinfo=UTC))
    with Database.open(tmp_path / "app.db") as db:
        apply_migrations(db, MIGRATIONS_DIR, tmp_path / "backups", clock)
        yield db, clock


def traced(db, action):
    statements: list[str] = []
    db.connection.set_trace_callback(statements.append)
    try:
        result = action()
    finally:
        db.connection.set_trace_callback(None)
    return result, statements


def writes_in(statements):
    return [s for s in statements if s.strip().upper().startswith(WRITES)]


def test_settings_load_of_a_missing_key_takes_no_write_lock_and_writes_nothing(workspace):
    db, clock = workspace
    loaded, statements = traced(db, lambda: SettingsService(db, clock).load(SESSIONS))
    assert (loaded.value, loaded.version) == (SessionSettings(), 0)
    assert statements, "the lookup should have run at least one statement"
    assert writes_in(statements) == []


def test_settings_load_of_a_stored_key_takes_no_write_lock_and_writes_nothing(workspace):
    db, clock = workspace
    service = SettingsService(db, clock)
    stored = SessionSettings(frequency="twice_weekly", length=5)
    service.save(SESSIONS, stored, expected_version=0)
    loaded, statements = traced(db, lambda: service.load(SESSIONS))
    assert (loaded.value, loaded.version) == (stored, 1)
    assert writes_in(statements) == []


def test_settings_load_succeeds_while_another_connection_holds_the_write_lock(workspace):
    db, clock = workspace
    service = SettingsService(db, clock)
    service.save(SESSIONS, SessionSettings(length=5), expected_version=0)
    other = sqlite3.connect(db.path, isolation_level=None, timeout=0)
    try:
        other.execute("BEGIN IMMEDIATE")
        loaded = service.load(SESSIONS)
        assert (loaded.value.length, loaded.version) == (5, 1)
    finally:
        other.execute("ROLLBACK")
        other.close()


def request_for(**app_state):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(**app_state)), state=SimpleNamespace()
    )


def test_session_offer_length_falls_back_when_the_workspace_is_closed():
    preferences = SessionPreferences(lambda: None, FrozenClock(datetime(2026, 10, 20, tzinfo=UTC)))
    request = request_for(session_preferences=preferences)
    assert session_offer_length(request) == SessionSettings().length == 3
    assert request.state.session_offer_length == 3


def test_session_offer_length_falls_back_without_session_preferences_on_the_app():
    request = request_for()
    assert session_offer_length(request) == 3


def test_session_offer_length_reads_the_stored_setting_once_per_request(workspace):
    db, clock = workspace
    SettingsService(db, clock).save(SESSIONS, SessionSettings(length=5), expected_version=0)
    preferences = SessionPreferences(lambda: db, clock)
    request = request_for(session_preferences=preferences)

    first, statements = traced(db, lambda: session_offer_length(request))
    assert first == 5
    assert statements, "the first call should read the setting"

    again, statements = traced(db, lambda: [session_offer_length(request) for _ in range(5)])
    assert again == [5] * 5
    assert statements == []
    # Another request reads again, so a saved change shows on the next page.
    assert session_offer_length(request_for(session_preferences=preferences)) == 5
