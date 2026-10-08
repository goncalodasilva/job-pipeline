"""Registering secret values with the log redactor (spec 2.9.9, ADR-047).

``RedactingSecretStore`` registers every value it reads or writes. Environment secrets are
registered at start-up with ``register_environment_secrets``, which takes only the
environment store: the keychain is read on demand, never at start-up (ADR-046).
"""

from app.domain.errors import SecretStoreUnavailable
from app.domain.redaction import Redactor
from app.modules.secrets import KNOWN_SECRET_KEYS, SecretStore
from app.modules.secrets.providers.env import EnvSecretStore


class RedactingSecretStore:
    def __init__(self, inner: SecretStore, redactor: Redactor) -> None:
        self._inner = inner
        self._redactor = redactor

    @property
    def available(self) -> bool:
        return self._inner.available

    def get(self, key: str) -> str | None:
        value = self._inner.get(key)
        if value is not None:
            self._redactor.register(value)
        return value

    def set(self, key: str, value: str) -> None:
        # Registered first, so even a failing write can't log the value unredacted.
        self._redactor.register(value)
        self._inner.set(key, value)

    def delete(self, key: str) -> None:
        self._inner.delete(key)


def register_environment_secrets(store: EnvSecretStore, redactor: Redactor) -> None:
    """Register every known secret set in the environment, at start-up."""
    for key in KNOWN_SECRET_KEYS:
        try:
            value = store.get(key)
        except SecretStoreUnavailable:
            continue
        if value is not None:
            redactor.register(value)
