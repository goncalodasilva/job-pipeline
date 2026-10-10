"""Provider setup service: the connection test (M1.18; AC-M1.3, AC-M1.5; ADR-044 item 6,
ADR-058).

``await ProviderSetupService.test_connection(kind, credential, *, data_dir)`` first saves like
``save`` (a refused save returns its field errors and builds no provider), then tests the saved
configuration: the fake from ``factory.fake_for_key(credential)`` (the pasted text only picks
the outcome; never stored or logged), a Claude kind from ``factory.ai_provider(settings,
data_dir=...)``. The model is ``factory.connection_model(settings)``: the settings' Fast tier
model, else the provider's Fast default. The call is awaited with
``asyncio.wait_for(..., CONNECTION_TEST_TIMEOUT)`` (90 s; the provider has its own 60 s); a
timeout reads as ``NetworkUnavailable``'s message and any unexpected exception as a generic
"didn't finish" message, logged by error type only. Blocking work (the save, settings reads)
runs in worker threads, never on the event loop. The result is a ``ProviderTestResult``
(``ok``, ``message``, ``errors``, ``model``).

New names are read from the module (``provider_setup.ProviderTestResult``) so each test fails
on its own until the behaviour exists. Settings live in a real SQLite database under
``tmp_path``; secrets in ``MemorySecretStore``; providers are scripted stand-ins or the
app-selectable fake. Every value is fictional and built at run time.
"""

import asyncio
import inspect
import logging
import threading
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import pytest
from pydantic import BaseModel

from app.domain.errors import (
    NetworkUnavailable,
    ProviderAuthError,
    UsageLimitReached,
)
from app.domain.redaction import Redactor
from app.domain.settings import (
    AI_PROVIDER,
    AIProviderSettings,
    OptionalSettingSpec,
    ProviderKind,
    SettingSpec,
    Tier,
    TierMap,
)
from app.modules.ai import AIProvider, ConnectionResult
from app.modules.ai.providers.claude_sdk import (
    CLAUDE_FAST_DEFAULT,
    CONNECTION_TIMEOUT_SECONDS,
)
from app.modules.ai.providers.fake import FAKE_FAST_MODEL
from app.modules.secrets.providers.memory import MemorySecretStore
from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from app.services import provider_setup
from app.services.provider_setup import ProviderSetupService
from app.services.settings import SettingsService, Versioned
from app.wiring import Wiring, build_wiring
from tests.fakes.clock import FrozenClock
from tests.fakes.keyring_backends import CountingKeyring
from tests.fakes.usage import CountingUsage

START = datetime(2026, 1, 3, 9, 0, 0, tzinfo=UTC)
TOKEN_KIND = "claude_subscription_token"  # noqa: S105 - a provider kind, not a secret
API_KEY_KIND = "claude_api_key"
EXISTING_KIND = "claude_existing_login"
FAKE_KIND = "fake"
TOKEN_KEY = "claude_code_oauth_token"  # noqa: S105 - a key name, not a secret
GENERIC_FAILURE = "The connection test didn't finish. Nothing was lost; try again."
TINY_TIMEOUT = 0.05  # seconds; the hanging provider never answers
REPORTED_MODEL = "claude-haiku-fictional"


def anthropic_shaped(kind: str, body: str) -> str:
    """An Anthropic-shaped fictional credential, built at run time."""
    return "-".join(["sk", "ant", kind, body])


FICTIONAL_TOKEN = anthropic_shaped("oat01", "FICTIONALunitM118Tq7Wv3Kp9Xr2")


def fake_key(keyword: str) -> str:
    return "-".join([keyword, "FICTIONAL", "u118", "Kd2Rv8Wz"])


# --- stand-ins ---------------------------------------------------------------------------------


class RecordingProvider:
    """Answers the connection test with ``result``; records the models asked for."""

    name = "recording"

    def __init__(self, result: ConnectionResult) -> None:
        self._result = result
        self.models: list[str] = []

    async def test_connection(self, model: str) -> ConnectionResult:
        self.models.append(model)
        return self._result


class HangingProvider:
    """Never answers until cancelled (a provider stuck past its own timeout)."""

    name = "hanging"

    def __init__(self) -> None:
        self.started = False
        self.cancelled = False

    async def test_connection(self, model: str) -> ConnectionResult:
        self.started = True
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")  # pragma: no cover


class RaisingProvider:
    """Breaks its contract: raises instead of returning a result. The error text holds a
    credential, so it must never reach a log or a result."""

    name = "raising"

    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def test_connection(self, model: str) -> ConnectionResult:
        raise self._error


class ScriptedFactory:
    """A real wiring (fakes allowed, test keyring) whose built providers are recorded and,
    when ``provider`` is given, replaced by it."""

    def __init__(self, provider: Any | None = None, *, demo: bool = False) -> None:
        self._wiring: Wiring = build_wiring(
            environ={"JOBPIPELINE_ALLOW_FAKE_PROVIDERS": "1"},
            demo=demo,
            redactor=Redactor(),
            keyring_backend=CountingKeyring(),
        )
        self._provider = provider
        self.built: list[tuple[AIProviderSettings, Path]] = []
        self.fakes = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wiring, name)

    def ai_provider(self, settings: AIProviderSettings, *, data_dir: Path) -> AIProvider:
        built = self._wiring.ai_provider(settings, data_dir=data_dir)
        self.built.append((settings, data_dir))
        return self._provider if self._provider is not None else built  # type: ignore[no-any-return]

    def fake_for_key(self, key: str | None) -> Any:
        self.fakes += 1
        built = self._wiring.fake_for_key(key)
        return self._provider if self._provider is not None else built

    def default_models(self, kind: ProviderKind | None) -> Mapping[Tier, str]:
        return self._wiring.default_models(kind)


M = TypeVar("M", bound=BaseModel)


class ThreadRecordingSettings(SettingsService):
    """Records the thread of every settings read and write."""

    def __init__(self, db: Database) -> None:
        super().__init__(db, FrozenClock(START))
        self.threads: list[int] = []

    def load(self, spec: Any) -> Any:
        self.threads.append(threading.get_ident())
        return super().load(spec)

    def save(
        self,
        spec: SettingSpec[M] | OptionalSettingSpec[M],
        value: M,
        *,
        expected_version: int,
    ) -> Versioned[M]:
        self.threads.append(threading.get_ident())
        return super().save(spec, value, expected_version=expected_version)


class Refreshes:
    def __init__(self) -> None:
        self.count = 0
        self.threads: list[int] = []

    def __call__(self) -> None:
        self.count += 1
        self.threads.append(threading.get_ident())


# --- fixtures ---------------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database.open(tmp_path / "data" / "app.db")
    apply_migrations(database, MIGRATIONS_DIR, tmp_path / "backups", FrozenClock(START))
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def settings(db: Database) -> SettingsService:
    return SettingsService(db, FrozenClock(START))


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "data"
    folder.joinpath("tmp", "ai").mkdir(parents=True, exist_ok=True)
    return folder


def service(
    settings: SettingsService,
    factory: ScriptedFactory,
    *,
    secrets: MemorySecretStore | None = None,
    refresh: Refreshes | None = None,
    **kwargs: Any,
) -> ProviderSetupService:
    return ProviderSetupService(
        settings,
        secrets if secrets is not None else MemorySecretStore(),
        factory,  # type: ignore[arg-type]
        frozenset(),
        refresh if refresh is not None else Refreshes(),
        usage=CountingUsage(),
        **kwargs,
    )


def current(settings: SettingsService) -> AIProviderSettings:
    return settings.load(AI_PROVIDER).value


# --- the constant and the result type ---------------------------------------------------------


def test_connection_test_timeout_is_longer_than_the_providers_own() -> None:
    timeout = provider_setup.CONNECTION_TEST_TIMEOUT
    assert timeout == 90
    assert timeout > CONNECTION_TIMEOUT_SECONDS, "the provider answers first in normal cases"
    parameter = inspect.signature(ProviderSetupService).parameters["connection_timeout"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default == timeout


async def test_connected_result_carries_the_reported_model(
    settings: SettingsService, data_dir: Path
) -> None:
    provider = RecordingProvider(ConnectionResult(ok=True, message="Connected", model="m-7"))
    result = await service(settings, ScriptedFactory(provider)).test_connection(
        EXISTING_KIND, "", data_dir=data_dir
    )
    assert isinstance(result, provider_setup.ProviderTestResult)
    assert result.ok is True
    assert result.message == "Connected"
    assert result.model == "m-7"
    assert dict(result.errors) == {}


# --- the model -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        pytest.param(AIProviderSettings(kind=API_KEY_KIND), CLAUDE_FAST_DEFAULT, id="claude"),
        pytest.param(AIProviderSettings(kind=FAKE_KIND), FAKE_FAST_MODEL, id="fake"),
        pytest.param(
            AIProviderSettings(kind=TOKEN_KIND, tiers=TierMap(fast="fictional-fast-model")),
            "fictional-fast-model",
            id="settings_fast_tier",
        ),
        pytest.param(
            AIProviderSettings(kind=TOKEN_KIND, tiers=TierMap(thorough="fictional-thorough")),
            CLAUDE_FAST_DEFAULT,
            id="thorough_set_only",
        ),
    ],
)
def test_wiring_connection_model_is_the_fast_tier(
    stored: AIProviderSettings, expected: str
) -> None:
    wiring = build_wiring(
        environ={"JOBPIPELINE_ALLOW_FAKE_PROVIDERS": "1"},
        demo=False,
        redactor=Redactor(),
        keyring_backend=CountingKeyring(),
    )
    assert wiring.connection_model(stored) == expected


async def test_claude_kind_uses_the_fast_default_model(
    settings: SettingsService, data_dir: Path
) -> None:
    provider = RecordingProvider(ConnectionResult(ok=True, message="Connected", model="x"))
    factory = ScriptedFactory(provider)
    await service(settings, factory).test_connection(EXISTING_KIND, "", data_dir=data_dir)
    assert provider.models == [CLAUDE_FAST_DEFAULT]
    assert [(built.kind, folder) for built, folder in factory.built] == [(EXISTING_KIND, data_dir)]
    assert factory.fakes == 0


async def test_settings_fast_tier_model_is_used(settings: SettingsService, data_dir: Path) -> None:
    settings.save(
        AI_PROVIDER,
        AIProviderSettings(tiers=TierMap(fast="fictional-fast-model")),
        expected_version=0,
    )
    provider = RecordingProvider(ConnectionResult(ok=True, message="Connected", model="x"))
    factory = ScriptedFactory(provider)
    secrets = MemorySecretStore()
    result = await service(settings, factory, secrets=secrets).test_connection(
        TOKEN_KIND, FICTIONAL_TOKEN, data_dir=data_dir
    )
    assert result.ok is True, result
    assert provider.models == ["fictional-fast-model"]
    built, _ = factory.built[0]
    assert built.kind == TOKEN_KIND
    assert built.saved_sign_ins == (TOKEN_KIND,), "tested after the save"
    assert secrets.get(TOKEN_KEY) == FICTIONAL_TOKEN


async def test_fake_reports_its_fast_model(settings: SettingsService, data_dir: Path) -> None:
    factory = ScriptedFactory()
    result = await service(settings, factory).test_connection(
        FAKE_KIND, fake_key("fake-connected"), data_dir=data_dir
    )
    assert result.ok is True
    assert result.message == "Connected"
    assert result.model == FAKE_FAST_MODEL
    assert factory.fakes == 1 and factory.built == [], "the fake comes from the pasted key"


# --- outcomes ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("keyword", "message"),
    [
        pytest.param("fake-auth-rejected", ProviderAuthError().user_message, id="auth"),
        pytest.param("fake-usage-limit", UsageLimitReached().user_message, id="usage_limit"),
        pytest.param("fake-offline", NetworkUnavailable().user_message, id="network"),
        pytest.param("no-keyword", ProviderAuthError().user_message, id="unknown_key"),
    ],
)
async def test_fake_key_picks_the_mapped_message(
    settings: SettingsService,
    data_dir: Path,
    caplog: pytest.LogCaptureFixture,
    keyword: str,
    message: str,
) -> None:
    key = fake_key(keyword)
    secrets = MemorySecretStore()
    with caplog.at_level(logging.DEBUG):
        result = await service(settings, ScriptedFactory(), secrets=secrets).test_connection(
            FAKE_KIND, key, data_dir=data_dir
        )
    assert result.ok is False
    assert result.message == message
    assert result.model is None
    assert current(settings).kind == FAKE_KIND, "saved before testing"
    assert key not in repr(result)
    assert key not in caplog.text
    for record in caplog.records:
        assert key not in repr(record.args)
    assert secrets.get(TOKEN_KEY) is None


async def test_provider_failure_message_passes_through(
    settings: SettingsService, data_dir: Path
) -> None:
    failed = ConnectionResult(ok=False, message=UsageLimitReached().user_message)
    result = await service(settings, ScriptedFactory(RecordingProvider(failed))).test_connection(
        EXISTING_KIND, "", data_dir=data_dir
    )
    assert (result.ok, result.message, result.model) == (
        False,
        UsageLimitReached().user_message,
        None,
    )


async def test_timeout_reads_as_no_connection(
    settings: SettingsService, data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    provider = HangingProvider()
    setup = service(settings, ScriptedFactory(provider), connection_timeout=TINY_TIMEOUT)
    with caplog.at_level(logging.DEBUG):
        result = await setup.test_connection(EXISTING_KIND, "", data_dir=data_dir)
    assert provider.started is True
    assert provider.cancelled is True, "the hanging call is cancelled, not left running"
    assert result.ok is False
    assert result.message == NetworkUnavailable().user_message
    assert current(settings).kind == EXISTING_KIND, "the save stands"


async def test_unexpected_exception_reads_as_generic_message(
    settings: SettingsService, data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    leaked = f"fictional crash quoting {FICTIONAL_TOKEN}"
    setup = service(settings, ScriptedFactory(RaisingProvider(RuntimeError(leaked))))
    with caplog.at_level(logging.DEBUG):
        result = await setup.test_connection(TOKEN_KIND, FICTIONAL_TOKEN, data_dir=data_dir)
    assert result.ok is False
    assert result.message == GENERIC_FAILURE
    assert "RuntimeError" in caplog.text, "logged by error type"
    assert "fictional crash" not in caplog.text, "never the error text"
    assert FICTIONAL_TOKEN not in caplog.text
    for record in caplog.records:
        assert FICTIONAL_TOKEN not in repr(record.args)
        assert record.exc_info is None, "no traceback: it can quote the error text"
    assert FICTIONAL_TOKEN not in repr(result)


async def test_mapped_error_raised_by_a_provider_still_answers(
    settings: SettingsService, data_dir: Path
) -> None:
    """``test_connection`` should never raise mapped errors (ADR-058); if one does, the user
    still gets an answer rather than a crash."""
    setup = service(settings, ScriptedFactory(RaisingProvider(ProviderAuthError())))
    result = await setup.test_connection(EXISTING_KIND, "", data_dir=data_dir)
    assert result.ok is False
    assert result.message == ProviderAuthError().user_message
    assert result.failure == "auth"


async def test_provider_not_on_offer_reads_as_generic_message(
    settings: SettingsService, data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Wiring refuses to build a provider (``ValueError``) after the save: the user gets the
    generic message and the log names the error type only."""

    class RefusingFactory(ScriptedFactory):
        def ai_provider(self, settings: AIProviderSettings, *, data_dir: Path) -> AIProvider:
            raise ValueError(f"fictional refusal quoting {FICTIONAL_TOKEN}")

    with caplog.at_level(logging.DEBUG):
        result = await service(settings, RefusingFactory()).test_connection(
            TOKEN_KIND, FICTIONAL_TOKEN, data_dir=data_dir
        )
    assert (result.ok, result.message, result.failure) == (False, GENERIC_FAILURE, "other")
    assert "ValueError" in caplog.text
    assert "fictional refusal" not in caplog.text
    assert FICTIONAL_TOKEN not in caplog.text


async def test_result_reports_failure_kind_and_tested_version(
    settings: SettingsService, data_dir: Path
) -> None:
    """The step words failures per route and checks nothing changed while testing."""
    setup = service(settings, ScriptedFactory())
    offline = await setup.test_connection(FAKE_KIND, fake_key("fake-offline"), data_dir=data_dir)
    assert (offline.failure, offline.version) == ("network", setup.settings_version())
    limit = await setup.test_connection(FAKE_KIND, fake_key("fake-usage-limit"), data_dir=data_dir)
    assert limit.failure == "usage_limit"
    connected = await setup.test_connection(
        FAKE_KIND, fake_key("fake-connected"), data_dir=data_dir
    )
    assert (connected.failure, connected.version) == (None, setup.settings_version())

    hanging = service(settings, ScriptedFactory(HangingProvider()), connection_timeout=TINY_TIMEOUT)
    timed_out = await hanging.test_connection(EXISTING_KIND, "", data_dir=data_dir)
    assert timed_out.failure == "timeout"
    assert timed_out.message == NetworkUnavailable().user_message


async def test_failure_kind_comes_from_the_result_not_its_text(
    settings: SettingsService, data_dir: Path
) -> None:
    """ADR-063 follow-up: the service reads ``ConnectionResult.failure``; the message text is
    only shown, never matched."""
    worded = ConnectionResult(ok=False, message="Fictional provider wording.", failure="auth")
    result = await service(settings, ScriptedFactory(RecordingProvider(worded))).test_connection(
        EXISTING_KIND, "", data_dir=data_dir
    )
    assert (result.ok, result.message, result.failure) == (
        False,
        "Fictional provider wording.",
        "auth",
    )

    # A provider that says nothing about the kind is "other", whatever its text says.
    silent = ConnectionResult(ok=False, message=UsageLimitReached().user_message)
    result = await service(settings, ScriptedFactory(RecordingProvider(silent))).test_connection(
        EXISTING_KIND, "", data_dir=data_dir
    )
    assert (result.message, result.failure) == (UsageLimitReached().user_message, "other")


# --- a refused save runs no test ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "credential", "field"),
    [
        pytest.param(TOKEN_KIND, "FICTIONAL-short", "credential", id="bad_shape"),
        pytest.param(API_KEY_KIND, "", "credential", id="nothing_to_use"),
        pytest.param("openai", "", "kind", id="unknown_kind"),
    ],
)
async def test_refused_save_builds_no_provider(
    settings: SettingsService, data_dir: Path, kind: str, credential: str, field: str
) -> None:
    factory = ScriptedFactory()
    refresh = Refreshes()
    result = await service(settings, factory, refresh=refresh).test_connection(
        kind, credential, data_dir=data_dir
    )
    assert result.ok is False
    assert field in result.errors
    assert factory.built == [] and factory.fakes == 0
    assert current(settings).kind is None
    assert refresh.count == 0
    assert "FICTIONAL-short" not in repr(result)


async def test_fake_refused_while_fakes_are_off(settings: SettingsService, data_dir: Path) -> None:
    factory = ScriptedFactory()
    factory._wiring = build_wiring(
        environ={}, demo=False, redactor=Redactor(), keyring_backend=CountingKeyring()
    )
    result = await service(settings, factory).test_connection(
        FAKE_KIND, fake_key("fake-connected"), data_dir=data_dir
    )
    assert result.ok is False
    assert "kind" in result.errors
    assert factory.fakes == 0


# --- blocking work stays off the event loop ---------------------------------------------------


async def test_blocking_work_runs_off_the_event_loop(db: Database, data_dir: Path) -> None:
    loop_thread = threading.get_ident()
    recording = ThreadRecordingSettings(db)
    refresh = Refreshes()
    provider = RecordingProvider(ConnectionResult(ok=True, message="Connected", model="x"))
    result = await service(recording, ScriptedFactory(provider), refresh=refresh).test_connection(
        EXISTING_KIND, "", data_dir=data_dir
    )
    assert result.ok is True
    assert recording.threads, "settings were read and written"
    assert loop_thread not in recording.threads, "no database call on the event loop"
    assert refresh.threads and loop_thread not in refresh.threads


async def test_event_loop_stays_responsive_while_the_test_waits(
    settings: SettingsService, data_dir: Path
) -> None:
    """While the provider waits, other coroutines run (the wait is awaited, not blocking)."""
    provider = HangingProvider()
    setup = service(settings, ScriptedFactory(provider), connection_timeout=TINY_TIMEOUT)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(ticker())
    try:
        await setup.test_connection(EXISTING_KIND, "", data_dir=data_dir)
    finally:
        task.cancel()
    assert ticks > 1


async def test_fake_provider_is_built_from_the_key_not_from_settings(
    settings: SettingsService, data_dir: Path
) -> None:
    """The saved fake setting never holds the key; the outcome comes from the pasted text
    of this request (ADR-054)."""
    factory = ScriptedFactory()
    setup = service(settings, factory)
    first = await setup.test_connection(FAKE_KIND, fake_key("fake-offline"), data_dir=data_dir)
    second = await setup.test_connection(FAKE_KIND, fake_key("fake-connected"), data_dir=data_dir)
    assert first.message == NetworkUnavailable().user_message
    assert second.ok is True
    assert factory.fakes == 2
    assert "u118" not in current(settings).model_dump_json(), "no key in the setting"
