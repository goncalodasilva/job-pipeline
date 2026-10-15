"""App factory and lifespan (spec 2.6, 2.13; ADR-030; ADR-044 item 3).

The lifespan starts the task runner and builds the services. It opens the workspace (the data
folder and its database, migrated) only when ``opens_workspace_at_startup`` says so; on a first
run the app starts without one and step 1 of the guide calls ``app.state.open_workspace``.
Shutdown stops the runner before closing the database. Logging is set up by the CLI before the
app is created. Providers come from the injected ``Wiring`` (``app/wiring.py``); whenever a
workspace opens, leftover AI call folders are cleared and provider health is computed once and
cached in ``app.state.provider_health``, never on GET (spec 2.9.12).
"""

import os
import sqlite3
import threading
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta, tzinfo
from pathlib import Path

from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

from app import __version__
from app.api.errors import register_error_handlers
from app.api.events import (
    DEFAULT_EVENT_QUEUE_SIZE,
    DEFAULT_KEEPALIVE_SECONDS,
    ShutdownSignal,
    TaskEvents,
    register_events,
)
from app.api.fact_editor import register_fact_editor_routes
from app.api.facts import register_fact_pages
from app.api.facts_import import register_fact_import_routes
from app.api.health import router as health_router
from app.api.pages import register_pages
from app.api.security import SecurityMiddleware, generate_launch_token, page_route_matcher
from app.api.settings import register_settings_pages
from app.api.settings_usage import register_usage_settings
from app.api.setup import register_setup_pages
from app.api.setup_redirect import SetupRedirectMiddleware
from app.api.tasks import register_task_routes
from app.api.templating import STATIC_DIR, STATIC_URL
from app.config import (
    BOOTSTRAP_FILE,
    DATABASE_FILE,
    RuntimeConfig,
    data_dir_is_explicit,
    is_demo_workspace,
    opens_workspace_at_startup,
    owned_by_another_user,
    refused_data_folders,
    write_bootstrap,
)
from app.domain.clock import Clock, SystemClock
from app.domain.errors import DataFolderNotYours, DataFolderUnavailable
from app.domain.ids import IdFactory, UuidIdFactory
from app.domain.redaction import Redactor
from app.domain.settings import AI_PROVIDER, TASK_RUNNER, AIProviderSettings
from app.domain.tasks import TaskChange, TaskStatus, TaskTypeSpec
from app.modules.ai import ProviderHealth
from app.modules.extract import extract_text, sniff_type
from app.modules.storage import (
    MIGRATIONS_DIR,
    Database,
    DataFolderLock,
    apply_migrations,
    lock_data_folder,
)
from app.onboarding.progress import SetupProgress
from app.onboarding.registry import (
    DONE,
    PREFERENCES,
    PROVIDER,
    WEB_SEARCH,
    WELCOME,
    StepHandler,
    StepRegistry,
    m1_registry,
    require_handlers,
)
from app.onboarding.startup import OnboardingState, StartupInputs
from app.onboarding.steps import PendingStep
from app.onboarding.steps.done import DoneStep
from app.onboarding.steps.preferences import PreferencesStep
from app.onboarding.steps.provider import ProviderStep
from app.onboarding.steps.web_search import WebSearchStep
from app.onboarding.steps.welcome import WelcomeSettings, WelcomeStep
from app.services.budget_gate import BudgetGate
from app.services.cv_import import (
    EXTRACT_CV,
    EXTRACT_CV_LABEL,
    PARSE_TIMEOUT_SECONDS,
    CvImportService,
    import_target,
    sweep_orphan_imports,
)
from app.services.fact_editor import FactEditor
from app.services.fact_history import FactHistory
from app.services.fact_library import FactLibrary
from app.services.fact_merge import FactMerge
from app.services.fact_views import FactBank, FactViews
from app.services.facts import FactService
from app.services.health import HealthService
from app.services.practice import PRACTICE, PRACTICE_LABEL, PracticeService
from app.services.provider_setup import ProviderSetupService
from app.services.roles import RoleService
from app.services.settings import SettingsService
from app.services.setup_state import SetupStateService
from app.services.skills import SkillService
from app.services.task_views import TaskView, TaskViews, failed_message
from app.services.tasks import TaskService
from app.services.usage import UsageService
from app.services.usage_panel import UsagePanel
from app.services.usage_settings import UsageSettings
from app.tasks import HandlerRegistry, TaskRunner
from app.tasks.cv_import import cv_import_handler
from app.tasks.practice import practice_handler
from app.wiring import (
    TASK_TIMEOUT_FLOOR,
    Wiring,
    build_wiring,
    clear_ai_temp,
    local_timezone,
)

BACKUP_FOLDER = "backups"
LOG_FOLDER = "logs"
# Longer than one provider run plus its close, so a slow step is never taken for a hung task.
PRACTICE_TIMEOUT = TASK_TIMEOUT_FLOOR + timedelta(minutes=1)
# One Thorough call and its close, plus the task's one read of the stored file (the upload
# route made the other), with twice its parse timeout as margin between beats.
CV_IMPORT_TIMEOUT = (
    TASK_TIMEOUT_FLOOR + timedelta(seconds=2 * PARSE_TIMEOUT_SECONDS) + timedelta(minutes=1)
)


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
    lock: DataFolderLock | None = None,
) -> Workspace:
    """Create the folder (0700, ADR-043), lock it, open and migrate its database, then log
    into it.

    Blocking (mkdir, migrations, backups). Raises ``DataFolderNotYours`` for a folder owned by
    another account, ``DataFolderInUse`` when another copy of the app holds its lock (ADR-072)
    and ``DataFolderUnavailable`` when it can't be created, opened or logged into; a failed
    migration raises ``MigrationError``. ``lock`` is one the caller already holds on ``folder``
    (the CLI takes it before logging into the folder); it stays the caller's to release, and
    one no longer held is refused with ``DataFolderUnavailable`` (POSIX). Without it the lock is
    taken here, before anything is written into the folder, released if the open fails and
    otherwise when the workspace's database closes. ``before_logging`` runs on the migrated
    database; whatever it raises closes the database and propagates. File logging is attached
    last, so a failed open leaves logging untouched and the user can choose another folder.
    """
    _prepare_folder(folder)
    if lock is not None:
        if not lock.held and os.name == "posix":
            raise DataFolderUnavailable(detail="the given lock is not held")
        return _open_locked(folder, migrations_dir, clock, attach_log_folder, before_logging)
    own_lock = lock_data_folder(folder)
    try:
        workspace = _open_locked(folder, migrations_dir, clock, attach_log_folder, before_logging)
    except BaseException:
        own_lock.release()
        raise
    workspace.db.call_on_close(own_lock.release)
    return workspace


def _open_locked(
    folder: Path,
    migrations_dir: Path,
    clock: Clock,
    attach_log_folder: Callable[[Path], None] | None,
    before_logging: Callable[[Database], None] | None,
) -> Workspace:
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


def _prepare_workspace(
    folder: Path,
    db: Database,
    *,
    wiring: Wiring,
    onboarding: OnboardingState,
    registry: StepRegistry,
    clock: Clock,
    ids: IdFactory,
) -> ProviderHealth:
    """Run when a workspace opens, before logging moves into it and before its task service
    is handed to the runner: clear leftover AI call folders, mark tasks a killed process left
    running as interrupted (ADR-076 item 10), remove stored CV files no import names (M3.4:
    the folder lock is held, the database is migrated and no upload can be stored yet), then
    check the provider (ADR-049, ADR-057)."""
    try:
        clear_ai_temp(folder)
    except OSError as error:
        raise DataFolderUnavailable(detail=f"can't prepare ({type(error).__name__})") from None
    # Nothing listens yet: the workspace isn't published and the runner hasn't got it.
    try:
        TaskService(db, clock, ids, _task_listeners()).recover_interrupted()
    except sqlite3.Error as error:
        detail = f"can't recover tasks ({type(error).__name__})"
        raise DataFolderUnavailable(detail=detail) from None
    try:
        sweep_orphan_imports(folder, db)
    except sqlite3.Error as error:
        detail = f"can't list imports ({type(error).__name__})"
        raise DataFolderUnavailable(detail=detail) from None
    return _check_provider(db, wiring=wiring, onboarding=onboarding, registry=registry, clock=clock)


def _check_provider(
    db: Database,
    *,
    wiring: Wiring,
    onboarding: OnboardingState,
    registry: StepRegistry,
    clock: Clock,
) -> ProviderHealth:
    """Compute provider health from the stored settings (configuration only; ADR-046) and
    refresh the setup decision with it (ADR-053)."""
    settings = SettingsService(db, clock).load(AI_PROVIDER).value
    health = wiring.provider_health(settings)
    onboarding.refresh(_progress(registry, db, clock), provider_healthy=health.ok)
    return health


def _task_listeners(
    *listeners: Callable[[TaskChange], None],
) -> Callable[[TaskChange], None]:
    """One ``on_change`` that tells each listener in turn, on the committing thread."""

    def on_change(change: TaskChange) -> None:
        for listener in listeners:
            listener(change)

    return on_change


def _attach_tasks(
    runner: TaskRunner,
    db: Database,
    *,
    clock: Clock,
    ids: IdFactory,
    publish: Callable[[TaskChange], None],
) -> TaskService:
    """Build the workspace's task service and hand it to the runner with the stored
    ``TASK_RUNNER`` settings. Every committed change wakes the runner and then goes to
    ``publish`` (the event streams; it only schedules, so nothing here waits). Blocking
    (a settings read): runs in the thread opening the
    workspace (step 1's worker thread, or the loop itself during the lifespan, before the app
    serves requests); the hand-off itself is thread-safe."""

    def wake_runner(change: TaskChange) -> None:
        if change.status == TaskStatus.QUEUED:
            runner.notify()
        elif change.status == TaskStatus.RUNNING:  # a claim or a cancel request
            runner.notify_cancel(change.id)

    service = TaskService(db, clock, ids, _task_listeners(wake_runner, publish))
    runner.attach(service, settings=SettingsService(db, clock).load(TASK_RUNNER).value)
    return service


def _step_handlers(
    app: FastAPI,
    config: RuntimeConfig,
    *,
    registry: StepRegistry,
    onboarding: OnboardingState,
    clock: Clock,
    wiring: Wiring,
) -> Mapping[str, StepHandler]:
    """One handler per registered step (ADR-052); steps still to come are placeholders."""

    def workspace_folder() -> Path | None:
        workspace: Workspace | None = getattr(app.state, "workspace", None)
        return workspace.data_dir if workspace is not None else None

    def open_workspace(folder: Path) -> None:
        app.state.open_workspace(folder)

    def save_bootstrap(folder: Path) -> None:
        write_bootstrap(config.config_dir, data_dir=folder, version=__version__)

    welcome = WelcomeStep(
        WelcomeSettings(
            suggested_folder=config.data_dir,
            folder_fixed=data_dir_is_explicit(config),
            demo=config.demo,
            refused_folders=refused_data_folders(config),
            config_folder=config.config_dir,
            bootstrap_name=BOOTSTRAP_FILE,
        ),
        onboarding=onboarding,
        workspace_folder=workspace_folder,
        open_workspace=open_workspace,
        bootstrap_exists=lambda: (config.config_dir / BOOTSTRAP_FILE).exists(),
        write_bootstrap=save_bootstrap,
        progress=lambda: _progress(registry, _current_db(app), clock),
    )
    handlers: dict[str, StepHandler] = {step.id: PendingStep() for step in registry.steps}
    handlers[WELCOME.id] = welcome

    def mark_provider_done() -> None:
        progress = _progress(registry, _current_db(app), clock)
        if progress is None:  # pragma: no cover - a connection test needs a workspace
            raise RuntimeError("no workspace is open")
        progress.mark_done(PROVIDER.id)
        app.state.refresh_provider_health()  # also refreshes the setup decision

    provider = ProviderStep(
        _provider_setup(app, wiring=wiring, clock=clock),
        data_dir=workspace_folder,
        mark_done=mark_provider_done,
        is_done=lambda: onboarding.statuses.get(PROVIDER.id) == "done",
        demo=wiring.demo_workspace,
    )
    handlers[PROVIDER.id] = provider
    app.state.connection_tester = provider  # POST /setup/provider/test (M1.18)
    app.state.provider_settings = provider  # Settings > Providers shares it (M1.21)

    def mark_done(step_id: str) -> Callable[[], None]:
        """Mark ``step_id`` done and refresh the setup decision (provider health as stored).
        Finishing the Done step also ends a ``--setup`` reopening (M1.20)."""

        def mark() -> None:
            progress = _progress(registry, _current_db(app), clock)
            if progress is None:  # pragma: no cover - these steps check for a workspace first
                raise RuntimeError("no workspace is open")
            progress.mark_done(step_id)
            onboarding.refresh(progress, end_reopening=step_id == registry.last.id)

        return mark

    def settings() -> SettingsService | None:
        db = _current_db(app)
        return SettingsService(db, clock) if db is not None else None

    def skip_web_search() -> None:
        progress = _progress(registry, _current_db(app), clock)
        if progress is None:  # pragma: no cover - the step checks for a workspace first
            raise RuntimeError("no workspace is open")
        progress.skip(WEB_SEARCH.id)
        onboarding.refresh(progress)

    if WEB_SEARCH.id in handlers:  # a registry passed to create_app (tests) may leave it out
        handlers[WEB_SEARCH.id] = WebSearchStep(
            app.state.usage_settings,
            mark_done=mark_done(WEB_SEARCH.id),
            skip=skip_web_search,
        )
    handlers[PREFERENCES.id] = PreferencesStep(settings, mark_done=mark_done(PREFERENCES.id))
    handlers[DONE.id] = DoneStep(
        registry,
        statuses=lambda: onboarding.statuses,
        mark_done=mark_done(DONE.id),
        attention=lambda: onboarding.decision.banners,  # "Needs attention" (ADR-066)
    )
    return require_handlers(registry, handlers)


def _provider_setup(
    app: FastAPI, *, wiring: Wiring, clock: Clock
) -> Callable[[], ProviderSetupService | None]:
    """The provider setup service once a workspace is open; built once, so its cached
    keychain status lasts for the run (a workspace opens at most once per app)."""
    built: list[ProviderSetupService] = []
    lock = threading.Lock()  # submits run in worker threads

    def refresh_health() -> object:
        return app.state.refresh_provider_health()

    def service() -> ProviderSetupService | None:
        db = _current_db(app)
        if db is None:
            return None
        with lock:
            if not built:
                built.append(
                    ProviderSetupService(
                        SettingsService(db, clock),
                        wiring.secrets,
                        wiring,
                        wiring.environment_keys(),
                        refresh_health,
                        usage=app.state.usage,  # set whenever a workspace is open
                    )
                )
            return built[0]

    return service


def _open_usage(app: FastAPI) -> Callable[[], UsageService | None]:
    """The open workspace's usage service, read from the app's state at call time; None
    before a workspace is open."""
    return lambda: getattr(app.state, "usage", None) if _current_db(app) else None


def _usage_settings(app: FastAPI, *, wiring: Wiring, clock: Clock) -> UsageSettings:
    """Settings > Usage budgets (M2.22): like the practice service, it reads the open
    workspace through the app's state at call time."""

    def settings() -> SettingsService | None:
        db = _current_db(app)
        return SettingsService(db, clock) if db is not None else None

    return UsageSettings(
        usage=_open_usage(app),
        settings=settings,
        allow_fakes=wiring.allow_fakes,
    )


def _practice(
    app: FastAPI, *, wiring: Wiring, clock: Clock, registry: HandlerRegistry
) -> PracticeService:
    """The practice task's service (M2.17): it reads the open workspace through the app's
    state at call time, so one instance serves the whole run of the app."""

    def workspace() -> Workspace | None:
        found: Workspace | None = getattr(app.state, "workspace", None)
        return found

    def settings() -> AIProviderSettings | None:
        db = _current_db(app)
        return SettingsService(db, clock).load(AI_PROVIDER).value if db is not None else None

    def data_dir() -> Path | None:
        found = workspace()
        return found.data_dir if found is not None else None

    return PracticeService(
        factory=wiring,
        allow_fakes=wiring.allow_fakes,
        script=wiring.script_practice,
        registered=lambda: registry.get(PRACTICE) is not None,
        settings=settings,
        tasks=lambda: getattr(app.state, "task_service", None) if workspace() else None,
        usage=lambda: getattr(app.state, "usage", None) if workspace() else None,
        data_dir=data_dir,
    )


def _cv_import(app: FastAPI, *, wiring: Wiring, clock: Clock, ids: IdFactory) -> CvImportService:
    """The CV import service (M3.4): like the practice service, it reads the open workspace
    through the app's state at call time."""

    def workspace() -> Workspace | None:
        found: Workspace | None = getattr(app.state, "workspace", None)
        return found

    def settings() -> AIProviderSettings | None:
        db = _current_db(app)
        return SettingsService(db, clock).load(AI_PROVIDER).value if db is not None else None

    return CvImportService(
        factory=wiring,
        sniff_type=sniff_type,
        extract_text=extract_text,
        clock=clock,
        ids=ids,
        workspace=workspace,
        settings=settings,
        tasks=lambda: getattr(app.state, "task_service", None) if workspace() else None,
        usage=_open_usage(app),
    )


def _publish_fact_services(
    app: FastAPI, db: Database | None, *, clock: Clock, ids: IdFactory
) -> None:
    """The fact bank's services over the open workspace's database (M3.5), on the app's
    state for the routes; None while no workspace is open. Called just before the workspace
    itself is published."""
    app.state.facts = FactService(db, clock, ids) if db is not None else None
    app.state.fact_library = FactLibrary(db) if db is not None else None
    app.state.roles = RoleService(db, clock, ids) if db is not None else None
    app.state.skills = SkillService(db, clock, ids) if db is not None else None


def _fact_bank(app: FastAPI) -> Callable[[], FactBank | None]:
    """The open workspace's fact services, read through the app's state at call time; None
    while no workspace is open."""

    def bank() -> FactBank | None:
        db = _current_db(app)
        if db is None:
            return None
        state = app.state
        return FactBank(db, state.facts, state.fact_library, state.roles, state.skills)

    return bank


def _publish_fact_screens(app: FastAPI, *, tz: tzinfo) -> None:
    """The editor, the history and the merge screen (M3.6), over the same fact services."""
    bank = _fact_bank(app)
    app.state.fact_editor = FactEditor(bank=bank)
    app.state.fact_history = FactHistory(bank=bank, tz=tz)
    app.state.fact_merge = FactMerge(bank=bank)


def _fact_views(app: FastAPI, *, cv_import: CvImportService, tz: tzinfo) -> FactViews:
    """What the Fact bank page shows (M3.5): like the other view services, it reads the open
    workspace through the app's state at call time."""

    def latest_task(import_id: str) -> TaskView | None:
        views: TaskViews = app.state.task_views
        return views.latest_for(EXTRACT_CV, import_target(import_id))

    return FactViews(
        bank=_fact_bank(app),
        imports=cv_import.imports,
        latest_task=latest_task,
        provider_kind=cv_import.saved_provider_kind,
        import_failed_said=failed_message(EXTRACT_CV_LABEL),
        tz=tz,
    )


def _task_labels(registry: HandlerRegistry) -> Callable[[str], str | None]:
    """The label of a registered task type; None for a type no handler is registered for."""

    def label_for(task_type: str) -> str | None:
        entry = registry.get(task_type)
        return entry[0].label if entry is not None else None

    return label_for


def create_app(
    config: RuntimeConfig,
    *,
    clock: Clock | None = None,
    migrations_dir: Path = MIGRATIONS_DIR,
    attach_log_folder: Callable[[Path], None] | None = None,
    open_at_startup: bool | None = None,
    wiring: Wiring | None = None,
    setup_registry: StepRegistry | None = None,
    event_keepalive_seconds: float = DEFAULT_KEEPALIVE_SECONDS,
    event_queue_size: int = DEFAULT_EVENT_QUEUE_SIZE,
    data_folder_lock: DataFolderLock | None = None,
    task_registry: HandlerRegistry | None = None,
    tz: tzinfo | None = None,
) -> FastAPI:
    """Build the app. ``open_at_startup`` (default: ``opens_workspace_at_startup(config)``)
    says whether the lifespan opens ``config.data_dir``; the CLI passes the value it used.
    ``wiring`` (default: built from the process environment with a new redactor) builds the
    providers; the CLI passes the one whose redactor its logging uses. ``setup_registry``
    (default: ``m1_registry()``) lists the guide's steps; steps without a handler of their
    own get a placeholder. ``event_keepalive_seconds`` is how often an idle ``GET /events``
    stream sends a comment line (finite and greater than 0, else ``ValueError``).
    ``event_queue_size`` is how many task-change frames one stream may have waiting before
    the server ends it (at least 1, else ``ValueError``); ``app.state.task_events`` publishes
    every committed task change to the open streams.
    ``app.state.shutdown_signal`` is made here, not in the lifespan: the CLI sets it when a
    stop is requested, before uvicorn waits for open streams to end (ADR-076 item 1).
    ``data_folder_lock`` is the lock the CLI already holds on ``config.data_dir``; the
    lifespan's open reuses it instead of locking the folder again (ADR-072).
    ``task_registry`` (default: the CV import task, and the practice task while fakes are
    allowed; either way ``app.state.task_registry``, refusing
    timeouts not longer than ``TASK_TIMEOUT_FLOOR``, one provider run plus its close) maps
    task types to their handlers; whenever a workspace opens, its task service
    (``app.state.task_service``, None before) is handed to the runner, and its usage service
    (``app.state.usage``, None before; one per workspace) counts provider calls in budget
    cycles of ``tz`` (default: the computer's time zone; ADR-076 item 8)."""
    app_clock = clock if clock is not None else SystemClock()
    app_tz = tz if tz is not None else local_timezone()
    app_wiring = (
        wiring
        if wiring is not None
        else build_wiring(
            environ=os.environ,
            demo=config.demo,
            demo_workspace=is_demo_workspace(config),
            redactor=Redactor(),
            clock=app_clock,
        )
    )
    open_now = opens_workspace_at_startup(config) if open_at_startup is None else open_at_startup
    open_lock = threading.Lock()
    closed = threading.Event()
    app_ids = UuidIdFactory()
    shutdown_signal = ShutdownSignal()
    task_events = TaskEvents(shutdown_signal, queue_size=event_queue_size)  # ValueError: no bound
    handlers = task_registry if task_registry is not None else HandlerRegistry()
    handlers.require_min_timeout(TASK_TIMEOUT_FLOOR)  # ValueError for a too-short type

    def open_workspace(folder: Path) -> Workspace:
        """Open the data folder chosen in step 1 (see ``open_workspace_folder``).

        Blocking: it creates folders, runs migrations and may copy a backup. Call it from a
        sync route or a worker thread, never directly inside an async handler. Raises
        ``WorkspaceAlreadyOpen`` when one is open and ``WorkspaceClosed`` after shutdown.

        The setup decision and provider health are refreshed from the new database before
        logging is attached and the workspace is published; if anything fails, the database
        is closed, the decision goes back to "no workspace" and the user can retry.
        """
        with open_lock:
            if closed.is_set():
                raise WorkspaceClosed("the app has shut down")
            if getattr(app.state, "workspace", None) is not None:
                raise WorkspaceAlreadyOpen("a workspace is already open")
            onboarding: OnboardingState = app.state.onboarding
            registry: StepRegistry = app.state.setup_registry

            health: list[ProviderHealth] = []

            def refresh_onboarding(db: Database) -> None:
                health.append(
                    _prepare_workspace(
                        folder,
                        db,
                        wiring=app_wiring,
                        onboarding=onboarding,
                        registry=registry,
                        clock=app_clock,
                        ids=app_ids,
                    )
                )

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
            try:
                task_service = _attach_tasks(
                    app.state.runner,
                    workspace.db,
                    clock=app_clock,
                    ids=app_ids,
                    publish=task_events.publish,
                )
            except BaseException:
                workspace.db.close()  # releases the folder lock, so the user can retry
                onboarding.refresh(None)
                raise
            app.state.task_service = task_service
            app.state.usage = UsageService(workspace.db, app_clock, app_ids, app_tz)
            app.state.provider_health = health[-1]
            _publish_fact_services(app, workspace.db, clock=app_clock, ids=app_ids)
            app.state.workspace = workspace
            return workspace

    def refresh_provider_health() -> ProviderHealth:
        """Recheck the provider after a state-changing request (a saved provider or sign-in):
        updates ``app.state.provider_health`` and the setup decision. Never call it on GET.

        Blocking (a database read): call it from a sync route or a worker thread. Raises
        ``RuntimeError`` when no workspace is open and ``WorkspaceClosed`` after shutdown.
        """
        with open_lock:
            if closed.is_set():
                raise WorkspaceClosed("the app has shut down")
            workspace: Workspace | None = getattr(app.state, "workspace", None)
            if workspace is None:
                raise RuntimeError("no workspace is open")
            health = _check_provider(
                workspace.db,
                wiring=app_wiring,
                onboarding=app.state.onboarding,
                registry=app.state.setup_registry,
                clock=app_clock,
            )
            app.state.provider_health = health
            return health

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        registry = setup_registry if setup_registry is not None else m1_registry()
        onboarding = OnboardingState(registry, _startup_inputs(config))
        runner = TaskRunner(registry=handlers)
        workspace = None
        health: ProviderHealth | None = None
        if open_now:

            def prepare(db: Database) -> None:
                nonlocal health
                health = _prepare_workspace(
                    config.data_dir,
                    db,
                    wiring=app_wiring,
                    onboarding=onboarding,
                    registry=registry,
                    clock=app_clock,
                    ids=app_ids,
                )

            workspace = open_workspace_folder(
                config.data_dir,
                migrations_dir=migrations_dir,
                clock=app_clock,
                attach_log_folder=attach_log_folder,
                before_logging=prepare,
                lock=data_folder_lock,
            )
        try:
            task_service = (
                _attach_tasks(
                    runner,
                    workspace.db,
                    clock=app_clock,
                    ids=app_ids,
                    publish=task_events.publish,
                )
                if workspace is not None
                else None
            )
            await runner.start()
            app.state.provider_health = health  # None until a workspace opens
            app.state.setup_registry = registry
            app.state.onboarding = onboarding
            app.state.setup_handlers = _step_handlers(
                app,
                config,
                registry=registry,
                onboarding=onboarding,
                clock=app_clock,
                wiring=app_wiring,
            )
            _publish_fact_services(
                app,
                workspace.db if workspace is not None else None,
                clock=app_clock,
                ids=app_ids,
            )
            app.state.workspace = workspace
            app.state.open_workspace = open_workspace
            app.state.refresh_provider_health = refresh_provider_health
            app.state.runner = runner
            app.state.task_service = task_service
            app.state.usage = (
                UsageService(workspace.db, app_clock, app_ids, app_tz)
                if workspace is not None
                else None
            )
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
    app.state.wiring = app_wiring
    practice = _practice(app, wiring=app_wiring, clock=app_clock, registry=handlers)
    app.state.practice = practice
    # The budget check in front of a started action (M2.24): the open workspace's usage.
    app.state.budget_gate = BudgetGate(_open_usage(app))
    usage_settings = _usage_settings(app, wiring=app_wiring, clock=app_clock)
    app.state.usage_settings = usage_settings
    # The usage panel (M2.23) reads the open workspace's usage service at call time too.
    app.state.usage_panel = UsagePanel(
        _open_usage(app),
        offered=usage_settings.offered,
        label_for=_task_labels(handlers),
        tz=app_tz,
    )
    app.state.task_views = TaskViews(
        tasks=lambda: (
            getattr(app.state, "task_service", None)
            if getattr(app.state, "workspace", None)
            else None
        ),
        label_for=_task_labels(handlers),
        clock=app_clock,
        tz=app_tz,
    )
    if task_registry is None and app_wiring.allow_fakes:  # the default registry only
        handlers.register(
            PRACTICE, TaskTypeSpec(PRACTICE_TIMEOUT, PRACTICE_LABEL), practice_handler(practice)
        )
    cv_import = _cv_import(app, wiring=app_wiring, clock=app_clock, ids=app_ids)
    app.state.cv_import = cv_import
    app.state.fact_views = _fact_views(app, cv_import=cv_import, tz=app_tz)
    _publish_fact_screens(app, tz=app_tz)
    if task_registry is None:  # a real task type: with or without fakes
        handlers.register(
            EXTRACT_CV,
            TaskTypeSpec(CV_IMPORT_TIMEOUT, EXTRACT_CV_LABEL),
            cv_import_handler(cv_import),
        )
    app.state.shutdown_signal = shutdown_signal
    app.state.task_events = task_events
    app.state.task_registry = handlers
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
    register_error_handlers(app)
    app.include_router(health_router)
    register_pages(app)
    register_settings_pages(app)
    register_usage_settings(app)
    register_task_routes(app)
    register_fact_import_routes(app)
    register_fact_pages(app)
    register_fact_editor_routes(app)
    register_setup_pages(app)
    register_events(
        app,
        shutdown=shutdown_signal,
        keepalive_seconds=event_keepalive_seconds,
        task_events=task_events,
    )
    app.mount(STATIC_URL, StaticFiles(directory=STATIC_DIR), name="static")
    return app
