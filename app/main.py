"""App factory and lifespan (spec 2.6, 2.13; ADR-030; ADR-044 item 3).

The lifespan starts the task runner and builds the services. It opens the workspace (the data
folder and its database, migrated) only when ``opens_workspace_at_startup`` says so; on a first
run the app starts without one and step 1 of the guide calls ``app.state.open_workspace``.
Shutdown stops the runner before closing the database. Logging is set up by the CLI before the
app is created. Providers come from the injected ``Wiring`` (``app/wiring.py``); whenever a
workspace opens, leftover AI call folders are cleared and provider health is computed once and
cached in ``app.state.provider_health``, never on GET (spec 2.9.12).
"""

import asyncio
import concurrent.futures
import logging
import os
import sqlite3
import threading
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta, tzinfo
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

from app import __version__
from app.api.application_contacts import register_application_contact_routes
from app.api.application_detail import register_application_detail_routes
from app.api.board import register_board_routes
from app.api.companies import register_company_routes
from app.api.company_brief import register_company_brief_routes
from app.api.company_contacts import register_company_contact_routes
from app.api.cv_step import register_cv_routes
from app.api.debrief import register_debrief_routes
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
from app.api.facts_capture import register_fact_capture_routes
from app.api.facts_coverage import register_fact_coverage_routes
from app.api.facts_import import register_fact_import_routes
from app.api.facts_refine import register_fact_refine_routes
from app.api.health import router as health_router
from app.api.inbox import register_inbox_routes
from app.api.inbox_organisation import register_inbox_organisation_routes
from app.api.job_detail import register_job_detail_routes
from app.api.job_fit import register_job_fit_routes
from app.api.job_intake import register_job_intake_routes
from app.api.job_summary import register_job_summary_routes
from app.api.letter_step import register_letter_routes
from app.api.notifications import register_notification_routes
from app.api.pages import register_pages
from app.api.prep import register_prep_routes
from app.api.prep_adapt import register_adapt_routes
from app.api.prep_cards import register_card_routes
from app.api.prep_practice import register_practice_routes
from app.api.reminders import register_reminder_routes
from app.api.security import SecurityMiddleware, generate_launch_token, page_route_matcher
from app.api.sessions import register_session_routes
from app.api.settings import register_settings_pages
from app.api.settings_appearance import register_appearance_routes
from app.api.settings_areas import register_settings_area_pages
from app.api.settings_board import register_board_settings_routes
from app.api.settings_data import register_data_routes
from app.api.settings_details import register_details_routes
from app.api.settings_lists import register_lists_routes
from app.api.settings_sessions import register_session_settings_routes
from app.api.settings_task_tiers import register_task_tier_routes
from app.api.settings_templates import register_template_routes
from app.api.settings_usage import register_usage_settings
from app.api.setup import register_setup_pages
from app.api.setup_redirect import SetupRedirectMiddleware
from app.api.setup_restore import register_setup_restore
from app.api.submit_step import register_submit_routes
from app.api.tailoring import register_tailoring_routes
from app.api.tasks import register_task_routes
from app.api.templating import STATIC_DIR, STATIC_URL
from app.api.today import register_today_routes
from app.config import (
    BOOTSTRAP_FILE,
    DATABASE_FILE,
    RuntimeConfig,
    data_dir_is_explicit,
    is_demo_workspace,
    load_bootstrap,
    opens_workspace_at_startup,
    owned_by_another_user,
    refused_data_folders,
    write_bootstrap,
)
from app.domain.clock import Clock, SystemClock
from app.domain.errors import ConfigError, DataFolderNotYours, DataFolderUnavailable
from app.domain.ids import IdFactory, UuidIdFactory
from app.domain.redaction import Redactor
from app.domain.settings import AI_PROVIDER, TASK_RUNNER, AIProviderSettings
from app.domain.tasks import TaskChange, TaskStatus, TaskTypeSpec
from app.modules.ai import ProviderHealth
from app.modules.extract import extract_text, sniff_type
from app.modules.secrets import KNOWN_SECRET_KEYS
from app.modules.storage import (
    MIGRATIONS_DIR,
    Database,
    DataFolderLock,
    apply_migrations,
    lock_data_folder,
)
from app.onboarding.progress import SetupProgress
from app.onboarding.registry import (
    CV_IMPORT,
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
from app.onboarding.steps.cv_import import CvImportStep
from app.onboarding.steps.done import DoneStep
from app.onboarding.steps.preferences import PreferencesStep
from app.onboarding.steps.provider import ProviderStep
from app.onboarding.steps.web_search import WebSearchStep
from app.onboarding.steps.welcome import WelcomeSettings, WelcomeStep
from app.services.adapt_ai import ADAPT_ANSWER
from app.services.answer_ai import ANSWER_FEEDBACK
from app.services.answer_practice import AnswerPracticeService
from app.services.appearance import AppearancePreferences
from app.services.application_detail import ApplicationDetail
from app.services.applications import ApplicationService, ApplicationStatuses
from app.services.board import BoardQuery
from app.services.board_views import BoardViews
from app.services.budget_gate import BudgetGate
from app.services.capture import CaptureService
from app.services.companies import CompaniesService
from app.services.company_brief import BRIEF, BRIEF_LABEL, BriefService
from app.services.contacts import ContactsService
from app.services.controlled_lists import ControlledLists
from app.services.coverage_views import CoverageViews
from app.services.cv_import import (
    EXTRACT_CV,
    EXTRACT_CV_LABEL,
    PARSE_TIMEOUT_SECONDS,
    CvImportService,
    fact_bank_started,
    import_target,
    sweep_orphan_imports,
)
from app.services.cv_templates import DRAFT_LABEL, CvTemplateService
from app.services.data_erasure import DataErasureService
from app.services.data_portability import DataPortabilityService, sweep_restore_staging
from app.services.debrief import DEBRIEF_FACTS, DEBRIEF_FACTS_LABEL, DebriefService
from app.services.documents import TAILOR_CV_LABEL, DocumentService
from app.services.export_files import sweep_orphan_exports
from app.services.exports import ExportService
from app.services.fact_duplicates import FactDuplicates
from app.services.fact_editor import FactEditor
from app.services.fact_history import FactHistory
from app.services.fact_library import FactLibrary
from app.services.fact_merge import FactMerge
from app.services.fact_refine import REFINE_FACT, REFINE_FACT_LABEL, FactRefineService
from app.services.fact_views import FactBank, FactViews
from app.services.facts import FactService
from app.services.health import HealthService
from app.services.inbox import InboxQuery
from app.services.inbox_organisation import InboxOrganisation
from app.services.inbox_views import InboxViews
from app.services.job_detail import JobDetailService
from app.services.job_fit import ASSESS_FIT, FIT_LABEL, JobFitService
from app.services.job_intake import JobIntakeService
from app.services.job_preferences import JobPreferencesView
from app.services.job_summary import SUMMARIZE_JOB, SUMMARIZE_LABEL, JobSummaryService
from app.services.job_triage import JobTriageService
from app.services.letters import DRAFT_COVER_LETTER_LABEL, LetterService
from app.services.narrative_ai import DRAFT_NARRATIVE
from app.services.personal_details import PersonalDetailsService, sweep_orphan_photos
from app.services.practice import PRACTICE, PRACTICE_LABEL, PracticeService
from app.services.prep import QUESTIONS, WEB_QUESTIONS, PrepService
from app.services.prep_documents import (
    ADAPT_ANSWER_LABEL,
    DRAFT_NARRATIVE_LABEL,
    PrepDocumentService,
)
from app.services.provider_setup import ProviderSetupService
from app.services.reminders import ReminderRules, ReminderService
from app.services.roles import RoleService
from app.services.session_preferences import SessionPreferences
from app.services.session_screen import SessionScreens
from app.services.session_suggestion import SessionSuggestions
from app.services.sessions import (
    ANSWER_LABEL,
    ENRICHMENT_QUESTION,
    QUESTION_LABEL,
    STRUCTURE_ANSWER,
    SessionService,
)
from app.services.settings import SettingsService
from app.services.setup_state import SetupStateService
from app.services.skills import SkillService
from app.services.tailoring import (
    GAP_CHECK_LABEL,
    PROPOSE_ANGLES_LABEL,
    TailoringService,
)
from app.services.tailoring_ai import (
    DRAFT_COVER_LETTER,
    GAP_CHECK,
    PROPOSE_ANGLES,
    TAILOR_CV,
)
from app.services.task_views import TaskView, TaskViews, failed_message
from app.services.tasks import TaskService
from app.services.template_ai import DRAFT_CV_TEMPLATE
from app.services.today import TodayService
from app.services.usage import UsageService
from app.services.usage_panel import UsagePanel
from app.services.usage_settings import UsageSettings
from app.tasks import HandlerRegistry, TaskRunner
from app.tasks.answer_feedback import answer_feedback_handler
from app.tasks.company_brief import company_brief_handler
from app.tasks.cv_import import cv_import_handler
from app.tasks.cv_templates import draft_cv_template_handler
from app.tasks.debrief import debrief_facts_handler
from app.tasks.documents import draft_cover_letter_handler, tailor_cv_handler
from app.tasks.fact_refine import fact_refine_handler
from app.tasks.job_fit import job_fit_handler
from app.tasks.job_summary import job_summary_handler
from app.tasks.practice import practice_handler
from app.tasks.prep import prep_handler
from app.tasks.prep_documents import adapt_answer_handler, draft_narrative_handler
from app.tasks.sessions import enrichment_question_handler, structure_answer_handler
from app.tasks.tailoring import gap_check_handler, propose_angles_handler
from app.wiring import (
    TASK_TIMEOUT_FLOOR,
    Wiring,
    build_wiring,
    clear_ai_temp,
    local_timezone,
    system_time_zone_name,
)

_log = logging.getLogger(__name__)

BACKUP_FOLDER = "backups"
LOG_FOLDER = "logs"
# Longer than one provider run plus its close, so a slow step is never taken for a hung task.
PRACTICE_TIMEOUT = TASK_TIMEOUT_FLOOR + timedelta(minutes=1)
# One Thorough call and its close, plus the task's one read of the stored file (the upload
# route made the other), with twice its parse timeout as margin between beats.
CV_IMPORT_TIMEOUT = (
    TASK_TIMEOUT_FLOOR + timedelta(seconds=2 * PARSE_TIMEOUT_SECONDS) + timedelta(minutes=1)
)
# One Fast call and its close, with a minute of margin between beats.
REFINE_TIMEOUT = TASK_TIMEOUT_FLOOR + timedelta(minutes=1)
# How long closing the workspace waits for running tasks to end interrupted.
RUNNER_RESTART_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True)
class Workspace:
    """The open data folder and its migrated database."""

    data_dir: Path
    db: Database


class WorkspaceAlreadyOpen(RuntimeError):
    """``open_workspace`` was called while a workspace is already open."""


class WorkspaceClosed(RuntimeError):
    """``open_workspace`` was called after the app shut down."""


class WorkspaceBusy(RuntimeError):
    """``open_workspace`` was called while delete all data is running."""


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
    running as interrupted (ADR-076 item 10), remove stored photo files no setting names (M6.6)
    and stored CV files no import names (M3.4: the folder lock is held, the database is
    migrated and no upload can be stored yet), mark
    the CV import step done when the fact bank already holds something (M3.7; ADR-052), then
    check the provider (ADR-049, ADR-057), which refreshes the setup decision."""
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
    try:
        sweep_orphan_photos(folder, db)
    except sqlite3.Error as error:
        detail = f"can't read the photo setting ({type(error).__name__})"
        raise DataFolderUnavailable(detail=detail) from None
    try:
        sweep_orphan_exports(folder, db)
    except sqlite3.Error as error:
        detail = f"can't list exports ({type(error).__name__})"
        raise DataFolderUnavailable(detail=detail) from None
    sweep_restore_staging(folder)  # best effort; it swallows its own OS errors
    _mark_cv_import_if_started(db, registry=registry, clock=clock)
    return _check_provider(db, wiring=wiring, onboarding=onboarding, registry=registry, clock=clock)


def _mark_cv_import_if_started(db: Database, *, registry: StepRegistry, clock: Clock) -> None:
    """Setup's CV import step is done once the workspace holds a fact or an upload, however
    they got there (ADR-052). Only when the step is registered and not done yet. A setup
    state that can't be read fails like the refresh after it would; the fact bank check is a
    convenience for an optional step, so when it can't be made the step is left as it is
    rather than the workspace refused."""
    progress = _progress(registry, db, clock)
    if progress is None or CV_IMPORT.id not in {step.id for step in registry.steps}:
        return
    if progress.statuses().get(CV_IMPORT.id) == "done":
        return
    try:
        started = fact_bank_started(db)
    except sqlite3.Error as error:  # sqlite messages can hold paths: log the type only
        _log.warning("The fact bank couldn't be checked (%s).", type(error).__name__)
        return
    if started:
        progress.mark_done(CV_IMPORT.id)


def _workspace_restored(app: FastAPI, *, clock: Clock) -> Callable[[], None]:
    """What the app does once a restore (Settings > Data and privacy) has filled the open
    workspace: the task runner takes the restored concurrency setting, and the setup decision
    and provider health are computed again from the restored rows. The other services read the
    same database at call time, so they need nothing. Blocking: call it from a worker thread;
    ``RuntimeError`` when no workspace is open."""

    def refresh() -> None:
        db = _current_db(app)
        tasks = getattr(app.state, "task_service", None)
        if db is None or tasks is None:
            raise RuntimeError("no workspace is open")
        app.state.runner.attach(tasks, settings=SettingsService(db, clock).load(TASK_RUNNER).value)
        app.state.refresh_provider_health()

    return refresh


def _data_folder(app: FastAPI) -> Path | None:
    workspace: Workspace | None = getattr(app.state, "workspace", None)
    return workspace.data_dir if workspace is not None else None


def _fact_bank_used(app: FastAPI, *, clock: Clock) -> Callable[[], None]:
    """What the Fact bank tells after an upload is kept or a fact is added (M3.7; ADR-052):
    setup's CV import step is then done, and the setup decision refreshed. It reads the
    registry, the decision and the database through the app's state at call time, does
    nothing once the step is done (from the cached statuses), and never raises: the user's
    upload or fact is saved either way."""

    def mark() -> None:
        registry: StepRegistry | None = getattr(app.state, "setup_registry", None)
        onboarding: OnboardingState | None = getattr(app.state, "onboarding", None)
        if registry is None or onboarding is None:
            return
        if CV_IMPORT.id not in {step.id for step in registry.steps}:
            return
        if onboarding.statuses.get(CV_IMPORT.id) == "done":
            return
        progress = _progress(registry, _current_db(app), clock)
        if progress is None:
            return
        try:
            progress.mark_done(CV_IMPORT.id)
            onboarding.refresh(progress)
        except Exception as error:  # sqlite messages can hold paths: log the type only
            _log.warning("The CV import step couldn't be marked done (%s).", type(error).__name__)

    return mark


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

    def skip_cv_import() -> None:
        progress = _progress(registry, _current_db(app), clock)
        if progress is None:  # pragma: no cover - the step checks for a workspace first
            raise RuntimeError("no workspace is open")
        progress.skip(CV_IMPORT.id)
        onboarding.refresh(progress)

    if CV_IMPORT.id in handlers:  # a registry passed to create_app (tests) may leave it out
        handlers[CV_IMPORT.id] = CvImportStep(
            app.state.cv_import,
            app.state.budget_gate,
            workspace_open=lambda: _current_db(app) is not None,
            mark_done=mark_done(CV_IMPORT.id),
            skip=skip_cv_import,
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
    """The provider setup service once a workspace is open. One per open database, so its
    cached keychain status lasts as long as that workspace: deleting all data opens a later
    workspace (a new database) and gets a fresh service, never one over a closed database."""
    built: list[tuple[Database, ProviderSetupService]] = []
    lock = threading.Lock()  # submits run in worker threads

    def refresh_health() -> object:
        return app.state.refresh_provider_health()

    def service() -> ProviderSetupService | None:
        db = _current_db(app)
        if db is None:
            return None
        with lock:
            if not built or built[0][0] is not db:
                built[:] = [
                    (
                        db,
                        ProviderSetupService(
                            SettingsService(db, clock),
                            wiring.secrets,
                            wiring,
                            wiring.environment_keys(),
                            refresh_health,
                            usage=app.state.usage,  # set whenever a workspace is open
                        ),
                    )
                ]
            return built[0][1]

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


def _job_summary(
    app: FastAPI, *, wiring: Wiring, clock: Clock, ids: IdFactory
) -> JobSummaryService:
    def workspace() -> Workspace | None:
        found: Workspace | None = getattr(app.state, "workspace", None)
        return found

    def settings() -> AIProviderSettings | None:
        db = _current_db(app)
        return SettingsService(db, clock).load(AI_PROVIDER).value if db else None

    return JobSummaryService(
        factory=wiring,
        clock=clock,
        ids=ids,
        workspace=workspace,
        settings=settings,
        tasks=lambda: getattr(app.state, "task_service", None),
        usage=_open_usage(app),
    )


def _job_fit(app: FastAPI, *, wiring: Wiring, clock: Clock, ids: IdFactory) -> JobFitService:
    def workspace() -> Workspace | None:
        return getattr(app.state, "workspace", None)

    def settings() -> AIProviderSettings | None:
        db = _current_db(app)
        return SettingsService(db, clock).load(AI_PROVIDER).value if db else None

    return JobFitService(
        factory=wiring,
        clock=clock,
        ids=ids,
        workspace=workspace,
        settings=settings,
        tasks=lambda: getattr(app.state, "task_service", None),
        usage=_open_usage(app),
        gate=lambda: app.state.budget_gate,
    )


def _tailoring(app: FastAPI, *, wiring: Wiring, clock: Clock, ids: IdFactory) -> TailoringService:
    def workspace() -> Workspace | None:
        return getattr(app.state, "workspace", None)

    def settings() -> AIProviderSettings | None:
        db = _current_db(app)
        return SettingsService(db, clock).load(AI_PROVIDER).value if db else None

    return TailoringService(
        factory=wiring,
        clock=clock,
        ids=ids,
        workspace=workspace,
        settings=settings,
        tasks=lambda: getattr(app.state, "task_service", None),
        usage=_open_usage(app),
    )


def _prep(app: FastAPI, *, wiring: Wiring, clock: Clock, ids: IdFactory, tz: tzinfo) -> PrepService:
    def settings() -> AIProviderSettings | None:
        db = _current_db(app)
        return SettingsService(db, clock).load(AI_PROVIDER).value if db else None

    return PrepService(
        factory=wiring,
        clock=clock,
        ids=ids,
        workspace=lambda: getattr(app.state, "workspace", None),
        settings=settings,
        tasks=lambda: getattr(app.state, "task_service", None),
        usage=_open_usage(app),
        search=wiring.search_provider,
        search_settings=wiring.search_settings,
        transient=wiring.search_results,
        tz=tz,
    )


def _company_brief(
    app: FastAPI, *, wiring: Wiring, clock: Clock, ids: IdFactory, tz: tzinfo
) -> BriefService:
    def settings() -> AIProviderSettings | None:
        db = _current_db(app)
        return SettingsService(db, clock).load(AI_PROVIDER).value if db else None

    return BriefService(
        factory=wiring,
        clock=clock,
        ids=ids,
        workspace=lambda: getattr(app.state, "workspace", None),
        settings=settings,
        tasks=lambda: getattr(app.state, "task_service", None),
        usage=_open_usage(app),
        search=wiring.search_provider,
        search_settings=wiring.search_settings,
        transient=wiring.search_results,
        tz=tz,
    )


def _answer_practice(
    app: FastAPI, *, wiring: Wiring, clock: Clock, ids: IdFactory
) -> AnswerPracticeService:
    def settings() -> AIProviderSettings | None:
        db = _current_db(app)
        return SettingsService(db, clock).load(AI_PROVIDER).value if db else None

    return AnswerPracticeService(
        factory=wiring,
        clock=clock,
        ids=ids,
        workspace=lambda: getattr(app.state, "workspace", None),
        settings=settings,
        tasks=lambda: getattr(app.state, "task_service", None),
        usage=_open_usage(app),
        prep=app.state.prep,
    )


def _debrief(app: FastAPI, *, wiring: Wiring, clock: Clock, ids: IdFactory) -> DebriefService:
    def settings() -> AIProviderSettings | None:
        db = _current_db(app)
        return SettingsService(db, clock).load(AI_PROVIDER).value if db else None

    return DebriefService(
        factory=wiring,
        clock=clock,
        ids=ids,
        workspace=lambda: getattr(app.state, "workspace", None),
        settings=settings,
        tasks=lambda: getattr(app.state, "task_service", None),
        usage=_open_usage(app),
        # Created after the prep services, so it is read when a debrief needs it.
        applications=lambda: app.state.applications,
    )


def _sessions(app: FastAPI, *, wiring: Wiring, clock: Clock, ids: IdFactory) -> SessionService:
    def settings() -> AIProviderSettings | None:
        db = _current_db(app)
        return SettingsService(db, clock).load(AI_PROVIDER).value if db else None

    return SessionService(
        factory=wiring,
        clock=clock,
        ids=ids,
        workspace=lambda: getattr(app.state, "workspace", None),
        settings=settings,
        tasks=lambda: getattr(app.state, "task_service", None),
        usage=_open_usage(app),
        gate=lambda: app.state.budget_gate,
    )


def _documents(app: FastAPI, *, wiring: Wiring, clock: Clock, ids: IdFactory) -> DocumentService:
    return DocumentService(**_document_args(app, wiring, clock, ids))


def _letters(app: FastAPI, *, wiring: Wiring, clock: Clock, ids: IdFactory) -> LetterService:
    return LetterService(**_document_args(app, wiring, clock, ids))


def _prep_documents(
    app: FastAPI, *, wiring: Wiring, clock: Clock, ids: IdFactory
) -> PrepDocumentService:
    return PrepDocumentService(**_document_args(app, wiring, clock, ids))


def _document_args(app: FastAPI, wiring: Wiring, clock: Clock, ids: IdFactory) -> dict[str, Any]:
    """What the CV and the cover letter services are built from (they share their stored
    documents)."""

    def workspace() -> Workspace | None:
        return getattr(app.state, "workspace", None)

    def settings() -> AIProviderSettings | None:
        db = _current_db(app)
        return SettingsService(db, clock).load(AI_PROVIDER).value if db else None

    return {
        "factory": wiring,
        "clock": clock,
        "ids": ids,
        "workspace": workspace,
        "settings": settings,
        "tasks": lambda: getattr(app.state, "task_service", None),
        "usage": _open_usage(app),
        "facts": lambda: getattr(app.state, "facts", None),
    }


def _cv_templates(
    app: FastAPI, *, wiring: Wiring, clock: Clock, ids: IdFactory
) -> CvTemplateService:
    def workspace() -> Workspace | None:
        return getattr(app.state, "workspace", None)

    def settings() -> AIProviderSettings | None:
        db = _current_db(app)
        return SettingsService(db, clock).load(AI_PROVIDER).value if db else None

    return CvTemplateService(
        factory=wiring,
        clock=clock,
        ids=ids,
        workspace=workspace,
        settings=settings,
        tasks=lambda: getattr(app.state, "task_service", None),
        usage=_open_usage(app),
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
        on_stored=_fact_bank_used(app, clock=clock),
    )


def _fact_refine(
    app: FastAPI, *, wiring: Wiring, clock: Clock, ids: IdFactory
) -> FactRefineService:
    """Refine with AI (M3.8): like the CV import service, it reads the open workspace
    through the app's state at call time."""

    def workspace() -> Workspace | None:
        found: Workspace | None = getattr(app.state, "workspace", None)
        return found

    def settings() -> AIProviderSettings | None:
        db = _current_db(app)
        return SettingsService(db, clock).load(AI_PROVIDER).value if db is not None else None

    return FactRefineService(
        factory=wiring,
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


def _publish_prompt_editor(app: FastAPI, wiring: Wiring, data_dir: Path | None) -> None:
    """The prompt editor over the open workspace's overrides folder (M9.3), on the app's
    state for the routes; None while no workspace is open. Builds nothing on disk."""
    app.state.prompt_editor = wiring.prompt_editor(data_dir) if data_dir is not None else None


def _restart_runner(runner: TaskRunner, loop: list[asyncio.AbstractEventLoop]) -> None:
    """Stop the runner on its event loop, let go of the closing workspace and start it again,
    so a later workspace's tasks run. Called from a worker thread holding the open lock; on
    the loop's own thread it would wait for itself, so that is refused."""
    if not loop:
        return  # the lifespan hasn't started the runner
    try:
        running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop[0]:
        raise RuntimeError("The workspace can't be closed from the event loop.")

    async def cycle() -> None:
        await runner.stop()
        runner.detach()
        await runner.start()

    future = asyncio.run_coroutine_threadsafe(cycle(), loop[0])
    try:
        future.result(timeout=RUNNER_RESTART_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise RuntimeError("The task runner didn't stop in time.") from None


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


def _publish_fact_screens(app: FastAPI, *, tz: tzinfo, clock: Clock, ids: IdFactory) -> None:
    """The editor, the history and the merge screen (M3.6), the capture inbox and the
    possible duplicates (M3.7), over the same fact services and database."""
    bank = _fact_bank(app)
    app.state.fact_editor = FactEditor(bank=bank, on_created=_fact_bank_used(app, clock=clock))
    app.state.fact_history = FactHistory(bank=bank, tz=tz)
    app.state.fact_merge = FactMerge(bank=bank)
    app.state.capture = CaptureService(lambda: _current_db(app), clock, ids)
    app.state.fact_duplicates = FactDuplicates(bank=bank, clock=clock)


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
    detach_log_folder: Callable[[], None] | None = None,
    reattach_log_folder: Callable[[Path], None] | None = None,
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
    cycles of ``tz`` (default: the computer's time zone; ADR-076 item 8).
    ``reattach_log_folder`` logs into the folder again when delete all data reopens a fixed one.
    ``detach_log_folder`` stops writing the log file (delete all data closes the workspace);
    ``app.state.data_erasure`` is the delete-all-data service (ADR-131 item 2)."""
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
    server_loop: list[asyncio.AbstractEventLoop] = []  # set by the lifespan
    erased_folder: list[Path] = []  # the folder a half-finished erase still has to empty
    erasing = threading.Event()  # delete all data is running: no other open meanwhile
    app_ids = UuidIdFactory()
    shutdown_signal = ShutdownSignal()
    task_events = TaskEvents(shutdown_signal, queue_size=event_queue_size)  # ValueError: no bound
    handlers = task_registry if task_registry is not None else HandlerRegistry()
    handlers.require_min_timeout(TASK_TIMEOUT_FLOOR)  # ValueError for a too-short type

    def open_workspace(folder: Path) -> Workspace:
        """Open the data folder chosen in step 1 (see ``open_workspace_folder``).

        Blocking: it creates folders, runs migrations and may copy a backup. Call it from a
        sync route or a worker thread, never directly inside an async handler. Raises
        ``WorkspaceAlreadyOpen`` when one is open, ``WorkspaceBusy`` while delete all data
        runs and ``WorkspaceClosed`` after shutdown.

        The setup decision and provider health are refreshed from the new database before
        logging is attached and the workspace is published; if anything fails, the database
        is closed, the decision goes back to "no workspace" and the user can retry.
        """
        return open_folder(folder, lock=None, reopening=False)

    def open_folder(folder: Path, *, lock: DataFolderLock | None, reopening: bool) -> Workspace:
        """``open_workspace``; ``lock`` is a data-folder lock the caller holds on ``folder``
        (see ``open_workspace_folder``) and ``reopening`` is delete all data opening its own
        fresh workspace, which the hold-off on other opens does not apply to."""
        with open_lock:
            if closed.is_set():
                raise WorkspaceClosed("the app has shut down")
            if erasing.is_set() and not reopening:
                raise WorkspaceBusy("delete all data is running")
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
                    attach_log_folder=attach_log_folder
                    if attach_log_folder is not None
                    else reattach_log_folder,
                    before_logging=refresh_onboarding,
                    lock=lock,
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
            _publish_prompt_editor(app, app_wiring, workspace.data_dir)
            app.state.workspace = workspace
            erased_folder.clear()
            return workspace

    def close_workspace() -> None:
        """Close the open workspace for good (delete all data, ADR-131 item 2): stop the task
        runner (running tasks end interrupted) and start it again with nothing attached,
        close the database (which releases the folder lock), forget what the workspace held in
        memory and send the guide back to "no workspace". Nothing is open afterwards, so a
        later ``open_workspace`` works once the erase is over (``settle_erase``). Safe to call
        again. Blocking: call it from a worker thread, never from the event loop
        (``RuntimeError``)."""
        with open_lock:
            if closed.is_set():
                return
            erasing.set()  # until settle_erase: no other open meanwhile
            workspace: Workspace | None = getattr(app.state, "workspace", None)
            if workspace is not None:
                _restart_runner(app.state.runner, server_loop)
                erased_folder[:] = [workspace.data_dir]
                app.state.workspace = None
                app.state.task_service = None
                app.state.usage = None
                app.state.provider_health = None
                _publish_fact_services(app, None, clock=app_clock, ids=app_ids)
                _publish_prompt_editor(app, app_wiring, None)
                workspace.db.close()
                if not data_dir_is_explicit(config) and data_folder_lock is not None:
                    # A folder chosen in step 1 may be opened again by step 1: the lock the
                    # command line took for it must not stay held. (A fixed folder is
                    # reopened by ``after_erase``, which reuses the lock.)
                    data_folder_lock.release()
            app_wiring.search_results.clear()
            if detach_log_folder is not None:
                detach_log_folder()
            onboarding: OnboardingState = app.state.onboarding
            onboarding.refresh(None, bootstrap_exists=(config.config_dir / BOOTSTRAP_FILE).exists())

    def after_erase() -> None:
        """Run once everything was removed: the setup file is gone, so the guide starts at step
        1, and a folder fixed by ``--data-dir``, the environment or ``--demo`` is opened again
        as a fresh workspace (reusing the lock the command line holds on it)."""
        erased_folder.clear()
        onboarding: OnboardingState = app.state.onboarding
        onboarding.refresh(None, bootstrap_exists=(config.config_dir / BOOTSTRAP_FILE).exists())
        if data_dir_is_explicit(config):
            held = data_folder_lock is not None and data_folder_lock.held
            open_folder(
                config.data_dir,
                lock=data_folder_lock if held else None,
                reopening=True,
            )

    def settle_erase() -> None:
        """A delete-all-data run is over, finished or stopped: other opens are allowed."""
        erasing.clear()

    def bootstrap_folder() -> Path | None:
        """The folder ``config.toml`` names; None when there is no file, ``ConfigError`` when
        it can't be read."""
        found = load_bootstrap(config.config_dir)
        return found.data_dir if found is not None else None

    def erasable_folder() -> Path | None:
        """The open workspace's folder, else the folder a stopped erase left behind, else (after
        a restart that opened nothing) the folder ``config.toml`` names, when it exists."""
        workspace: Workspace | None = getattr(app.state, "workspace", None)
        if workspace is not None:
            return workspace.data_dir
        if erased_folder:
            return erased_folder[0]
        if config.demo:
            return None  # config.toml belongs to the real setup
        try:
            named = bootstrap_folder()
        except ConfigError:
            return None
        return named if named is not None and named.is_dir() else None

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
        server_loop[:] = [asyncio.get_running_loop()]
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
            _publish_prompt_editor(
                app, app_wiring, workspace.data_dir if workspace is not None else None
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
    app.state.prompt_editor = None  # published with the workspace
    app.state.data_erasure = DataErasureService(
        erasable_folder,
        app_wiring.secrets,
        KNOWN_SECRET_KEYS,
        config.config_dir,
        close_workspace=close_workspace,
        reopen=after_erase,
        demo=config.demo,
        bootstrap_folder=bootstrap_folder,
        settle=settle_erase,
    )
    # Export and restore (ADR-131 items 3-4) read the open workspace at call time. One restore or
    # import at a time (Settings > Data and privacy and setup's restore share the slot).
    app.state.import_active = False
    app.state.data_portability = DataPortabilityService(
        lambda: _current_db(app),
        lambda: _data_folder(app),
        app_clock,
        migrations_dir=migrations_dir,
    )
    app.state.workspace_restored = _workspace_restored(app, clock=app_clock)
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
    app.state.job_summary = _job_summary(app, wiring=app_wiring, clock=app_clock, ids=app_ids)
    app.state.job_fit = _job_fit(app, wiring=app_wiring, clock=app_clock, ids=app_ids)
    if task_registry is None:
        handlers.register(
            SUMMARIZE_JOB,
            TaskTypeSpec(TASK_TIMEOUT_FLOOR + timedelta(seconds=30), SUMMARIZE_LABEL),
            job_summary_handler(app.state.job_summary, app.state.job_fit.after_summary),
        )
        handlers.register(
            ASSESS_FIT,
            TaskTypeSpec(TASK_TIMEOUT_FLOOR + timedelta(seconds=30), FIT_LABEL),
            job_fit_handler(app.state.job_fit),
        )
    app.state.tailoring = _tailoring(app, wiring=app_wiring, clock=app_clock, ids=app_ids)
    app.state.sessions = _sessions(app, wiring=app_wiring, clock=app_clock, ids=app_ids)
    app.state.prep = _prep(app, wiring=app_wiring, clock=app_clock, ids=app_ids, tz=app_tz)
    app.state.company_brief = _company_brief(
        app, wiring=app_wiring, clock=app_clock, ids=app_ids, tz=app_tz
    )
    app.state.answer_practice = _answer_practice(
        app, wiring=app_wiring, clock=app_clock, ids=app_ids
    )
    app.state.debrief = _debrief(app, wiring=app_wiring, clock=app_clock, ids=app_ids)
    if task_registry is None:
        handlers.register(
            QUESTIONS,
            TaskTypeSpec(TASK_TIMEOUT_FLOOR + timedelta(seconds=30), "Interview questions"),
            prep_handler(app.state.prep),
        )
        handlers.register(
            WEB_QUESTIONS,
            TaskTypeSpec(TASK_TIMEOUT_FLOOR + timedelta(minutes=3), "Reported online questions"),
            prep_handler(app.state.prep, web=True),
        )
        handlers.register(
            BRIEF,
            TaskTypeSpec(TASK_TIMEOUT_FLOOR + timedelta(minutes=3), BRIEF_LABEL),
            company_brief_handler(app.state.company_brief),
        )
        handlers.register(
            ANSWER_FEEDBACK,
            TaskTypeSpec(TASK_TIMEOUT_FLOOR + timedelta(seconds=30), "Answer feedback"),
            answer_feedback_handler(app.state.answer_practice),
        )
        handlers.register(
            DEBRIEF_FACTS,
            TaskTypeSpec(TASK_TIMEOUT_FLOOR + timedelta(minutes=2), DEBRIEF_FACTS_LABEL),
            debrief_facts_handler(app.state.debrief),
        )
        handlers.register(
            STRUCTURE_ANSWER,
            TaskTypeSpec(TASK_TIMEOUT_FLOOR + timedelta(seconds=30), ANSWER_LABEL),
            structure_answer_handler(app.state.sessions),
        )
        handlers.register(
            ENRICHMENT_QUESTION,
            TaskTypeSpec(TASK_TIMEOUT_FLOOR + timedelta(seconds=30), QUESTION_LABEL),
            enrichment_question_handler(app.state.sessions),
        )
    # Settings > Your details (M6.6): the setting and the photo file; no AI task reads them.
    app.state.cv_templates = _cv_templates(app, wiring=app_wiring, clock=app_clock, ids=app_ids)
    app.state.documents = _documents(app, wiring=app_wiring, clock=app_clock, ids=app_ids)
    app.state.letters = _letters(app, wiring=app_wiring, clock=app_clock, ids=app_ids)
    app.state.prep_documents = _prep_documents(app, wiring=app_wiring, clock=app_clock, ids=app_ids)
    app.state.personal_details = PersonalDetailsService(
        lambda: _current_db(app),
        app_clock,
        app_ids,
        lambda: getattr(getattr(app.state, "workspace", None), "data_dir", None),
    )
    app.state.exports = ExportService(
        lambda: _current_db(app),
        lambda: getattr(getattr(app.state, "workspace", None), "data_dir", None),
        app_clock,
        app_ids,
        app.state.documents,
        app.state.letters,
        app.state.personal_details,
        app_wiring.document_renderers(),
    )
    if task_registry is None:
        handlers.register(
            GAP_CHECK,
            TaskTypeSpec(TASK_TIMEOUT_FLOOR + timedelta(seconds=30), GAP_CHECK_LABEL),
            gap_check_handler(app.state.tailoring),
        )
        handlers.register(
            PROPOSE_ANGLES,
            TaskTypeSpec(TASK_TIMEOUT_FLOOR + timedelta(seconds=30), PROPOSE_ANGLES_LABEL),
            propose_angles_handler(app.state.tailoring),
        )
        handlers.register(
            DRAFT_CV_TEMPLATE,
            TaskTypeSpec(TASK_TIMEOUT_FLOOR + timedelta(seconds=30), DRAFT_LABEL),
            draft_cv_template_handler(app.state.cv_templates),
        )
        handlers.register(
            TAILOR_CV,
            TaskTypeSpec(TASK_TIMEOUT_FLOOR + timedelta(seconds=30), TAILOR_CV_LABEL),
            tailor_cv_handler(app.state.documents),
        )
        handlers.register(
            DRAFT_COVER_LETTER,
            TaskTypeSpec(TASK_TIMEOUT_FLOOR + timedelta(seconds=30), DRAFT_COVER_LETTER_LABEL),
            draft_cover_letter_handler(app.state.letters),
        )
        handlers.register(
            DRAFT_NARRATIVE,
            TaskTypeSpec(TASK_TIMEOUT_FLOOR + timedelta(seconds=30), DRAFT_NARRATIVE_LABEL),
            draft_narrative_handler(app.state.prep_documents),
        )
        handlers.register(
            ADAPT_ANSWER,
            TaskTypeSpec(TASK_TIMEOUT_FLOOR + timedelta(seconds=30), ADAPT_ANSWER_LABEL),
            adapt_answer_handler(app.state.prep_documents),
        )
    app.state.job_intake = JobIntakeService(
        lambda: _current_db(app), app_clock, app_ids, app_wiring.job_page_fetcher()
    )
    app.state.job_triage = JobTriageService(lambda: _current_db(app), app_clock, app_tz)
    app.state.applications = ApplicationService(
        lambda: _current_db(app), app_clock, app_ids, app_tz
    )
    app.state.application_statuses = ApplicationStatuses(
        lambda: _current_db(app), app_clock, app_ids
    )
    app.state.application_detail = ApplicationDetail(
        lambda: _current_db(app), app_clock, app_ids, app_wiring.calendar_provider(), app_tz
    )
    app.state.board = BoardQuery(lambda: _current_db(app), app_clock)
    app.state.board_views = BoardViews(
        app.state.applications, app.state.application_statuses, app.state.board, app_clock, app_tz
    )
    # The computer's IANA time zone (a name, or None when it can't be read safely): read once
    # here, it is the interview dialog's default (ADR-102 item 6).
    app.state.system_time_zone = system_time_zone_name()
    app.state.inbox_views = InboxViews(
        lambda: _current_db(app),
        InboxQuery(lambda: _current_db(app), app_clock, app_tz),
        app_clock,
        app_tz,
    )
    app.state.job_detail = JobDetailService(
        lambda: _current_db(app), app_clock, app_ids, app.state.inbox_views
    )
    app.state.inbox_organisation = InboxOrganisation(lambda: _current_db(app), app_clock, app_ids)
    app.state.companies = CompaniesService(lambda: _current_db(app), app_clock, app_tz)
    app.state.contacts = ContactsService(lambda: _current_db(app), app_clock, app_ids)
    # Reminders are computed when read; nothing here is scheduled (ADR-013).
    app.state.reminders = ReminderService(lambda: _current_db(app), app_clock, app_ids, app_tz)
    app.state.today = TodayService(lambda: _current_db(app), app_clock, app.state.reminders, app_tz)
    app.state.reminder_rules = ReminderRules(lambda: _current_db(app), app_clock, app_ids)
    app.state.controlled_lists = ControlledLists(lambda: _current_db(app), app_clock, app_ids)
    cv_import = _cv_import(app, wiring=app_wiring, clock=app_clock, ids=app_ids)
    app.state.cv_import = cv_import
    app.state.fact_views = _fact_views(app, cv_import=cv_import, tz=app_tz)
    _publish_fact_screens(app, tz=app_tz, clock=app_clock, ids=app_ids)
    app.state.session_screens = SessionScreens(app.state.sessions, _fact_bank(app), app_clock)
    app.state.coverage_views = CoverageViews(bank=_fact_bank(app), clock=app_clock)
    app.state.session_preferences = SessionPreferences(lambda: _current_db(app), app_clock)
    app.state.appearance = AppearancePreferences(lambda: _current_db(app), app_clock)
    app.state.job_preferences = JobPreferencesView(lambda: _current_db(app), app_clock)
    app.state.session_suggestions = SessionSuggestions(
        lambda: _current_db(app), app_clock, app.state.sessions
    )
    app.state.session_zone = app_tz
    fact_refine = _fact_refine(app, wiring=app_wiring, clock=app_clock, ids=app_ids)
    app.state.fact_refine = fact_refine
    if task_registry is None:  # real task types: with or without fakes
        handlers.register(
            EXTRACT_CV,
            TaskTypeSpec(CV_IMPORT_TIMEOUT, EXTRACT_CV_LABEL),
            cv_import_handler(cv_import),
        )
        handlers.register(
            REFINE_FACT,
            TaskTypeSpec(REFINE_TIMEOUT, REFINE_FACT_LABEL),
            fact_refine_handler(fact_refine),
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
    register_session_routes(app)
    register_settings_pages(app)
    register_settings_area_pages(app)
    register_data_routes(app)
    register_usage_settings(app)
    register_board_settings_routes(app)
    register_lists_routes(app)
    register_session_settings_routes(app)
    register_task_tier_routes(app)
    register_appearance_routes(app)
    register_details_routes(app)
    register_template_routes(app)
    register_task_routes(app)
    register_fact_import_routes(app)
    # Before the fact pages: their "{id:path}/discard" would also match ".../refine/discard".
    register_fact_refine_routes(app)
    register_fact_pages(app)
    register_fact_editor_routes(app)
    register_fact_capture_routes(app)
    register_fact_coverage_routes(app)
    register_inbox_routes(app)
    register_inbox_organisation_routes(app)
    register_job_intake_routes(app)
    register_job_summary_routes(app)
    register_job_fit_routes(app)
    register_tailoring_routes(app)
    register_submit_routes(app)
    register_cv_routes(app)
    register_letter_routes(app)
    register_job_detail_routes(app)
    register_company_routes(app)
    register_company_brief_routes(app)
    register_company_contact_routes(app)
    register_board_routes(app)
    register_application_detail_routes(app)
    register_prep_routes(app)
    register_practice_routes(app)
    register_debrief_routes(app)
    register_adapt_routes(app)
    register_card_routes(app)
    register_application_contact_routes(app)
    register_reminder_routes(app)
    register_notification_routes(app)
    register_today_routes(app)
    register_setup_pages(app)
    register_setup_restore(app)
    register_events(
        app,
        shutdown=shutdown_signal,
        keepalive_seconds=event_keepalive_seconds,
        task_events=task_events,
    )
    app.mount(STATIC_URL, StaticFiles(directory=STATIC_DIR), name="static")
    return app
