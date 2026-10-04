"""Shared SecretStore contract (M1.3; AC-M1.4 part, AC-M1.5 part; spec 2.4, 4.1; ADR-046).

Every writable secret store must behave the same way: missing keys read as None, writes
round-trip and overwrite, deletes are idempotent, empty values are refused and keys don't
leak into each other. The suite runs against the memory store, the keyring store over an
in-memory backend, the chained keychain+environment store and the redacting wrapper.

All values are fictional and built at runtime, so secret scanners stay quiet.
"""

from collections.abc import Callable

import pytest
from keyring.backends import fail

from app.domain.errors import SecretStoreUnavailable
from app.domain.redaction import Redactor
from app.modules.secrets import ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, SecretStore
from app.modules.secrets.providers.chained import ChainedSecretStore
from app.modules.secrets.providers.env import EnvSecretStore
from app.modules.secrets.providers.keyring_store import KeyringSecretStore
from app.modules.secrets.providers.memory import MemorySecretStore
from app.modules.secrets.providers.redacting import RedactingSecretStore
from tests.fakes.keyring_backends import MemoryKeyring

pytestmark = pytest.mark.contract

FAKE_SECRET = "fake-" + "secret-" + "x" * 8
OTHER_SECRET = "fake-" + "secret-" + "y" * 8
KEY = ANTHROPIC_API_KEY
OTHER_KEY = CLAUDE_CODE_OAUTH_TOKEN


def _memory() -> SecretStore:
    return MemorySecretStore()


def _keyring() -> SecretStore:
    return KeyringSecretStore(backend=MemoryKeyring())


def _chained() -> SecretStore:
    return ChainedSecretStore(KeyringSecretStore(backend=MemoryKeyring()), EnvSecretStore({}))


def _redacting() -> SecretStore:
    return RedactingSecretStore(MemorySecretStore(), Redactor())


FACTORIES: dict[str, Callable[[], SecretStore]] = {
    "memory": _memory,
    "keyring": _keyring,
    "chained": _chained,
    "redacting": _redacting,
}


@pytest.fixture(params=list(FACTORIES), ids=list(FACTORIES))
def store(request: pytest.FixtureRequest) -> SecretStore:
    factory = FACTORIES[request.param]
    return factory()


def test_get_missing_returns_none(store: SecretStore) -> None:
    assert store.get(KEY) is None
    assert store.get("never_saved_key") is None


def test_set_then_get_round_trip(store: SecretStore) -> None:
    store.set(KEY, FAKE_SECRET)

    assert store.get(KEY) == FAKE_SECRET


def test_overwrite_replaces(store: SecretStore) -> None:
    store.set(KEY, FAKE_SECRET)
    store.set(KEY, OTHER_SECRET)

    assert store.get(KEY) == OTHER_SECRET


def test_delete_removes_and_is_idempotent(store: SecretStore) -> None:
    store.set(KEY, FAKE_SECRET)

    store.delete(KEY)
    assert store.get(KEY) is None

    store.delete(KEY)  # deleting again is not an error
    store.delete("never_saved_key")
    assert store.get(KEY) is None


def test_unicode_and_long_values(store: SecretStore) -> None:
    unicode_value = "fake-" + "sécrèt-üñîçødé-" + "☃" * 4
    # 1,000 ASCII characters: well under Windows Credential Manager's 2,560-byte limit
    # (ADR-046), so the same suite holds on every OS.
    long_value = "fake-" + "secret-" + "z" * 988
    assert len(long_value) == 1000

    store.set(KEY, unicode_value)
    assert store.get(KEY) == unicode_value

    store.set(OTHER_KEY, long_value)
    assert store.get(OTHER_KEY) == long_value


def test_available_flag_consistent(store: SecretStore) -> None:
    assert store.available is True

    store.set(KEY, FAKE_SECRET)

    assert store.available is True
    assert store.get(KEY) == FAKE_SECRET


def test_empty_value_rejected(store: SecretStore) -> None:
    with pytest.raises(ValueError):
        store.set(KEY, "")
    assert store.get(KEY) is None

    store.set(KEY, FAKE_SECRET)
    with pytest.raises(ValueError):
        store.set(KEY, "")
    assert store.get(KEY) == FAKE_SECRET  # a refused write leaves the old value


def test_keys_are_independent(store: SecretStore) -> None:
    store.set(KEY, FAKE_SECRET)
    store.set(OTHER_KEY, OTHER_SECRET)

    assert store.get(KEY) == FAKE_SECRET
    assert store.get(OTHER_KEY) == OTHER_SECRET

    store.delete(KEY)

    assert store.get(KEY) is None
    assert store.get(OTHER_KEY) == OTHER_SECRET


# The same promises when no keychain is usable: nothing is stored, and errors carry no value.
UNAVAILABLE_FACTORIES: dict[str, Callable[[], SecretStore]] = {
    "memory": lambda: MemorySecretStore(available=False),
    "keyring": lambda: KeyringSecretStore(backend=fail.Keyring()),
    "chained": lambda: ChainedSecretStore(
        KeyringSecretStore(backend=fail.Keyring()), EnvSecretStore({})
    ),
    "redacting": lambda: RedactingSecretStore(MemorySecretStore(available=False), Redactor()),
    "env": lambda: EnvSecretStore({}),
}


@pytest.fixture(params=sorted(UNAVAILABLE_FACTORIES))
def unavailable_store(request: pytest.FixtureRequest) -> SecretStore:
    return UNAVAILABLE_FACTORIES[request.param]()


def test_unavailable_store_refuses_writes_without_echoing_the_value(
    unavailable_store: SecretStore,
) -> None:
    value = "fake-" + "secret-" + "u" * 8
    assert unavailable_store.available is False

    for write in (
        lambda: unavailable_store.set(ANTHROPIC_API_KEY, value),
        lambda: unavailable_store.delete(ANTHROPIC_API_KEY),
    ):
        with pytest.raises(SecretStoreUnavailable) as raised:
            write()
        assert value not in str(raised.value)
        assert value not in repr(raised.value)
