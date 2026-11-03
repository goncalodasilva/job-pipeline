"""Step 1: welcome and data folder (M1.16; AC-M1.1, AC-M1.6; ADR-044, ADR-049, ADR-050).

The user picks the data folder; submitting opens it (folder and database), writes
``config.toml`` naming it and marks the step done. A folder from ``--data-dir``, the environment
or demo mode is fixed, and so is a workspace that is already open: then the step never opens it
again and only writes ``config.toml`` when none exists (never in demo mode, and never over an
existing one). When ``config.toml`` names a folder that has gone, the user can locate it or
start fresh.

"Restore from an export" (M9.1 part D; ``app/api/setup_restore.py``) opens the folder the way
"Start fresh" does (``open_for_restore``) and, once the export is restored, names it in
``config.toml`` under the same rule as finishing this step (``finish_restore``). It never marks
this step done: the restored ``setup_state`` decides.

Collaborators are injected by wiring, so this module touches neither the app state nor files
beyond the read-only folder inspection.
"""

import logging
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.domain.errors import ConfigError
from app.onboarding.progress import SetupProgress
from app.onboarding.registry import WELCOME, Form, StepResult
from app.onboarding.startup import OnboardingState
from app.services.data_folder import FolderState, inspect_data_folder

_log = logging.getLogger(__name__)

DEMO_COMMAND = "uv run jobpipeline --demo"
ACTIONS = ("open", "locate", "fresh")
ACCEPTED: Mapping[str, frozenset[FolderState]] = {
    "open": frozenset({"missing", "empty", "workspace"}),
    "locate": frozenset({"workspace"}),
    "fresh": frozenset({"missing", "empty"}),
    "restore": frozenset({"missing", "empty"}),  # setup's "Restore from an export"
}

MSG_EMPTY = "Enter the folder where the app should keep your data."
MSG_INVALID = "This folder name contains characters that can't be used. Choose another folder."
MSG_RELATIVE = (
    "Enter the full path, starting from the top of the drive "
    "(for example /Users/you/JobSearch or C:\\Users\\you\\JobSearch)."
)
MSG_RESERVED = "This folder is kept for the app's own settings. Choose another folder."
MSG_FILE = "That's a file, not a folder. Choose a folder."
MSG_NOT_EMPTY = "This folder already holds other files. Choose an empty or new folder."
MSG_NOT_FOUND = "No data from this app was found in that folder. Check the path and try again."
MSG_HAS_DATA = (
    "This folder isn't empty, so starting fresh here could mix up files. "
    "Choose an empty or new folder."
)
MSG_ALREADY_WORKSPACE = (
    "This folder already holds data from this app. To keep using it, choose "
    '"Locate the folder"; to start fresh, choose an empty or new folder.'
)
# "Restore from an export" (setup) says "restoring", not "starting fresh", and never points at
# buttons that step 1 may not show.
MSG_HAS_DATA_RESTORE = (
    "This folder isn't empty, so restoring here could mix up files. Choose an empty or new folder."
)
MSG_ALREADY_WORKSPACE_RESTORE = (
    "This folder already holds data from this app, so restoring here could mix up your data. "
    "Choose an empty or new folder."
)
MSG_UNKNOWN_ACTION = "That choice isn't available. Reload the page and try again."
MSG_OPEN_FAILED = (
    "That folder couldn't be used. Check that you can save files there, or choose another "
    "folder. Nothing was changed."
)
MSG_SAVE_FAILED = "Your choice couldn't be saved. Try again."
MSG_BOOTSTRAP_FAILED = (
    "Your data folder is ready, but the app couldn't save where it is. Try again."
)


@dataclass(frozen=True)
class WelcomeSettings:
    suggested_folder: Path  # the configured folder: the default, the bootstrap's or an explicit one
    folder_fixed: bool  # from --data-dir, JOBPIPELINE_DATA_DIR or --demo
    demo: bool
    refused_folders: Sequence[Path]  # the config folder and the other mode's default folder
    config_folder: Path  # may be the data folder itself (macOS, Windows; ADR-060)
    bootstrap_name: str = "config.toml"


class WelcomeStep:
    echo_fields: tuple[str, ...] = ("folder",)  # shown back as typed after a refused submit

    def __init__(
        self,
        settings: WelcomeSettings,
        *,
        onboarding: OnboardingState,
        workspace_folder: Callable[[], Path | None],
        open_workspace: Callable[[Path], None],
        bootstrap_exists: Callable[[], bool],
        write_bootstrap: Callable[[Path], None],
        progress: Callable[[], SetupProgress | None],
    ) -> None:
        self._settings = settings
        self._onboarding = onboarding
        self._workspace_folder = workspace_folder
        self._open_workspace = open_workspace
        self._bootstrap_exists = bootstrap_exists
        self._write_bootstrap = write_bootstrap
        self._progress = progress
        self._opened: Path | None = None  # the folder this step opened, if any
        # The folder a restore attempt opened and the step has not been continued from since: the
        # page then says it is open now, not that the app was started with it.
        self._restore_opened: Path | None = None

    def context(self) -> Mapping[str, object]:
        open_folder = self._workspace_folder()
        locate = open_folder is None and self._onboarding.decision.kind == "locate_or_fresh"
        return {
            "folder": str(open_folder or self._settings.suggested_folder),
            "folder_fixed": open_folder is not None or self._settings.folder_fixed,
            "explicit_folder": self._settings.folder_fixed,
            "opened_for_restore": open_folder is not None and open_folder == self._restore_opened,
            "mode": "locate_or_fresh" if locate else "choose",
            "demo": self._settings.demo,
            "demo_command": DEMO_COMMAND,
        }

    def submit(self, form: Form) -> StepResult:
        action = _first(form, "action") or "open"
        if action not in ACTIONS:
            return StepResult(ok=False, errors={"form": MSG_UNKNOWN_ACTION})
        open_folder = self._workspace_folder()
        if open_folder is not None:
            return self._finish_open(open_folder)
        if self._settings.folder_fixed:  # explicit folders open at start-up; never from the form
            return StepResult(ok=False, errors={"form": MSG_OPEN_FAILED})
        folder = self._parse_folder(_first(form, "folder"))
        if isinstance(folder, str):  # the problem, in plain words
            return StepResult(ok=False, errors={"folder": folder})
        return self._open(folder, action=action)

    # --- helpers ---------------------------------------------------------------------------

    def _parse_folder(self, raw: str | None) -> Path | str:
        """The folder, or the message explaining why the text can't be used."""
        text = (raw or "").strip()
        if not text:
            return MSG_EMPTY
        if "\x00" in text:
            return MSG_INVALID
        try:
            folder = Path(text).expanduser()
        except RuntimeError:  # "~user" that can't be expanded
            return MSG_RELATIVE
        if not folder.is_absolute():
            return MSG_RELATIVE
        try:
            reserved = any(
                _same_or_inside(folder, refused) for refused in self._settings.refused_folders
            )
        except (OSError, RuntimeError):  # a symlink loop, or a path the system can't read
            return MSG_INVALID
        if reserved:
            return MSG_RESERVED
        return folder

    def open_for_restore(self, raw_folder: str | None) -> StepResult:
        """Open the folder a restore goes into (setup's "Restore from an export"), as "Start
        fresh" does: only a missing or empty folder, the same messages, the workspace opened and
        ``self._opened`` set. A workspace that is already open is used as it is and
        ``raw_folder`` is ignored. Never marks the step done. ``ok`` means a workspace is open."""
        if self._workspace_folder() is not None:
            return StepResult(ok=True)
        if self._settings.folder_fixed:  # explicit folders open at start-up; never from the form
            return StepResult(ok=False, errors={"form": MSG_OPEN_FAILED})
        folder = self._parse_folder(raw_folder)
        if isinstance(folder, str):
            return StepResult(ok=False, errors={"folder": folder})
        opened = self._open_folder(folder, action="restore")
        if opened.ok:
            self._restore_opened = folder
        return opened

    def finish_restore(self) -> bool:
        """After a restore: ``config.toml`` names the folder exactly when finishing this step
        would write it (``_finish_open``), then the setup decision is computed again from the
        restored rows. The step itself stays as the restored data has it. False when either
        could not be saved (the data is restored; the caller says so)."""
        folder = self._workspace_folder()
        if folder is None:
            return False
        self._restore_opened = None  # restored: the folder is the restored workspace's now
        write = not self._settings.demo and (not self._bootstrap_exists() or self._opened == folder)
        if write:
            try:
                self._write_bootstrap(folder)
            except (ConfigError, OSError) as error:
                _log.warning("Settings file can't be saved (%s).", type(error).__name__)
                return False
        try:
            self._onboarding.refresh(self._progress(), bootstrap_exists=True if write else None)
        except Exception as error:  # sqlite messages can hold paths: log the type only
            _log.warning("Setup progress can't be read (%s).", type(error).__name__)
            return False
        return True

    def _open(self, folder: Path, *, action: str) -> StepResult:
        opened = self._open_folder(folder, action=action)
        if not opened.ok:
            return opened
        return self._save_and_finish(folder, write=not self._settings.demo)

    def _open_folder(self, folder: Path, *, action: str) -> StepResult:
        """Inspect, check and open ``folder``; ``ok`` once its workspace is open."""
        try:
            ignored = (
                frozenset({self._settings.bootstrap_name})
                if _same_place(folder, self._settings.config_folder)
                else frozenset()
            )
            state = inspect_data_folder(folder, ignored=ignored)
        except OSError as error:
            _log.warning("Data folder can't be inspected (%s).", type(error).__name__)
            return StepResult(ok=False, errors={"folder": MSG_OPEN_FAILED})
        if state not in ACCEPTED[action]:
            return StepResult(ok=False, errors={"folder": _refusal(state, action)})
        try:
            self._open_workspace(folder)
        except ConfigError as error:
            _log.warning("Data folder can't be opened (%s).", type(error).__name__)
            return StepResult(ok=False, errors={"folder": error.user_message})
        except Exception as error:  # sqlite, OSError, a workspace already open: plain copy
            _log.warning("Data folder can't be opened (%s).", type(error).__name__)
            return StepResult(ok=False, errors={"folder": MSG_OPEN_FAILED})
        # This step chose the folder, so config.toml names it, even over an older one (locate).
        self._opened = folder
        return StepResult(ok=True)

    def _finish_open(self, folder: Path) -> StepResult:
        # Never reopened. config.toml is written when missing, or when this step opened the
        # folder and saving failed before (a retry); a folder from --data-dir or the
        # environment never replaces an existing one (ADR-050).
        write = not self._settings.demo and (not self._bootstrap_exists() or self._opened == folder)
        return self._save_and_finish(folder, write=write)

    def _save_and_finish(self, folder: Path, *, write: bool) -> StepResult:
        if write:
            try:
                self._write_bootstrap(folder)
            except (ConfigError, OSError) as error:
                _log.warning("Settings file can't be saved (%s).", type(error).__name__)
                return StepResult(ok=False, errors={"form": MSG_BOOTSTRAP_FAILED})
        try:
            progress = self._progress()
            if progress is None:  # pragma: no cover - a workspace is open by now
                return StepResult(ok=False, errors={"form": MSG_SAVE_FAILED})
            progress.mark_done(WELCOME.id)
            self._onboarding.refresh(progress, bootstrap_exists=True if write else None)
        except Exception as error:  # sqlite messages can hold paths: log the type only
            _log.warning("Setup progress can't be saved (%s).", type(error).__name__)
            return StepResult(ok=False, errors={"form": MSG_SAVE_FAILED})
        self._restore_opened = None  # continued: no longer the open question of a restore
        return StepResult(ok=True)


def _first(form: Form, name: str) -> str | None:
    values = form.get(name)
    return values[0] if values else None


def _same_or_inside(folder: Path, other: Path) -> bool:
    """Whether ``folder`` is ``other`` or inside it, also on case-insensitive file systems."""
    candidate, reserved = folder.resolve(strict=False), other.resolve(strict=False)
    return any(_same_place(place, reserved) for place in (candidate, *candidate.parents))


def _same_place(first: Path, second: Path) -> bool:
    """The same folder: by identity when both exist, else by name (ignoring case on macOS and
    Windows, whose default file systems do)."""
    try:
        if first.exists() and second.exists():
            return os.path.samefile(first, second)
    except OSError:
        pass
    return _fold(first.resolve(strict=False)) == _fold(second.resolve(strict=False))


def _fold(path: Path) -> str:
    text = os.path.normcase(str(path))
    return text.casefold() if sys.platform in ("darwin", "win32") else text


def _refusal(state: FolderState, action: str) -> str:
    if state == "file":
        return MSG_FILE
    if action == "locate":
        return MSG_NOT_FOUND
    if action == "fresh":
        return MSG_ALREADY_WORKSPACE if state == "workspace" else MSG_HAS_DATA
    if action == "restore":
        return MSG_ALREADY_WORKSPACE_RESTORE if state == "workspace" else MSG_HAS_DATA_RESTORE
    return MSG_NOT_EMPTY
