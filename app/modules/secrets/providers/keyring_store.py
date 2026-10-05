"""Secret store backed by the operating system's keychain (spec 2.4, 2.9.9; ADR-007).

The only module allowed to import ``keyring``. Each secret is stored under the service
name (``jobpipeline``, or ``jobpipeline-demo`` in demo mode, ADR-044) with the secret's
key as the keyring user name.
"""

from typing import Final

import keyring
from keyring.backend import KeyringBackend as KeyringBackend  # re-exported for wiring
from keyring.backends import chainer, fail, null
from keyring.errors import PasswordDeleteError

from app.domain.errors import SecretStoreUnavailable

SERVICE_NAME: Final = "jobpipeline"
DEMO_SERVICE_NAME: Final = "jobpipeline-demo"  # demo runs never touch real credentials
_PROBE_KEY: Final = "__jobpipeline_probe__"


class KeyringSecretStore:
    def __init__(self, service: str = SERVICE_NAME, backend: KeyringBackend | None = None) -> None:
        self._service = service
        self._backend = backend
        self._available = False

    @property
    def available(self) -> bool:
        """False when no keychain is usable. Probed by reading a fixed key; only success is
        remembered, so a keychain that was locked can be used once the user unlocks it."""
        if not self._available:
            self._available = self._probe()
        return self._available

    def get(self, key: str) -> str | None:
        backend = self._usable_backend()
        failure = None
        try:
            return backend.get_password(self._service, key)
        except Exception as error:  # any backend failure means the keychain is unusable
            failure = type(error).__name__
        raise SecretStoreUnavailable(detail=failure)

    def set(self, key: str, value: str) -> None:
        if not value:
            raise ValueError("A secret can't be empty.")
        backend = self._usable_backend()
        failure = None
        try:
            backend.set_password(self._service, key, value)
            return
        except Exception as error:
            failure = type(error).__name__
        raise SecretStoreUnavailable(detail=failure)

    def delete(self, key: str) -> None:
        backend = self._usable_backend()
        failure = None
        try:
            backend.delete_password(self._service, key)
        except PasswordDeleteError:
            # Raised for "not found" but also for a denied delete (macOS), so check below.
            pass
        except Exception as error:
            failure = type(error).__name__
        if failure is None and self.get(key) is None:
            return
        raise SecretStoreUnavailable(detail=failure or "PasswordDeleteError")

    def _usable_backend(self) -> KeyringBackend:
        if not self.available or self._backend is None:
            raise SecretStoreUnavailable()
        return self._backend

    def _probe(self) -> bool:
        try:
            if self._backend is None:
                self._backend = keyring.get_keyring()
            if not _can_store(self._backend):
                return False
            self._backend.get_password(self._service, _PROBE_KEY)
        except Exception:  # a backend that can't be loaded or read is unusable
            return False
        return True


def _can_store(backend: KeyringBackend) -> bool:
    if isinstance(backend, fail.Keyring | null.Keyring):
        return False
    return not (isinstance(backend, chainer.ChainerBackend) and not backend.backends)
