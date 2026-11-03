"""CV import: store an upload, then have the AI list what it states as drafts (M3.4; AC-M3.1,
AC-M3.7; spec 2.9 rule 5, 3.1, 3.3; ADR-088, ADR-089 items 8 and 11, ADR-090 items 7 and 8).

Storing decides the type by content, writes the uploaded bytes under a generated name in
``<data>/imports`` (0600, folder 0700), reads the text in a child process and refuses a text
over ``MAX_CV_TEXT_CHARS`` whole, never cut; any failure leaves no file and no row. Running
(the ``extract_cv`` task) reads the stored file again, makes one AI call through the usage
meter and returns the writer that stores every unit as a draft, the roles as draft roles and
the import as done, inside the task's one result transaction. Nothing an import makes is
confirmed. A unit is marked ``needs_check`` when its quote isn't in the CV, when its wording
holds a number or a word its own quote doesn't, when its role's employer, title or a year is
not in the CV, or when it names a skill the CV doesn't (``app/domain/cv_checks.py``); such a
skill is not kept.

Files in ``<data>/imports`` that no row names (a kill between the file write and the row
insert) are removed when a workspace opens (``sweep_orphan_imports``).

The CV's text, the model's answer and the client's file name are untrusted and personal:
none is logged or stored anywhere but the fact bank (spec 2.10). Everything here blocks
except ``run``.
"""

import asyncio
import logging
import os
import re
import sqlite3
import stat
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Protocol

from app.domain.clock import Clock
from app.domain.cv_checks import (
    CvText,
    has_duplicates,
    normalise,
    real_partial_date,
    says_no_more_than,
    unescape_lt,
)
from app.domain.errors import (
    CvImportAlreadyDone,
    CvImportBusy,
    CvImportNotFound,
    CvTextTooLong,
    InvalidModelOutput,
    NothingExtracted,
    ProviderNotConfigured,
)
from app.domain.facts import FactType
from app.domain.ids import IdFactory
from app.domain.settings import AIProviderSettings, Tier
from app.domain.tasks import TaskRef, dedupe_key, is_task_id
from app.domain.tiers import task_tier
from app.domain.usage import SERVICE_BY_PROVIDER_KIND, ServiceId
from app.modules.ai import AIProviderFactory, CancelToken, TaskResult
from app.modules.storage import Database, cv_imports_repo, facts_repo, tasks_repo
from app.services.facts import DraftInput, DraftOrigin, DraftRole, FactService
from app.services.tasks import TaskService
from app.services.usage import UsageService
from app.services.usage_meter import ServiceUsageMeter

EXTRACT_CV: Final = "extract_cv"  # the task type, the AI task and the prompt id
EXTRACT_CV_LABEL: Final = "CV import"
MAX_CV_TEXT_CHARS: Final = 60_000
PARSE_TIMEOUT_SECONDS: Final = 30
FAST: Final[Tier] = "fast"
IMPORTS_FOLDER: Final = "imports"
SOURCE_CV_IMPORT: Final = "cv_import"
STATE_DONE: Final = "done"
STATE_UPLOADED: Final = "uploaded"

_TARGET_PREFIX: Final = "cv_import"
_SEPARATOR: Final = "/"  # never part of an id
_EXTENSIONS: Final[Mapping[str, str]] = {"text": "txt", "pdf": "pdf", "docx": "docx"}
# The names ``store`` gives its files: an id and the extension of the sniffed kind.
_GENERATED_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\.(txt|pdf|docx)")

_log = logging.getLogger(__name__)

SniffType = Callable[[bytes], str]
"""The extract module's ``sniff_type``: "pdf", "docx" or "text" by content, or
``UnsupportedDocumentType``."""

ExtractText = Callable[[Path, str, float], str]
"""The extract module's ``extract_text(path, kind, timeout)``: blocking, the parse runs in a
child process killed at the deadline. Both are injected by ``app/main.py``: services use
module interfaces only (spec 2.3 rule 4), so this module names no extract provider."""

ResultWriter = Callable[[sqlite3.Connection], str]
"""Stores an import's result on the connection of the task's result transaction and returns
the ``result_ref``; it must not commit."""


class OpenWorkspace(Protocol):
    @property
    def data_dir(self) -> Path: ...

    @property
    def db(self) -> Database: ...


class ImportContext(Protocol):
    """The part of the runner's ``TaskContext`` an import uses."""

    @property
    def task_id(self) -> str: ...

    @property
    def input_ref(self) -> str | None: ...

    @property
    def cancel_flag(self) -> CancelToken: ...

    def heartbeat(self, progress: int | None = None) -> Awaitable[None]: ...

    def check_cancelled(self) -> None: ...


@dataclass(frozen=True)
class ImportSummary:
    """One kept upload as a page lists it: never its file name."""

    id: str
    kind: str
    state: str  # "uploaded" (can be started or removed) or "done"
    created_at: datetime
    task_id: str | None  # the task that made its drafts, once done


@dataclass(frozen=True)
class StartPlan:
    """What a start will be counted on: the budget gate is asked about exactly this."""

    service: ServiceId
    tier: Tier


def import_target(import_id: str) -> str:
    """The dedupe target of an import's task: one active task per import."""
    return f"{_TARGET_PREFIX}:{import_id}"


def import_result_ref(import_id: str) -> str:
    """Names the import whose drafts a succeeded task made."""
    return import_target(import_id)


def tier_from_form(form: Mapping[str, Sequence[str]]) -> Tier | None:
    """``fast`` when the form asks for one run on Fast (the budget dialog's offer); None,
    the prompt's own tier, for anything else."""
    return FAST if tuple(form.get("tier", ())) == (FAST,) else None


# --- the model's answer as drafts (pure) --------------------------------------------------------


def drafts_from(
    data: Mapping[str, Any], cv_text: str
) -> tuple[tuple[DraftRole, ...], tuple[DraftInput, ...]]:
    """The roles and drafts of a schema-valid ``extract_cv`` answer, each unit checked
    against the CV's text. A role given under two keys (equal employer, title and dates) is
    one role, and a role no unit names is not stored. Raises ``InvalidModelOutput`` for a
    role key given twice and ``NothingExtracted`` for an answer without units."""
    if has_duplicates(role["key"] for role in data["roles"]):
        raise InvalidModelOutput(detail="extract_cv: a role key is given twice")
    cv = CvText(cv_text)
    roles = _Roles()
    for given in data["roles"]:
        roles.add(given, cv)
    drafts = tuple(_draft(unit, cv, roles) for unit in data["units"])
    if not drafts:
        raise NothingExtracted(detail="extract_cv: no units")
    used = {draft.role_key for draft in drafts}
    return tuple(role for role in roles.stored if role.key in used), drafts


class _Roles:
    """The roles of one answer: each distinct role once, under the first key that named it."""

    def __init__(self) -> None:
        self._stored: dict[str, DraftRole] = {}
        self._first_key: dict[tuple[str, str, str | None, str | None], str] = {}
        self._stored_key: dict[str, str] = {}
        self._sound: dict[str, bool] = {}

    @property
    def stored(self) -> tuple[DraftRole, ...]:
        return tuple(self._stored.values())

    def add(self, given: Mapping[str, Any], cv: CvText) -> None:
        start, start_dropped = _stated_date(given["start_date"], cv)
        end, end_dropped = _stated_date(given["end_date"], cv)
        role = DraftRole(
            key=given["key"],
            employer=given["employer"] or "",
            title=given["title"] or "",
            start_date=start,
            end_date=end,
        )
        same = (normalise(role.employer), normalise(role.title), start, end)
        first = self._first_key.setdefault(same, role.key)
        self._stored.setdefault(first, role)
        self._stored_key[role.key] = first
        self._sound[role.key] = cv.names(role.employer, role.title) and not (
            start_dropped or end_dropped
        )

    def key_for(self, key: str | None) -> str | None:
        """The key the unit's role is stored under; None for no role or an unknown key."""
        return self._stored_key.get(key) if key is not None else None

    def sound(self, key: str | None) -> bool:
        """Whether a unit under ``key`` has nothing to check about its role: no role, or a
        known one whose employer, title and years the CV gives."""
        return key is None or self._sound.get(key, False)


def _stated_date(text: str | None, cv: CvText) -> tuple[str | None, bool]:
    """A role date as stored, and whether it was dropped because the CV never writes its
    year. A date the calendar doesn't have is dropped without a mark, as before."""
    date = real_partial_date(text)
    if date is not None and not cv.has_year(date):
        return None, True
    return date, False


def _draft(unit: Mapping[str, Any], cv: CvText, roles: _Roles) -> DraftInput:
    """One unit as a draft. It needs a check unless its quote is in the CV, its wording
    holds only numbers and words of that quote, its role (when it has one) is a known one
    the CV names and dates, and every skill is named in the CV. A skill that isn't is left
    out. ``&lt;`` in the answer is stored as ``<``."""
    key: str | None = unit["role_key"]
    quote: str = unit["source_quote"]
    parts = tuple(
        unescape_lt(part) if part else part
        for part in (unit["text"], unit["situation"], unit["action"], unit["result"])
    )
    skills = tuple(skill for skill in unit["skills"] if cv.mentions(skill))
    faithful = (
        cv.states(quote)
        and says_no_more_than(quote, parts)
        and roles.sound(key)
        and len(skills) == len(unit["skills"])
    )
    return DraftInput(
        type=FactType(unit["type"]),
        text=parts[0],
        situation=parts[1],
        action=parts[2],
        result=parts[3],
        role_key=roles.key_for(key),
        skills=skills,
        original_answer=unescape_lt(quote),
        needs_check=not faithful,
    )


# --- the stored file ----------------------------------------------------------------------------


def _imports_dir(data_dir: Path) -> Path:
    """``<data>/imports`` as a real folder only its owner can open."""
    folder = data_dir / IMPORTS_FOLDER
    folder.mkdir(mode=0o700, exist_ok=True)
    if not stat.S_ISDIR(folder.lstat().st_mode):
        raise NotADirectoryError("the imports folder is a link or not a folder")
    if os.name == "posix":
        folder.chmod(0o700)
    return folder


def _write_private(path: Path, data: bytes) -> None:
    """Create ``path`` (never an existing file or a link) readable only by its owner; a
    failed write leaves no file."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
    except BaseException:
        _remove_file(path)
        raise


def _remove_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        _log.warning("Couldn't remove a stored CV file (%s)", type(error).__name__)


def _is_file_name(name: str) -> bool:
    """Whether a stored name read from the database is one ``store`` could have given: a
    plain name, never a path. Only such a name is joined to the imports folder."""
    return Path(name).name == name and _GENERATED_NAME.fullmatch(name) is not None


def sweep_orphan_imports(data_dir: Path, db: Database) -> int:
    """Remove the stored CV files no import row names (left by a kill between the file
    write and the row insert) and return how many went. Only regular files directly inside
    ``<data>/imports`` whose name is a generated one are removed: never a link, a folder or
    another name, and nothing when the folder is missing or is itself a link. Call it while
    the workspace opens, before any upload can be stored. Blocking. Only the count is
    logged."""
    folder = data_dir / IMPORTS_FOLDER
    try:
        if not stat.S_ISDIR(folder.lstat().st_mode):
            return 0
        with os.scandir(folder) as entries:
            found = [
                entry.name
                for entry in entries
                if _GENERATED_NAME.fullmatch(entry.name) and entry.is_file(follow_symlinks=False)
            ]
    except OSError:  # no folder yet, or one that can't be read: nothing to sweep
        return 0
    if not found:
        return 0
    with db.read() as conn:
        known = cv_imports_repo.stored_names(conn)
    removed = 0
    for name in sorted(set(found) - known):
        try:
            os.unlink(folder / name)  # never follows a link
        except OSError:
            continue
        removed += 1
    if removed:
        _log.info("Removed %d stored CV file(s) no import names", removed)
    return removed


def fact_bank_started(db: Database) -> bool:
    """Whether the workspace already holds a fact (of any status) or an upload: setup's CV
    import step then counts as done (M3.7; ADR-052). Reads only."""
    with db.read() as conn:
        return bool(facts_repo.count_by_status(conn)) or bool(cv_imports_repo.list_imports(conn))


def _checked_length(text: str) -> str:
    if len(text) > MAX_CV_TEXT_CHARS:
        raise CvTextTooLong(detail="the CV text is over the limit")
    return text


def _parse_input(input_ref: str | None) -> tuple[str, Tier | None]:
    import_id, _, tier = (input_ref or "").partition(_SEPARATOR)
    if not is_task_id(import_id) or tier not in ("", FAST):
        raise CvImportNotFound(detail="extract_cv: the input names no import")
    return import_id, FAST if tier else None


class CvImportService:
    def __init__(
        self,
        *,
        factory: AIProviderFactory,
        sniff_type: SniffType,
        extract_text: ExtractText,
        clock: Clock,
        ids: IdFactory,
        workspace: Callable[[], OpenWorkspace | None],
        settings: Callable[[], AIProviderSettings | None],
        tasks: Callable[[], TaskService | None],
        usage: Callable[[], UsageService | None],
        on_stored: Callable[[], None] = lambda: None,
    ) -> None:
        """``workspace``, ``settings`` (blocking: a database read), ``tasks`` and ``usage``
        answer None while no workspace is open. ``on_stored`` is told after each upload is
        kept (wiring marks setup's CV import step done with it, M3.7); it never raises."""
        self._factory = factory
        self._sniff_type = sniff_type
        self._extract_text = extract_text
        self._clock = clock
        self._ids = ids
        self._workspace = workspace
        self._settings = settings
        self._tasks = tasks
        self._usage = usage
        self._on_stored = on_stored

    # --- for routes (blocking) ------------------------------------------------------------------

    def store(self, data: bytes) -> str:
        """Keep an upload and return its import id. The type is decided by content and the
        file name is generated. Raises the document errors of ``sniff_type`` and
        ``extract_text``, or ``CvTextTooLong``; then, and on any other failure, no file and
        no row are left."""
        workspace = self._open()
        kind = self._sniff_type(data)
        import_id = self._ids.new()
        path = _imports_dir(workspace.data_dir) / f"{self._ids.new()}.{_EXTENSIONS[kind]}"
        _write_private(path, data)
        try:
            _checked_length(self._extract_text(path, kind, PARSE_TIMEOUT_SECONDS))
            now = self._clock.now().isoformat()
            with workspace.db.transaction() as conn:
                cv_imports_repo.insert_import(
                    conn, import_id=import_id, kind=kind, stored_name=path.name, now=now
                )
        except BaseException:
            _remove_file(path)
            raise
        _log.info("CV import %s stored (%s)", import_id, kind)
        self._on_stored()
        return import_id

    def plan_start(self, import_id: str, *, tier: Tier | None = None) -> StartPlan:
        """The usage service and tier a start of this import would be counted on, for the
        budget gate. Raises ``CvImportNotFound``, ``CvImportAlreadyDone`` or
        ``ProviderNotConfigured``. Writes nothing."""
        self._startable(import_id)
        settings, service = self._provider()
        # One run on Fast (the budget dialog) wins; else the user's override or the default.
        return StartPlan(service, tier if tier is not None else task_tier(EXTRACT_CV, settings))

    def start(self, import_id: str, *, tier: Tier | None = None) -> TaskRef:
        """Queue the import's task; one already queued or running is returned instead.
        ``tier`` runs it once on that tier. Raises ``CvImportNotFound`` or
        ``CvImportAlreadyDone``."""
        self._startable(import_id)
        tasks = self._tasks()
        if tasks is None:
            raise CvImportNotFound(detail="no workspace is open")
        input_ref = import_id if tier is None else f"{import_id}{_SEPARATOR}{tier}"
        ref = tasks.enqueue(EXTRACT_CV, import_target(import_id), input_ref=input_ref)
        _log.info("CV import %s: task %s (new: %s)", import_id, ref.id, ref.created)
        return ref

    def remove(self, import_id: str) -> None:
        """Delete an import's row and file; drafts it made stay, no longer tied to it
        (ADR-090 item 7). Raises ``CvImportNotFound``, or ``CvImportBusy`` while its task is
        queued or running."""
        workspace = self._workspace()
        if workspace is None or not is_task_id(import_id):
            raise CvImportNotFound(detail="not an import id")
        with workspace.db.transaction() as conn:
            stored = cv_imports_repo.get_import(conn, import_id)
            if stored is None:
                raise CvImportNotFound(detail="no such import")
            key = dedupe_key(EXTRACT_CV, import_target(import_id))
            if tasks_repo.get_active(conn, key) is not None:
                raise CvImportBusy(detail="the import's task is active")
            cv_imports_repo.delete_import(conn, import_id)
        if _is_file_name(stored.stored_name):
            _remove_file(workspace.data_dir / IMPORTS_FOLDER / stored.stored_name)
        _log.info("CV import %s removed", import_id)

    def imports(self) -> tuple[ImportSummary, ...]:
        """Every kept upload, newest first: those still to be imported (``uploaded``, which
        can be started or removed) and those done. Empty while no workspace is open."""
        workspace = self._workspace()
        if workspace is None:
            return ()
        with workspace.db.read() as conn:
            listed = cv_imports_repo.list_imports(conn)
        return tuple(
            ImportSummary(
                id=item.id,
                kind=item.kind,
                state=item.state,
                created_at=datetime.fromisoformat(item.created_at),
                task_id=item.task_id,
            )
            for item in listed
        )

    def saved_provider_kind(self) -> str | None:
        """The kind of the saved provider, or None when none is saved. Reads only."""
        settings = self._settings()
        return settings.kind if settings is not None else None

    # --- for the task handler -------------------------------------------------------------------

    async def run(self, ctx: ImportContext) -> ResultWriter:
        """Read the stored CV, make the one AI call and return the writer for the task's
        result transaction. Nothing is written here. Provider errors, ``InvalidModelOutput``,
        ``NothingExtracted``, the document errors and the cancel signal propagate;
        ``CvImportNotFound`` or ``CvImportAlreadyDone`` when the import is gone or done."""
        import_id, tier = _parse_input(ctx.input_ref)
        await ctx.heartbeat(0)
        workspace = self._open()
        stored = await asyncio.to_thread(self._startable, import_id)
        settings, service = await asyncio.to_thread(self._provider)
        usage = self._usage()
        if usage is None:
            raise CvImportNotFound(detail="no workspace is open")
        try:
            provider = self._factory.ai_provider(settings, data_dir=workspace.data_dir)
        except ValueError:  # the saved provider is no longer on offer
            raise ProviderNotConfigured(detail="the saved provider isn't on offer") from None
        runner = self._factory.task_runner(provider, lambda: settings, data_dir=workspace.data_dir)
        path = workspace.data_dir / IMPORTS_FOLDER / stored.stored_name
        text = _checked_length(
            await asyncio.to_thread(self._extract_text, path, stored.kind, PARSE_TIMEOUT_SECONDS)
        )
        await ctx.heartbeat(10)
        result = await runner.run_task(
            EXTRACT_CV,
            {"cv_text": text, "import_id": import_id},
            ctx.cancel_flag,
            meter=ServiceUsageMeter(usage, service),
            tier=tier,
            task_id=ctx.task_id,
        )
        ctx.check_cancelled()
        roles, drafts = drafts_from(result.data, text)
        flagged = sum(draft.needs_check for draft in drafts)
        _log.info(
            "CV import %s: %d drafts (%d to check), %d roles",
            import_id,
            len(drafts),
            flagged,
            len(roles),
        )
        origin = _origin(import_id, provider.name, result)
        return self._writer(workspace.db, ctx.task_id, origin, roles, drafts)

    # --- helpers --------------------------------------------------------------------------------

    def _open(self) -> OpenWorkspace:
        workspace = self._workspace()
        if workspace is None:  # setup sends every request to the guide until one is open
            raise RuntimeError("no workspace is open")
        return workspace

    def _startable(self, import_id: str) -> cv_imports_repo.StoredImport:
        """The stored import, still to be imported. Only an id of the safe shape is looked
        up."""
        workspace = self._workspace()
        if workspace is None or not is_task_id(import_id):
            raise CvImportNotFound(detail="not an import id")
        with workspace.db.read() as conn:
            stored = cv_imports_repo.get_import(conn, import_id)
        if stored is None:
            raise CvImportNotFound(detail="no such import")
        if stored.state == STATE_DONE:
            raise CvImportAlreadyDone(detail="the import is done")
        if not _is_file_name(stored.stored_name):
            raise CvImportNotFound(detail="the stored name is not a file name")
        return stored

    def _provider(self) -> tuple[AIProviderSettings, ServiceId]:
        """The saved provider and the usage service its calls are counted on: the same one
        for the budget gate and the meter."""
        settings = self._settings()
        service = SERVICE_BY_PROVIDER_KIND.get(settings.kind or "") if settings else None
        if settings is None or service is None:
            raise ProviderNotConfigured(detail="no provider is saved")
        return settings, service

    def _writer(
        self,
        db: Database,
        task_id: str,
        origin: DraftOrigin,
        roles: tuple[DraftRole, ...],
        drafts: tuple[DraftInput, ...],
    ) -> ResultWriter:
        import_id = origin.cv_import_id or ""
        facts = FactService(db, self._clock, self._ids)

        def write(conn: sqlite3.Connection) -> str:
            now = self._clock.now().isoformat()
            if not cv_imports_repo.mark_done(conn, import_id, task_id=task_id, now=now):
                # Removed, or finished by another attempt, while this one ran.
                raise CvImportNotFound(detail="the import is gone or done")
            facts.create_drafts_in(conn, origin, drafts, roles=roles)
            return import_result_ref(import_id)

        return write


def _origin(import_id: str, provider: str, result: TaskResult) -> DraftOrigin:
    """What made the drafts, kept on every version so fake-made ones can be labelled."""
    return DraftOrigin(
        source=SOURCE_CV_IMPORT,
        provider=provider,
        model=result.model,
        prompt_id=result.prompt_id,
        prompt_version=result.prompt_version,
        cv_import_id=import_id,
    )


__all__ = [
    "EXTRACT_CV",
    "EXTRACT_CV_LABEL",
    "MAX_CV_TEXT_CHARS",
    "PARSE_TIMEOUT_SECONDS",
    "CvImportService",
    "ImportSummary",
    "StartPlan",
    "drafts_from",
    "import_result_ref",
    "import_target",
    "sweep_orphan_imports",
    "tier_from_form",
]
