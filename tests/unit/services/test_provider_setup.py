"""Provider setup service: save a provider choice and its credential (M1.17; AC-M1.4, AC-M1.5).

``ProviderSetupService(settings, secrets, factory, environment_keys, refresh_health,
probe_timeout)`` checks the chosen kind against ``factory.provider_choices()``, checks the
pasted credential's shape, probes the keychain in a worker thread with a timeout (a timeout
counts as unavailable; ADR-046), saves the credential under the kind's secret key and records
only which kinds have a saved key (``saved_sign_ins``) in the AI provider setting, then
refreshes provider health. Switching kind keeps the other saved keys; each is removed on its
own (user decision, 2026-10-05). Writes, deletes and lookups use the same timed worker.
``view()`` reads the settings row only, never the keychain (spec 2.9.12).
Results carry ``ok`` and field ``errors`` like a step result.

Settings live in a real SQLite database under ``tmp_path``; the secret stores are in-memory
fakes; every value is fictional.
"""

import logging
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, TypeVar, get_args

import pytest
from pydantic import BaseModel

from app.domain.errors import SecretStoreUnavailable, VersionConflict
from app.domain.redaction import Redactor
from app.domain.settings import (
    AI_PROVIDER,
    AIProviderSettings,
    OptionalSettingSpec,
    ProviderKind,
    SettingSpec,
    TierMap,
    claude_auth_mode,
)
from app.modules.ai.providers.claude_sdk import SECRET_KEY_BY_MODE
from app.modules.secrets import SecretLookup, SecretLookupStore, SecretStore
from app.modules.secrets.providers.memory import MemorySecretStore
from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from app.services.provider_setup import SECRET_KEY_BY_KIND, ProviderSetupService
from app.services.settings import SettingsService, Versioned
from app.wiring import Wiring, build_wiring
from tests.fakes.clock import FrozenClock
from tests.fakes.keyring_backends import CountingKeyring
from tests.fakes.usage import CountingUsage

START = datetime(2026, 1, 3, 9, 0, 0, tzinfo=UTC)
TOKEN_KIND: Final = "claude_subscription_token"  # noqa: S105 - a provider kind, not a secret
API_KEY_KIND: Final = "claude_api_key"
EXISTING_KIND = "claude_existing_login"
TOKEN_KEY = "claude_code_oauth_token"  # noqa: S105 - a key name, not a secret
API_KEY_KEY = "anthropic_api_key"
TOKEN_VARIABLE = "CLAUDE_CODE_OAUTH_TOKEN"  # noqa: S105 - a variable name, not a secret
API_KEY_VARIABLE = "ANTHROPIC_API_KEY"
SHORT_PROBE_TIMEOUT = 0.05  # seconds; the slow store never answers before it is released
RELEASE_LIMIT = 10.0  # a stuck worker thread is released at teardown at the latest


def anthropic_shaped(kind: str, body: str) -> str:
    """An Anthropic-shaped fictional credential, built at run time."""
    return "-".join(["sk", "ant", kind, body])


FICTIONAL_TOKEN = anthropic_shaped("oat01", "FICTIONALunitTq7Wv3Kp9Xr2Lm5Jd8")
FICTIONAL_API_KEY = anthropic_shaped("api03", "FICTIONALunitKx4Gq8Vn2Zs6Kc9Rw")


# --- fakes and fixtures ----------------------------------------------------------------------


class SlowProbeStore(MemorySecretStore):
    """A keychain whose availability probe hangs (a Linux Secret Service unlock prompt nobody
    answers) until the test releases it."""

    def __init__(self) -> None:
        super().__init__(available=True)
        self.release = threading.Event()
        self.probe_started = threading.Event()
        self.sets: list[str] = []

    @property
    def available(self) -> bool:
        self.probe_started.set()
        self.release.wait(RELEASE_LIMIT)
        return True

    def set(self, key: str, value: str) -> None:
        self.sets.append(key)
        super().set(key, value)


class EchoingFailureStore(MemorySecretStore):
    """Available, but every write fails with an error whose text holds the value (a careless
    backend); the service must log error types only."""

    def set(self, key: str, value: str) -> None:
        raise SecretStoreUnavailable(detail=f"refused {key}={value}")


class ForbiddenStore:
    """Fails the test on any use: ``view()`` must never touch the keychain."""

    @property
    def available(self) -> bool:
        raise AssertionError("view() probed the keychain")

    def get(self, key: str) -> str | None:
        raise AssertionError("view() read the keychain")

    def set(self, key: str, value: str) -> None:
        raise AssertionError("view() wrote the keychain")

    def delete(self, key: str) -> None:
        raise AssertionError("view() deleted from the keychain")


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


def factory(*, allow_fakes: bool = True, demo: bool = False) -> Wiring:
    return build_wiring(
        environ={"JOBPIPELINE_ALLOW_FAKE_PROVIDERS": "1"} if allow_fakes else {},
        demo=demo,
        redactor=Redactor(),
        keyring_backend=CountingKeyring(),
    )


class Refreshes:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> None:
        self.count += 1


def service(
    settings: SettingsService,
    secrets: SecretStore,
    *,
    environment_keys: frozenset[str] = frozenset(),
    refresh: Refreshes | None = None,
    probe_timeout: float = 5.0,
    wiring: Wiring | None = None,
) -> ProviderSetupService:
    return ProviderSetupService(
        settings,
        secrets,
        wiring if wiring is not None else factory(),
        environment_keys,
        refresh if refresh is not None else Refreshes(),
        usage=CountingUsage(),
        probe_timeout=probe_timeout,
    )


def current(settings: SettingsService) -> AIProviderSettings:
    return settings.load(AI_PROVIDER).value


# --- SECRET_KEY_BY_KIND matches the Claude provider -----------------------------------------------


@pytest.mark.parametrize("kind", get_args(ProviderKind))
def test_secret_key_by_kind_matches_claude_provider_secret_key_by_mode(kind: ProviderKind) -> None:
    mode = claude_auth_mode(kind)
    if mode is None:
        assert SECRET_KEY_BY_KIND.get(kind) is None, "only Claude kinds have a secret key"
    else:
        assert SECRET_KEY_BY_KIND.get(kind) == SECRET_KEY_BY_MODE[mode]


def test_secret_key_by_kind_names_the_credential_keys() -> None:
    assert SECRET_KEY_BY_KIND.get(TOKEN_KIND) == TOKEN_KEY
    assert SECRET_KEY_BY_KIND.get(API_KEY_KIND) == API_KEY_KEY
    assert SECRET_KEY_BY_KIND.get(EXISTING_KIND) is None


# --- the keychain probe -----------------------------------------------------------------------


def test_probe_timeout_is_treated_as_unavailable(
    settings: SettingsService, caplog: pytest.LogCaptureFixture
) -> None:
    store = SlowProbeStore()
    refresh = Refreshes()
    setup = service(settings, store, refresh=refresh, probe_timeout=SHORT_PROBE_TIMEOUT)
    try:
        with caplog.at_level(logging.DEBUG):
            result = setup.save(TOKEN_KIND, FICTIONAL_TOKEN)
        assert store.probe_started.is_set(), "the probe ran"
        assert result.ok is False
        assert TOKEN_VARIABLE in result.errors["credential"], "explains the environment fallback"
        assert store.sets == [], "nothing written after a timed-out probe"
        assert current(settings).kind is None, "nothing saved"
        assert refresh.count == 0
    finally:
        store.release.set()
    assert FICTIONAL_TOKEN not in caplog.text


def test_probe_runs_off_the_calling_thread(settings: SettingsService) -> None:
    """The probe may block for a prompt; it runs in a worker thread."""
    caller = threading.get_ident()
    seen: list[int] = []

    class RecordingStore(MemorySecretStore):
        @property
        def available(self) -> bool:
            seen.append(threading.get_ident())
            return True

    store = RecordingStore()
    result = service(settings, store).save(API_KEY_KIND, FICTIONAL_API_KEY)
    assert result.ok is True, result.errors
    assert seen, "the keychain was probed before writing"
    assert caller not in seen
    assert store.get(API_KEY_KEY) == FICTIONAL_API_KEY


@pytest.mark.parametrize(
    ("kind", "value", "variable"),
    [
        pytest.param(TOKEN_KIND, FICTIONAL_TOKEN, TOKEN_VARIABLE, id="token"),
        pytest.param(API_KEY_KIND, FICTIONAL_API_KEY, API_KEY_VARIABLE, id="api_key"),
    ],
)
def test_unavailable_keychain_names_the_kinds_variable(
    settings: SettingsService, kind: str, value: str, variable: str
) -> None:
    refresh = Refreshes()
    result = service(settings, MemorySecretStore(available=False), refresh=refresh).save(
        kind, value
    )
    assert result.ok is False
    assert variable in result.errors["credential"]
    assert current(settings).kind is None
    assert refresh.count == 0


def test_write_failure_logs_no_value(
    settings: SettingsService, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        result = service(settings, EchoingFailureStore()).save(TOKEN_KIND, FICTIONAL_TOKEN)
    assert result.ok is False
    assert TOKEN_VARIABLE in result.errors["credential"]
    assert FICTIONAL_TOKEN not in result.errors["credential"]
    assert FICTIONAL_TOKEN not in caplog.text
    for record in caplog.records:
        assert FICTIONAL_TOKEN not in repr(record.args)
    assert current(settings).saved_sign_ins == ()


# --- settings: kind changes and the saved sign-ins -----------------------------------------------


def test_kind_change_keeps_saved_sign_ins(settings: SettingsService) -> None:
    tiers = TierMap(fast="fictional-fast-model")
    settings.save(
        AI_PROVIDER,
        AIProviderSettings(tiers=tiers, task_tiers={"fictional_task": "fast"}),
        expected_version=0,
    )
    store = MemorySecretStore()
    refresh = Refreshes()
    setup = service(settings, store, refresh=refresh)

    assert setup.save(TOKEN_KIND, FICTIONAL_TOKEN).ok
    saved = current(settings)
    assert (saved.kind, saved.saved_sign_ins) == (TOKEN_KIND, (TOKEN_KIND,))
    assert store.get(TOKEN_KEY) == FICTIONAL_TOKEN
    assert refresh.count == 1

    # Same kind, empty field: the saved sign-in is kept.
    assert setup.save(TOKEN_KIND, "").ok
    assert current(settings).saved_sign_ins == (TOKEN_KIND,)

    # Another kind with its own credential: both saved keys are kept.
    assert setup.save(API_KEY_KIND, FICTIONAL_API_KEY).ok
    saved = current(settings)
    assert (saved.kind, saved.saved_sign_ins) == (API_KEY_KIND, (API_KEY_KIND, TOKEN_KIND))

    # A kind without a credential: the saved keys are kept.
    assert setup.save(EXISTING_KIND, "").ok
    saved = current(settings)
    assert (saved.kind, saved.saved_sign_ins) == (EXISTING_KIND, (API_KEY_KIND, TOKEN_KIND))
    assert saved.tiers == tiers
    assert dict(saved.task_tiers) == {"fictional_task": "fast"}

    # The fake: kept too, and a pasted key is never stored.
    assert setup.save("fake", "fake-connected-FICTIONALunitPasted").ok
    saved = current(settings)
    assert (saved.kind, saved.saved_sign_ins) == ("fake", (API_KEY_KIND, TOKEN_KIND))
    assert store.get(TOKEN_KEY) == FICTIONAL_TOKEN
    assert store.get(API_KEY_KEY) == FICTIONAL_API_KEY
    assert refresh.count == 5


def test_switching_back_uses_the_kept_key(settings: SettingsService) -> None:
    store = MemorySecretStore()
    setup = service(settings, store)
    assert setup.save(TOKEN_KIND, FICTIONAL_TOKEN).ok
    assert setup.save(EXISTING_KIND, "").ok

    back = setup.save(TOKEN_KIND, "")
    assert back.ok is True, back.errors
    assert back.message == "Saved. The app will use the token saved in your system keychain."
    assert current(settings).kind == TOKEN_KIND


def test_saved_key_gone_from_the_keychain_is_forgotten(settings: SettingsService) -> None:
    store = MemorySecretStore()
    refresh = Refreshes()
    setup = service(settings, store, refresh=refresh)
    assert setup.save(TOKEN_KIND, FICTIONAL_TOKEN).ok
    assert setup.save(EXISTING_KIND, "").ok
    store.delete(TOKEN_KEY)  # removed outside the app

    refused = setup.save(TOKEN_KIND, "")
    assert refused.ok is False
    assert refused.errors == {
        "credential": "No saved token was found in your keychain. Paste it again."
    }
    saved = current(settings)
    assert (saved.kind, saved.saved_sign_ins) == (EXISTING_KIND, ()), "kind unchanged"
    assert refresh.count == 3, "health follows the forgotten key"


def test_saved_key_gone_but_environment_provides_it(settings: SettingsService) -> None:
    store = MemorySecretStore()
    setup = service(settings, store, environment_keys=frozenset({TOKEN_KEY}))
    assert setup.save(TOKEN_KIND, FICTIONAL_TOKEN).ok
    store.delete(TOKEN_KEY)

    result = setup.save(TOKEN_KIND, "")
    assert result.ok is True, result.errors
    assert result.message == f"Saved. The app will use the {TOKEN_VARIABLE} environment variable."
    assert current(settings).saved_sign_ins == ()


def test_environment_key_allows_an_empty_credential(settings: SettingsService) -> None:
    store = MemorySecretStore()
    setup = service(settings, store, environment_keys=frozenset({API_KEY_KEY}))
    assert setup.save(API_KEY_KIND, "").ok
    saved = current(settings)
    assert (saved.kind, saved.saved_sign_ins) == (API_KEY_KIND, ())
    assert store.get(API_KEY_KEY) is None

    # The environment key for one kind doesn't cover the other.
    refused = setup.save(TOKEN_KIND, "")
    assert refused.ok is False
    assert "credential" in refused.errors


def test_kind_must_be_offered_by_the_factory(settings: SettingsService) -> None:
    store = MemorySecretStore()
    no_fakes = service(settings, store, wiring=factory(allow_fakes=False))
    refused = no_fakes.save("fake", "")
    assert refused.ok is False
    assert "kind" in refused.errors

    demo = service(settings, store, wiring=factory(demo=True))
    refused = demo.save(EXISTING_KIND, "")
    assert refused.ok is False
    assert "kind" in refused.errors

    for unknown in ("", "claude", "openai"):
        result = no_fakes.save(unknown, FICTIONAL_TOKEN)
        assert result.ok is False
        assert "kind" in result.errors
    assert current(settings).kind is None
    assert store.get(TOKEN_KEY) is None


# --- delete --------------------------------------------------------------------------------


def test_delete_credential_removes_item_and_clears_saved_sign_in(
    settings: SettingsService,
) -> None:
    store = MemorySecretStore()
    refresh = Refreshes()
    setup = service(settings, store, refresh=refresh)
    assert setup.save(TOKEN_KIND, FICTIONAL_TOKEN).ok
    assert setup.save(API_KEY_KIND, FICTIONAL_API_KEY).ok

    result = setup.delete_credential(TOKEN_KIND)
    assert result.ok is True, result.errors
    assert result.message == "The saved token was removed."
    assert store.get(TOKEN_KEY) is None
    assert store.get(API_KEY_KEY) == FICTIONAL_API_KEY, "only the chosen key goes"
    saved = current(settings)
    assert (saved.kind, saved.saved_sign_ins) == (API_KEY_KIND, (API_KEY_KIND,))
    assert refresh.count == 3


@pytest.mark.parametrize("target", [TOKEN_KIND, EXISTING_KIND, "fake", "", "openai"])
def test_delete_with_no_saved_key_is_a_form_error(settings: SettingsService, target: str) -> None:
    store = MemorySecretStore()
    refresh = Refreshes()
    setup = service(settings, store, refresh=refresh)
    assert setup.save(API_KEY_KIND, FICTIONAL_API_KEY).ok

    result = setup.delete_credential(target)
    assert result.ok is False
    assert result.errors == {"form": "There's no saved key to remove."}
    assert result.message is None
    assert current(settings).saved_sign_ins == (API_KEY_KIND,)
    assert store.get(API_KEY_KEY) == FICTIONAL_API_KEY
    assert refresh.count == 1


def test_delete_with_unavailable_keychain_changes_nothing(settings: SettingsService) -> None:
    settings.save(
        AI_PROVIDER,
        AIProviderSettings(kind="claude_api_key", saved_sign_ins=("claude_api_key",)),
        expected_version=0,
    )
    refresh = Refreshes()
    result = service(settings, MemorySecretStore(available=False), refresh=refresh)
    outcome = result.delete_credential(API_KEY_KIND)
    assert outcome.ok is False
    assert "keychain" in outcome.errors["form"]
    assert current(settings).saved_sign_ins == (API_KEY_KIND,)
    assert refresh.count == 0


def test_delete_failure_from_the_store_changes_nothing(settings: SettingsService) -> None:
    class RefusingDelete(MemorySecretStore):
        def delete(self, key: str) -> None:
            raise SecretStoreUnavailable(detail="fictional: delete denied")

    store = RefusingDelete()
    refresh = Refreshes()
    setup = service(settings, store, refresh=refresh)
    assert setup.save(TOKEN_KIND, FICTIONAL_TOKEN).ok
    outcome = setup.delete_credential(TOKEN_KIND)
    assert outcome.ok is False
    assert "keychain" in outcome.errors["form"]
    assert current(settings).saved_sign_ins == (TOKEN_KIND,)
    assert store.get(TOKEN_KEY) == FICTIONAL_TOKEN
    assert refresh.count == 1


# --- view ------------------------------------------------------------------------------------


def test_view_reads_no_secret(settings: SettingsService) -> None:
    settings.save(
        AI_PROVIDER,
        AIProviderSettings(kind="claude_subscription_token", saved_sign_ins=(TOKEN_KIND,)),
        expected_version=0,
    )
    setup = service(settings, ForbiddenStore(), environment_keys=frozenset({API_KEY_KEY}))
    view = setup.view()
    assert FICTIONAL_TOKEN not in repr(view)


# --- the timed keychain worker --------------------------------------------------------------


def test_second_save_while_the_probe_is_stuck_then_success_after_release(
    settings: SettingsService,
) -> None:
    store = SlowProbeStore()
    setup = service(settings, store, probe_timeout=SHORT_PROBE_TIMEOUT)
    try:
        assert setup.save(TOKEN_KIND, FICTIONAL_TOKEN).ok is False
        assert setup.view().keychain_unavailable is True

        # Still stuck: refused at once, without starting another probe.
        store.probe_started.clear()
        second = setup.save(TOKEN_KIND, FICTIONAL_TOKEN)
        assert second.ok is False
        assert TOKEN_VARIABLE in second.errors["credential"]
        assert not store.probe_started.is_set(), "no second probe while one is stuck"
        assert store.sets == []
    finally:
        store.release.set()

    # Released: the stuck worker finishes and the next save probes again and succeeds.
    deadline = threading.Event()
    for _ in range(200):
        if setup.save(TOKEN_KIND, FICTIONAL_TOKEN).ok:
            break
        deadline.wait(SHORT_PROBE_TIMEOUT)
    else:
        pytest.fail("the save never succeeded after the probe was released")
    assert store.sets == [TOKEN_KEY]
    assert current(settings).saved_sign_ins == (TOKEN_KIND,)
    assert setup.view().keychain_unavailable is False


def test_probe_exception_counts_as_unavailable(
    settings: SettingsService, caplog: pytest.LogCaptureFixture
) -> None:
    class BrokenProbe(MemorySecretStore):
        @property
        def available(self) -> bool:
            raise RuntimeError("fictional backend crash")

    with caplog.at_level(logging.DEBUG):
        result = service(settings, BrokenProbe()).save(API_KEY_KIND, FICTIONAL_API_KEY)
    assert result.ok is False
    assert API_KEY_VARIABLE in result.errors["credential"]
    assert "RuntimeError" in caplog.text, "logged by type"
    assert "fictional backend crash" not in caplog.text, "never the error text"
    assert current(settings).kind is None


def test_slow_write_counts_as_unavailable(settings: SettingsService) -> None:
    class SlowWrite(MemorySecretStore):
        def __init__(self) -> None:
            super().__init__()
            self.release = threading.Event()

        def set(self, key: str, value: str) -> None:
            self.release.wait(RELEASE_LIMIT)

    store = SlowWrite()
    try:
        result = service(settings, store, probe_timeout=SHORT_PROBE_TIMEOUT).save(
            TOKEN_KIND, FICTIONAL_TOKEN
        )
        assert result.ok is False
        assert TOKEN_VARIABLE in result.errors["credential"]
        assert current(settings).kind is None
    finally:
        store.release.set()


# --- settings failures and partial success ------------------------------------------------


M = TypeVar("M", bound=BaseModel)


class FailingSettings(SettingsService):
    """Raises ``errors`` (in order) from the next saves, then saves normally."""

    def __init__(self, db: Database, errors: list[Exception]) -> None:
        super().__init__(db, FrozenClock(START))
        self.errors = errors
        self.saves = 0

    def save(
        self,
        spec: SettingSpec[M] | OptionalSettingSpec[M],
        value: M,
        *,
        expected_version: int,
    ) -> Versioned[M]:
        self.saves += 1
        if self.errors:
            raise self.errors.pop(0)
        return super().save(spec, value, expected_version=expected_version)


def test_version_conflict_is_retried_once(db: Database) -> None:
    flaky = FailingSettings(db, [VersionConflict(detail="fictional")])
    store = MemorySecretStore()
    result = service(flaky, store).save(TOKEN_KIND, FICTIONAL_TOKEN)
    assert result.ok is True, result.errors
    assert flaky.saves == 2
    assert current(flaky).saved_sign_ins == (TOKEN_KIND,)
    assert store.get(TOKEN_KEY) == FICTIONAL_TOKEN


@pytest.mark.parametrize(
    "errors",
    [
        pytest.param(
            [VersionConflict(detail="fictional"), VersionConflict(detail="fictional")],
            id="conflict_twice",
        ),
        pytest.param([sqlite3.OperationalError("fictional: disk I/O error")], id="sqlite"),
    ],
)
def test_settings_failure_after_a_write_removes_the_new_item(
    db: Database, caplog: pytest.LogCaptureFixture, errors: list[Exception]
) -> None:
    flaky = FailingSettings(db, errors)
    store = MemorySecretStore()
    refresh = Refreshes()
    with caplog.at_level(logging.DEBUG):
        result = service(flaky, store, refresh=refresh).save(TOKEN_KIND, FICTIONAL_TOKEN)
    assert result.ok is False
    assert result.errors == {"form": "Your choice couldn't be saved. Try again."}
    assert store.get(TOKEN_KEY) is None, "the item no setting records is removed"
    assert current(flaky).kind is None
    assert refresh.count == 0
    assert "fictional: disk" not in caplog.text, "error types only"


def test_settings_failure_keeps_an_item_that_was_already_saved(db: Database) -> None:
    flaky = FailingSettings(db, [])
    store = MemorySecretStore()
    setup = service(flaky, store)
    assert setup.save(TOKEN_KIND, FICTIONAL_TOKEN).ok

    second = anthropic_shaped("oat01", "FICTIONALunitSecondQ4w8Zr2Lp6")
    flaky.errors = [RuntimeError("fictional"), RuntimeError("fictional")]
    result = setup.save(TOKEN_KIND, second)
    assert result.ok is False
    assert "form" in result.errors
    assert store.get(TOKEN_KEY) == second, "a saved kind's item is not removed"
    assert current(flaky).saved_sign_ins == (TOKEN_KIND,)


def test_settings_failure_without_a_write(db: Database) -> None:
    flaky = FailingSettings(db, [RuntimeError("fictional")])
    result = service(flaky, MemorySecretStore()).save(EXISTING_KIND, "")
    assert result.ok is False
    assert result.errors == {"form": "Your choice couldn't be saved. Try again."}
    assert current(flaky).kind is None


def test_refresh_failure_still_saves(
    settings: SettingsService, caplog: pytest.LogCaptureFixture
) -> None:
    def broken_refresh() -> None:
        raise RuntimeError("fictional: no workspace")

    setup = ProviderSetupService(
        settings,
        MemorySecretStore(),
        factory(),
        frozenset(),
        broken_refresh,
        usage=CountingUsage(),
    )
    with caplog.at_level(logging.DEBUG):
        result = setup.save(TOKEN_KIND, FICTIONAL_TOKEN)
    assert result.ok is True, result.errors
    assert current(settings).saved_sign_ins == (TOKEN_KIND,)
    assert "RuntimeError" in caplog.text


# --- cached lookup shown by view() ------------------------------------------------------------


def test_view_shows_the_cached_lookup_without_reading_again(settings: SettingsService) -> None:
    class CountingLookup(MemorySecretStore):
        def __init__(self) -> None:
            super().__init__()
            self.lookups = 0

        def lookup(self, key: str) -> SecretLookup:
            self.lookups += 1
            return SecretLookup(None, None, keychain_failed=True)

    store = CountingLookup()
    assert isinstance(store, SecretLookupStore)
    setup = service(settings, store, environment_keys=frozenset({TOKEN_KEY}))
    assert setup.save(TOKEN_KIND, FICTIONAL_TOKEN).ok
    assert store.lookups == 1, "looked up once, after the save"

    view = setup.view()
    assert view.saved is True
    assert view.keychain_failed is True
    assert view.environment_variable == TOKEN_VARIABLE
    assert store.lookups == 1, "view() reads no keychain"


@pytest.mark.parametrize(
    ("kind", "credential", "message"),
    [
        (TOKEN_KIND, FICTIONAL_TOKEN, "Saved. Your token is in your system keychain."),
        (API_KEY_KIND, FICTIONAL_API_KEY, "Saved. Your API key is in your system keychain."),
        (EXISTING_KIND, "", "Saved. The app will use the Claude Code sign-in on this computer."),
        (
            "fake",
            "fake-connected-FICTIONALunitPasted",
            "Saved. The app will use the fake provider, for testing only. "
            "The pasted text wasn't needed, so it wasn't kept.",
        ),
    ],
)
def test_saved_message_says_what_happened(
    settings: SettingsService, kind: str, credential: str, message: str
) -> None:
    result = service(settings, MemorySecretStore()).save(kind, credential)
    assert result.ok is True, result.errors
    assert result.message == message
