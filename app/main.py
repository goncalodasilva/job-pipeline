"""App factory and lifespan (spec 2.6, 2.13; ADR-030; ADR-044 item 3).

The lifespan starts the task runner and builds the services. It opens the workspace (the data
folder and its database, migrated) only when ``opens_workspace_at_startup`` says so; on a first
run the app starts without one and step 1 of the guide calls ``app.state.open_workspace``.
Shutdown stops the runner before closing the database. Logging is set up by the CLI before the
app is created.
"""

import sqlite3
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

from app import __version__
from app.api.health import router as health_router
from app.api.pages import register_pages
from app.api.security import SecurityMiddleware, generate_launch_token, page_route_matcher
from app.api.setup import register_setup_pages
from app.api.setup_redirect import SetupRedirectMiddleware
from app.api.templating import STATIC_DIR, STATIC_URL
from app.config import (
    BOOTSTRAP_FILE,
    DATABASE_FILE,
    RuntimeConfig,
    data_dir_is_explicit,
    opens_workspace_at_startup,
    owned_by_another_user,
)
from app.domain.clock import Clock, SystemClock
from app.domain.errors import DataFolderNotYours, DataFolderUnavailable
from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from app.onboarding.progress import SetupProgress
from app.onboarding.registry import StepRegistry, m1_registry
from app.onboarding.startup import OnboardingState, StartupInputs
from app.services.health import HealthService
from app.services.setup_state import SetupStateService
from app.tasks import TaskRunner

BACKUP_FOLDER = "backups"
LOG_FOLDER = "logs"


@dataclass(frozen=True)
class Workspace:
    """The open data folder and its migrated database."""

    data_dir: Path
    db: Database


class WorkspaceAlreadyOpen(RuntimeError):
    """``open_workspace`` was called while a workspace is already open."""


class WorkspaceClosed(RuntimeError):
    """``open_workspace`` was called after the app shut down."""


def open_workspace_folder(
    folder: Path,
    *,
    migrations_dir: Path,
    clock: Clock,
    attach_log_folder: Callable[[Path], None] | None = None,
    before_logging: Callable[[Database], None] | None = None,
) -> Workspace:
    """Create the folder (0700, ADR-043), open and migrate its database, then log into it.

    Blocking (mkdir, migrations, backups). Raises ``DataFolderNotYours`` for a folder owned by
    another account and ``DataFolderUnavailable`` when it can't be created, opened or logged
    into; a failed migration raises ``MigrationError``. ``before_logging`` runs on the migrated
    database; whatever it raises closes the database and propagates. File logging is attached
    last, so a failed open leaves logging untouched and the user can choose another folder.
    """
    _prepare_folder(folder)
    db = _open_database(folder)
    try:
        apply_migrations(db, migrations_dir, folder / BACKUP_FOLDER, clock)
        if before_logging is not None:
            before_logging(db)
        if attach_log_folder is not None:
            _attach_logs(folder, attach_log_folder)
    except BaseException:
        db.close()
        raise
    return Workspace(data_dir=folder, db=db)


def _prepare_folder(folder: Path) -> None:
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as error:
        raise DataFolderUnavailable(detail=f"can't create ({type(error).__name__})") from None
    # Checked after mkdir, which writes nothing into an existing folder, and before anything
    # else is written, so a folder someone else created in the meantime is refused too.
    if owned_by_another_user(folder):
        raise DataFolderNotYours(detail="owned by another user")


def _open_database(folder: Path) -> Database:
    try:
        return Database.open(folder / DATABASE_FILE)
    except (OSError, sqlite3.Error) as error:
        raise DataFolderUnavailable(detail=f"can't open ({type(error).__name__})") from None


def _attach_logs(folder: Path, attach_log_folder: Callable[[Path], None]) -> None:
    try:
        attach_log_folder(folder / LOG_FOLDER)
    except OSError as error:
        raise DataFolderUnavailable(detail=f"can't log ({type(error).__name__})") from None


def _current_db(app: FastAPI) -> Database | None:
    workspace: Workspace | None = getattr(app.state, "workspace", None)
    return workspace.db if workspace is not None else None


def _startup_inputs(config: RuntimeConfig) -> StartupInputs:
    return StartupInputs(
        bootstrap_exists=(config.config_dir / BOOTSTRAP_FILE).exists(),
        data_dir_explicit=data_dir_is_explicit(config),
        demo=config.demo,
        force_setup=config.setup,
    )


def _progress(registry: StepRegistry, db: Database | None, clock: Clock) -> SetupProgress | None:
    if db is None:
        return None
    return SetupProgress(registry, SetupStateService(db, clock))


def create_app(
    config: RuntimeConfig,
    *,
    clock: Clock | None = None,
    migrations_dir: Path = MIGRATIONS_DIR,
    attach_log_folder: Callable[[Path], None] | None = None,
    open_at_startup: bool | None = None,
) -> FastAPI:
    """Build the app. ``open_at_startup`` (default: ``opens_workspace_at_startup(config)``)
    says whether the lifespan opens ``config.data_dir``; the CLI passes the value it used."""
    app_clock = clock if clock is not None else SystemClock()
    open_now = opens_workspace_at_startup(config) if open_at_startup is None else open_at_startup
    open_lock = threading.Lock()
    closed = threading.Event()

    def open_workspace(folder: Path) -> Workspace:
        """Open the data folder chosen in step 1 (see ``open_workspace_folder``).

        Blocking: it creates folders, runs migrations and may copy a backup. Call it from a
        sync route or a worker thread, never directly inside an async handler. Raises
        ``WorkspaceAlreadyOpen`` when one is open and ``WorkspaceClosed`` after shutdown.

        The setup decision is refreshed from the new database before logging is attached and
        the workspace is published; if anything fails, the database is closed, the decision
        goes back to "no workspace" and the user can retry.
        """
        with open_lock:
            if closed.is_set():
                raise WorkspaceClosed("the app has shut down")
            if getattr(app.state, "workspace", None) is not None:
                raise WorkspaceAlreadyOpen("a workspace is already open")
            onboarding: OnboardingState = app.state.onboarding
            registry: StepRegistry = app.state.setup_registry

            def refresh_onboarding(db: Database) -> None:
                onboarding.refresh(_progress(registry, db, app_clock))

            try:
                workspace = open_workspace_folder(
                    folder,
                    migrations_dir=migrations_dir,
                    clock=app_clock,
                    attach_log_folder=attach_log_folder,
                    before_logging=refresh_onboarding,
                )
            except BaseException:
                onboarding.refresh(None)  # no I/O: back to the no-workspace decision
                raise
            app.state.workspace = workspace
            return workspace

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        workspace = None
        if open_now:
            workspace = open_workspace_folder(
                config.data_dir,
                migrations_dir=migrations_dir,
                clock=app_clock,
                attach_log_folder=attach_log_folder,
            )
        runner = TaskRunner()
        try:
            await runner.start()
            registry = m1_registry()
            onboarding = OnboardingState(registry, _startup_inputs(config))
            onboarding.refresh(
                _progress(registry, workspace.db if workspace is not None else None, app_clock)
            )
            app.state.setup_registry = registry
            app.state.onboarding = onboarding
            app.state.workspace = workspace
            app.state.open_workspace = open_workspace
            app.state.runner = runner
            app.state.health = HealthService(
                lambda: _current_db(app), lambda: runner.status, __version__
            )
            yield
        finally:
            try:
                await runner.stop()
            finally:
                with open_lock:
                    closed.set()
                    current: Workspace | None = getattr(app.state, "workspace", None) or workspace
                    if current is not None:
                        current.db.close()

    # No generated API docs: page GETs need no token, so they would expose every route.
    app = FastAPI(
        title="Job Search Pipeline",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    launch_token = generate_launch_token()
    app.state.launch_token = launch_token
    # Inside the security middleware: rejections come first and redirects keep its headers.
    app.add_middleware(
        SetupRedirectMiddleware, onboarding=lambda: getattr(app.state, "onboarding", None)
    )
    # Added last, so it stays the outermost user middleware: nothing runs before these checks.
    app.add_middleware(
        SecurityMiddleware,
        port=config.port,
        launch_token=launch_token,
        is_page=page_route_matcher(lambda: app.router.routes),
    )
    app.include_router(health_router)
    register_pages(app)
    register_setup_pages(app)
    app.mount(STATIC_URL, StaticFiles(directory=STATIC_DIR), name="static")
    return app
