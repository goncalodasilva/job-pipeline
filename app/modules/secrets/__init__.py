"""Secrets module interface (spec 2.4, 2.9.9, ADR-007).

Implementations (M1): KeyringSecretStore, EnvSecretStore (read-only fallback) and a memory
store for tests. Secret values never go to the database, logs, responses or exports.
"""

from typing import Protocol


class SecretStore(Protocol):
    @property
    def available(self) -> bool:
        """False when no keychain is usable (the setup guide then explains the fallback)."""
        ...

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...

    def delete(self, key: str) -> None: ...
