"""The data-folder lock (M2.4; ADR-072, ADR-076 item 9; POSIX only, ADR-061).

Opening a workspace takes ``fcntl.flock(LOCK_EX | LOCK_NB)`` on ``<data>/.lock`` (mode 0600)
in ``open_workspace_folder``, after the folder is created and its owner checked, and before the
database is opened and migrated or the AI temp folder is cleared. The lock is held while the
workspace is open: closing the workspace's database (the setup harness does that) or shutting
the app down releases it, and the OS releases it when the process exits or is killed. A second
open of a locked folder raises ``DataFolderInUse`` with plain copy that names no folder; step 1
of the guide shows it as an error on the folder field.

The tests hold the lock themselves through a separate open of ``<data>/.lock``: ``flock`` locks
belong to the open file, so a second open in the same process conflicts as another process
would. ``DataFolderInUse`` is imported inside the tests, so a missing class fails each test on
its own. Every folder lives under ``tmp_path`` and every value is fictional.
"""

import fcntl
import html
import logging
import os
import shutil
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as app_main
from app.config import RuntimeConfig
from app.domain.errors import AppError, DataFolderUnavailable
from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from tests.fakes.clock import FrozenClock
from tests.integration.test_setup_welcome import (
    assert_field_error,
    bootstrap_file,
    running,
    snapshot,
    submit,
)
from tests.setup_harness import seed_workspace

pytestmark = pytest.mark.integration

START = datetime(2026, 1, 3, 9, 0, 0, tzinfo=UTC)
LOCK_FILE = ".lock"
# A distinctive fictional folder name, so a leak of the name alone is caught too.
FOLDER_NAME = "fictional-kestrel-workspace-4417"
LOCKED_MESSAGE = (
    "This folder is already open in another copy of the app. "
    "Close that copy first, or choose another folder."
)


# --- helpers -------------------------------------------------------------------------------


def _open(
    folder: Path,
    *,
    migrations_dir: Path = MIGRATIONS_DIR,
    attach_log_folder: Callable[[Path], None] | None = None,
    before_logging: Callable[[Database], None] | None = None,
) -> app_main.Workspace:
    return app_main.open_workspace_folder(
        folder,
        migrations_dir=migrations_dir,
        clock=FrozenClock(START),
        attach_log_folder=attach_log_folder,
        before_logging=before_logging,
    )


def _try_lock(path: Path) -> bool:
    """Whether a separate open of ``path`` can take the exclusive lock now (released at once)."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


@contextmanager
def held_lock(folder: Path) -> Iterator[None]:
    """Hold ``<folder>/.lock`` as another copy of the app would."""
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(folder / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)  # closing the file releases the lock


def _config(tmp_path: Path, source: str, data_dir: Path) -> RuntimeConfig:
    return RuntimeConfig(
        data_dir=data_dir,
        config_dir=tmp_path / "cfg",
        port=8000,
        log_level="INFO",
        demo=False,
        setup=False,
        no_browser=False,
        data_dir_source=source,  # type: ignore[arg-type]
    )


def _tables(db_path: Path) -> set[str]:
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _assert_no_path(text: str, folder: Path) -> None:
    for leak in (str(folder), str(folder.resolve()), folder.name, folder.parent.name):
        assert leak not in text, f"the error text names the folder: {text!r}"


# --- the named M2.4 tests ----------------------------------------------------------------------


def test_second_open_of_same_folder_refused_while_first_open(tmp_path: Path) -> None:
    from app.domain.errors import DataFolderInUse

    folder = tmp_path / FOLDER_NAME
    first = _open(folder)
    try:
        with pytest.raises(DataFolderInUse) as refused:
            _open(folder)
        # The first workspace is untouched by the refused open.
        assert first.db.connection.execute("SELECT 1").fetchone() == (1,)
    finally:
        first.db.close()

    assert isinstance(refused.value, AppError)
    assert refused.value.user_message == LOCKED_MESSAGE


def test_reopen_after_close_allowed(tmp_path: Path) -> None:
    from app.domain.errors import DataFolderInUse

    folder = tmp_path / FOLDER_NAME
    first = _open(folder)
    try:
        with pytest.raises(DataFolderInUse):
            _open(folder)
    finally:
        first.db.close()  # closing the workspace's database releases the lock

    assert _try_lock(folder / LOCK_FILE), "closing the database must release the lock"
    second = _open(folder)
    try:
        assert second.data_dir == folder
        assert not _try_lock(folder / LOCK_FILE), "the reopened workspace holds the lock again"
    finally:
        second.db.close()


def test_step1_locked_folder_shows_field_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from app.domain.errors import DataFolderInUse

    folder = tmp_path / FOLDER_NAME
    seed_workspace(folder, clock=FrozenClock(START))  # a workspace another copy has open
    root = tmp_path / "first-run"
    root.mkdir()
    with held_lock(folder), caplog.at_level(logging.DEBUG), running(root, "default") as run:
        before = snapshot(folder)
        response = submit(run, str(folder), action="open")

        assert_field_error(response, str(folder))
        assert DataFolderInUse().user_message in html.unescape(response.text)
        assert LOCKED_MESSAGE in html.unescape(response.text)
        assert run.app.state.workspace is None
        assert snapshot(folder) == before, "a locked folder gains no logs, backups or temp files"
        assert not bootstrap_file(root).exists()

    for record in caplog.records:
        assert str(folder) not in record.getMessage(), "the folder path is never logged"


def test_lock_error_detail_has_no_path(tmp_path: Path) -> None:
    from app.domain.errors import DataFolderInUse

    folder = tmp_path / FOLDER_NAME
    with held_lock(folder), pytest.raises(DataFolderInUse) as refused:
        _open(folder)

    error = refused.value
    _assert_no_path(error.user_message, folder)
    _assert_no_path(str(error), folder)  # the detail, which goes to the logs
    _assert_no_path(repr(error), folder)
    for arg in error.args:
        _assert_no_path(str(arg), folder)


# --- extras --------------------------------------------------------------------------------


def test_open_workspace_holds_exclusive_lock_on_lock_file(tmp_path: Path) -> None:
    folder = tmp_path / FOLDER_NAME
    workspace = _open(folder)
    try:
        assert (folder / LOCK_FILE).is_file()
        assert not _try_lock(folder / LOCK_FILE), "an open workspace holds <data>/.lock"
    finally:
        workspace.db.close()


def test_lock_file_is_0600(tmp_path: Path) -> None:
    folder = tmp_path / FOLDER_NAME
    workspace = _open(folder)
    try:
        mode = stat.S_IMODE((folder / LOCK_FILE).stat().st_mode)
    finally:
        workspace.db.close()

    assert mode == 0o600


def test_locked_fresh_folder_gets_no_database(tmp_path: Path) -> None:
    from app.domain.errors import DataFolderInUse

    folder = tmp_path / FOLDER_NAME
    with held_lock(folder), pytest.raises(DataFolderInUse):
        _open(folder)

    assert sorted(p.name for p in folder.iterdir()) == [LOCK_FILE], "nothing but the lock file"


def test_lock_taken_before_migrations_and_backup(tmp_path: Path) -> None:
    """A locked folder with a pending migration is neither migrated nor backed up."""
    from app.domain.errors import DataFolderInUse

    only_first = tmp_path / "migrations-v1"
    only_first.mkdir()
    shutil.copy(MIGRATIONS_DIR / "0001_initial.sql", only_first / "0001_initial.sql")
    with_second = tmp_path / "migrations-v2"
    shutil.copytree(only_first, with_second)
    (with_second / "0002_fictional_note.sql").write_text(
        "CREATE TABLE fictional_note (id TEXT PRIMARY KEY);\n", encoding="utf-8"
    )
    folder = tmp_path / FOLDER_NAME
    with Database.open(folder / "app.db") as db:
        apply_migrations(db, only_first, tmp_path / "unused-backups", FrozenClock(START))

    with held_lock(folder), pytest.raises(DataFolderInUse):
        _open(folder, migrations_dir=with_second)

    assert "fictional_note" not in _tables(folder / "app.db"), "no migration on a locked folder"
    assert not (folder / "backups").exists(), "no backup on a locked folder"


def test_locked_folder_leaves_logging_untouched_and_database_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.domain.errors import DataFolderInUse

    folder = tmp_path / FOLDER_NAME
    seed_workspace(folder, clock=FrozenClock(START))
    leftover = folder / "tmp" / "ai" / "jobpipeline-ai-fictional-leftover"
    leftover.mkdir(parents=True)
    opened: list[Database] = []
    real_open = Database.open

    def recording_open(path: Path) -> Database:
        db = real_open(path)
        opened.append(db)
        return db

    monkeypatch.setattr("app.main.Database.open", recording_open)
    attached: list[Path] = []
    prepared: list[Database] = []

    with held_lock(folder), pytest.raises(DataFolderInUse):
        _open(folder, attach_log_folder=attached.append, before_logging=prepared.append)

    assert attached == [], "logging never moves into a locked folder"
    assert prepared == [], "the AI temp clean-up and provider check never run"
    assert leftover.is_dir(), "the other copy's AI call folder is left alone"
    assert not (folder / "logs").exists()
    for db in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            db.connection.execute("SELECT 1")  # closed


def test_failed_open_releases_the_lock(tmp_path: Path) -> None:
    def failing_attach(_folder: Path) -> None:
        raise PermissionError("fictional attach failure")

    folder = tmp_path / FOLDER_NAME
    with pytest.raises(DataFolderUnavailable):
        _open(folder, attach_log_folder=failing_attach)

    assert _try_lock(folder / LOCK_FILE), "a failed open must not keep the folder locked"
    workspace = _open(folder)  # the user can retry the same folder
    workspace.db.close()


def test_app_startup_on_locked_folder_raises_data_folder_in_use(tmp_path: Path) -> None:
    """A folder from --data-dir, the environment or demo mode opens in the lifespan."""
    from app.domain.errors import DataFolderInUse

    folder = tmp_path / FOLDER_NAME
    app = app_main.create_app(_config(tmp_path, "cli", folder), clock=FrozenClock(START))

    with held_lock(folder), pytest.raises(DataFolderInUse), TestClient(app):
        pass  # pragma: no cover - the lifespan refuses the folder

    assert not (folder / "app.db").exists()


def test_second_app_on_same_folder_refused_until_first_shuts_down(tmp_path: Path) -> None:
    from app.domain.errors import DataFolderInUse

    folder = tmp_path / FOLDER_NAME
    first = app_main.create_app(_config(tmp_path, "cli", folder), clock=FrozenClock(START))
    second = app_main.create_app(_config(tmp_path, "cli", folder), clock=FrozenClock(START))

    with TestClient(first):
        assert first.state.workspace is not None
        with pytest.raises(DataFolderInUse), TestClient(second):
            pass  # pragma: no cover - the lifespan refuses the folder

    assert _try_lock(folder / LOCK_FILE), "shutting the app down releases the lock"
    with TestClient(second):
        assert second.state.workspace is not None


# --- a lock the caller holds (the CLI takes it before logging; code review M2.4) ------------


def test_callers_lock_is_reused_and_stays_the_callers(tmp_path: Path) -> None:
    """``create_app(data_folder_lock=...)`` opens under the caller's lock and never releases it."""
    from app.domain.errors import DataFolderInUse
    from app.modules.storage import lock_data_folder

    folder = tmp_path / FOLDER_NAME
    folder.mkdir(mode=0o700)
    lock = lock_data_folder(folder)
    try:
        app = app_main.create_app(
            _config(tmp_path, "cli", folder), clock=FrozenClock(START), data_folder_lock=lock
        )
        with TestClient(app):
            assert app.state.workspace is not None
            with pytest.raises(DataFolderInUse):
                _open(folder)  # another open file is still refused
        assert lock.held, "the app never releases a lock it was given"
        assert not _try_lock(folder / LOCK_FILE), "the caller still holds the lock"
    finally:
        lock.release()
    assert _try_lock(folder / LOCK_FILE)


def test_failed_lifespan_leaves_callers_lock_held(tmp_path: Path) -> None:
    from app.modules.storage import lock_data_folder

    folder = tmp_path / FOLDER_NAME
    (folder / "app.db").mkdir(parents=True)  # the database can't be opened
    lock = lock_data_folder(folder)
    try:
        app = app_main.create_app(
            _config(tmp_path, "cli", folder), clock=FrozenClock(START), data_folder_lock=lock
        )
        with pytest.raises(DataFolderUnavailable), TestClient(app):
            pass  # pragma: no cover - the lifespan fails
        assert lock.held
        assert not _try_lock(folder / LOCK_FILE), "a failed open leaves the caller's lock held"
    finally:
        lock.release()


def test_released_lock_passed_in_is_refused(tmp_path: Path) -> None:
    from app.modules.storage import lock_data_folder

    folder = tmp_path / FOLDER_NAME
    folder.mkdir(mode=0o700)
    lock = lock_data_folder(folder)
    lock.release()

    with pytest.raises(DataFolderUnavailable) as refused:
        app_main.open_workspace_folder(
            folder, migrations_dir=MIGRATIONS_DIR, clock=FrozenClock(START), lock=lock
        )

    _assert_no_path(str(refused.value), folder)
    assert not (folder / "app.db").exists(), "nothing is opened without the lock"
