"""What a folder offered as the data folder holds (step 1; ADR-049 item 3).

The app only opens a folder that is missing, empty or already one of its workspaces, so it
never migrates a foreign SQLite file or drops ``logs/`` and ``backups/`` into an unrelated
folder. Reads only.
"""

from pathlib import Path
from typing import Literal

from app.modules.storage import LOCK_FILE, is_workspace_database

DATABASE_FILE = "app.db"

FolderState = Literal["missing", "empty", "workspace", "not_empty", "file"]

# Files the system adds to folders by itself (Finder, Explorer); they don't make one "not empty".
SYSTEM_FILES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
# The app's own lock file (ADR-072): left behind by a refused or closed open, it holds no data.
APP_FILES = frozenset({LOCK_FILE})


def inspect_data_folder(folder: Path, *, ignored: frozenset[str] = frozenset()) -> FolderState:
    """``ignored`` names top-level entries that don't count (``config.toml`` when the config
    folder is the data folder, ADR-060)."""
    try:
        if not folder.exists() and not folder.is_symlink():
            return "missing"
        if not folder.is_dir():
            return "file"
        skipped = SYSTEM_FILES | APP_FILES | ignored
        has_entries = any(entry.name not in skipped for entry in folder.iterdir())
    except NotADirectoryError:  # a parent is a file: nothing can be there
        return "missing"
    if not has_entries:
        return "empty"
    if is_workspace_database(folder / DATABASE_FILE):
        return "workspace"
    return "not_empty"
