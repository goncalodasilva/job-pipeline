"""Keychain first, environment second (ADR-044 item 7, ADR-047).

Writes and deletes go to the keychain only; without one they raise
``SecretStoreUnavailable`` and the environment is left alone. With a ``redactor``, every
value read or written is registered with it, so this one object is what wiring injects.
"""

from app.domain.errors import SecretStoreUnavailable
from app.domain.redaction import Redactor
from app.modules.secrets import SecretLookup, SecretSource, SecretStore


class ChainedSecretStore:
    def __init__(
        self, primary: SecretStore, fallback: SecretStore, *, redactor: Redactor | None = None
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._redactor = redactor

    @property
    def available(self) -> bool:
        return self._primary.available

    def get(self, key: str) -> str | None:
        return self.lookup(key).value

    def source(self, key: str) -> SecretSource | None:
        return self.lookup(key).source

    def lookup(self, key: str) -> SecretLookup:
        """Value and source from one read, so the two can't disagree."""
        found = self._find(key)
        if found.value is not None and self._redactor is not None:
            self._redactor.register(found.value)
        return found

    def set(self, key: str, value: str) -> None:
        if self._redactor is not None:
            self._redactor.register(value)  # before writing: a failed write can't log it
        self._primary.set(key, value)

    def delete(self, key: str) -> None:
        self._primary.delete(key)

    def _find(self, key: str) -> SecretLookup:
        keychain_failed = False
        if self._primary.available:
            try:
                value = self._primary.get(key)
            except SecretStoreUnavailable:
                value, keychain_failed = None, True
            if value is not None:
                return SecretLookup(value, "keychain")
        try:
            value = self._fallback.get(key)
        except SecretStoreUnavailable:
            value = None
        source: SecretSource | None = "environment" if value is not None else None
        return SecretLookup(value, source, keychain_failed)
