"""Settings service on a real SQLite database (M1.7; spec 2.7, 2.8, 2.9.9, 3.4, 3.5).

Typed values are stored as JSON in the ``settings`` table with optimistic concurrency.
Every value here is fictional.
"""

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.domain.errors import VersionConflict
from app.domain.settings import (
    AI_PROVIDER,
    JOB_PREFERENCES,
    AIProviderSettings,
    JobPreferences,
    Salary,
    TierMap,
)
from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from app.modules.storage.settings_repo import (
    get_setting,
    insert_setting,
    update_setting,
)
from app.services.settings import SettingsService, Versioned
from tests.fakes.clock import FrozenClock

pytestmark = pytest.mark.integration

START = datetime(2026, 1, 3, 9, 0, 0, tzinfo=UTC)
LATER = START + timedelta(hours=2, minutes=15)


# --- fixtures and helpers ---------------------------------------------------------


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(START)


@pytest.fixture
def db(tmp_path: Path, clock: FrozenClock) -> Iterator[Database]:
    database = Database.open(tmp_path / "data" / "app.db")
    apply_migrations(database, MIGRATIONS_DIR, tmp_path / "backups", clock)
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def service(db: Database, clock: FrozenClock) -> SettingsService:
    return SettingsService(db, clock)


def row(db: Database, key: str) -> tuple[Any, ...] | None:
    found: tuple[Any, ...] | None = db.connection.execute(
        "SELECT value, created_at, updated_at, version FROM settings WHERE key = ?", (key,)
    ).fetchone()
    return found


def all_settings_text(db: Database) -> str:
    rows = db.connection.execute(
        "SELECT key, value, created_at, updated_at, version FROM settings"
    ).fetchall()
    return "\n".join(" ".join(str(cell) for cell in r) for r in rows)


def write_raw(db: Database, key: str, value: str, version: int = 1) -> None:
    stamp = START.isoformat()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO settings (key, value, created_at, updated_at, version)"
            " VALUES (?, ?, ?, ?, ?)",
            (key, value, stamp, stamp, version),
        )


def as_utc_instant(text: str) -> datetime:
    parsed = datetime.fromisoformat(text)
    assert parsed.utcoffset() == timedelta(0), text
    return parsed


def provider_settings() -> AIProviderSettings:
    return AIProviderSettings(
        kind="claude_subscription_token",
        tiers=TierMap(fast="model-fast-fictional", thorough="model-thorough-fictional"),
        task_tiers={"assess_fit": "thorough"},
    )


def preferences() -> JobPreferences:
    return JobPreferences(
        target_roles=("Lighthouse Keeper", "Tide Analyst"),
        locations=("Atlantis Harbour",),
        work_modes=("remote",),
        minimum_salary=Salary(amount=42000, currency="EUR"),
        must_haves=("Sea view",),
        deal_breakers=("Night shifts on land",),
        cv_country="PT",
    )


# --- tests ---------------------------------------------------------------------


def test_save_and_load_typed_setting(service: SettingsService, db: Database) -> None:
    saved = service.save(AI_PROVIDER, provider_settings(), expected_version=0)
    assert saved == Versioned(provider_settings(), 1)

    loaded = service.load(AI_PROVIDER)
    assert loaded.value == provider_settings()
    assert isinstance(loaded.value, AIProviderSettings)
    assert loaded.version == 1

    stored = row(db, "ai_provider")
    assert stored is not None
    assert AIProviderSettings.model_validate(json.loads(stored[0])) == provider_settings()

    changed = AIProviderSettings(kind="claude_existing_login")
    updated = service.save(AI_PROVIDER, changed, expected_version=1)
    assert updated.version == 2
    assert updated.value == changed
    assert service.load(AI_PROVIDER) == Versioned(changed, 2)

    service.save(JOB_PREFERENCES, preferences(), expected_version=0)
    assert service.load(JOB_PREFERENCES) == Versioned(preferences(), 1)
    assert service.load(AI_PROVIDER) == Versioned(changed, 2)


def test_load_missing_returns_default_with_version_zero(service: SettingsService) -> None:
    assert service.load(AI_PROVIDER) == Versioned(AIProviderSettings(), 0)
    assert service.load(JOB_PREFERENCES) == Versioned(None, 0)


def test_save_with_stale_version_raises_version_conflict(
    service: SettingsService, db: Database
) -> None:
    first = provider_settings()
    service.save(AI_PROVIDER, first, expected_version=0)
    service.save(AI_PROVIDER, AIProviderSettings(kind="claude_api_key"), expected_version=1)
    before = row(db, "ai_provider")

    with pytest.raises(VersionConflict):
        service.save(AI_PROVIDER, AIProviderSettings(kind="fake"), expected_version=1)
    with pytest.raises(VersionConflict):
        service.save(AI_PROVIDER, AIProviderSettings(kind="fake"), expected_version=0)
    with pytest.raises(VersionConflict):
        service.save(AI_PROVIDER, AIProviderSettings(kind="fake"), expected_version=7)

    assert row(db, "ai_provider") == before
    assert service.load(AI_PROVIDER) == Versioned(AIProviderSettings(kind="claude_api_key"), 2)

    # Updating a key that was never stored is also a conflict, and stores nothing.
    with pytest.raises(VersionConflict):
        service.save(JOB_PREFERENCES, preferences(), expected_version=1)
    assert row(db, "job_preferences") is None


def test_invalid_stored_json_falls_back_to_default_and_logs(
    service: SettingsService, db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    # Built at run time so secret scanners don't flag a fictional value (no allowlist).
    leak_marker = "fictional-" + "leak-marker-" + "91"
    stored_text = json.dumps({"kind": "teleporter-kind", "api_key": leak_marker})
    write_raw(db, "ai_provider", stored_text, version=3)
    write_raw(db, "job_preferences", json.dumps(["Harbour", "Lagoon"]), version=2)
    caplog.set_level(logging.WARNING)

    assert service.load(AI_PROVIDER) == Versioned(AIProviderSettings(), 3)
    assert service.load(JOB_PREFERENCES) == Versioned(None, 2)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    messages = [r.getMessage() for r in warnings]
    assert any("ai_provider" in m and "ValidationError" in m for m in messages), messages
    assert any("job_preferences" in m and "ValidationError" in m for m in messages), messages
    for forbidden in ("teleporter-kind", leak_marker, "Harbour", "Lagoon"):
        assert forbidden not in caplog.text
        for record in caplog.records:
            assert forbidden not in record.getMessage()
            assert forbidden not in repr(record.args)

    # Loading never rewrites the stored row.
    stored = row(db, "ai_provider")
    assert stored is not None
    assert stored[0] == stored_text
    assert stored[3] == 3


def test_settings_uses_injected_clock(
    service: SettingsService, db: Database, clock: FrozenClock
) -> None:
    service.save(AI_PROVIDER, provider_settings(), expected_version=0)
    stored = row(db, "ai_provider")
    assert stored is not None
    assert as_utc_instant(stored[1]) == START
    assert as_utc_instant(stored[2]) == START

    clock.advance(LATER - START)
    service.save(AI_PROVIDER, AIProviderSettings(kind="fake"), expected_version=1)
    stored = row(db, "ai_provider")
    assert stored is not None
    assert as_utc_instant(stored[1]) == START
    assert as_utc_instant(stored[2]) == LATER
    assert stored[3] == 2


def test_save_then_overwrite_after_invalid_row(service: SettingsService, db: Database) -> None:
    write_raw(db, "ai_provider", json.dumps({"kind": "teleporter-kind"}), version=4)

    fallback = service.load(AI_PROVIDER)
    assert fallback == Versioned(AIProviderSettings(), 4)

    saved = service.save(AI_PROVIDER, provider_settings(), expected_version=fallback.version)
    assert saved == Versioned(provider_settings(), 5)
    assert service.load(AI_PROVIDER) == Versioned(provider_settings(), 5)


def test_ac_m1_4_secret_never_stored_in_settings(service: SettingsService, db: Database) -> None:
    # Built at run time so no key-shaped literal sits in the repository.
    secret = "-".join(["sk", "ant", "fictional", "Q7wz" * 6])

    for field in ("api_key", "token", "secret", "credential"):
        with pytest.raises(ValidationError):
            AIProviderSettings.model_validate({"kind": "claude_api_key", field: secret})

    service.save(AI_PROVIDER, AIProviderSettings(kind="claude_api_key"), expected_version=0)
    service.save(JOB_PREFERENCES, preferences(), expected_version=0)

    text = all_settings_text(db)
    assert text
    assert secret not in text
    assert secret[-12:] not in text


# --- repository interface ------------------------------------------------------


def test_settings_repo_insert_get_update(db: Database) -> None:
    stamp = START.isoformat()
    later = LATER.isoformat()
    with db.transaction() as conn:
        assert get_setting(conn, "ai_provider") is None
        insert_setting(conn, "ai_provider", '{"kind": "fake"}', stamp)

    with db.transaction() as conn:
        stored = get_setting(conn, "ai_provider")
    assert stored is not None
    assert stored.key == "ai_provider"
    assert json.loads(stored.value) == {"kind": "fake"}
    assert stored.version == 1
    assert stored.updated_at == stamp

    with db.transaction() as conn:
        assert update_setting(conn, "ai_provider", "{}", expected_version=2, now=later) is False
        assert update_setting(conn, "ai_provider", "{}", expected_version=1, now=later) is True
        assert update_setting(conn, "missing_key", "{}", expected_version=1, now=later) is False

    with db.transaction() as conn:
        stored = get_setting(conn, "ai_provider")
    assert stored is not None
    assert stored.value == "{}"
    assert stored.version == 2
    assert stored.updated_at == later
