"""Workspace lifecycle: start without a database, open it on demand (M1.5; AC-M1.1 cases 1
and 2 plumbing; ADR-023 as amended by ADR-044 item 3; ADR-038, ADR-043).

The app is built with a ``RuntimeConfig`` whose ``data_dir_source`` says where the data folder
came from. A first run ("default") or a bootstrap config pointing at a folder without a
database ("bootstrap") must start without creating anything; step 1 of the guide later calls
``app.state.open_workspace(folder)``. Every folder lives under ``tmp_path`` and every value is
fictional.
"""

import dataclasses
import json
import logging
import os
import shutil
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app as app_package
import app.main as app_main
from app.config import RuntimeConfig, write_bootstrap
from app.domain.errors import ConfigError, DataFolderNotYours, DataFolderUnavailable
from app.domain.redaction import REDACTED, Redactor
from app.logging_config import LOG_FILE_NAME, configure_logging
from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from app.onboarding.detection import StartupDecision
from app.services.setup_state import SetupStateService
from tests.fakes.clock import FrozenClock

pytestmark = pytest.mark.integration

START = datetime(2026, 1, 3, 9, 0, 0, tzinfo=UTC)
BASE_URL = "http://127.0.0.1:8000"  # must pass the host check (spec 2.9 rule 1)
TABLES_FROM_0001 = {"schema_migrations", "settings", "setup_state", "tasks", "usage_records"}
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")
FICTIONAL_HIDDEN = "FictionalWorkspaceSecret-0042-notreal"
EXPLICIT_SOURCES = ("cli", "environment", "demo")
ALL_SOURCES = ("cli", "environment", "demo", "bootstrap", "default")


# --- helpers -------------------------------------------------------------------------------


def _config(tmp_path: Path, source: str, data_dir: Path | None = None) -> RuntimeConfig:
    return RuntimeConfig(
        data_dir=data_dir if data_dir is not None else tmp_path / "data",
        config_dir=tmp_path / "cfg",
        port=8000,
        log_level="INFO",
        demo=source == "demo",
        setup=False,
        no_browser=False,
        data_dir_source=source,  # type: ignore[arg-type]
    )


def _app(
    tmp_path: Path,
    source: str,
    *,
    data_dir: Path | None = None,
    migrations_dir: Path = MIGRATIONS_DIR,
    attach_log_folder: Callable[[Path], None] | None = None,
) -> FastAPI:
    return app_main.create_app(
        _config(tmp_path, source, data_dir),
        clock=FrozenClock(START),
        migrations_dir=migrations_dir,
        attach_log_folder=attach_log_folder,
    )


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _tables(db_path: Path) -> set[str]:
    with closing(_read_only(db_path)) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _health(client: TestClient) -> tuple[int, dict[str, object]]:
    response = client.get("/health")
    return response.status_code, response.json()


def _tree(folder: Path) -> list[Path]:
    return sorted(folder.rglob("*")) if folder.exists() else []


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Save and restore root and uvicorn logger state around a test."""
    root = logging.getLogger()
    saved_root = (list(root.handlers), list(root.filters), root.level)
    saved_uvicorn = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).propagate,
            logging.getLogger(name).level,
        )
        for name in UVICORN_LOGGERS
    }
    yield
    for handler in root.handlers:
        if handler not in saved_root[0]:
            handler.close()
    root.handlers[:] = saved_root[0]
    root.filters[:] = saved_root[1]
    root.setLevel(saved_root[2])
    for name, (handlers, propagate, level) in saved_uvicorn.items():
        logger = logging.getLogger(name)
        logger.handlers[:] = handlers
        logger.propagate = propagate
        logger.setLevel(level)
    logging.captureWarnings(False)


# --- AC-M1.1 case 1: first run ------------------------------------------------------------------


def test_ac_m1_1_first_run_starts_without_creating_database(tmp_path: Path) -> None:
    app = _app(tmp_path, "default")
    data_dir = tmp_path / "data"

    with TestClient(app):
        assert app.state.workspace is None
        assert app.state.runner.status == "running"
        assert callable(app.state.open_workspace)
        assert not data_dir.exists(), "a first run must not create the data folder"

    assert not data_dir.exists()
    assert list(tmp_path.iterdir()) == [], "nothing may be created anywhere on a first run"


def test_health_200_with_database_not_created_before_setup(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path, "default"), base_url=BASE_URL) as client:
        status, body = _health(client)
        text = client.get("/health").text

    assert status == 200
    assert body == {
        "version": app_package.__version__,
        "database": "not_created",
        "runner": "running",
    }
    assert str(tmp_path) not in text


# --- AC-M1.1 case 2: bootstrap config pointing at a folder without a database ----------------


def test_ac_m1_1_bootstrap_with_missing_database_does_not_create_it(tmp_path: Path) -> None:
    data_dir = tmp_path / "moved-data"
    data_dir.mkdir()
    app = _app(tmp_path, "bootstrap", data_dir=data_dir)

    with TestClient(app, base_url=BASE_URL) as client:
        assert app.state.workspace is None
        status, body = _health(client)

    assert (status, body["database"]) == (200, "not_created")
    assert list(data_dir.iterdir()) == [], "the moved folder's database must not be recreated"


def test_bootstrap_with_missing_folder_does_not_create_it(tmp_path: Path) -> None:
    data_dir = tmp_path / "deleted-data"
    app = _app(tmp_path, "bootstrap", data_dir=data_dir)

    with TestClient(app):
        assert app.state.workspace is None

    assert not data_dir.exists()


# --- opening at start-up ----------------------------------------------------------------------


@pytest.mark.parametrize("source", ALL_SOURCES)
def test_existing_database_opened_at_startup_for_any_source(tmp_path: Path, source: str) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    Database.open(data_dir / "app.db").close()  # an existing, not yet migrated database
    app = _app(tmp_path, source, data_dir=data_dir)

    with TestClient(app, base_url=BASE_URL) as client:
        workspace = app.state.workspace
        assert isinstance(workspace, app_main.Workspace)
        assert workspace.data_dir == data_dir
        assert workspace.db.path == data_dir / "app.db"
        status, body = _health(client)

    assert (status, body["database"]) == (200, "ok")
    assert _tables(data_dir / "app.db") >= TABLES_FROM_0001


@pytest.mark.parametrize("source", EXPLICIT_SOURCES)
def test_explicit_folder_sources_open_workspace_at_startup(tmp_path: Path, source: str) -> None:
    """A folder named by --data-dir, JOBPIPELINE_DATA_DIR or --demo is opened straight away."""
    data_dir = tmp_path / "explicit"
    app = _app(tmp_path, source, data_dir=data_dir)

    with TestClient(app, base_url=BASE_URL) as client:
        workspace = app.state.workspace
        assert isinstance(workspace, app_main.Workspace)
        assert workspace.data_dir == data_dir
        assert (data_dir / "app.db").is_file()
        status, body = _health(client)

    assert (status, body["database"]) == (200, "ok")
    assert _tables(data_dir / "app.db") >= TABLES_FROM_0001


# --- open_workspace -----------------------------------------------------------------------------


def test_open_workspace_creates_folder_db_and_migrates(tmp_path: Path) -> None:
    chosen = tmp_path / "chosen" / "nested" / "data"  # parents are created too
    app = _app(tmp_path, "default")

    with TestClient(app, base_url=BASE_URL) as client:
        assert _health(client)[1]["database"] == "not_created"

        workspace = app.state.open_workspace(chosen)

        assert isinstance(workspace, app_main.Workspace)
        assert app.state.workspace is workspace
        assert workspace.data_dir == chosen
        assert workspace.db.path == chosen / "app.db"
        assert _tables(chosen / "app.db") >= TABLES_FROM_0001
        status, body = _health(client)

    assert (status, body["database"]) == (200, "ok")
    assert not (tmp_path / "data").exists(), "only the chosen folder is used"
    if os.name == "posix":
        assert stat.S_IMODE(chosen.stat().st_mode) == 0o700
    backups = chosen / "backups"
    assert not backups.exists() or list(backups.iterdir()) == [], "no backup on a fresh database"


def test_workspace_is_a_frozen_dataclass(tmp_path: Path) -> None:
    app = _app(tmp_path, "default")
    with TestClient(app):
        workspace = app.state.open_workspace(tmp_path / "chosen")

    with pytest.raises(dataclasses.FrozenInstanceError):
        workspace.data_dir = tmp_path / "elsewhere"
    assert dataclasses.is_dataclass(workspace)


def test_open_workspace_backs_up_existing_database_into_its_folder(tmp_path: Path) -> None:
    """Pending migrations on an existing database copy it to ``<folder>/backups`` first."""
    only_first = tmp_path / "migrations-v1"
    only_first.mkdir()
    shutil.copy(MIGRATIONS_DIR / "0001_initial.sql", only_first / "0001_initial.sql")
    with_second = tmp_path / "migrations-v2"
    shutil.copytree(only_first, with_second)
    (with_second / "0002_fictional_note.sql").write_text(
        "CREATE TABLE fictional_note (id TEXT PRIMARY KEY);\n", encoding="utf-8"
    )
    chosen = tmp_path / "chosen"
    with Database.open(chosen / "app.db") as db:
        apply_migrations(db, only_first, tmp_path / "unused-backups", FrozenClock(START))
    app = _app(tmp_path, "default", migrations_dir=with_second)

    with TestClient(app):
        app.state.open_workspace(chosen)

    assert "fictional_note" in _tables(chosen / "app.db")
    assert len(list((chosen / "backups").iterdir())) == 1
    assert not (tmp_path / "unused-backups").exists()


def test_open_workspace_refuses_folder_owned_by_another_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    other_user = foreign.stat().st_uid + 1
    attached: list[Path] = []
    app = _app(tmp_path, "default", attach_log_folder=attached.append)

    with TestClient(app, base_url=BASE_URL) as client:
        with monkeypatch.context() as patch, pytest.raises(DataFolderNotYours) as refused:
            patch.setattr("app.config.os.geteuid", lambda: other_user, raising=False)
            app.state.open_workspace(foreign)
        assert app.state.workspace is None
        status, body = _health(client)

    assert (status, body["database"]) == (200, "not_created")
    assert list(foreign.iterdir()) == [], "nothing may be written into a foreign folder"
    assert attached == [], "logs must not move into a foreign folder"
    assert isinstance(refused.value, ConfigError)
    assert refused.value.user_message == (
        "This folder belongs to another user. Choose a folder you own."
    )
    assert str(foreign) not in str(refused.value)


def test_open_workspace_checks_ownership_after_creating_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder someone else creates just before mkdir (a race) is refused too (ADR-043)."""
    target = tmp_path / "raced"
    app = _app(tmp_path, "default")

    with TestClient(app):
        with monkeypatch.context() as patch, pytest.raises(DataFolderNotYours):
            patch.setattr("app.config.os.geteuid", lambda: -1, raising=False)  # owns nothing
            app.state.open_workspace(target)
        assert app.state.workspace is None

    assert not (target / "app.db").exists()
    assert _tree(target) == []


def test_open_workspace_retry_after_failed_open_succeeds(
    tmp_path: Path, restore_logging: None
) -> None:
    """A folder that can't be opened leaves logging untouched, so another folder still works."""
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "app.db").write_bytes(b"not a database")
    good = tmp_path / "good"
    setup = configure_logging(None, "INFO", Redactor())
    logger = logging.getLogger("jobpipeline.test.workspace")
    try:
        app = _app(tmp_path, "default", attach_log_folder=setup.attach_log_folder)
        with TestClient(app, base_url=BASE_URL) as client:
            with pytest.raises(DataFolderUnavailable) as failed:
                app.state.open_workspace(bad)
            assert app.state.workspace is None

            workspace = app.state.open_workspace(good)
            logger.warning("fictional record after the retry")
            for handler in logging.getLogger().handlers:
                handler.flush()
            status, body = _health(client)
            assert app.state.workspace is workspace
    finally:
        setup.close()

    assert isinstance(failed.value, ConfigError)
    assert failed.value.user_message == "This folder can't be used. Choose another folder."
    assert str(bad) not in str(failed.value)
    assert (status, body["database"]) == (200, "ok")
    assert not (bad / "logs").exists(), "the rejected folder gains no logs"
    text = (good / "logs" / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert "fictional record after the retry" in text


def test_open_workspace_folder_that_cannot_be_created(tmp_path: Path) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("fictional", encoding="utf-8")
    attached: list[Path] = []
    app = _app(tmp_path, "default", attach_log_folder=attached.append)

    with TestClient(app):
        with pytest.raises(DataFolderUnavailable):
            app.state.open_workspace(blocker / "data")
        assert app.state.workspace is None

    assert attached == []


def test_open_workspace_log_attach_failure_closes_database(tmp_path: Path) -> None:
    opened: list[Database] = []
    real_open = Database.open

    def recording_open(path: Path) -> Database:
        db = real_open(path)
        opened.append(db)
        return db

    def failing_attach(_folder: Path) -> None:
        raise PermissionError("fictional attach failure")

    app = _app(tmp_path, "default", attach_log_folder=failing_attach)
    with TestClient(app):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr("app.main.Database.open", recording_open)
            with pytest.raises(DataFolderUnavailable):
                app.state.open_workspace(tmp_path / "chosen")
        assert app.state.workspace is None

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].connection.execute("SELECT 1")


def test_open_workspace_attaches_file_logging_and_flushes_buffer(
    tmp_path: Path, restore_logging: None
) -> None:
    redactor = Redactor()
    redactor.register(FICTIONAL_HIDDEN)
    setup = configure_logging(None, "INFO", redactor)
    chosen = tmp_path / "chosen"
    logger = logging.getLogger("jobpipeline.test.workspace")
    try:
        app = _app(tmp_path, "default", attach_log_folder=setup.attach_log_folder)
        with TestClient(app):
            logger.warning("fictional record before opening with %s", FICTIONAL_HIDDEN)
            assert not chosen.exists()

            app.state.open_workspace(chosen)

            logger.warning("fictional record after opening")
            for handler in logging.getLogger().handlers:
                handler.flush()
            text = (chosen / "logs" / LOG_FILE_NAME).read_text(encoding="utf-8")
    finally:
        setup.close()

    messages = [json.loads(line)["message"] for line in text.splitlines() if line.strip()]
    before = messages.index(f"fictional record before opening with {REDACTED}")
    after = messages.index("fictional record after opening")
    assert before < after
    assert FICTIONAL_HIDDEN not in text


def test_open_workspace_twice_is_rejected(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    app = _app(tmp_path, "default")

    with TestClient(app):
        workspace = app.state.open_workspace(first)
        with pytest.raises(app_main.WorkspaceAlreadyOpen):
            app.state.open_workspace(second)
        with pytest.raises(app_main.WorkspaceAlreadyOpen):
            app.state.open_workspace(first)
        assert app.state.workspace is workspace

    assert issubclass(app_main.WorkspaceAlreadyOpen, RuntimeError)
    assert not second.exists()


def test_open_workspace_after_startup_workspace_is_rejected(tmp_path: Path) -> None:
    app = _app(tmp_path, "cli")
    other = tmp_path / "other"

    with TestClient(app):
        opened = app.state.workspace
        assert opened is not None
        with pytest.raises(app_main.WorkspaceAlreadyOpen):
            app.state.open_workspace(other)
        assert app.state.workspace is opened

    assert not other.exists()


@pytest.mark.parametrize(("source", "given"), [("default", True), ("cli", False)])
def test_create_app_uses_given_open_at_startup(tmp_path: Path, source: str, given: bool) -> None:
    """The CLI decides once and passes it; the app doesn't decide again (M1.5 review)."""
    data_dir = tmp_path / "data"
    app = app_main.create_app(
        _config(tmp_path, source, data_dir),
        clock=FrozenClock(START),
        open_at_startup=given,
    )

    with TestClient(app):
        assert (app.state.workspace is not None) is given

    assert (data_dir / "app.db").exists() is given


# --- shutdown -----------------------------------------------------------------------------------


def test_shutdown_closes_workspace_database(tmp_path: Path) -> None:
    app = _app(tmp_path, "default")

    with TestClient(app):
        db = app.state.open_workspace(tmp_path / "chosen").db
        runner = app.state.runner

    assert runner.status == "stopped"
    with pytest.raises(sqlite3.ProgrammingError):
        db.connection.execute("SELECT 1")


def test_open_workspace_after_shutdown_is_rejected(tmp_path: Path) -> None:
    app = _app(tmp_path, "default")
    with TestClient(app):
        open_workspace = app.state.open_workspace

    with pytest.raises(app_main.WorkspaceClosed):
        open_workspace(tmp_path / "late")

    assert issubclass(app_main.WorkspaceClosed, RuntimeError)
    assert not (tmp_path / "late").exists()


def test_shutdown_without_workspace_is_clean(tmp_path: Path) -> None:
    app = _app(tmp_path, "default")

    with TestClient(app):
        runner = app.state.runner

    assert runner.status == "stopped"
    assert list(tmp_path.iterdir()) == []


def test_open_workspace_refresh_failure_closes_and_allows_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing setup-state read leaves no workspace, no logging and an open retry (M1.9)."""
    moved = tmp_path / "moved-away"
    write_bootstrap(tmp_path / "cfg", data_dir=moved, version=app_package.__version__)
    attached: list[Path] = []
    failed_dbs: list[Database] = []
    original_statuses = SetupStateService.statuses

    def statuses_failing_once(self: SetupStateService) -> dict[str, str]:
        if not failed_dbs:
            failed_dbs.append(self._db)
            raise sqlite3.OperationalError("fictional read failure")
        return dict(original_statuses(self))

    monkeypatch.setattr(SetupStateService, "statuses", statuses_failing_once)
    app = _app(tmp_path, "bootstrap", data_dir=moved, attach_log_folder=attached.append)

    with TestClient(app, base_url=BASE_URL):
        assert app.state.onboarding.decision == StartupDecision("locate_or_fresh")
        with pytest.raises(sqlite3.OperationalError):
            app.state.open_workspace(moved)

        assert app.state.workspace is None
        assert attached == [], "logging is attached only after the refresh succeeds"
        with pytest.raises(sqlite3.ProgrammingError):
            failed_dbs[0].connection.execute("SELECT 1")  # closed
        assert app.state.onboarding.decision == StartupDecision("locate_or_fresh")

        workspace = app.state.open_workspace(moved)

        assert app.state.workspace is workspace
        assert attached == [moved / "logs"]
        assert app.state.onboarding.decision == StartupDecision("guide", "welcome")
