"""Keyring backends for tests, so no test ever reaches the real system keychain (M1.2, ADR-044).

``tests/conftest.py`` pins ``MemoryKeyring`` for every test, and ``tests/running_app.py``
points the app subprocess at ``FileKeyring`` through ``PYTHON_KEYRING_BACKEND``. The other
backends simulate a broken or locked keychain. All stored values are fictional.

Only ``MemoryKeyring`` (priority 1) and ``GuardKeyring`` (priority 0, never preferred) have a
usable ``priority``; the others raise from it, which keyring reads as "not viable", unless
they are chosen explicitly.
"""

import json
import os
from pathlib import Path

from keyring.backend import KeyringBackend
from keyring.compat.properties import classproperty
from keyring.errors import InitError, KeyringError, KeyringLocked, PasswordDeleteError

FILE_VARIABLE = "JOBPIPELINE_TEST_KEYRING_FILE"
MEMORY_BACKEND_NAME = "tests.fakes.keyring_backends.MemoryKeyring"
FILE_BACKEND_NAME = "tests.fakes.keyring_backends.FileKeyring"


class MemoryKeyring(KeyringBackend):
    """Keeps passwords in a dict keyed by ``(service, username)``."""

    @classproperty
    def priority(cls) -> float:
        return 1

    def __init__(self) -> None:
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self.items[(service, username)]
        except KeyError:
            raise PasswordDeleteError("Password not found") from None


class FailingKeyring(KeyringBackend):
    """Every operation fails, like a keychain daemon that is not running."""

    MESSAGE = "fictional backend failure"

    @classproperty
    def priority(cls) -> float:
        raise RuntimeError("test-only backend; choose it explicitly")

    def get_password(self, service: str, username: str) -> str | None:
        raise KeyringError(self.MESSAGE)

    def set_password(self, service: str, username: str, password: str) -> None:
        raise KeyringError(self.MESSAGE)

    def delete_password(self, service: str, username: str) -> None:
        raise KeyringError(self.MESSAGE)


class LockedKeyring(KeyringBackend):
    """Reads find nothing; writes and deletes fail because the keychain is locked.

    The write error echoes the value on purpose, so tests prove the store never passes
    a backend error's text on.
    """

    MESSAGE_PREFIX = "fictional keychain is locked; refused"

    @classproperty
    def priority(cls) -> float:
        raise RuntimeError("test-only backend; choose it explicitly")

    def get_password(self, service: str, username: str) -> str | None:
        return None

    def set_password(self, service: str, username: str, password: str) -> None:
        raise KeyringLocked(f"{self.MESSAGE_PREFIX} {service}/{username}={password}")

    def delete_password(self, service: str, username: str) -> None:
        raise KeyringLocked(f"{self.MESSAGE_PREFIX} {service}/{username}")


def _file_path() -> Path:
    value = os.environ.get(FILE_VARIABLE)
    if not value:
        raise InitError(f"{FILE_VARIABLE} is not set")
    return Path(value)


class FileKeyring(KeyringBackend):
    """JSON file storage at ``$JOBPIPELINE_TEST_KEYRING_FILE``, so values survive restarts.

    Used by the app subprocess in smoke and e2e tests; the file sits in the test's temp folder.
    """

    @classproperty
    def priority(cls) -> float:
        _file_path()
        return 1

    def __init__(self) -> None:
        self.path = _file_path()
        super().__init__()

    def _load(self) -> dict[str, dict[str, str]]:
        if not self.path.exists():
            return {}
        data: dict[str, dict[str, str]] = json.loads(self.path.read_text(encoding="utf-8"))
        return data

    def _save(self, data: dict[str, dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")

    def get_password(self, service: str, username: str) -> str | None:
        return self._load().get(service, {}).get(username)

    def set_password(self, service: str, username: str, password: str) -> None:
        data = self._load()
        data.setdefault(service, {})[username] = password
        self._save(data)

    def delete_password(self, service: str, username: str) -> None:
        data = self._load()
        entries = data.get(service, {})
        if username not in entries:
            raise PasswordDeleteError("Password not found")
        del entries[username]
        self._save(data)


GUARD_BACKEND_NAME = "tests.fakes.keyring_backends.GuardKeyring"


class GuardKeyring(KeyringBackend):
    """Pinned for the whole test session, outside any test body: every use is a test bug.

    Session-scoped fixtures, hooks and import-time code that touch keyring fail loudly here
    instead of reaching the real keychain.
    """

    @classproperty
    def priority(cls) -> float:
        # Loadable by name; as low as keyring's own fail backend, so discovery never picks it.
        return 0

    def get_password(self, service: str, username: str) -> str | None:
        raise AssertionError("keyring used outside a test; the real keychain is off limits")

    set_password = delete_password = get_password  # type: ignore[assignment]


class DenyingDeleteKeyring(MemoryKeyring):
    """Like the macOS backend refusing a delete: raises PasswordDeleteError, keeps the item."""

    def delete_password(self, service: str, username: str) -> None:
        raise PasswordDeleteError("fictional: delete was denied")


class CountingKeyring(MemoryKeyring):
    """A ``MemoryKeyring`` that counts every backend call, so tests can prove that start-up,
    provider health and GET requests read nothing from the keychain (ADR-046, ADR-047)."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0
        self.writes = 0
        self.deletes = 0

    @property
    def calls(self) -> int:
        return self.reads + self.writes + self.deletes

    def get_password(self, service: str, username: str) -> str | None:
        self.reads += 1
        return super().get_password(service, username)

    def set_password(self, service: str, username: str, password: str) -> None:
        self.writes += 1
        super().set_password(service, username, password)

    def delete_password(self, service: str, username: str) -> None:
        self.deletes += 1
        super().delete_password(service, username)
