"""Keyring-backed secret store (M1.2; AC-M1.5 part; spec 2.4, 2.9.9, 2.11; ADR-007, ADR-044).

``KeyringSecretStore`` keeps secrets in the OS keychain under the service ``jobpipeline``
(``jobpipeline-demo`` in demo mode), with the secret's key as the keyring username. It reports
``available`` False for keyring's fail backend, an empty chainer or a backend that errors on a
probe read, and turns every backend failure into ``SecretStoreUnavailable`` whose detail is
only the backend error's type name.

Every backend here is a test fake or keyring's own fail/chainer backend; the real keychain is
never reached. All values are fictional and built at runtime so secret scanners stay quiet.
"""

import os
import re
import subprocess
import sys
import traceback
from pathlib import Path

import keyring
import keyring.core
import pytest
from keyring.backends import fail, null
from keyring.backends.chainer import ChainerBackend
from keyring.errors import InitError, KeyringLocked

from app.domain.errors import AppError, SecretStoreUnavailable
from app.modules.secrets import SecretStore
from app.modules.secrets.providers.keyring_store import SERVICE_NAME, KeyringSecretStore
from tests.fakes.keyring_backends import (
    DenyingDeleteKeyring,
    FailingKeyring,
    LockedKeyring,
    MemoryKeyring,
)

REPO_ROOT = Path(__file__).resolve().parents[4]

FAKE_SECRET = "fake-" + "secret-" + "x" * 8
OTHER_SECRET = "fake-" + "secret-" + "y" * 8
KEY = "anthropic_api_key"
PROBE_USERNAME = "__jobpipeline_probe__"
TYPE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Neutral wording: also raised by get and delete, where "nothing was saved" would be wrong.
UNAVAILABLE_MESSAGE = (
    "Your system keychain isn't available. You can use an environment variable instead."
)


def _assert_every_operation_unavailable(store: KeyringSecretStore) -> None:
    with pytest.raises(SecretStoreUnavailable):
        store.get(KEY)
    with pytest.raises(SecretStoreUnavailable):
        store.set(KEY, FAKE_SECRET)
    with pytest.raises(SecretStoreUnavailable):
        store.delete(KEY)


def test_keyring_store_round_trip_with_memory_backend() -> None:
    backend = MemoryKeyring()
    store = KeyringSecretStore(backend=backend)

    assert store.available is True
    assert backend.items == {}  # the availability probe only reads
    assert store.get(KEY) is None

    store.set(KEY, FAKE_SECRET)
    assert store.get(KEY) == FAKE_SECRET

    store.set(KEY, OTHER_SECRET)
    assert store.get(KEY) == OTHER_SECRET

    store.delete(KEY)
    assert store.get(KEY) is None
    store.delete(KEY)  # deleting a missing key is not an error
    store.delete("never_saved_key")
    assert backend.items == {}


def test_keyring_store_unavailable_with_fail_backend() -> None:
    store = KeyringSecretStore(backend=fail.Keyring())

    assert store.available is False
    _assert_every_operation_unavailable(store)


def test_keyring_store_unavailable_with_empty_chainer(monkeypatch: pytest.MonkeyPatch) -> None:
    # keyring falls back to a chainer over every viable backend; with none it silently
    # stores nothing, which must count as "no keychain".
    monkeypatch.setattr(ChainerBackend, "backends", [])
    chainer = ChainerBackend()
    assert chainer.backends == []

    store = KeyringSecretStore(backend=chainer)

    assert store.available is False
    _assert_every_operation_unavailable(store)


def test_keyring_store_unavailable_when_backend_raises_on_probe() -> None:
    store = KeyringSecretStore(backend=FailingKeyring())

    assert store.available is False
    assert store.available is False  # cached, still False
    _assert_every_operation_unavailable(store)


def test_keyring_store_set_raises_secret_store_unavailable_when_locked() -> None:
    store = KeyringSecretStore(backend=LockedKeyring())

    assert store.available is True  # reads work on a locked keychain
    assert store.get(KEY) is None

    with pytest.raises(SecretStoreUnavailable) as raised:
        store.set(KEY, FAKE_SECRET)
    assert str(raised.value) == "KeyringLocked"

    with pytest.raises(SecretStoreUnavailable) as raised_delete:
        store.delete(KEY)
    assert str(raised_delete.value) == "KeyringLocked"


def test_keyring_store_uses_service_name_jobpipeline() -> None:
    assert SERVICE_NAME == "jobpipeline"

    backend = MemoryKeyring()
    KeyringSecretStore(backend=backend).set(KEY, FAKE_SECRET)
    assert backend.items == {("jobpipeline", KEY): FAKE_SECRET}

    demo_backend = MemoryKeyring()
    demo = KeyringSecretStore(service="jobpipeline-demo", backend=demo_backend)
    demo.set(KEY, OTHER_SECRET)
    assert demo_backend.items == {("jobpipeline-demo", KEY): OTHER_SECRET}
    assert demo.get(KEY) == OTHER_SECRET
    assert KeyringSecretStore(backend=demo_backend).get(KEY) is None


def test_keyring_store_default_backend_resolved_lazily(
    monkeypatch: pytest.MonkeyPatch, memory_keyring: MemoryKeyring
) -> None:
    calls: list[str] = []
    original = keyring.core.get_keyring

    def counting_get_keyring() -> keyring.backend.KeyringBackend:
        calls.append("get_keyring")
        return original()

    monkeypatch.setattr(keyring, "get_keyring", counting_get_keyring)
    monkeypatch.setattr(keyring.core, "get_keyring", counting_get_keyring)

    store = KeyringSecretStore()
    assert calls == []

    store.set(KEY, FAKE_SECRET)
    assert store.get(KEY) == FAKE_SECRET
    assert store.available is True
    assert calls == ["get_keyring"]  # resolved once, on first use
    assert memory_keyring.items == {(SERVICE_NAME, KEY): FAKE_SECRET}


@pytest.mark.parametrize(
    ("backend_factory", "backend_message"),
    [
        pytest.param(LockedKeyring, LockedKeyring.MESSAGE_PREFIX, id="locked"),
        pytest.param(FailingKeyring, FailingKeyring.MESSAGE, id="failing"),
    ],
)
def test_keyring_errors_never_include_value(
    backend_factory: type[LockedKeyring] | type[FailingKeyring], backend_message: str
) -> None:
    store = KeyringSecretStore(backend=backend_factory())

    with pytest.raises(SecretStoreUnavailable) as raised:
        store.set(KEY, FAKE_SECRET)
    error = raised.value

    detail = str(error)
    texts = {
        "str": detail,
        "repr": repr(error),
        "args": repr(error.args),
        "user_message": error.user_message,
        "traceback": "".join(traceback.format_exception(error)),
    }
    for name, text in texts.items():
        assert FAKE_SECRET not in text, name
        assert backend_message not in text, name
    assert detail == error.message or TYPE_NAME.match(detail), detail
    assert error.__cause__ is None
    assert error.__context__ is None


def test_keyring_store_rejects_empty_value() -> None:
    backend = MemoryKeyring()
    store = KeyringSecretStore(backend=backend)

    with pytest.raises(ValueError):
        store.set(KEY, "")
    assert backend.items == {}


def test_keyring_store_satisfies_secret_store_protocol(tmp_path: Path) -> None:
    store: SecretStore = KeyringSecretStore(backend=MemoryKeyring())
    assert isinstance(store.available, bool)
    for name in ("get", "set", "delete"):
        assert callable(getattr(store, name))

    # Static check, the way tests/unit/modules/test_module_interfaces.py checks protocols.
    sample = tmp_path / "conformance.py"
    sample.write_text(
        "from app.modules.secrets import SecretStore\n"
        "from app.modules.secrets.providers.keyring_store import KeyringSecretStore\n"
        "\n"
        "store: SecretStore = KeyringSecretStore()\n"
        'demo: SecretStore = KeyringSecretStore(service="jobpipeline-demo")\n',
        encoding="utf-8",
    )
    # Fixed argv to the venv's own interpreter; no shell, no untrusted input.
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--no-incremental",
            "--cache-dir",
            str(tmp_path / ".mypy_cache"),
            str(sample),
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "MYPYPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_secret_store_unavailable_message() -> None:
    assert issubclass(SecretStoreUnavailable, AppError)
    assert SecretStoreUnavailable.message == UNAVAILABLE_MESSAGE

    plain = SecretStoreUnavailable()
    assert plain.user_message == UNAVAILABLE_MESSAGE

    with_detail = SecretStoreUnavailable(detail="KeyringLocked")
    assert with_detail.user_message == UNAVAILABLE_MESSAGE
    assert "KeyringLocked" not in with_detail.user_message
    assert str(with_detail) == "KeyringLocked"


def test_keyring_store_unavailable_with_null_backend() -> None:
    # The "disabled" backend accepts writes and drops them: never report it as usable.
    store = KeyringSecretStore(backend=null.Keyring())

    assert store.available is False
    with pytest.raises(SecretStoreUnavailable):
        store.set(KEY, FAKE_SECRET)


@pytest.mark.parametrize(
    "failure", [ModuleNotFoundError("no.such"), InitError("fictional"), RuntimeError("x")]
)
def test_keyring_store_unavailable_when_backend_cannot_be_loaded(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    def broken() -> None:
        raise failure

    monkeypatch.setattr(keyring, "get_keyring", broken)
    monkeypatch.setattr(keyring.core, "get_keyring", broken)
    store = KeyringSecretStore()

    assert store.available is False
    for call in (
        lambda: store.get(KEY),
        lambda: store.set(KEY, FAKE_SECRET),
        lambda: store.delete(KEY),
    ):
        with pytest.raises(SecretStoreUnavailable):
            call()


def test_keyring_store_delete_denied_is_reported() -> None:
    backend = DenyingDeleteKeyring()
    store = KeyringSecretStore(backend=backend)
    store.set(KEY, FAKE_SECRET)

    with pytest.raises(SecretStoreUnavailable) as raised:
        store.delete(KEY)

    assert FAKE_SECRET not in str(raised.value)
    assert raised.value.__context__ is None
    assert backend.items == {(SERVICE_NAME, KEY): FAKE_SECRET}


class _UnlockableKeyring(MemoryKeyring):
    def __init__(self) -> None:
        super().__init__()
        self.locked = True

    def get_password(self, service: str, username: str) -> str | None:
        if self.locked:
            raise KeyringLocked("fictional: locked")
        return super().get_password(service, username)


def test_keyring_store_recovers_after_keychain_unlocked() -> None:
    backend = _UnlockableKeyring()
    store = KeyringSecretStore(backend=backend)
    assert store.available is False

    backend.locked = False  # the user unlocked the keychain

    assert store.available is True
