"""The provider step's connection test (M1.18 review; ADR-052).

``ProviderStep.test`` runs one test at a time (a second one while a test runs is answered at
once, with no provider built), marks the step done only when the settings are still the version
that was tested, and keeps "Connected" in memory only when it was recorded. Failures are worded
for the chosen sign-in route; the outer timeout has its own words.

Settings live in a real SQLite database under ``tmp_path``; secrets in ``MemorySecretStore``;
providers are scripted stand-ins or the app-selectable fake. Every value is fictional.
"""

import asyncio
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.domain.errors import ProviderNotInstalled
from app.domain.redaction import Redactor
from app.domain.settings import AIProviderSettings
from app.modules.ai import AIProvider, ConnectionResult
from app.modules.secrets.providers.memory import MemorySecretStore
from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from app.onboarding.steps.provider import (
    MSG_BUSY,
    MSG_CHANGED,
    MSG_NOT_RECORDED,
    MSG_TIMEOUT,
    NOT_CONNECTED,
    ProviderStep,
)
from app.services.provider_setup import ProviderSetupService
from app.services.settings import SettingsService
from app.wiring import Wiring, build_wiring
from tests.fakes.clock import FrozenClock
from tests.fakes.keyring_backends import CountingKeyring

START = datetime(2026, 1, 3, 9, 0, 0, tzinfo=UTC)
EXISTING_KIND = "claude_existing_login"
FAKE_KIND = "fake"
TINY_TIMEOUT = 0.05


def fake_key(keyword: str) -> str:
    return "-".join([keyword, "FICTIONAL", "s118", "Hb6Ty2Qa"])


class ScriptedProvider:
    """Runs ``during`` inside its connection test, then answers ``Connected``; with
    ``hold`` it waits for the event first."""

    name = "scripted"

    def __init__(self, *, during: Any = None, hold: asyncio.Event | None = None) -> None:
        self._during = during
        self._hold = hold
        self.started = asyncio.Event()

    async def test_connection(self, model: str) -> ConnectionResult:
        self.started.set()
        if self._hold is not None:
            await self._hold.wait()
        if self._during is not None:
            self._during()
        return ConnectionResult(ok=True, message="Connected", model="scripted-model")


class HangingProvider:
    name = "hanging"

    async def test_connection(self, model: str) -> ConnectionResult:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover


class Factory:
    """A real wiring whose Claude providers are replaced by ``provider``; counts builds."""

    def __init__(self, provider: Any) -> None:
        self._wiring: Wiring = build_wiring(
            environ={"JOBPIPELINE_ALLOW_FAKE_PROVIDERS": "1"},
            demo=False,
            redactor=Redactor(),
            keyring_backend=CountingKeyring(),
        )
        self._provider = provider
        self.builds = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wiring, name)

    def ai_provider(self, settings: AIProviderSettings, *, data_dir: Path) -> AIProvider:
        self.builds += 1
        return self._provider  # type: ignore[no-any-return]

    def fake_for_key(self, key: str | None) -> Any:
        self.builds += 1
        return self._wiring.fake_for_key(key)


class Marks:
    def __init__(self, error: Exception | None = None) -> None:
        self.count = 0
        self._error = error

    def __call__(self) -> None:
        self.count += 1
        if self._error is not None:
            raise self._error


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database.open(tmp_path / "data" / "app.db")
    apply_migrations(database, MIGRATIONS_DIR, tmp_path / "backups", FrozenClock(START))
    try:
        yield database
    finally:
        database.close()


def build(
    db: Database,
    tmp_path: Path,
    provider: Any = None,
    *,
    marks: Marks | None = None,
    connection_timeout: float = 90,
) -> tuple[ProviderStep, ProviderSetupService, Factory, Marks]:
    factory = Factory(provider)
    service = ProviderSetupService(
        SettingsService(db, FrozenClock(START)),
        MemorySecretStore(),
        factory,  # type: ignore[arg-type]
        frozenset(),
        lambda: None,
        connection_timeout=connection_timeout,
    )
    marks = marks if marks is not None else Marks()
    step = ProviderStep(lambda: service, data_dir=lambda: tmp_path / "data", mark_done=marks)
    return step, service, factory, marks


def form(kind: str, credential: str = "") -> dict[str, list[str]]:
    return {"kind": [kind], "credential": [credential]}


async def test_connected_marks_done_and_is_cached(db: Database, tmp_path: Path) -> None:
    step, _, _, marks = build(db, tmp_path)
    check = await step.test(form(FAKE_KIND, fake_key("fake-connected")))
    assert check.ok is True
    assert check.message.startswith("Connected")
    assert marks.count == 1
    assert step.context()["connected"] == check.message


async def test_raising_mark_done_is_not_recorded_and_not_cached(
    db: Database, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    marks = Marks(RuntimeError("fictional: database is locked at /fictional/path"))
    step, _, _, _ = build(db, tmp_path, marks=marks)
    with caplog.at_level(logging.DEBUG):
        check = await step.test(form(FAKE_KIND, fake_key("fake-connected")))
    assert (check.ok, check.message) == (False, MSG_NOT_RECORDED)
    assert marks.count == 1
    assert step.context()["connected"] is None
    assert "RuntimeError" in caplog.text
    assert "fictional: database" not in caplog.text, "the error type only"


async def test_settings_changed_during_the_test_mark_nothing(db: Database, tmp_path: Path) -> None:
    """A save lands while the provider is being tested: what was tested is no longer what is
    saved, so nothing is marked or cached."""
    services: list[ProviderSetupService] = []
    provider = ScriptedProvider(during=lambda: services[0].save(FAKE_KIND, ""))
    step, service, _, marks = build(db, tmp_path, provider)
    services.append(service)

    check = await step.test(form(EXISTING_KIND))
    assert (check.ok, check.message) == (False, MSG_CHANGED)
    assert marks.count == 0
    assert step.context()["connected"] is None


async def test_a_second_test_while_one_runs_is_answered_at_once(
    db: Database, tmp_path: Path
) -> None:
    hold = asyncio.Event()
    provider = ScriptedProvider(hold=hold)
    step, _, factory, marks = build(db, tmp_path, provider)

    first = asyncio.create_task(step.test(form(EXISTING_KIND)))
    await asyncio.wait_for(provider.started.wait(), 5)
    assert factory.builds == 1

    second = await step.test(form(FAKE_KIND, fake_key("fake-connected")))
    assert (second.ok, second.message, dict(second.errors)) == (False, MSG_BUSY, {})
    assert factory.builds == 1, "no provider built for the second test"

    hold.set()
    done = await asyncio.wait_for(first, 5)
    assert done.ok is True
    assert marks.count == 1

    # Once the first has finished, a new test runs normally.
    again = await step.test(form(FAKE_KIND, fake_key("fake-connected")))
    assert again.ok is True


async def test_outer_timeout_has_its_own_words(db: Database, tmp_path: Path) -> None:
    step, _, _, marks = build(db, tmp_path, HangingProvider(), connection_timeout=TINY_TIMEOUT)
    check = await step.test(form(EXISTING_KIND))
    assert (check.ok, check.message) == (False, NOT_CONNECTED + MSG_TIMEOUT)
    assert marks.count == 0


async def test_fake_failures_keep_the_domain_words(db: Database, tmp_path: Path) -> None:
    step, _, _, _ = build(db, tmp_path)
    check = await step.test(form(FAKE_KIND, fake_key("fake-not-installed")))
    assert check.message == NOT_CONNECTED + ProviderNotInstalled().user_message
