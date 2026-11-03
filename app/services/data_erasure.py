"""Delete all data (ADR-131 item 2; spec 2.9 rules 9, 12 and 13).

``DataErasureService.erase`` removes every entry the app owns in the data folder, this
workspace's saved keychain items and ``config.toml`` (never in the demo). Entries the app does
not own are left and listed; ``.lock`` and the system's own files (``.DS_Store``) are neither.
It follows no link: a symbolic link or a hard link in a known entry refuses the whole erase
before anything changes. The workspace is closed first (tasks stopped, database closed, memory
cleared) through ``close_workspace``, which the app injects; the entries are then looked at
again, so what closing wrote is removed too. The steps run in a fixed order (entries with the
database files last, so a stopped run leaves a workspace that opens again after a restart;
keys; a final sweep for late writes; ``config.toml``) and stop at the first failure; the error
says what was done and what failed, and a second ``erase`` completes the rest. Saved keys and
``config.toml`` are removed only when there is no setup file or it names the erased folder:
a folder chosen with ``--data-dir`` next to a real setup keeps both. Environment-variable keys
are reported, not removed. Messages never name a folder or hold a value.
"""

import logging
import os
import shutil
import stat
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Final

from app.domain.errors import AppError
from app.modules.secrets import ENVIRONMENT_VARIABLES, SecretLookupStore, SecretStore
from app.modules.storage import LOCK_FILE
from app.services.data_folder import SYSTEM_FILES

_log = logging.getLogger(__name__)

CONFIRMATION_WORD: Final = "delete"
# The app's own entries. The database files go last (journal files before the database): what
# is left after a stopped run must still look like a workspace, so a restart can retry.
_DATABASE_FILES: Final[tuple[str, ...]] = ("app.db-wal", "app.db-shm", "app.db")
KNOWN_ENTRIES: Final[tuple[str, ...]] = (
    "backups",
    "documents",
    "imports",
    "assets",
    "prompts",
    "exports",
    "logs",
    "tmp",
    *_DATABASE_FILES,
)
# A killed restore leaves a full plaintext copy in a staging folder (data_portability).
_STAGING_PREFIX: Final = ".restore-"
# The setup file ``app.config.BOOTSTRAP_FILE`` (services never import app.config).
BOOTSTRAP_FILE: Final = "config.toml"
_KEY_PREFIX: Final = "key:"
MSG_KEPT_OTHER_FOLDER: Final = (
    "Saved keys and the setup file were kept because they belong to another data folder."
)
MSG_KEPT_UNREADABLE: Final = (
    "Saved keys and the setup file were kept because the setup file couldn't be read to "
    "check that they belong to this data folder."
)


class ErasureRefused(AppError):
    """Nothing was changed: a wrong confirmation, or a link in a known entry."""

    def __init__(self, user_message: str, *, detail: str | None = None) -> None:
        super().__init__(detail=detail if detail is not None else user_message)
        self._user_message = user_message

    @property
    def user_message(self) -> str:
        return self._user_message


class ErasureIncomplete(AppError):
    """A step failed. ``done`` lists the steps finished in this run and ``failed`` the one that
    stopped it (an entry's name, ``key:<name>`` or ``config.toml``); trying again completes
    the rest."""

    def __init__(self, done: Sequence[str], failed: str, *, detail: str | None = None) -> None:
        super().__init__(detail=detail if detail is not None else failed)
        self.done = tuple(done)
        self.failed = failed

    @property
    def user_message(self) -> str:
        if self.failed.startswith(_KEY_PREFIX):
            what = "a saved key couldn't be removed from your keychain. Unlock it"
        elif self.failed == BOOTSTRAP_FILE:
            what = "the settings file couldn't be removed. Check that you may change it"
        elif self.failed == "workspace":
            what = "the app couldn't be closed first, so nothing was deleted. Wait a moment"
        else:
            what = f'"{self.failed}" couldn\'t be removed. Check that you may change it'
        return (
            f"Delete all data stopped: {what}, then run it again. "
            "What was already deleted stays deleted."
        )


@dataclass(frozen=True)
class ErasurePlan:
    """What an erase would do: the app's own entries present, and the entries it will leave."""

    known: tuple[str, ...]
    unknown: tuple[str, ...]


@dataclass(frozen=True)
class ErasureResult:
    removed: tuple[str, ...]
    left: tuple[str, ...]
    keys_removed: tuple[str, ...]
    environment_keys: tuple[str, ...]
    config_removed: bool
    messages: tuple[str, ...]


class DataErasureService:
    def __init__(
        self,
        data_dir: Callable[[], Path | None],
        secrets: SecretStore,
        keys: Sequence[str],
        config_dir: Path,
        *,
        close_workspace: Callable[[], None],
        reopen: Callable[[], None] | None,
        demo: bool,
        bootstrap_folder: Callable[[], Path | None],
        settle: Callable[[], None] | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._secrets = secrets
        self._keys = tuple(keys)
        self._config_dir = config_dir
        self._close_workspace = close_workspace
        self._reopen = reopen
        self._demo = demo
        self._bootstrap_folder = bootstrap_folder  # the folder config.toml names; may raise
        self._settle = settle  # called when a run is over, finished or stopped
        self._lock = threading.Lock()

    def plan(self) -> ErasurePlan:
        folder = self._folder()
        if folder is None:
            return ErasurePlan((), ())
        known, unknown = self._classify(folder)
        return ErasurePlan(tuple(known), tuple(unknown))

    def erase(self, confirmation: str) -> ErasureResult:
        """Raises ``ErasureRefused`` (nothing changed) or ``ErasureIncomplete`` (retryable)."""
        if confirmation.strip() != CONFIRMATION_WORD:
            raise ErasureRefused(f"Type the word {CONFIRMATION_WORD} to confirm.")
        with self._lock:
            folder = self._folder()
            if folder is not None:
                self._refuse_links(folder, self._classify(folder)[0])
            try:
                return self._run(folder)
            finally:
                self._settled()

    def _settled(self) -> None:
        if self._settle is None:
            return
        try:
            self._settle()
        except Exception as error:
            _log.error("Finishing delete all data failed (%s)", type(error).__name__)

    # --- steps ---------------------------------------------------------------------------

    def _run(self, folder: Path | None) -> ErasureResult:
        done: list[str] = []
        try:
            self._close_workspace()
        except Exception as error:
            _log.error("Closing the workspace failed (%s)", type(error).__name__)
            raise ErasureIncomplete(
                done, "workspace", detail=f"workspace: {type(error).__name__}"
            ) from None
        removed: list[str] = []
        if folder is not None:
            # Looked at again: closing writes (a last checkpoint) and may have changed things.
            known = self._classify(folder)[0]
            self._refuse_links(folder, known)
            self._remove_entries(folder, known, done, removed)
        owned, kept_message = self._setup_belongs_here(folder)
        keys_removed: list[str] = []
        if owned:
            for key in self._keys:
                label = f"{_KEY_PREFIX}{key}"
                self._step(label, done, partial(self._remove_key, key, keys_removed))
        if folder is not None:  # work started before closing can land late
            self._remove_entries(folder, self._classify(folder)[0], done, removed)
        config_removed = False
        target = self._config_dir / BOOTSTRAP_FILE
        if owned and not self._demo and os.path.lexists(target):
            self._step(BOOTSTRAP_FILE, done, partial(_remove, target))
            config_removed = True
        environment_keys = self._environment_keys() if owned else []
        messages = self._environment_messages(environment_keys)
        if kept_message is not None:
            messages.append(kept_message)
        if self._reopen is not None:
            try:
                self._reopen()
            except Exception as error:
                _log.error("Reopening after erasing failed (%s)", type(error).__name__)
                messages.append(
                    "Everything was deleted, but the app couldn't start over by itself. "
                    "Restart the app."
                )
        left = self._classify(folder)[1] if folder is not None else []
        return ErasureResult(
            removed=tuple(removed),
            left=tuple(left),
            keys_removed=tuple(keys_removed),
            environment_keys=tuple(environment_keys),
            config_removed=config_removed,
            messages=tuple(messages),
        )

    def _remove_entries(
        self, folder: Path, names: list[str], done: list[str], removed: list[str]
    ) -> None:
        """Remove the known entries ``names`` (the setup file has its own step), the database
        files last."""
        for name in sorted(names, key=lambda name: name in _DATABASE_FILES):
            if name == BOOTSTRAP_FILE:
                continue
            self._step(name, done, partial(_remove, folder / name))
            if name not in removed:
                removed.append(name)

    def _setup_belongs_here(self, folder: Path | None) -> tuple[bool, str | None]:
        """Whether the saved keys and ``config.toml`` belong to the folder being erased: there
        is no setup file, or it names this folder (same real path). In the demo the keys are
        the demo's own and the setup file is never touched."""
        if self._demo or folder is None:
            return True, None
        if not os.path.lexists(self._config_dir / BOOTSTRAP_FILE):
            return True, None
        try:
            named = self._bootstrap_folder()
        except Exception:  # unreadable or invalid: it can't be said to belong here
            return False, MSG_KEPT_UNREADABLE
        if named is None:
            return True, None
        try:
            same = os.path.realpath(named) == os.path.realpath(folder)
        except OSError:
            same = False
        return (True, None) if same else (False, MSG_KEPT_OTHER_FOLDER)

    def _step(self, label: str, done: list[str], action: Callable[[], None]) -> None:
        try:
            action()
        except Exception as error:  # only the type: a message could hold a path or a value
            _log.error("Deleting %s failed (%s)", label, type(error).__name__)
            raise ErasureIncomplete(
                done, label, detail=f"{label}: {type(error).__name__}"
            ) from None
        done.append(label)

    def _remove_key(self, key: str, removed: list[str]) -> None:
        """Delete the key from the keychain without reading it (a key that isn't there counts
        as deleted). Without a keychain there is nothing to delete, and a key that lives in
        the environment must not fail the erase."""
        store = self._secrets
        if not store.available:
            return
        store.delete(key)
        removed.append(key)

    def _environment_keys(self) -> list[str]:
        """The keys still provided by the environment (read after the keychain was cleared)."""
        store = self._secrets
        if not isinstance(store, SecretLookupStore):
            return []
        return [key for key in self._keys if store.lookup(key).source == "environment"]

    def _environment_messages(self, keys: list[str]) -> list[str]:
        messages = []
        for key in keys:
            variable = ENVIRONMENT_VARIABLES.get(key, key)
            messages.append(
                f"{variable} is set in your environment, so it can't be removed by this app. "
                "Remove it from your environment yourself."
            )
        return messages

    # --- the folder ----------------------------------------------------------------------

    def _folder(self) -> Path | None:
        folder = self._data_dir()
        if folder is None or not folder.is_dir():
            return None
        return folder

    def _config_is_here(self, folder: Path) -> bool:
        """``config.toml`` beside the database is the app's own entry, outside the demo."""
        if self._demo:
            return False
        try:
            return os.path.realpath(self._config_dir) == os.path.realpath(folder)
        except OSError:
            return False

    def _classify(self, folder: Path) -> tuple[list[str], list[str]]:
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            return [], []
        present = set(names)
        known = [name for name in KNOWN_ENTRIES if name in present]
        if self._config_is_here(folder) and BOOTSTRAP_FILE in present:
            known.append(BOOTSTRAP_FILE)
        unknown = []
        for name in names:
            if name in KNOWN_ENTRIES or (name == BOOTSTRAP_FILE and BOOTSTRAP_FILE in known):
                continue
            info = _lstat(folder / name)
            if info is None or name == LOCK_FILE:
                continue
            is_dir = stat.S_ISDIR(info.st_mode)
            if name in SYSTEM_FILES and not is_dir:
                continue
            if name.startswith(_STAGING_PREFIX) and is_dir:
                known.append(name)
            else:
                unknown.append(name)
        return known, unknown

    def _refuse_links(self, folder: Path, known: list[str]) -> None:
        for name in known:
            try:
                linked = _contains_link(folder / name)
            except OSError:
                raise ErasureRefused(
                    f'"{name}" couldn\'t be checked, so nothing was deleted. '
                    "Check that you may read it, then try again."
                ) from None
            if linked:
                raise ErasureRefused(
                    f'"{name}" contains a link (symbolic or hard), which this app never '
                    "follows, so nothing was deleted. Remove the link yourself, then try again."
                )


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except OSError:
        return None


def _contains_link(root: Path) -> bool:
    """Whether ``root`` or anything under it is a symbolic link or a file with another name.
    Never follows a link."""
    pending = [root]
    while pending:
        path = pending.pop()
        info = _lstat(path)
        if info is None:
            continue
        if stat.S_ISLNK(info.st_mode):
            return True
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as entries:
                pending.extend(Path(entry.path) for entry in entries)
        elif stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            return True
    return False


def _remove(path: Path) -> None:
    """Remove a file, a link itself or a folder tree (never through a link); gone is done."""
    info = _lstat(path)
    if info is None:
        return
    if stat.S_ISDIR(info.st_mode):
        _refuse_mount_points(path)
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _refuse_mount_points(root: Path) -> None:
    """Raise ``OSError`` when ``root`` or a folder under it is a mount point: another volume's
    files are not the app's to delete. Nothing is removed before this check passes."""
    pending = [root]
    while pending:
        path = pending.pop()
        if os.path.ismount(path):
            raise OSError("a mount point is inside the folder")
        with os.scandir(path) as entries:
            pending.extend(
                Path(entry.path) for entry in entries if entry.is_dir(follow_symlinks=False)
            )
