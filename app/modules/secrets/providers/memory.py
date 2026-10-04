"""In-memory secret store for tests and demos (spec 4.2; ADR-044 item 2).

``available=False`` behaves like an unusable keychain.
"""

from app.domain.errors import SecretStoreUnavailable


class MemorySecretStore:
    def __init__(self, available: bool = True) -> None:
        self._available = available
        self._values: dict[str, str] = {}

    @property
    def available(self) -> bool:
        return self._available

    def get(self, key: str) -> str | None:
        self._require_available()
        return self._values.get(key)

    def set(self, key: str, value: str) -> None:
        if not value:
            raise ValueError("A secret can't be empty.")
        self._require_available()
        self._values[key] = value

    def delete(self, key: str) -> None:
        self._require_available()
        self._values.pop(key, None)

    def _require_available(self) -> None:
        if not self._available:
            raise SecretStoreUnavailable()
