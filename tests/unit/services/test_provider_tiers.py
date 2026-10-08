"""Provider setup service: saving the tier map when the setting changes meanwhile (M1.22
review).

``save_tiers`` saves through the version-checked update with one retry. A conflict whose
retry finds the same provider keeps the concurrent change and adds the new map; one whose
retry finds another provider (Claude to the fake) is refused on the ``tiers`` field with its
own message and saves nothing; a save that fails outright gives the generic message. Settings
live in a real SQLite database under ``tmp_path``; every value is fictional.
"""

import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from app.domain.settings import AI_PROVIDER, AIProviderSettings, TierMap
from app.modules.ai.providers.claude_sdk import CLAUDE_FAST_DEFAULT, CLAUDE_MOST_CAPABLE
from app.modules.secrets.providers.memory import MemorySecretStore
from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from app.services.provider_setup import (
    MSG_TIERS_PROVIDER_CHANGED,
    MSG_TIERS_SAVE_FAILED,
    MSG_TIERS_SAVED,
    ProviderSetupService,
)
from app.services.settings import SettingsService, Versioned
from tests.fakes.clock import FrozenClock
from tests.unit.services.test_provider_test import START, Refreshes, ScriptedFactory

API_KEY_KIND = "claude_api_key"
TOKEN_KIND = "claude_subscription_token"  # noqa: S105 - a provider kind, not a secret
SERVICE_LOGGER = "app.services.provider_setup"


class RacingSettings(SettingsService):
    """Before the first save, ``meanwhile`` rewrites the setting (so that save meets a version
    conflict); or every save raises ``failure``."""

    def __init__(
        self,
        db: Database,
        *,
        meanwhile: Callable[[AIProviderSettings], AIProviderSettings] | None = None,
        failure: Exception | None = None,
    ) -> None:
        super().__init__(db, FrozenClock(START))
        self._meanwhile = meanwhile
        self._failure = failure
        self.saves = 0

    def save(self, spec: Any, value: Any, *, expected_version: int) -> Versioned[Any]:
        self.saves += 1
        if self._failure is not None:
            raise self._failure
        if self._meanwhile is not None:
            meanwhile, self._meanwhile = self._meanwhile, None
            loaded = super().load(AI_PROVIDER)
            super().save(AI_PROVIDER, meanwhile(loaded.value), expected_version=loaded.version)
        return super().save(spec, value, expected_version=expected_version)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database.open(tmp_path / "data" / "app.db")
    apply_migrations(database, MIGRATIONS_DIR, tmp_path / "backups", FrozenClock(START))
    try:
        yield database
    finally:
        database.close()


def seed(db: Database, value: AIProviderSettings) -> None:
    SettingsService(db, FrozenClock(START)).save(AI_PROVIDER, value, expected_version=0)


def stored(db: Database) -> Versioned[AIProviderSettings]:
    return SettingsService(db, FrozenClock(START)).load(AI_PROVIDER)


def service(settings: SettingsService) -> ProviderSetupService:
    return ProviderSetupService(
        settings,
        MemorySecretStore(),
        ScriptedFactory(),  # type: ignore[arg-type]
        frozenset(),
        Refreshes(),
    )


def test_retry_with_the_same_provider_keeps_the_concurrent_change(db: Database) -> None:
    seed(db, AIProviderSettings(kind=API_KEY_KIND))
    settings = RacingSettings(
        db,
        meanwhile=lambda value: value.model_copy(update={"saved_sign_ins": (TOKEN_KIND,)}),
    )
    result = service(settings).save_tiers(CLAUDE_MOST_CAPABLE, CLAUDE_FAST_DEFAULT)
    assert result.ok, result.errors
    assert result.message == MSG_TIERS_SAVED
    assert settings.saves == 2, "saved on the retry"
    assert stored(db).value == AIProviderSettings(
        kind=API_KEY_KIND,
        saved_sign_ins=(TOKEN_KIND,),
        tiers=TierMap(fast=CLAUDE_MOST_CAPABLE, thorough=CLAUDE_FAST_DEFAULT),
    )


def test_retry_after_a_provider_change_is_refused(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    seed(db, AIProviderSettings(kind=API_KEY_KIND))
    settings = RacingSettings(db, meanwhile=lambda value: value.model_copy(update={"kind": "fake"}))
    tiers = service(settings)
    with caplog.at_level(logging.INFO, logger=SERVICE_LOGGER):
        result = tiers.save_tiers(CLAUDE_MOST_CAPABLE, CLAUDE_FAST_DEFAULT)
    assert not result.ok
    assert dict(result.errors) == {"tiers": MSG_TIERS_PROVIDER_CHANGED}
    after = stored(db)
    assert after.value == AIProviderSettings(kind="fake"), "only the concurrent change"
    assert after.version == 2
    service_records = [r for r in caplog.records if r.name == SERVICE_LOGGER]
    assert service_records, "the race is logged"
    assert all(r.levelno < logging.WARNING for r in service_records), "a normal race"
    assert CLAUDE_MOST_CAPABLE not in caplog.text and CLAUDE_FAST_DEFAULT not in caplog.text


def test_failed_save_gives_the_generic_message(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    seed(db, AIProviderSettings(kind=API_KEY_KIND))
    settings = RacingSettings(db, failure=RuntimeError("fictional database failure"))
    with caplog.at_level(logging.INFO):
        result = service(settings).save_tiers(CLAUDE_MOST_CAPABLE, CLAUDE_FAST_DEFAULT)
    assert not result.ok
    assert dict(result.errors) == {"tiers": MSG_TIERS_SAVE_FAILED}
    assert stored(db) == Versioned(AIProviderSettings(kind=API_KEY_KIND), 1)
    assert CLAUDE_MOST_CAPABLE not in caplog.text
