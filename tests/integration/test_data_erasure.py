"""M9.1 part C: delete all data (ADR-131 item 2; spec 2.9 rules 9, 12 and 13).

The service under test is ``app.services.data_erasure.DataErasureService``:

    DataErasureService(data_dir, secrets, keys, config_dir, *, close_workspace, reopen, demo)

- ``data_dir``: a callable giving the open data folder, or None when none is known.
- ``secrets``: the one ``SecretStore`` of this workspace (service ``jobpipeline``, or
  ``jobpipeline-demo`` in the demo); ``keys`` are the key names to delete from it.
- ``config_dir``: the folder holding ``config.toml``.
- ``close_workspace``: stops tasks, closes the database and clears wiring-owned memory.
- ``reopen``: called once, only after everything was removed; None when the app stays closed.
- ``bootstrap_folder``: the data folder ``config.toml`` names (None when there is no file;
  anything it raises means "can't tell"). Saved keys and ``config.toml`` are removed only when
  there is no file or it names the erased folder (same real path); else both are kept.
- ``settle``: called once the run is over, whether it finished or stopped.
- ``plan()`` gives ``ErasurePlan(known, unknown)``: the app's own entries present in the data
  folder, and every other entry (``.lock`` and system files are in neither).
- ``erase(confirmation)`` needs the typed word ``delete`` (after ``strip()``, lower case, exact),
  raises ``ErasureRefused`` for a wrong word or any link in a known entry (nothing changed,
  ``close_workspace`` not called), and returns ``ErasureResult``. A step that fails raises
  ``ErasureIncomplete`` (``done`` and ``failed`` step labels); a second ``erase`` completes.
  Step labels: an entry's name (``backups``), ``key:<name>`` and ``config.toml``.

The app tests go through ``create_app`` and ``app.state.data_erasure``. Every folder lives under
``tmp_path``; keychain items live in ``MemoryKeyring``; every value is fictional. Route tests
belong to part D. The imports of the new names are inside helpers, so each test fails on its own
while they are missing.
"""

import asyncio
import fcntl
import logging
import logging.handlers
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from app import __version__
from app import config as app_config
from app.config import RuntimeConfig, load_bootstrap, write_bootstrap
from app.domain.errors import SecretStoreUnavailable
from app.domain.redaction import Redactor
from app.domain.tasks import TaskTypeSpec
from app.logging_config import LOG_FILE_NAME, configure_logging
from app.main import WorkspaceBusy, create_app
from app.modules.search import TransientSearchResults
from app.modules.secrets import (
    ANTHROPIC_API_KEY,
    CLAUDE_CODE_OAUTH_TOKEN,
    KNOWN_SECRET_KEYS,
)
from app.modules.secrets.providers.chained import ChainedSecretStore
from app.modules.secrets.providers.env import EnvSecretStore
from app.modules.secrets.providers.keyring_store import (
    DEMO_SERVICE_NAME,
    SERVICE_NAME,
    KeyringSecretStore,
)
from app.modules.secrets.providers.memory import MemorySecretStore
from app.modules.storage import (
    Database,
    DataFolderLock,
    is_workspace_database,
    lock_data_folder,
)
from app.onboarding.detection import StartupDecision
from app.tasks.handlers import HandlerRegistry
from app.wiring import build_wiring
from tests.fakes.clock import FrozenClock
from tests.fakes.keyring_backends import MemoryKeyring
from tests.integration.test_setup_redirect import (
    BASE_URL,
    PORT,
    START,
    Running,
    assert_see_other,
)
from tests.setup_harness import HARNESS_PROVIDER, complete_setup, seed_workspace
from tests.waiting import wait_until

pytestmark = pytest.mark.integration

CONFIRM = "delete"
WELCOME = "/setup/welcome"
DEADLINE_S = 10.0
KNOWN_ENTRIES = frozenset(
    {
        "app.db",
        "app.db-wal",
        "app.db-shm",
        "backups",
        "documents",
        "imports",
        "assets",
        "prompts",
        "exports",
        "logs",
        "tmp",
    }
)
# Fictional markers: planted everywhere the app keeps data, then searched for after erasing.
MARKER = "fictional-kestrel-marker-8841"
SECRET_VALUES = {
    ANTHROPIC_API_KEY: "fictional-api-key-value-2207",
    CLAUDE_CODE_OAUTH_TOKEN: "fictional-token-value-2208",
}
PLANTED_FILES = {
    "app.db-wal": b"fictional wal bytes " + MARKER.encode(),
    "app.db-shm": b"fictional shm bytes",
    "backups/app-20260101T000000Z.db": b"fictional backup copy " + MARKER.encode(),
    "documents/app-example/export-0001.pdf": b"fictional pdf " + MARKER.encode(),
    "imports/fictional-cv.txt": b"fictional cv text " + MARKER.encode(),
    "assets/photo-fictional.png": b"fictional photo bytes",
    "prompts/overrides/refine_fact/prompt.md": b"fictional override " + MARKER.encode(),
    "exports/jobpipeline-export-20260103T090000000000Z.json": b'{"x": "' + MARKER.encode() + b'"}',
    "logs/app.log": b"fictional log line " + MARKER.encode(),
    "tmp/ai/leftover.txt": b"fictional leftover " + MARKER.encode(),
}
SYSTEM_FILES = {".DS_Store": b"fictional finder bytes"}


# --- helpers -------------------------------------------------------------------------------


def _module() -> Any:
    import app.services.data_erasure as module

    return module


class Recorder:
    """What the service asked the app to do, with the data folder as it was at that moment."""

    def __init__(self, data: Path) -> None:
        self.data = data
        self.events: list[tuple[str, list[str]]] = []

    def _names(self) -> list[str]:
        return sorted(os.listdir(self.data)) if self.data.is_dir() else []

    def close(self) -> None:
        self.events.append(("close", self._names()))

    def reopen(self) -> None:
        self.events.append(("reopen", self._names()))

    def count(self, name: str) -> int:
        return sum(1 for event, _ in self.events if event == name)


class FlakyStore(MemorySecretStore):
    """A store whose delete of one key fails until ``heal()`` (a locked keychain)."""

    def __init__(self, failing_key: str | None) -> None:
        super().__init__()
        self.failing_key = failing_key

    def heal(self) -> None:
        self.failing_key = None

    def delete(self, key: str) -> None:
        if key == self.failing_key:
            raise SecretStoreUnavailable(detail="fictional keychain refusal")
        super().delete(key)


class FlakyKeyring(MemoryKeyring):
    """A keychain backend whose delete of one key fails until ``heal()`` (a locked keychain);
    ``on_delete`` runs on every delete (work that lands late)."""

    def __init__(self, failing_key: str | None = None) -> None:
        super().__init__()
        self.failing_key = failing_key
        self.on_delete: Any = None

    def heal(self) -> None:
        self.failing_key = None

    def delete_password(self, service: str, username: str) -> None:
        if self.on_delete is not None:
            self.on_delete()
        if username == self.failing_key:
            raise RuntimeError("fictional keychain refusal")
        super().delete_password(service, username)


def make_workspace(root: Path, name: str = "data", *, files: bool = True) -> Path:
    """A real migrated workspace folder (database closed) with every kind of app entry."""
    data = root / name
    seed_workspace(data, done=("welcome",), clock=FrozenClock(START))
    with Database.open(data / "app.db") as db, db.transaction() as conn:
        conn.execute(
            "INSERT INTO settings(key, value, created_at, updated_at, version)"
            " VALUES('fictional_marker', ?, '2026-01-03T09:00:00Z', '2026-01-03T09:00:00Z', 1)",
            (f'"{MARKER}"',),
        )
    if files:
        for relative, content in PLANTED_FILES.items():
            path = data / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        for relative, content in SYSTEM_FILES.items():
            (data / relative).write_bytes(content)
    return data


def make_service(
    data: Path | None,
    secrets: Any,
    config_dir: Path,
    *,
    recorder: Recorder | None = None,
    reopen: bool = False,
    demo: bool = False,
    keys: tuple[str, ...] = tuple(KNOWN_SECRET_KEYS),
    settle: Any = None,
) -> Any:
    close = recorder.close if recorder is not None else (lambda: None)
    again = recorder.reopen if recorder is not None and reopen else None
    return _module().DataErasureService(
        lambda: data,
        secrets,
        keys,
        config_dir,
        close_workspace=close,
        reopen=again,
        demo=demo,
        bootstrap_folder=named_by(config_dir),
        settle=settle,
    )


def named_by(config_dir: Path) -> Any:
    """What the app injects: the folder ``config.toml`` names (raises ConfigError if unreadable)."""

    def folder() -> Path | None:
        found = load_bootstrap(config_dir)
        return found.data_dir if found is not None else None

    return folder


def planted_keys(store: Any) -> None:
    for key, value in SECRET_VALUES.items():
        store.set(key, value)


def snapshot(root: Path) -> dict[str, tuple[Any, ...]]:
    """Every entry under ``root`` by relative path: links by target, files by bytes."""
    found: dict[str, tuple[Any, ...]] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            found[relative] = ("link", os.readlink(path))
        elif path.is_dir():
            found[relative] = ("dir",)
        else:
            found[relative] = ("file", path.read_bytes())
    return found


def top_level(root: Path) -> set[str]:
    return set(os.listdir(root))


def config_dir_with_bootstrap(root: Path, data: Path | None = None) -> Path:
    config_dir = root / "cfg"
    write_bootstrap(config_dir, data_dir=data if data is not None else root / "data", version="1")
    return config_dir


def all_bytes_under(root: Path) -> bytes:
    chunks = []
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            chunks.append(path.name.encode())
            chunks.append(path.read_bytes())
    return b"\n".join(chunks)


def lock_is_free(data: Path) -> bool:
    """Whether another open of ``.lock`` can take the data-folder lock now."""
    fd = os.open(data / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


# --- the service: entries ------------------------------------------------------------------


def test_delete_all_data_removes_every_app_entry_incl_backups_exports_logs(tmp_path: Path) -> None:
    data = make_workspace(tmp_path)
    config_dir = config_dir_with_bootstrap(tmp_path, data)
    recorder = Recorder(data)
    store = MemorySecretStore()
    service = make_service(data, store, config_dir, recorder=recorder)

    plan = service.plan()
    assert set(plan.known) == KNOWN_ENTRIES, "every known entry that is present is listed"
    assert len(plan.known) == len(set(plan.known))
    assert plan.unknown == (), ".lock and system files are not listed as unknown entries"
    before = top_level(data)
    assert {"backups", "exports", "logs", "app.db"} <= before

    result = service.erase(CONFIRM)

    assert top_level(data) == {".lock", ".DS_Store"}, "only unknown/system entries remain"
    assert set(result.removed) == KNOWN_ENTRIES
    assert result.left == ()
    assert recorder.events[0][0] == "close", "the workspace is closed before anything is removed"
    assert set(recorder.events[0][1]) == before, "...and nothing had been removed at that point"
    assert recorder.count("close") == 1
    assert service.plan().known == (), "a second plan finds nothing to remove"


def test_ac_m9_3_delete_all_data_leaves_nothing_of_the_workspace(tmp_path: Path) -> None:
    """A leak scan: markers planted in the database, every app folder, the keychain and the
    configuration are found nowhere after erasing."""
    data = make_workspace(tmp_path)
    config_dir = config_dir_with_bootstrap(tmp_path, data)
    backend = MemoryKeyring()
    store = KeyringSecretStore(SERVICE_NAME, backend=backend)
    planted_keys(store)
    assert MARKER.encode() in all_bytes_under(data), "the scan can see what was planted"
    database = Database.open(data / "app.db")  # held open, as a running app holds it
    service = _module().DataErasureService(
        lambda: data,
        store,
        tuple(KNOWN_SECRET_KEYS),
        config_dir,
        close_workspace=database.close,
        reopen=None,
        demo=False,
        bootstrap_folder=named_by(config_dir),
    )

    service.erase(CONFIRM)

    remaining = all_bytes_under(tmp_path)
    assert MARKER.encode() not in remaining
    for value in SECRET_VALUES.values():
        assert value.encode() not in remaining
        assert value not in backend.items.values()
    assert not backend.items, "no key of this workspace is left in the keychain"
    assert not (data / "app.db").exists()
    assert load_bootstrap(config_dir) is None, "config.toml is gone"


def test_delete_all_data_runs_with_no_known_data_folder(tmp_path: Path) -> None:
    config_dir = config_dir_with_bootstrap(tmp_path, tmp_path / "moved-away")
    store = MemorySecretStore()
    planted_keys(store)
    recorder = Recorder(tmp_path / "moved-away")
    service = make_service(None, store, config_dir, recorder=recorder)

    assert service.plan().known == ()
    result = service.erase(CONFIRM)

    assert result.removed == () and result.left == ()
    assert store.get(ANTHROPIC_API_KEY) is None and store.get(CLAUDE_CODE_OAUTH_TOKEN) is None
    assert load_bootstrap(config_dir) is None
    assert not (tmp_path / "moved-away").exists(), "nothing is created while erasing"


def test_delete_all_data_leaves_unknown_entries_and_refuses_links(tmp_path: Path) -> None:
    # Unknown entries stay, are listed and are never followed.
    data = make_workspace(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "precious.txt").write_text("fictional outside file")
    (data / "my-notes").mkdir()
    (data / "my-notes" / "todo.txt").write_text("fictional note")
    (data / "README.txt").write_text("fictional readme")
    (data / "shortcut").symlink_to(outside)
    unknown = {"my-notes", "README.txt", "shortcut"}
    service = make_service(data, MemorySecretStore(), tmp_path / "cfg", recorder=Recorder(data))

    assert set(service.plan().unknown) == unknown
    result = service.erase(CONFIRM)

    assert set(result.left) == unknown, "unknown entries are listed, .lock and system files not"
    assert top_level(data) == unknown | {".lock", ".DS_Store"}
    assert (data / "my-notes" / "todo.txt").read_text() == "fictional note"
    assert os.readlink(data / "shortcut") == str(outside)
    assert (outside / "precious.txt").read_text() == "fictional outside file"
    assert not top_level(data) & KNOWN_ENTRIES

    # A link in a known entry is refused before anything changes (nothing deleted, nothing
    # closed, no key removed), whether it is a symlink or a hard link, at the top or nested.
    def symlink_entry(folder: Path, target: Path) -> str:
        (folder / "backups").rename(folder / "backups-real")
        (folder / "backups").symlink_to(target)
        return "backups"

    def symlink_nested(folder: Path, target: Path) -> str:
        (folder / "documents" / "app-example" / "linked.pdf").symlink_to(target / "precious.txt")
        return "documents"

    def hardlink_database(folder: Path, target: Path) -> str:
        os.link(folder / "app.db", target / "second-name-for-the-database")
        return "app.db"

    def hardlink_nested(folder: Path, target: Path) -> str:
        os.link(folder / "logs" / "app.log", target / "second-name-for-the-log")
        return "logs"

    for case in (symlink_entry, symlink_nested, hardlink_database, hardlink_nested):
        root = tmp_path / case.__name__
        root.mkdir()
        folder = make_workspace(root)
        foreign = root / "elsewhere"
        foreign.mkdir()
        (foreign / "precious.txt").write_text("fictional outside file")
        entry = case(folder, foreign)
        store = MemorySecretStore()
        planted_keys(store)
        config_dir = config_dir_with_bootstrap(root, folder)
        recorder = Recorder(folder)
        reopened = make_service(folder, store, config_dir, recorder=recorder, reopen=True)
        before = snapshot(root)

        with pytest.raises(_module().ErasureRefused) as refusal:
            reopened.erase(CONFIRM)

        message = str(refusal.value) + refusal.value.user_message
        assert "link" in message.lower(), (case.__name__, message)
        assert entry in message, (case.__name__, message)
        assert str(root) not in message, "the refusal names no folder path"
        assert snapshot(root) == before, f"{case.__name__}: nothing was deleted"
        assert recorder.events == [], f"{case.__name__}: the workspace was not closed or reopened"
        assert store.get(ANTHROPIC_API_KEY) == SECRET_VALUES[ANTHROPIC_API_KEY]
        assert load_bootstrap(config_dir) is not None


def test_delete_all_data_removes_leftover_restore_staging_folders(tmp_path: Path) -> None:
    """A killed restore leaves a full plaintext copy in ``.restore-*`` (data_portability): it is
    a known entry. A plain file or a link with that name is not a staging folder."""
    data = make_workspace(tmp_path)
    (data / ".restore-abc123" / "documents").mkdir(parents=True)
    (data / ".restore-abc123" / "app.db").write_bytes(b"fictional staged copy " + MARKER.encode())
    (data / ".restore-note").write_text("fictional file with the same prefix")
    (data / ".restore-link").symlink_to(tmp_path)
    service = make_service(data, MemorySecretStore(), tmp_path / "cfg", recorder=Recorder(data))

    plan = service.plan()
    assert ".restore-abc123" in plan.known
    assert {".restore-note", ".restore-link"} <= set(plan.unknown)

    result = service.erase(CONFIRM)

    assert ".restore-abc123" in result.removed
    assert not (data / ".restore-abc123").exists()
    assert MARKER.encode() not in all_bytes_under(data)
    assert (data / ".restore-note").exists() and (data / ".restore-link").is_symlink()
    assert {".restore-note", ".restore-link"} <= set(result.left)


# --- the service: keys ---------------------------------------------------------------------


def test_delete_all_data_removes_saved_keys_for_this_workspace_only(tmp_path: Path) -> None:
    data = make_workspace(tmp_path)
    backend = MemoryKeyring()
    mine = KeyringSecretStore(SERVICE_NAME, backend=backend)
    demo = KeyringSecretStore(DEMO_SERVICE_NAME, backend=backend)
    other_app = KeyringSecretStore("fictional-other-app", backend=backend)
    for store in (mine, demo, other_app):
        planted_keys(store)
    backend.set_password(SERVICE_NAME, "fictional_unrelated_name", "fictional-unrelated-value")
    service = make_service(data, mine, tmp_path / "cfg", recorder=Recorder(data))

    result = service.erase(CONFIRM)

    assert set(result.keys_removed) == set(KNOWN_SECRET_KEYS)
    for key in KNOWN_SECRET_KEYS:
        assert mine.get(key) is None, key
        assert demo.get(key) == SECRET_VALUES[key], "the demo keychain service is untouched"
        assert other_app.get(key) == SECRET_VALUES[key], "another service is untouched"
    # Only the app's known key names are deleted, never other items of the same service.
    assert backend.get_password(SERVICE_NAME, "fictional_unrelated_name") == (
        "fictional-unrelated-value"
    )


def test_delete_all_data_reports_environment_keys_it_cannot_remove(tmp_path: Path) -> None:
    data = make_workspace(tmp_path)
    backend = MemoryKeyring()
    keychain = KeyringSecretStore(SERVICE_NAME, backend=backend)
    environment = {"ANTHROPIC_API_KEY": "fictional-environment-value-3301"}
    chained = ChainedSecretStore(keychain, EnvSecretStore(environment))
    planted_keys(keychain)
    service = make_service(data, chained, tmp_path / "cfg", recorder=Recorder(data))

    result = service.erase(CONFIRM)

    assert keychain.get(ANTHROPIC_API_KEY) is None and keychain.get(CLAUDE_CODE_OAUTH_TOKEN) is None
    assert environment == {"ANTHROPIC_API_KEY": "fictional-environment-value-3301"}
    assert tuple(result.environment_keys) == (ANTHROPIC_API_KEY,)
    notice = " ".join(result.messages).lower()
    assert "environment" in notice and "can't be removed" in notice
    assert "fictional-environment-value-3301" not in repr(result) + " ".join(result.messages)


# --- the service: confirmation -------------------------------------------------------------


def test_delete_all_data_needs_typed_confirmation(tmp_path: Path) -> None:
    data = make_workspace(tmp_path)
    config_dir = config_dir_with_bootstrap(tmp_path, data)
    store = MemorySecretStore()
    planted_keys(store)
    recorder = Recorder(data)
    service = make_service(data, store, config_dir, recorder=recorder, reopen=True)
    before = snapshot(tmp_path)

    for typed in ("", " ", "del", "Delete", "DELETE", "delete all", "delete data", "yes", "delet"):
        with pytest.raises(_module().ErasureRefused):
            service.erase(typed)
        assert snapshot(tmp_path) == before, f"{typed!r}: nothing changed"
        assert recorder.events == [], f"{typed!r}: the workspace was left open"
        assert store.get(ANTHROPIC_API_KEY) == SECRET_VALUES[ANTHROPIC_API_KEY], typed
        assert store.get(CLAUDE_CODE_OAUTH_TOKEN) == SECRET_VALUES[CLAUDE_CODE_OAUTH_TOKEN], typed

    result = service.erase("  delete\n")  # surrounding whitespace is ignored

    assert KNOWN_ENTRIES & set(result.removed)
    assert not top_level(data) & KNOWN_ENTRIES
    assert store.get(ANTHROPIC_API_KEY) is None


# --- the service: configuration, demo ------------------------------------------------------


def test_delete_all_data_removes_config_outside_demo_and_never_in_demo(tmp_path: Path) -> None:
    other_file = "notes-of-the-config-folder.txt"

    # Outside the demo, config.toml in its own folder goes; the folder's other files stay.
    data = make_workspace(tmp_path / "plain", files=False)
    config_dir = config_dir_with_bootstrap(tmp_path / "plain", data)
    (config_dir / other_file).write_text("fictional config folder note")
    result = make_service(data, MemorySecretStore(), config_dir, recorder=Recorder(data)).erase(
        CONFIRM
    )
    assert result.config_removed is True
    assert not (config_dir / "config.toml").exists()
    assert (config_dir / other_file).read_text() == "fictional config folder note"

    # Outside the demo, config.toml beside app.db is removed too and is not an unknown entry.
    shared = make_workspace(tmp_path / "shared", files=False)
    write_bootstrap(shared, data_dir=shared, version="1")
    service = make_service(shared, MemorySecretStore(), shared, recorder=Recorder(shared))
    assert "config.toml" in service.plan().known
    result = service.erase(CONFIRM)
    assert result.config_removed is True and "config.toml" not in result.left
    assert top_level(shared) == {".lock"}

    # In the demo, config.toml is never touched (it belongs to the real setup).
    demo_data = make_workspace(tmp_path / "demo", files=False)
    demo_config = config_dir_with_bootstrap(tmp_path / "demo", tmp_path / "real-data")
    (demo_config / other_file).write_text("fictional config folder note")
    original = snapshot(demo_config)
    result = make_service(
        demo_data, MemorySecretStore(), demo_config, recorder=Recorder(demo_data), demo=True
    ).erase(CONFIRM)
    assert result.config_removed is False
    assert snapshot(demo_config) == original

    # In the demo with config.toml beside the database it is an unknown entry: left and listed.
    demo_shared = make_workspace(tmp_path / "demo-shared", files=False)
    write_bootstrap(demo_shared, data_dir=tmp_path / "real-data", version="1")
    original_config = (demo_shared / "config.toml").read_bytes()
    service = make_service(
        demo_shared, MemorySecretStore(), demo_shared, recorder=Recorder(demo_shared), demo=True
    )
    assert "config.toml" not in service.plan().known
    assert "config.toml" in service.plan().unknown
    result = service.erase(CONFIRM)
    assert result.config_removed is False
    assert "config.toml" in result.left
    assert (demo_shared / "config.toml").read_bytes() == original_config


def test_delete_all_data_in_demo_touches_only_demo_folder_and_demo_keychain(
    tmp_path: Path,
) -> None:
    real = make_workspace(tmp_path, "real-data")
    demo_data = make_workspace(tmp_path, "demo-data")
    config_dir = config_dir_with_bootstrap(tmp_path, real)
    backend = MemoryKeyring()
    demo_store = KeyringSecretStore(DEMO_SERVICE_NAME, backend=backend)
    real_store = KeyringSecretStore(SERVICE_NAME, backend=backend)
    planted_keys(demo_store)
    planted_keys(real_store)
    real_before = snapshot(real)
    config_before = snapshot(config_dir)
    service = make_service(
        demo_data, demo_store, config_dir, recorder=Recorder(demo_data), reopen=True, demo=True
    )

    result = service.erase(CONFIRM)

    assert not top_level(demo_data) & KNOWN_ENTRIES
    assert set(result.removed) == KNOWN_ENTRIES
    assert snapshot(real) == real_before, "the real data folder is untouched"
    assert snapshot(config_dir) == config_before, "the real config.toml is untouched"
    assert result.config_removed is False
    for key in KNOWN_SECRET_KEYS:
        assert demo_store.get(key) is None, key
        assert real_store.get(key) == SECRET_VALUES[key], "the real keychain service is untouched"


# --- the service: a partial failure is retryable --------------------------------------------


def test_delete_all_data_partial_failure_is_reported_and_retryable(tmp_path: Path) -> None:
    data = make_workspace(tmp_path)
    config_dir = config_dir_with_bootstrap(tmp_path, data)
    store = FlakyStore(failing_key=CLAUDE_CODE_OAUTH_TOKEN)
    planted_keys(store)
    recorder = Recorder(data)
    service = make_service(data, store, config_dir, recorder=recorder, reopen=True)

    with pytest.raises(_module().ErasureIncomplete) as failure:
        service.erase(CONFIRM)

    error = failure.value
    assert error.failed == f"key:{CLAUDE_CODE_OAUTH_TOKEN}"
    assert KNOWN_ENTRIES & set(error.done), "the removed entries are reported as done"
    assert error.failed not in error.done
    assert not top_level(data) & KNOWN_ENTRIES, "what could be removed was removed"
    assert store.get(CLAUDE_CODE_OAUTH_TOKEN) == SECRET_VALUES[CLAUDE_CODE_OAUTH_TOKEN]
    text = str(error) + error.user_message
    assert "keychain" in text.lower() or "key" in text.lower()
    assert str(tmp_path) not in text and SECRET_VALUES[CLAUDE_CODE_OAUTH_TOKEN] not in text
    assert recorder.count("reopen") == 0, "the app is not reopened on a half-erased folder"

    store.heal()
    result = service.erase(CONFIRM)  # the retry completes the remaining steps

    assert store.get(ANTHROPIC_API_KEY) is None and store.get(CLAUDE_CODE_OAUTH_TOKEN) is None
    assert CLAUDE_CODE_OAUTH_TOKEN in " ".join(result.keys_removed)
    assert result.config_removed is True
    assert load_bootstrap(config_dir) is None
    assert recorder.count("reopen") == 1


# --- in-memory holders ----------------------------------------------------------------------


def test_transient_search_results_can_be_cleared() -> None:
    held = TransientSearchResults()
    held.put("brief:company-1", {"fictional": "brief"})
    held.put("web:question-1", ["fictional result"])

    held.clear()

    assert held.get("brief:company-1") is None and held.get("web:question-1") is None
    held.put("brief:company-2", "after")  # still usable
    assert held.get("brief:company-2") == "after"


# --- the app ---------------------------------------------------------------------------------


@contextmanager
def started_app(
    tmp_path: Path,
    source: str,
    *,
    backend: MemoryKeyring | None = None,
    demo: bool = False,
    task_registry: HandlerRegistry | None = None,
    environ: dict[str, str] | None = None,
    data: Path | None = None,
    lock: DataFolderLock | None = None,
    **create_kwargs: Any,
) -> Iterator[Running]:
    """A running app on a finished setup (database under ``tmp_path/data`` by default)."""
    folder = data if data is not None else tmp_path / "data"
    config = RuntimeConfig(
        data_dir=folder,
        config_dir=tmp_path / "cfg",
        port=PORT,
        log_level="INFO",
        demo=demo,
        setup=False,
        no_browser=False,
        data_dir_source=source,  # type: ignore[arg-type]
    )
    wiring = build_wiring(
        environ={"JOBPIPELINE_ALLOW_FAKE_PROVIDERS": "1", **(environ or {})},
        demo=demo,
        redactor=Redactor(),
        keyring_backend=backend if backend is not None else MemoryKeyring(),
        clock=FrozenClock(START),
        demo_workspace=demo and source == "demo",
    )
    app = create_app(
        config,
        clock=FrozenClock(START),
        wiring=wiring,
        task_registry=task_registry,
        data_folder_lock=lock,
        **create_kwargs,
    )
    from collections import Counter

    from fastapi.testclient import TestClient

    with TestClient(app, base_url=BASE_URL, follow_redirects=False) as client:
        yield Running(app=app, client=client, hits=Counter())


def completed(tmp_path: Path) -> Path:
    complete_setup(tmp_path / "data", tmp_path / "cfg", clock=FrozenClock(START))
    data = tmp_path / "data"
    for relative, content in PLANTED_FILES.items():
        path = data / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return data


def test_delete_all_data_returns_app_to_first_run(tmp_path: Path) -> None:
    """The data folder came from the setup file, so the app stays closed and step 1 (choose
    the folder) is what every page redirects to."""
    data = completed(tmp_path)
    backend = MemoryKeyring()
    store = KeyringSecretStore(SERVICE_NAME, backend=backend)
    planted_keys(store)

    with started_app(tmp_path, "bootstrap", backend=backend) as run:
        assert run.client.get("/").status_code == 200, "the finished setup opens the app"
        service = run.app.state.data_erasure

        result = service.erase(CONFIRM)

        assert set(result.removed) >= {"app.db", "backups", "exports", "logs"}
        assert not (data / "app.db").exists()
        assert not top_level(data) & KNOWN_ENTRIES
        assert load_bootstrap(tmp_path / "cfg") is None, "config.toml is gone"
        assert not backend.items, "this workspace's keys are gone"
        assert run.app.state.workspace is None
        assert run.decision == StartupDecision("guide", "welcome")
        assert_see_other(run.client.get("/"), WELCOME)
        assert_see_other(run.client.get("/inbox"), WELCOME)
        assert run.client.get(WELCOME).status_code == 200
        assert lock_is_free(data), "the data-folder lock was released"
        assert run.app.state.runner.status == "running", "the runner is ready for a later open"

        # The guide can start over: a new folder opens, with its tasks running.
        fresh = tmp_path / "fresh-data"
        run.app.state.open_workspace(fresh)
        assert run.app.state.workspace is not None
        assert is_workspace_database(fresh / "app.db")
        assert run.app.state.task_service is not None
        assert run.app.state.runner.status == "running"


def test_delete_all_data_reopens_a_fresh_workspace_when_the_folder_is_fixed(
    tmp_path: Path,
) -> None:
    data = completed(tmp_path)
    with started_app(tmp_path, "cli") as run:
        assert run.client.get("/").status_code == 200
        before = run.app.state.workspace
        old_task_service = run.app.state.task_service

        run.app.state.data_erasure.erase(CONFIRM)

        assert run.app.state.workspace is not None and run.app.state.workspace is not before
        assert run.app.state.task_service is not old_task_service
        assert is_workspace_database(data / "app.db")
        # The fresh open database has its own journal files (WAL mode) and the app's usual folders.
        fresh = {"app.db", "app.db-wal", "app.db-shm", "logs", "backups", "tmp"}
        assert not top_level(data) & (KNOWN_ENTRIES - fresh)
        assert MARKER.encode() not in all_bytes_under(data)
        assert load_bootstrap(tmp_path / "cfg") is None
        assert run.decision == StartupDecision("guide", "welcome")
        assert_see_other(run.client.get("/"), WELCOME)
        assert run.client.get(WELCOME).status_code == 200
        with Database.open(data / "app.db") as peek:
            assert peek.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
            assert (
                peek.connection.execute(
                    "SELECT COUNT(*) FROM settings WHERE key='fictional_marker'"
                ).fetchone()[0]
                == 0
            )


@pytest.mark.parametrize("source", ["bootstrap", "cli"])
def test_delete_all_data_clears_in_memory_holders(tmp_path: Path, source: str) -> None:
    completed(tmp_path)
    with started_app(tmp_path, source) as run:
        state = run.app.state
        holder = state.wiring.search_results
        holder.put("brief:company-1", {"fictional": "brief text " + MARKER})
        holder.put("web:question-1", ["fictional result " + MARKER])
        old = {
            name: getattr(state, name)
            for name in ("facts", "fact_library", "roles", "skills", "usage", "task_service")
        }
        assert all(value is not None for value in old.values())

        state.data_erasure.erase(CONFIRM)

        assert holder.get("brief:company-1") is None
        assert holder.get("web:question-1") is None
        for name, value in old.items():
            assert getattr(state, name) is not value, f"{name} still points at the erased workspace"
        if source == "bootstrap":  # nothing is open: the services are gone, not replaced
            for name in ("workspace", "task_service", "usage", "provider_health", *old):
                assert getattr(state, name) is None, name
            assert run.client.get("/health").status_code == 200


def test_delete_all_data_stops_running_tasks_and_the_runner_can_start_again(
    tmp_path: Path,
) -> None:
    started, cancelled, ran = threading.Event(), threading.Event(), threading.Event()

    async def blocking(ctx: Any) -> str | None:
        started.set()
        try:
            await asyncio.Event().wait()  # until the runner cancels it
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return None

    async def quick(ctx: Any) -> str | None:
        ran.set()
        return None

    handlers = HandlerRegistry()
    handlers.register(
        "fictional_blocking",
        TaskTypeSpec(timeout=timedelta(minutes=10), label="Blocking"),
        blocking,
    )
    handlers.register(
        "fictional_quick", TaskTypeSpec(timeout=timedelta(minutes=10), label="Quick"), quick
    )
    completed(tmp_path)
    with started_app(tmp_path, "cli", task_registry=handlers) as run:
        run.app.state.task_service.enqueue("fictional_blocking", "target-1")
        wait_until(started.is_set, timeout=DEADLINE_S, message="the task started")

        run.app.state.data_erasure.erase(CONFIRM)

        assert cancelled.is_set(), "the running task was interrupted before the database closed"
        assert run.app.state.runner.status == "running", "the runner was started again"
        # The fresh workspace's runner claims and runs new work.
        run.app.state.task_service.enqueue("fictional_quick", "target-2")
        wait_until(ran.is_set, timeout=DEADLINE_S, message="a new task ran after erasing")


def test_delete_all_data_in_the_app_uses_this_workspaces_keychain_and_reports_environment(
    tmp_path: Path,
) -> None:
    completed(tmp_path)
    backend = MemoryKeyring()
    for service_name in (SERVICE_NAME, DEMO_SERVICE_NAME, "fictional-other-app"):
        planted_keys(KeyringSecretStore(service_name, backend=backend))
    environment = {"CLAUDE_CODE_OAUTH_TOKEN": "fictional-environment-value-4402"}

    with started_app(tmp_path, "bootstrap", backend=backend, environ=environment) as run:
        result = run.app.state.data_erasure.erase(CONFIRM)

    assert (SERVICE_NAME, ANTHROPIC_API_KEY) not in backend.items
    assert (SERVICE_NAME, CLAUDE_CODE_OAUTH_TOKEN) not in backend.items
    assert backend.items[(DEMO_SERVICE_NAME, ANTHROPIC_API_KEY)] == SECRET_VALUES[ANTHROPIC_API_KEY]
    assert (
        backend.items[("fictional-other-app", CLAUDE_CODE_OAUTH_TOKEN)]
        == SECRET_VALUES[CLAUDE_CODE_OAUTH_TOKEN]
    )
    assert tuple(result.environment_keys) == (CLAUDE_CODE_OAUTH_TOKEN,)


def test_delete_all_data_in_the_demo_app_leaves_the_real_setup_alone(tmp_path: Path) -> None:
    demo_data = tmp_path / "demo-data"
    all_steps = ("welcome", "provider", "web_search", "cv_import", "preferences", "done")
    seed_workspace(demo_data, done=all_steps, clock=FrozenClock(START), provider=HARNESS_PROVIDER)
    (demo_data / "logs").mkdir()
    (demo_data / "logs" / "app.log").write_text("fictional demo log " + MARKER)
    real = make_workspace(tmp_path, "real-data")
    write_bootstrap(tmp_path / "cfg", data_dir=real, version=__version__)
    real_before, config_before = snapshot(real), snapshot(tmp_path / "cfg")
    backend = MemoryKeyring()
    for service_name in (SERVICE_NAME, DEMO_SERVICE_NAME):
        planted_keys(KeyringSecretStore(service_name, backend=backend))

    with started_app(tmp_path, "demo", demo=True, backend=backend, data=demo_data) as run:
        result = run.app.state.data_erasure.erase(CONFIRM)

        assert result.config_removed is False
        assert run.app.state.workspace is not None, "the demo folder is fixed: reopened"
        assert not (demo_data / "logs" / "app.log").exists()
        assert run.decision == StartupDecision("guide", "welcome")

    assert snapshot(real) == real_before
    assert snapshot(tmp_path / "cfg") == config_before
    for key in KNOWN_SECRET_KEYS:
        assert (DEMO_SERVICE_NAME, key) not in backend.items
        assert backend.items[(SERVICE_NAME, key)] == SECRET_VALUES[key]


# --- review round: the service ----------------------------------------------------------------


def test_data_erasure_bootstrap_name_matches_the_config_module() -> None:
    assert _module().BOOTSTRAP_FILE == app_config.BOOTSTRAP_FILE


def test_delete_all_data_keeps_keys_and_config_that_belong_to_another_folder(
    tmp_path: Path,
) -> None:
    scratch = make_workspace(tmp_path, "scratch")
    real = make_workspace(tmp_path, "real-data", files=False)
    config_dir = config_dir_with_bootstrap(tmp_path, real)
    store = MemorySecretStore()
    planted_keys(store)
    before = snapshot(config_dir)
    service = make_service(scratch, store, config_dir, recorder=Recorder(scratch))

    result = service.erase(CONFIRM)

    assert not top_level(scratch) & KNOWN_ENTRIES, "the erased folder itself is emptied"
    assert snapshot(config_dir) == before, "the setup file belongs to another folder"
    assert store.get(ANTHROPIC_API_KEY) == SECRET_VALUES[ANTHROPIC_API_KEY]
    assert store.get(CLAUDE_CODE_OAUTH_TOKEN) == SECRET_VALUES[CLAUDE_CODE_OAUTH_TOKEN]
    assert result.config_removed is False and result.keys_removed == ()
    assert (
        "Saved keys and the setup file were kept because they belong to another data folder."
        in result.messages
    )
    assert str(tmp_path) not in " ".join(result.messages)


def test_delete_all_data_removes_keys_and_config_when_the_setup_file_names_this_folder(
    tmp_path: Path,
) -> None:
    data = make_workspace(tmp_path, "real-data")
    alias = tmp_path / "alias-of-the-data-folder"
    alias.symlink_to(data)  # the same real path under another name
    config_dir = config_dir_with_bootstrap(tmp_path, alias)
    store = MemorySecretStore()
    planted_keys(store)
    service = make_service(data, store, config_dir, recorder=Recorder(data))

    result = service.erase(CONFIRM)

    assert result.config_removed is True and load_bootstrap(config_dir) is None
    assert store.get(ANTHROPIC_API_KEY) is None and store.get(CLAUDE_CODE_OAUTH_TOKEN) is None
    assert not any("kept" in message for message in result.messages)


def test_delete_all_data_keeps_keys_and_config_when_the_setup_file_cant_be_read(
    tmp_path: Path,
) -> None:
    data = make_workspace(tmp_path)
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text("this is = not [valid toml")
    store = MemorySecretStore()
    planted_keys(store)
    service = make_service(data, store, config_dir, recorder=Recorder(data))

    result = service.erase(CONFIRM)

    assert (config_dir / "config.toml").exists() and result.config_removed is False
    assert store.get(ANTHROPIC_API_KEY) == SECRET_VALUES[ANTHROPIC_API_KEY]
    assert any("kept" in message for message in result.messages)


def test_delete_all_data_removes_the_database_last_so_a_failed_run_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = make_workspace(tmp_path)
    config_dir = config_dir_with_bootstrap(tmp_path, data)
    store = MemorySecretStore()
    planted_keys(store)
    module = _module()
    real_remove = module._remove

    def failing_remove(path: Path) -> None:
        if path.name == "documents":
            raise PermissionError("fictional refusal")
        real_remove(path)

    monkeypatch.setattr(module, "_remove", failing_remove)
    service = make_service(data, store, config_dir, recorder=Recorder(data))

    with pytest.raises(module.ErasureIncomplete) as failure:
        service.erase(CONFIRM)

    assert failure.value.failed == "documents"
    assert is_workspace_database(data / "app.db"), "a recognisable workspace is left to retry"
    assert (data / "app.db-wal").exists() and (data / "app.db-shm").exists()
    assert store.get(ANTHROPIC_API_KEY) is not None and load_bootstrap(config_dir) is not None

    monkeypatch.setattr(module, "_remove", real_remove)
    result = service.erase(CONFIRM)

    assert not top_level(data) & KNOWN_ENTRIES and result.config_removed is True


def test_delete_all_data_removes_the_database_files_after_every_other_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = make_workspace(tmp_path)
    (data / ".restore-abc123").mkdir()
    module = _module()
    order: list[str] = []
    real_remove = module._remove

    def spy(path: Path) -> None:
        order.append(path.name)
        real_remove(path)

    monkeypatch.setattr(module, "_remove", spy)

    make_service(data, MemorySecretStore(), tmp_path / "cfg", recorder=Recorder(data)).erase(
        CONFIRM
    )

    entries = [name for name in order if name in KNOWN_ENTRIES or name.startswith(".restore-")]
    assert entries[-3:] == ["app.db-wal", "app.db-shm", "app.db"]
    assert ".restore-abc123" in entries[:-3]


def test_delete_all_data_settles_after_a_finished_a_stopped_and_a_refused_run(
    tmp_path: Path,
) -> None:
    data = make_workspace(tmp_path)
    settled: list[str] = []
    store = FlakyStore(failing_key=CLAUDE_CODE_OAUTH_TOKEN)
    planted_keys(store)
    config_dir = config_dir_with_bootstrap(tmp_path, data)
    service = make_service(
        data,
        store,
        config_dir,
        recorder=Recorder(data),
        settle=lambda: settled.append("settled"),
    )

    with pytest.raises(_module().ErasureRefused):
        service.erase("nope")
    assert settled == [], "a refusal never closed anything"

    with pytest.raises(_module().ErasureIncomplete):
        service.erase(CONFIRM)
    assert settled == ["settled"]

    store.heal()
    service.erase(CONFIRM)
    assert settled == ["settled", "settled"]


def test_delete_all_data_settles_when_closing_fails(tmp_path: Path) -> None:
    data = make_workspace(tmp_path)
    settled: list[str] = []

    def failing_close() -> None:
        raise RuntimeError("fictional close failure")

    service = _module().DataErasureService(
        lambda: data,
        MemorySecretStore(),
        tuple(KNOWN_SECRET_KEYS),
        tmp_path / "cfg",
        close_workspace=failing_close,
        reopen=None,
        demo=False,
        bootstrap_folder=named_by(tmp_path / "cfg"),
        settle=lambda: settled.append("settled"),
    )

    with pytest.raises(_module().ErasureIncomplete) as failure:
        service.erase(CONFIRM)

    assert failure.value.failed == "workspace" and settled == ["settled"]
    assert top_level(data) >= KNOWN_ENTRIES, "nothing was deleted"


def test_delete_all_data_looks_again_after_closing_and_sweeps_late_writes(
    tmp_path: Path,
) -> None:
    data = make_workspace(tmp_path)
    (data / "exports").rename(data / "exports-moved")  # no exports yet
    (data / "exports-moved").rename(data / "my-exports")  # an unknown entry, left alone

    class LateStore(MemorySecretStore):
        def delete(self, key: str) -> None:
            late = data / "logs"
            late.mkdir(exist_ok=True)
            (late / "app.log").write_text("fictional late log " + MARKER)
            super().delete(key)

    def close() -> None:
        (data / "exports").mkdir()  # the closing itself wrote a known entry
        (data / "exports" / "late.json").write_text("fictional late export " + MARKER)

    service = _module().DataErasureService(
        lambda: data,
        LateStore(),
        tuple(KNOWN_SECRET_KEYS),
        tmp_path / "cfg",
        close_workspace=close,
        reopen=None,
        demo=False,
        bootstrap_folder=named_by(tmp_path / "cfg"),
    )

    result = service.erase(CONFIRM)

    assert not top_level(data) & KNOWN_ENTRIES, "the entry made while closing and the late log go"
    assert len(result.removed) == len(set(result.removed))
    assert "exports" in result.removed and "logs" in result.removed
    assert "my-exports" in result.left


def test_delete_all_data_refuses_a_link_that_appears_while_closing(tmp_path: Path) -> None:
    data = make_workspace(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "precious.txt").write_text("fictional outside file")

    def close() -> None:
        (data / "backups").rename(data / "backups-real")
        (data / "backups").symlink_to(outside)

    service = _module().DataErasureService(
        lambda: data,
        MemorySecretStore(),
        tuple(KNOWN_SECRET_KEYS),
        tmp_path / "cfg",
        close_workspace=close,
        reopen=None,
        demo=False,
        bootstrap_folder=named_by(tmp_path / "cfg"),
    )

    with pytest.raises(_module().ErasureRefused):
        service.erase(CONFIRM)

    assert (outside / "precious.txt").read_text() == "fictional outside file"
    assert (data / "documents").exists() and (data / "app.db").exists()


def test_delete_all_data_never_reads_a_secret_to_check_it_is_there(tmp_path: Path) -> None:
    data = make_workspace(tmp_path)

    class WriteOnlyStore(MemorySecretStore):
        def get(self, key: str) -> str | None:
            raise AssertionError("a secret was read")

    store = WriteOnlyStore()
    store.set(ANTHROPIC_API_KEY, SECRET_VALUES[ANTHROPIC_API_KEY])
    service = make_service(data, store, tmp_path / "cfg", recorder=Recorder(data))

    result = service.erase(CONFIRM)

    assert set(result.keys_removed) == set(KNOWN_SECRET_KEYS)
    assert store._values == {}


def test_delete_all_data_without_a_keychain_skips_the_keys(tmp_path: Path) -> None:
    data = make_workspace(tmp_path)
    service = make_service(
        data, MemorySecretStore(available=False), tmp_path / "cfg", recorder=Recorder(data)
    )

    result = service.erase(CONFIRM)

    assert result.keys_removed == () and not top_level(data) & KNOWN_ENTRIES


def test_delete_all_data_stops_at_a_mount_point_inside_an_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = make_workspace(tmp_path)
    (data / "backups" / "volume").mkdir()
    (data / "backups" / "volume" / "inside.db").write_bytes(b"fictional volume content")
    mounted = data / "backups" / "volume"
    real_ismount = os.path.ismount
    monkeypatch.setattr(
        os.path, "ismount", lambda path: Path(path) == mounted or real_ismount(path)
    )
    module = _module()
    service = make_service(data, MemorySecretStore(), tmp_path / "cfg", recorder=Recorder(data))

    with pytest.raises(module.ErasureIncomplete) as failure:
        service.erase(CONFIRM)

    assert failure.value.failed == "backups"
    assert (mounted / "inside.db").read_bytes() == b"fictional volume content"
    assert is_workspace_database(data / "app.db"), "the database is still there to retry"


# --- review round: the app --------------------------------------------------------------------


def test_delete_all_data_finds_the_folder_the_setup_file_names_after_a_restart(
    tmp_path: Path,
) -> None:
    data = completed(tmp_path)
    backend = FlakyKeyring(failing_key=CLAUDE_CODE_OAUTH_TOKEN)
    planted_keys(KeyringSecretStore(SERVICE_NAME, backend=backend))
    with started_app(tmp_path, "bootstrap", backend=backend) as run:
        with pytest.raises(_module().ErasureIncomplete) as failure:
            run.app.state.data_erasure.erase(CONFIRM)
        assert failure.value.failed == f"key:{CLAUDE_CODE_OAUTH_TOKEN}"
    assert not (data / "app.db").exists(), "the entries were removed before the key failed"
    (data / "logs").mkdir()
    (data / "logs" / "app.log").write_text("fictional leftover " + MARKER)
    backend.heal()

    with started_app(tmp_path, "bootstrap", backend=backend) as again:
        assert again.app.state.workspace is None, "no database: step 1 is next"
        service = again.app.state.data_erasure
        assert service.plan().known == ("logs",), "the folder config.toml names is used"

        result = service.erase(CONFIRM)

        assert result.removed == ("logs",) and result.config_removed is True
        assert load_bootstrap(tmp_path / "cfg") is None
        assert not backend.items


def test_delete_all_data_leaves_a_workspace_that_a_restart_opens_and_erases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = completed(tmp_path)
    module = _module()
    real_remove = module._remove

    def failing_remove(path: Path) -> None:
        if path.name == "documents":
            raise PermissionError("fictional refusal")
        real_remove(path)

    with started_app(tmp_path, "bootstrap") as run:
        monkeypatch.setattr(module, "_remove", failing_remove)
        with pytest.raises(module.ErasureIncomplete) as failure:
            run.app.state.data_erasure.erase(CONFIRM)
        assert failure.value.failed == "documents"
    monkeypatch.setattr(module, "_remove", real_remove)
    assert is_workspace_database(data / "app.db")

    with started_app(tmp_path, "bootstrap") as again:
        assert again.app.state.workspace is not None, "a restart opens the workspace again"

        again.app.state.data_erasure.erase(CONFIRM)

        assert not top_level(data) & KNOWN_ENTRIES
        assert load_bootstrap(tmp_path / "cfg") is None


def test_delete_all_data_partial_failure_and_retry_through_the_app(tmp_path: Path) -> None:
    data = completed(tmp_path)
    backend = FlakyKeyring(failing_key=CLAUDE_CODE_OAUTH_TOKEN)
    planted_keys(KeyringSecretStore(SERVICE_NAME, backend=backend))
    closes: list[str] = []

    with started_app(
        tmp_path, "cli", backend=backend, detach_log_folder=lambda: closes.append("closed")
    ) as run:
        service = run.app.state.data_erasure
        with pytest.raises(_module().ErasureIncomplete):
            service.erase(CONFIRM)

        assert closes == ["closed"]
        assert run.app.state.workspace is None, "no reopen on a half-erased folder"
        assert service.plan().unknown == (), "the folder is still known: no workspace is open"
        (data / "logs").mkdir(exist_ok=True)
        (data / "logs" / "app.log").write_text("fictional late log " + MARKER)
        assert service.plan().known == ("logs",), "the remembered folder is used"

        backend.heal()
        result = service.erase(CONFIRM)

        assert closes == ["closed", "closed"], "closing again is safe"
        assert "logs" in result.removed
        assert CLAUDE_CODE_OAUTH_TOKEN in result.keys_removed
        assert run.app.state.workspace is not None, "reopened after the retry succeeded"
        assert MARKER.encode() not in all_bytes_under(data)
        assert not backend.items


@pytest.mark.parametrize("source", ["cli", "bootstrap"])
def test_delete_all_data_rebuilds_the_provider_setup_for_the_next_workspace(
    tmp_path: Path, source: str
) -> None:
    completed(tmp_path)
    with started_app(tmp_path, source) as run:
        step = run.app.state.provider_settings
        assert step.context()["workspace_open"] is True
        assert run.client.get("/setup/provider").status_code == 200

        run.app.state.data_erasure.erase(CONFIRM)

        if source == "bootstrap":
            assert step.context()["workspace_open"] is False
            assert run.client.get("/setup/provider").status_code == 200
            run.app.state.open_workspace(tmp_path / "fresh-data")
        context = step.context()  # a stale service raises on its closed database
        assert context["workspace_open"] is True and context["saved_kinds"] == ()
        assert run.client.get("/setup/provider").status_code == 200
        assert run.client.get("/settings").status_code in (200, 303)


@pytest.mark.parametrize("source", ["cli", "bootstrap"])
def test_delete_all_data_hands_the_command_lines_folder_lock_on(
    tmp_path: Path, source: str
) -> None:
    data = completed(tmp_path)
    lock = lock_data_folder(data)
    try:
        with started_app(tmp_path, source, lock=lock) as run:
            service = run.app.state.data_erasure
            service.erase(CONFIRM)

            if source == "cli":
                assert lock.held, "a fixed folder keeps the lock for its fresh workspace"
                assert run.app.state.workspace is not None
                service.erase(CONFIRM)  # and again: the lock is reused each time
                assert run.app.state.workspace is not None and lock.held
            else:
                assert not lock.held and lock_is_free(data)
                run.app.state.open_workspace(data)  # the same folder at step 1: not "in use"
                assert run.app.state.workspace is not None
    finally:
        lock.release()


def test_delete_all_data_keeps_the_setup_file_of_another_folder_in_the_app(
    tmp_path: Path,
) -> None:
    scratch = tmp_path / "scratch"
    complete_setup(scratch, tmp_path / "scratch-cfg", clock=FrozenClock(START))
    real = make_workspace(tmp_path, "real-data", files=False)
    write_bootstrap(tmp_path / "cfg", data_dir=real, version=__version__)
    backend = MemoryKeyring()
    planted_keys(KeyringSecretStore(SERVICE_NAME, backend=backend))
    config_before = snapshot(tmp_path / "cfg")

    with started_app(tmp_path, "cli", backend=backend, data=scratch) as run:
        result = run.app.state.data_erasure.erase(CONFIRM)

        assert result.config_removed is False and result.keys_removed == ()
        assert any("another data folder" in message for message in result.messages)

    assert snapshot(tmp_path / "cfg") == config_before
    assert len(backend.items) == len(KNOWN_SECRET_KEYS), "the saved keys are untouched"


def test_delete_all_data_holds_off_new_opens_until_it_has_finished(tmp_path: Path) -> None:
    completed(tmp_path)
    backend = FlakyKeyring(failing_key=CLAUDE_CODE_OAUTH_TOKEN)
    planted_keys(KeyringSecretStore(SERVICE_NAME, backend=backend))
    outcomes: list[str] = []

    with started_app(tmp_path, "bootstrap", backend=backend) as run:

        def try_open() -> None:
            try:
                run.app.state.open_workspace(tmp_path / "other-data")
            except WorkspaceBusy:
                outcomes.append("busy")
            else:  # pragma: no cover - the failure being tested
                outcomes.append("opened")

        backend.on_delete = try_open  # during the key step, after the workspace closed
        with pytest.raises(_module().ErasureIncomplete):
            run.app.state.data_erasure.erase(CONFIRM)
        backend.on_delete = None

        assert outcomes and set(outcomes) == {"busy"}
        assert run.app.state.workspace is None
        # After a stopped run opening is allowed again.
        run.app.state.open_workspace(tmp_path / "other-data")
        assert run.app.state.workspace is not None


def test_delete_all_data_restart_runner_refuses_the_event_loop_and_a_slow_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main_module

    class SlowRunner:
        def __init__(self) -> None:
            self.detached = False

        async def stop(self) -> None:
            await asyncio.Event().wait()  # never ends

        def detach(self) -> None:
            self.detached = True

        async def start(self) -> None:  # pragma: no cover - never reached
            raise AssertionError("started after a stop that didn't finish")

    ready = threading.Event()
    holder: dict[str, Any] = {}

    def serve() -> None:
        loop = asyncio.new_event_loop()
        holder["loop"] = loop
        loop.call_soon(ready.set)
        loop.run_forever()
        loop.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(DEADLINE_S)
    loop = holder["loop"]
    try:
        # Nothing to restart before the lifespan has started the runner.
        main_module._restart_runner(SlowRunner(), [])  # type: ignore[arg-type]

        monkeypatch.setattr(main_module, "RUNNER_RESTART_TIMEOUT_SECONDS", 0.2)
        runner = SlowRunner()
        with pytest.raises(RuntimeError, match="in time"):
            main_module._restart_runner(runner, [loop])  # type: ignore[arg-type]
        assert runner.detached is False

        # On the loop's own thread it would wait for itself.
        async def from_the_loop() -> None:
            with pytest.raises(RuntimeError, match="event loop"):
                main_module._restart_runner(SlowRunner(), [loop])  # type: ignore[arg-type]

        asyncio.run_coroutine_threadsafe(from_the_loop(), loop).result(DEADLINE_S)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(DEADLINE_S)


def test_delete_all_data_detaches_and_reattaches_the_log_file_of_a_fixed_folder(
    tmp_path: Path,
) -> None:
    data = completed(tmp_path)
    root = logging.getLogger()
    saved = (list(root.handlers), root.level)
    setup = configure_logging(data / "logs", "INFO", Redactor())  # the command line's own
    probe = logging.getLogger("jobpipeline.test.erasure")
    try:
        with started_app(
            tmp_path,
            "cli",
            detach_log_folder=setup.detach_log_folder,
            reattach_log_folder=setup.attach_log_folder,
        ) as run:
            probe.info("before the erase %s", MARKER)

            run.app.state.data_erasure.erase(CONFIRM)

            probe.info("after the erase")
            for handler in root.handlers:
                handler.flush()
            text = (data / "logs" / LOG_FILE_NAME).read_text(encoding="utf-8")
            assert "after the erase" in text, "the new workspace logs into its fresh folder"
            assert MARKER not in text
            rotating = logging.handlers.RotatingFileHandler
            files = [h for h in root.handlers if isinstance(h, rotating)]
            assert len(files) == 1
    finally:
        setup.close()
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])


def test_the_runner_idles_after_a_bootstrap_erase_with_no_claim_errors(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    completed(tmp_path)
    with started_app(tmp_path, "bootstrap") as run:
        runner = run.app.state.runner
        with caplog.at_level(logging.ERROR):
            run.app.state.data_erasure.erase(CONFIRM)
            runner.notify()
            # An observation window: a worker still holding the closed database claims at once.
            threading.Event().wait(0.5)

        assert runner.status == "running"
        assert "claiming a task failed" not in caplog.text
