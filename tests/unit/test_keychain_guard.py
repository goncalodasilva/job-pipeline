"""Tests never reach the real system keychain (M1.2, ADR-044, golden rule 2).

``tests/conftest.py`` pins an in-memory keyring backend for every test, and
``tests/running_app.py`` pins a file-backed test keyring in the app subprocess's environment,
with its file in the test's temporary folder. All values are fictional.
"""

import json
import os
from pathlib import Path

import keyring
import keyring.core
import pytest
from keyring.errors import InitError, PasswordDeleteError

from tests.fakes.keyring_backends import (
    FILE_BACKEND_NAME,
    FILE_VARIABLE,
    GUARD_BACKEND_NAME,
    MEMORY_BACKEND_NAME,
    FileKeyring,
    GuardKeyring,
    MemoryKeyring,
)
from tests.running_app import isolated_env

FAKE_SECRET = "fake-" + "secret-" + "x" * 8
SERVICE = "jobpipeline"
USERNAME = "anthropic_api_key"


def test_tests_never_use_real_keychain(memory_keyring: MemoryKeyring) -> None:
    # Imported here so the other guard tests run even before the store exists.
    from app.modules.secrets.providers.keyring_store import KeyringSecretStore

    assert isinstance(keyring.get_keyring(), MemoryKeyring)
    assert keyring.get_keyring() is memory_keyring
    assert os.environ["PYTHON_KEYRING_BACKEND"] == MEMORY_BACKEND_NAME
    # Even a fresh resolution from the environment lands on the fake.
    assert isinstance(keyring.core.load_env(), MemoryKeyring)

    KeyringSecretStore().set(USERNAME, FAKE_SECRET)

    assert memory_keyring.items == {(SERVICE, USERNAME): FAKE_SECRET}


def test_running_app_env_pins_fake_keyring_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.macOS.Keyring")
    monkeypatch.setenv(FILE_VARIABLE, str(tmp_path / "elsewhere" / "inherited.json"))
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"

    env = isolated_env(data_dir, config_dir)

    assert env["PYTHON_KEYRING_BACKEND"] == FILE_BACKEND_NAME
    keyring_file = Path(env[FILE_VARIABLE])
    assert keyring_file == tmp_path / "keyring.json"
    assert keyring_file.is_relative_to(tmp_path)

    # The pinned name loads in a process that has only this environment.
    monkeypatch.setenv(FILE_VARIABLE, env[FILE_VARIABLE])
    loaded = keyring.core.load_keyring(env["PYTHON_KEYRING_BACKEND"])
    assert isinstance(loaded, FileKeyring)
    assert loaded.path == keyring_file


def test_file_keyring_persists_between_instances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "keyring.json"
    monkeypatch.setenv(FILE_VARIABLE, str(path))

    first = FileKeyring()
    assert first.get_password(SERVICE, USERNAME) is None
    first.set_password(SERVICE, USERNAME, FAKE_SECRET)

    second = FileKeyring()
    assert second.get_password(SERVICE, USERNAME) == FAKE_SECRET
    assert json.loads(path.read_text(encoding="utf-8")) == {SERVICE: {USERNAME: FAKE_SECRET}}

    second.delete_password(SERVICE, USERNAME)
    assert FileKeyring().get_password(SERVICE, USERNAME) is None
    with pytest.raises(PasswordDeleteError):
        FileKeyring().delete_password(SERVICE, USERNAME)


def test_file_keyring_requires_its_path_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FILE_VARIABLE, raising=False)

    with pytest.raises(InitError):
        FileKeyring()
    assert FileKeyring.viable is False  # never picked by keyring's own discovery


def test_keychain_guard_applies_without_requesting_it() -> None:
    # No fixture named: the autouse fixture alone must pin the fake.
    assert isinstance(keyring.get_keyring(), MemoryKeyring)
    assert os.environ["PYTHON_KEYRING_BACKEND"] == MEMORY_BACKEND_NAME


def test_outside_test_bodies_keyring_resolves_to_a_failing_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Undo the per-test fake (same monkeypatch instance) to see the session-wide state.
    monkeypatch.undo()

    assert os.environ["PYTHON_KEYRING_BACKEND"] == GUARD_BACKEND_NAME
    backend = keyring.get_keyring()
    assert isinstance(backend, GuardKeyring)
    with pytest.raises(AssertionError):
        backend.get_password(SERVICE, USERNAME)
    # A fresh resolution never reaches a real backend either.
    assert isinstance(keyring.core.load_env(), GuardKeyring)
