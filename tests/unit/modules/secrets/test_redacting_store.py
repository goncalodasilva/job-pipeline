"""Redacting secret store (M1.3; AC-M1.4 part; spec 2.9 rule 9, 2.9.9; ADR-030, ADR-046).

``RedactingSecretStore`` registers every secret value it reads or writes with the log
``Redactor``, so a value that passed through the store can never reach a log line
unredacted. ``register_environment_secrets`` registers environment secrets at start-up and
accepts only the environment store, so start-up never reads the keychain (ADR-046, ADR-047).

All values are fictional and built at runtime. They deliberately don't look like Anthropic
keys, so only registration (not the key-shape pattern) can hide them.
"""

import pytest

from app.domain.errors import SecretStoreUnavailable
from app.domain.redaction import REDACTED, Redactor
from app.modules.secrets import ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN
from app.modules.secrets.providers.env import EnvSecretStore
from app.modules.secrets.providers.memory import MemorySecretStore
from app.modules.secrets.providers.redacting import (
    RedactingSecretStore,
    register_environment_secrets,
)

READ_SECRET = "fake-" + "secret-" + "r" * 8
WRITTEN_SECRET = "fake-" + "secret-" + "w" * 8
ENV_KEY_SECRET = "fake-" + "secret-" + "a" * 8
ENV_TOKEN_SECRET = "fake-" + "secret-" + "t" * 8
LATER_SECRET = "fake-" + "secret-" + "l" * 8


def _hidden(redactor: Redactor, value: str) -> bool:
    redacted = redactor.redact(f"x {value} y")
    return value not in redacted and redacted == f"x {REDACTED} y"


class _CountingStore:
    """A SecretStore that records calls, to check the wrapper passes them through."""

    def __init__(self, *, available: bool) -> None:
        self._available = available
        self.calls: list[tuple[str, str]] = []

    @property
    def available(self) -> bool:
        return self._available

    def get(self, key: str) -> str | None:
        self.calls.append(("get", key))
        return None

    def set(self, key: str, value: str) -> None:
        self.calls.append(("set", key))

    def delete(self, key: str) -> None:
        self.calls.append(("delete", key))


def test_ac_m1_4_redacting_store_registers_values_read_and_written() -> None:
    inner = MemorySecretStore()
    inner.set(ANTHROPIC_API_KEY, READ_SECRET)  # saved before the wrapper existed
    redactor = Redactor()
    store = RedactingSecretStore(inner, redactor)

    assert not _hidden(redactor, READ_SECRET)  # nothing preloaded, nothing read yet

    assert store.get(ANTHROPIC_API_KEY) == READ_SECRET
    assert _hidden(redactor, READ_SECRET)

    store.set(CLAUDE_CODE_OAUTH_TOKEN, WRITTEN_SECRET)
    assert inner.get(CLAUDE_CODE_OAUTH_TOKEN) == WRITTEN_SECRET
    assert _hidden(redactor, WRITTEN_SECRET)

    assert store.get("never_saved_key") is None  # a missing value registers nothing


def test_ac_m1_4_redacting_store_registers_before_a_failing_write() -> None:
    redactor = Redactor()
    store = RedactingSecretStore(MemorySecretStore(available=False), redactor)

    with pytest.raises(SecretStoreUnavailable) as raised:
        store.set(ANTHROPIC_API_KEY, WRITTEN_SECRET)

    assert WRITTEN_SECRET not in str(raised.value)
    assert _hidden(redactor, WRITTEN_SECRET)


def test_ac_m1_4_env_secrets_registered_at_startup() -> None:
    env = {"ANTHROPIC_API_KEY": ENV_KEY_SECRET, "CLAUDE_CODE_OAUTH_TOKEN": ENV_TOKEN_SECRET}
    redactor = Redactor()

    env_store = EnvSecretStore(env)
    register_environment_secrets(env_store, redactor)
    store = RedactingSecretStore(env_store, redactor)

    # Registered at start-up, before anything is read through the store.
    assert _hidden(redactor, ENV_KEY_SECRET)
    assert _hidden(redactor, ENV_TOKEN_SECRET)

    # A value that appears later is registered when it is read.
    env["ANTHROPIC_API_KEY"] = LATER_SECRET
    assert not _hidden(redactor, LATER_SECRET)
    assert store.get(ANTHROPIC_API_KEY) == LATER_SECRET
    assert _hidden(redactor, LATER_SECRET)


def test_environment_registration_skips_unset_variables() -> None:
    redactor = Redactor()

    register_environment_secrets(EnvSecretStore({"ANTHROPIC_API_KEY": "  "}), redactor)

    assert redactor.redact("nothing registered") == "nothing registered"


def test_redacting_store_passes_available_and_delete_through() -> None:
    redactor = Redactor()
    assert RedactingSecretStore(MemorySecretStore(), redactor).available is True
    assert RedactingSecretStore(MemorySecretStore(available=False), redactor).available is False
    assert RedactingSecretStore(EnvSecretStore({}), redactor).available is False

    inner = MemorySecretStore()
    store = RedactingSecretStore(inner, redactor)
    store.set(ANTHROPIC_API_KEY, WRITTEN_SECRET)
    store.delete(ANTHROPIC_API_KEY)
    assert inner.get(ANTHROPIC_API_KEY) is None
    store.delete(ANTHROPIC_API_KEY)  # still idempotent through the wrapper

    counting = _CountingStore(available=True)
    wrapped = RedactingSecretStore(counting, redactor)
    wrapped.delete(CLAUDE_CODE_OAUTH_TOKEN)
    assert counting.calls == [("delete", CLAUDE_CODE_OAUTH_TOKEN)]

    with pytest.raises(SecretStoreUnavailable):
        RedactingSecretStore(MemorySecretStore(available=False), redactor).delete(ANTHROPIC_API_KEY)
