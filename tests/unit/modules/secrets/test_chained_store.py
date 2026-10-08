"""Chained keychain-then-environment secret store (M1.3; AC-M1.5 part; spec 2.4, 2.9.9; ADR-044).

``ChainedSecretStore(primary, fallback)`` reads the keychain first and falls back to the
environment when the keychain has nothing, is unusable or fails to read. Writes and deletes go
to the keychain only, so the environment is never changed. ``source(key)`` says where a value
came from, for the setup guide and settings.

Every backend is a test fake; all values are fictional and built at runtime.
"""

from collections.abc import Callable

import pytest
from keyring.backends import fail
from keyring.errors import KeyringLocked

from app.domain.errors import SecretStoreUnavailable
from app.domain.redaction import REDACTED, Redactor
from app.modules.secrets import ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, SecretStore
from app.modules.secrets.providers.chained import ChainedSecretStore
from app.modules.secrets.providers.env import EnvSecretStore
from app.modules.secrets.providers.keyring_store import SERVICE_NAME, KeyringSecretStore
from app.modules.secrets.providers.memory import MemorySecretStore
from tests.fakes.keyring_backends import MemoryKeyring

KEYCHAIN_SECRET = "fake-" + "secret-" + "k" * 8
ENV_SECRET = "fake-" + "secret-" + "e" * 8
NEW_SECRET = "fake-" + "secret-" + "n" * 8
PROBE_USERNAME = "__jobpipeline_probe__"


class _ReadFailingKeyring(MemoryKeyring):
    """Passes the availability probe, then fails every real read, like a keychain that locks
    after start-up or refuses access to one item."""

    def get_password(self, service: str, username: str) -> str | None:
        if username == PROBE_USERNAME:
            return None
        raise KeyringLocked("fictional: locked while reading")


class _ReadFailingStore:
    """A SecretStore that claims to be available but whose reads fail."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, str]] = []

    @property
    def available(self) -> bool:
        return True

    def get(self, key: str) -> str | None:
        raise SecretStoreUnavailable(detail="KeyringLocked")

    def set(self, key: str, value: str) -> None:
        self.writes.append((key, value))

    def delete(self, key: str) -> None:
        raise SecretStoreUnavailable(detail="KeyringLocked")


class _UnavailableFallback:
    """A fallback whose reads raise, which the chain must report as "no value"."""

    @property
    def available(self) -> bool:
        return False

    def get(self, key: str) -> str | None:
        raise SecretStoreUnavailable()

    def set(self, key: str, value: str) -> None:
        raise SecretStoreUnavailable()

    def delete(self, key: str) -> None:
        raise SecretStoreUnavailable()


def test_chained_prefers_keychain_over_environment() -> None:
    keychain = MemorySecretStore()
    keychain.set(ANTHROPIC_API_KEY, KEYCHAIN_SECRET)
    env = {"ANTHROPIC_API_KEY": ENV_SECRET, "CLAUDE_CODE_OAUTH_TOKEN": ENV_SECRET}
    store = ChainedSecretStore(keychain, EnvSecretStore(env))

    assert store.get(ANTHROPIC_API_KEY) == KEYCHAIN_SECRET
    # Nothing in the keychain for the token: the environment fills the gap.
    assert store.get(CLAUDE_CODE_OAUTH_TOKEN) == ENV_SECRET

    # Same with the real keyring adapter over an in-memory backend.
    backend = MemoryKeyring()
    backend.items[(SERVICE_NAME, ANTHROPIC_API_KEY)] = KEYCHAIN_SECRET
    keyring_chain = ChainedSecretStore(KeyringSecretStore(backend=backend), EnvSecretStore(env))
    assert keyring_chain.get(ANTHROPIC_API_KEY) == KEYCHAIN_SECRET


@pytest.mark.parametrize(
    "keychain_factory",
    [
        pytest.param(lambda: MemorySecretStore(available=False), id="memory-unavailable"),
        pytest.param(lambda: KeyringSecretStore(backend=fail.Keyring()), id="keyring-fail"),
    ],
)
def test_chained_falls_back_to_environment_when_keychain_unavailable(
    keychain_factory: Callable[[], SecretStore],
) -> None:
    keychain = keychain_factory()
    store = ChainedSecretStore(keychain, EnvSecretStore({"ANTHROPIC_API_KEY": ENV_SECRET}))

    assert store.available is False
    assert store.get(ANTHROPIC_API_KEY) == ENV_SECRET
    assert store.get(CLAUDE_CODE_OAUTH_TOKEN) is None


def test_chained_falls_back_when_keychain_read_fails() -> None:
    env = EnvSecretStore({"ANTHROPIC_API_KEY": ENV_SECRET})

    keyring_backed = KeyringSecretStore(backend=_ReadFailingKeyring())
    assert keyring_backed.available is True  # the probe passed; the real read will fail
    store = ChainedSecretStore(keyring_backed, env)
    assert store.get(ANTHROPIC_API_KEY) == ENV_SECRET
    assert store.get(CLAUDE_CODE_OAUTH_TOKEN) is None
    assert store.source(ANTHROPIC_API_KEY) == "environment"

    failing = _ReadFailingStore()
    fake_backed = ChainedSecretStore(failing, env)
    assert fake_backed.get(ANTHROPIC_API_KEY) == ENV_SECRET
    assert fake_backed.source(ANTHROPIC_API_KEY) == "environment"
    # The fallback is reported, so the interface can say the keychain couldn't be read.
    assert fake_backed.lookup(ANTHROPIC_API_KEY).keychain_failed is True
    # Writes still go to the keychain even when its reads fail.
    fake_backed.set(ANTHROPIC_API_KEY, NEW_SECRET)
    assert failing.writes == [(ANTHROPIC_API_KEY, NEW_SECRET)]

    # A fallback that can't be read means "no value", not an error.
    neither = ChainedSecretStore(_ReadFailingStore(), _UnavailableFallback())
    assert neither.get(ANTHROPIC_API_KEY) is None
    assert neither.source(ANTHROPIC_API_KEY) is None


def test_chained_source_reports_keychain_or_environment() -> None:
    keychain = MemorySecretStore()
    keychain.set(ANTHROPIC_API_KEY, KEYCHAIN_SECRET)
    env = {"ANTHROPIC_API_KEY": ENV_SECRET, "CLAUDE_CODE_OAUTH_TOKEN": ENV_SECRET}
    store = ChainedSecretStore(keychain, EnvSecretStore(env))

    assert store.source(ANTHROPIC_API_KEY) == "keychain"
    assert store.source(CLAUDE_CODE_OAUTH_TOKEN) == "environment"
    assert store.source("unknown_key") is None

    del env["CLAUDE_CODE_OAUTH_TOKEN"]
    assert store.source(CLAUDE_CODE_OAUTH_TOKEN) is None

    keychain.delete(ANTHROPIC_API_KEY)
    assert store.source(ANTHROPIC_API_KEY) == "environment"

    without_keychain = ChainedSecretStore(MemorySecretStore(available=False), EnvSecretStore(env))
    assert without_keychain.source(ANTHROPIC_API_KEY) == "environment"
    assert without_keychain.source(CLAUDE_CODE_OAUTH_TOKEN) is None


def test_chained_set_without_keychain_raises_secret_store_unavailable() -> None:
    env = {"ANTHROPIC_API_KEY": ENV_SECRET}
    snapshot = dict(env)

    for keychain in (
        MemorySecretStore(available=False),
        KeyringSecretStore(backend=fail.Keyring()),
    ):
        store = ChainedSecretStore(keychain, EnvSecretStore(env))

        with pytest.raises(SecretStoreUnavailable) as raised:
            store.set(ANTHROPIC_API_KEY, NEW_SECRET)
        assert NEW_SECRET not in str(raised.value)
        assert NEW_SECRET not in repr(raised.value)

        with pytest.raises(SecretStoreUnavailable):
            store.set(CLAUDE_CODE_OAUTH_TOKEN, NEW_SECRET)
        with pytest.raises(SecretStoreUnavailable):
            store.delete(ANTHROPIC_API_KEY)

        assert env == snapshot  # the environment is never written
        assert store.get(ANTHROPIC_API_KEY) == ENV_SECRET


def test_chained_writes_and_deletes_only_the_keychain() -> None:
    backend = MemoryKeyring()
    env = {"ANTHROPIC_API_KEY": ENV_SECRET}
    snapshot = dict(env)
    store = ChainedSecretStore(KeyringSecretStore(backend=backend), EnvSecretStore(env))

    store.set(ANTHROPIC_API_KEY, NEW_SECRET)
    assert backend.items == {(SERVICE_NAME, ANTHROPIC_API_KEY): NEW_SECRET}
    assert env == snapshot
    assert store.get(ANTHROPIC_API_KEY) == NEW_SECRET
    assert store.source(ANTHROPIC_API_KEY) == "keychain"

    store.delete(ANTHROPIC_API_KEY)
    assert backend.items == {}
    assert env == snapshot
    # The keychain item is gone; the environment variable still answers.
    assert store.get(ANTHROPIC_API_KEY) == ENV_SECRET
    assert store.source(ANTHROPIC_API_KEY) == "environment"

    store.set(CLAUDE_CODE_OAUTH_TOKEN, NEW_SECRET)
    assert backend.items == {(SERVICE_NAME, CLAUDE_CODE_OAUTH_TOKEN): NEW_SECRET}
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_chained_available_follows_keychain() -> None:
    env = EnvSecretStore({"ANTHROPIC_API_KEY": ENV_SECRET})

    assert ChainedSecretStore(MemorySecretStore(), env).available is True
    assert ChainedSecretStore(MemorySecretStore(available=False), env).available is False
    assert ChainedSecretStore(KeyringSecretStore(backend=MemoryKeyring()), env).available is True
    assert ChainedSecretStore(KeyringSecretStore(backend=fail.Keyring()), env).available is False


def test_chained_lookup_returns_value_and_source_from_one_read() -> None:
    keychain = MemorySecretStore()
    keychain.set(ANTHROPIC_API_KEY, KEYCHAIN_SECRET)
    store = ChainedSecretStore(keychain, EnvSecretStore({"CLAUDE_CODE_OAUTH_TOKEN": ENV_SECRET}))

    found = store.lookup(ANTHROPIC_API_KEY)
    assert (found.value, found.source, found.keychain_failed) == (
        KEYCHAIN_SECRET,
        "keychain",
        False,
    )
    fallback = store.lookup(CLAUDE_CODE_OAUTH_TOKEN)
    assert (fallback.value, fallback.source, fallback.keychain_failed) == (
        ENV_SECRET,
        "environment",
        False,
    )
    missing = store.lookup("unknown")
    assert (missing.value, missing.source) == (None, None)
    # The value never shows in the lookup's repr.
    assert KEYCHAIN_SECRET not in repr(found)


def test_chained_with_redactor_registers_values_read_and_written() -> None:
    redactor = Redactor()
    keychain = MemorySecretStore()
    keychain.set(ANTHROPIC_API_KEY, KEYCHAIN_SECRET)
    store = ChainedSecretStore(
        keychain, EnvSecretStore({"CLAUDE_CODE_OAUTH_TOKEN": ENV_SECRET}), redactor=redactor
    )

    store.get(ANTHROPIC_API_KEY)
    store.source(CLAUDE_CODE_OAUTH_TOKEN)
    with pytest.raises(SecretStoreUnavailable):
        ChainedSecretStore(
            MemorySecretStore(available=False), EnvSecretStore({}), redactor=redactor
        ).set(ANTHROPIC_API_KEY, NEW_SECRET)

    for value in (KEYCHAIN_SECRET, ENV_SECRET, NEW_SECRET):
        assert redactor.redact(f"x {value} y") == f"x {REDACTED} y"
