"""Storage module public interface: the database, the migration runner and the data-folder
lock (spec 2.4, 2.7; ADR-072)."""

from app.modules.storage.database import Database
from app.modules.storage.runner import (
    MIGRATIONS_DIR,
    Migration,
    MigrationError,
    MigrationResult,
    apply_migrations,
    load_migrations,
)
from app.modules.storage.settings_repo import (
    StoredSetting,
    get_setting,
    insert_setting,
    update_setting,
)
from app.modules.storage.setup_state_repo import StoredStep, list_steps, upsert_step
from app.modules.storage.workspace_check import is_workspace_database
from app.modules.storage.workspace_lock import LOCK_FILE, DataFolderLock, lock_data_folder

__all__ = [
    "LOCK_FILE",
    "MIGRATIONS_DIR",
    "DataFolderLock",
    "Database",
    "Migration",
    "MigrationError",
    "MigrationResult",
    "StoredSetting",
    "StoredStep",
    "apply_migrations",
    "get_setting",
    "insert_setting",
    "is_workspace_database",
    "list_steps",
    "load_migrations",
    "lock_data_folder",
    "update_setting",
    "upsert_step",
]
