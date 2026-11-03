"""The tailored CV (M6.7; AC-M6.1 to AC-M6.4; spec 3.1, 3.2; ADR-090, ADR-106 items 5, 8, 9, 11 and
12): generating, regenerating, editing and approving.

Generation is a background task (``tailor_cv:<application id>``). Its whole result, the draft
version with its sent facts, units, citations and honesty flags, is written by the writer that
``ctx.complete`` runs in the transaction that marks the task succeeded, and nowhere else. The
model supplies content lines only: headings, role headers and the contact block come from code
(``document_views``), and the contact block is read from the saved details by ``view`` alone,
never by a task, so no request can hold it.

Every edit, addition, removal and confirmation writes a NEW draft version in one transaction,
after checking the caller's version is the document's current one. Earlier versions, approved
ones included, are never touched. A flag is resolved by editing the line, removing it, or
confirming it as a fact: the confirmation creates the confirmed fact and the version citing it in
one transaction. Approval re-runs the approval checks with the facts as they are now.
"""

import asyncio
import json
import sqlite3
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, ClassVar, Final

from pydantic import ValidationError

from app.domain.clock import Clock
from app.domain.cv_templates import TemplateSpec
from app.domain.documents import DocumentStatus, DocumentUnit, UnitOrigin, merge_regenerated
from app.domain.errors import (
    ApplicationNotFound,
    FitNotAvailable,
    ProviderNotConfigured,
    VersionConflict,
)
from app.domain.fact_selection import (
    AIFact,
    FactRecord,
    fact_text,
    facts_for_ai,
    worded_then,
)
from app.domain.facts import FactContent, FactRef, fact_ref_text
from app.domain.honesty import GeneratedUnit, check_units, new_claims
from app.domain.ids import IdFactory, is_safe_id
from app.domain.master_cv import RolePeriod, build_master_cv, cv_diff
from app.domain.settings import (
    JOB_PREFERENCES,
    PERSONAL_DETAILS,
    AIProviderSettings,
    PersonalDetails,
    Tier,
    has_control_characters,
)
from app.domain.tailored_cv import (
    MAX_UNIT_TEXT,
    TailoredCv,
    check_added_details,
    check_fact_ids,
)
from app.domain.tasks import TaskRef
from app.domain.tiers import task_tier
from app.domain.usage import SERVICE_BY_PROVIDER_KIND
from app.modules.ai import AIProviderFactory, AITaskRunner, UsageMeter
from app.modules.storage import (
    documents_repo,
    facts_repo,
    job_fit_repo,
    roles_repo,
    skills_repo,
    tailoring_repo,
)
from app.services.document_views import (
    PRACTICE_NOTICE,
    Blocker,
    ContactBlock,
    DocumentView,
    FlagView,
    UnitView,
    VersionInfo,
    blockers_of,
    contact_block,
    lines_needing_check,
    role_label,
    sections_of,
)
from app.services.facts import SOURCE_DOCUMENT, FactInput, FactService
from app.services.job_summary import ResultWriter, SummaryContext, SummaryPlan, SummaryWorkspace
from app.services.settings import SettingsService
from app.services.tailoring import (
    NO_FACTS,
    NO_REQUIREMENTS,
    NO_SUMMARY,
    FactWording,
    TaskState,
)
from app.services.tailoring_ai import (
    MAX_NOTE_CHARS,
    TAILOR_CV,
    ChosenAngle,
    TailoringJob,
    run_tailor_cv,
)
from app.services.tasks import TaskService
from app.services.usage import UsageService
from app.services.usage_meter import ServiceUsageMeter

TAILOR_CV_LABEL: Final = "Tailor CV"
SAVE_AS_FACT: Final = "Save this as a fact?"
NO_COUNTRY: Final = "Choose the CV country first, so the right template is used."
NO_TEMPLATE: Final = "Approve a CV template for this country in Settings first."
NOTE_TOO_LONG: Final = f"Keep the note to {MAX_NOTE_CHARS} characters or fewer."
REPLACE_NOT_CONFIRMED: Final = "Confirm that you want to replace everything, including your edits."
NOTE_BAD_CHARACTERS: Final = "Use plain text in the note, without special characters."
BAD_TIER: Final = "Choose the fast or thorough tier."
IN_PROGRESS: Final = "A CV is already being made. Wait for it to finish."
NOTHING_TO_APPROVE: Final = "There is nothing to approve yet."
MAX_VISIBLE_UNITS: Final = 120
TOO_MANY_UNITS: Final = (
    f"A CV can have at most {MAX_VISIBLE_UNITS} lines. Remove one before adding another."
)
_ACTIVE: Final = ("queued", "running")
TIERS: Final = (None, "fast", "thorough")
_NOTE_REFUSED: Final = frozenset({"Cc", "Cf", "Zl", "Zp"})


class GenerateNotReady(ValueError):
    """A CV can't be generated yet. ``reason`` is ``summary``, ``requirements``, ``facts``,
    ``country`` or ``template``; ``message`` says what to do first, in plain words."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class ReplaceNotConfirmed(ValueError):
    """ "Replace everything" was asked for without the explicit confirmation (nothing queued)."""

    def __init__(self) -> None:
        super().__init__(REPLACE_NOT_CONFIRMED)
        self.message = REPLACE_NOT_CONFIRMED


class GenerateInProgress(ValueError):
    """A document is already being made and this request differs from it (nothing queued)."""

    def __init__(self, message: str = IN_PROGRESS) -> None:
        super().__init__(message)
        self.message = message


class NothingToApprove(ValueError):
    """The version has no visible line, so there is nothing to approve (nothing changed)."""

    def __init__(self) -> None:
        super().__init__(NOTHING_TO_APPROVE)
        self.message = NOTHING_TO_APPROVE


class ApprovalBlocked(ValueError):
    """Lines still need the user's check; nothing changed. ``blockers`` lists them."""

    def __init__(self, blockers: tuple[Blocker, ...], noun: str = "line") -> None:
        count = lines_needing_check(blockers)
        message = (
            f"1 {noun} needs your check." if count == 1 else f"{count} {noun}s need your check."
        )
        super().__init__(message)
        self.blockers = blockers
        self.message = message


class UnitInvalid(ValueError):
    """The line, place or role can't be used (nothing written); ``message`` says what to change."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnitNotFound(LookupError):
    """The line is not a (visible) line of that version."""

    def __init__(self) -> None:
        super().__init__("That line wasn't found in this version.")
        self.message = "That line wasn't found in this version."


@dataclass(frozen=True)
class FactOffer:
    """ "Save this as a fact?": the new claims an edited line makes, and the line they are in."""

    question: str
    claims: tuple[str, ...]
    unit_id: str


@dataclass(frozen=True)
class EditOutcome:
    """The new draft version, the changed line's id in it, and the offer (None when the line
    makes no claim its facts do not)."""

    version_id: str
    unit_id: str
    offer: FactOffer | None


@dataclass(frozen=True)
class DocumentSource:
    job_id: str
    inputs: job_fit_repo.FitInputs
    workspace: tailoring_repo.StoredWorkspace | None
    country: str
    template: tailoring_repo.StoredTemplate | None
    skills: tuple[str, ...]
    roles: tuple[roles_repo.StoredRole, ...]
    document: documents_repo.StoredDocument | None
    previous: tuple[documents_repo.StoredUnit, ...]


@dataclass(frozen=True)
class StartRequest:
    application_id: str
    tier: Tier | None
    note: str
    replace_all: bool
    tone: str | None = None
    length: str | None = None


@dataclass(frozen=True)
class DocumentDraft:
    """What an edit starts from: the current version's lines and everything its re-check needs."""

    document: documents_repo.StoredDocument
    parent: documents_repo.StoredVersion
    lines: list[documents_repo.StoredUnit]
    roles: tuple[roles_repo.StoredRole, ...]
    template: TemplateSpec | None
    names: tuple[str, ...] = ()  # extra name sources (a letter: the job's title and company)


def task_state_of(row: tuple[str, str, str | None, int] | None) -> TaskState:
    if row is None:
        return TaskState()
    return TaskState(row[1] in _ACTIVE, row[1], row[2], f"{row[0]}:{row[3]}")


def request_text(
    aid: str,
    tier: Tier | None,
    note: str,
    replace_all: bool,
    tone: str | None = None,
    length: str | None = None,
) -> str:
    """What a start stores in the task (a letter also stores its tone and length)."""
    asked: dict[str, Any] = {
        "application": aid,
        "tier": tier,
        "note": note,
        "replace": replace_all,
    }
    if tone is not None and length is not None:
        asked.update(tone=tone, length=length)
    return json.dumps(asked, ensure_ascii=False)


def request_from(text: str | None) -> StartRequest:
    """What a start stored; ``ApplicationNotFound`` for anything else."""
    try:
        loaded: Any = json.loads(text or "")
        aid, tier, note, replace_all = (
            loaded["application"],
            loaded["tier"],
            loaded["note"],
            loaded["replace"],
        )
        if not is_safe_id(aid) or tier not in TIERS or not isinstance(note, str):
            raise ValueError
        if len(note) > MAX_NOTE_CHARS or not isinstance(replace_all, bool):
            raise ValueError
        tone, length = loaded.get("tone"), loaded.get("length")
        if not all(item is None or isinstance(item, str) for item in (tone, length)):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ApplicationNotFound() from None
    return StartRequest(aid, tier, note, replace_all, tone, length)


def clean_note(value: str) -> str:
    """The note as stored: no NUL, line ends as LF (a browser posts CRLF), trimmed. Any other
    control, bidi or invisible format character is refused (``ValueError``, fixed message);
    line breaks and tabs stay."""
    cleaned = value.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    for char in cleaned:
        if char not in "\n\t" and unicodedata.category(char) in _NOTE_REFUSED:
            raise ValueError(NOTE_BAD_CHARACTERS)
    return cleaned


def template_of(snapshot: str | None) -> TemplateSpec | None:
    if not snapshot:
        return None
    try:
        return TemplateSpec.model_validate_json(snapshot)
    except ValidationError:
        return None


def _checked_text(text: str, noun: str, limit: int) -> str:
    if not text.strip():
        raise UnitInvalid(f"Write the {noun} before saving.")
    if len(text) > limit:
        raise UnitInvalid(f"Keep the {noun} to {limit} characters or fewer.")
    if has_control_characters(text):
        raise UnitInvalid("Use plain text on one line, without special characters.")
    return text


def checked_ids(fact_ids: Sequence[str]) -> tuple[str, ...]:
    try:
        return check_fact_ids(fact_ids)
    except ValueError as error:
        raise UnitInvalid(str(error)) from None


def checked_details(details: Sequence[str]) -> tuple[str, ...]:
    try:
        return check_added_details(details)
    except ValueError as error:
        raise UnitInvalid(str(error)) from None


def ref_pairs(refs: Sequence[str]) -> tuple[tuple[int, int], ...]:
    pairs: list[tuple[int, int]] = []
    for ref in refs:
        try:
            parsed = FactRef.parse(ref)
        except ValueError:
            continue
        pairs.append((parsed.number, parsed.version))
    return tuple(pairs)


def document_unit_of(unit: documents_repo.StoredUnit, labels: dict[str, str]) -> DocumentUnit:
    return DocumentUnit(
        unit.id,
        "structure" if unit.kind == "structure" else "content",
        unit.section,
        labels.get(unit.role_id or ""),
        unit.text,
        UnitOrigin(unit.origin),
        unit.refs,
        unit.added_details,
        unit.removed,
    )


def _citable(conn: sqlite3.Connection, version_id: str) -> frozenset[tuple[int, int]]:
    """The fact versions a line of the version may cite with a stored citation: the ones its
    request held and the ones the user saved from a document."""
    return frozenset(
        (row.number, row.version)
        for row in (
            *documents_repo.sent_facts(conn, version_id),
            *documents_repo.facts_saved_from_documents(conn),
        )
    )


def _flags_of(flags: Sequence[tuple[str, str]]) -> tuple[documents_repo.StoredFlag, ...]:
    return tuple(documents_repo.StoredFlag(kind, claim) for kind, claim in flags)


@dataclass(frozen=True)
class DocumentRuntime:
    """The provider and runner a task run uses, and the meter its usage is recorded with."""

    provider_name: str
    runner: AITaskRunner
    meter: UsageMeter


class DocumentBase:
    """What a CV and a cover letter share: the stored document, its versions and lines, the
    checks, and the edit, remove, confirm and approve actions. A subclass names its document
    ``KIND``, what its units are called (``NOUN``), the longest unit text, and its task type."""

    KIND: ClassVar[str] = "cv"
    NOUN: ClassVar[str] = "line"
    MAX_TEXT: ClassVar[int] = MAX_UNIT_TEXT
    TASK: ClassVar[str] = TAILOR_CV

    def __init__(
        self,
        *,
        factory: AIProviderFactory,
        clock: Clock,
        ids: IdFactory,
        workspace: Callable[[], SummaryWorkspace | None],
        settings: Callable[[], AIProviderSettings | None],
        tasks: Callable[[], TaskService | None],
        usage: Callable[[], UsageService | None],
        facts: Callable[[], FactService | None],
    ) -> None:
        self._factory, self._clock, self._ids = factory, clock, ids
        self._workspace, self._settings, self._tasks, self._usage = (
            workspace,
            settings,
            tasks,
            usage,
        )
        self._facts = facts

    def _open(self) -> SummaryWorkspace:
        opened = self._workspace()
        if opened is None:
            raise ApplicationNotFound()
        return opened

    def _now(self) -> str:
        return self._clock.now().isoformat()

    def _text(self, text: str) -> str:
        """The text of a unit the user wrote, or ``UnitInvalid`` saying what to change."""
        return _checked_text(text, self.NOUN, self.MAX_TEXT)

    # --- reading what a start needs ---------------------------------------------------------

    def _source(self, aid: str, *, full: bool = True) -> DocumentSource:
        """The job, the facts, the country and its template; ``full`` also reads what only a run
        needs (the known skills and the current version's lines)."""
        if not is_safe_id(aid):
            raise ApplicationNotFound()
        opened = self._open()
        preferred = SettingsService(opened.db, self._clock).load(JOB_PREFERENCES).value
        with opened.db.read() as conn:
            job_id = tailoring_repo.application_job_id(conn, aid)
            if job_id is None:
                raise ApplicationNotFound()
            workspace = tailoring_repo.workspace(conn, aid)
            country = (workspace.cv_country if workspace else None) or (
                preferred.cv_country if preferred else ""
            )
            document = documents_repo.document_for_application(conn, aid, self.KIND)
            return DocumentSource(
                job_id,
                job_fit_repo.inputs(conn, job_id),
                workspace,
                country,
                tailoring_repo.get_template(conn, country) if country else None,
                tuple(skill.name for skill in skills_repo.list_skills(conn)) if full else (),
                tuple(roles_repo.list_roles(conn)),
                document,
                documents_repo.units(conn, document.current_version_id)
                if full and document
                else (),
            )

    @staticmethod
    def _chosen_angle(source: DocumentSource) -> ChosenAngle | None:
        workspace = source.workspace
        if workspace and workspace.angle_id:
            return ChosenAngle(workspace.angle_name or "", workspace.angle_description or "")
        return None

    @staticmethod
    def _require_inputs(source: DocumentSource) -> None:
        """``GenerateNotReady`` unless the job has a current summary, its requirements and a
        usable confirmed fact."""
        if source.inputs.summary_id is None:
            raise GenerateNotReady("summary", NO_SUMMARY)
        if not source.inputs.requirements:
            raise GenerateNotReady("requirements", NO_REQUIREMENTS)
        if not facts_for_ai(source.inputs.facts):
            raise GenerateNotReady("facts", NO_FACTS)

    def _plan(self, tier: Tier | None) -> SummaryPlan:
        """What a start would use: the provider's service and the tier (``ProviderNotConfigured``
        without a usable provider)."""
        settings = self._settings()
        service = SERVICE_BY_PROVIDER_KIND.get(settings.kind or "") if settings else None
        if settings is None or service is None:
            raise ProviderNotConfigured()
        return SummaryPlan(service, tier or task_tier(self.TASK, settings))

    async def _runtime(self) -> DocumentRuntime:
        """The provider, runner and meter of a run (``ProviderNotConfigured`` without them)."""
        opened, usage = self._open(), self._usage()
        settings = await asyncio.to_thread(self._settings)
        service = SERVICE_BY_PROVIDER_KIND.get(settings.kind or "") if settings else None
        if settings is None or usage is None or service is None:
            raise ProviderNotConfigured()
        try:
            provider = self._factory.ai_provider(settings, data_dir=opened.data_dir)
        except ValueError:
            raise ProviderNotConfigured() from None
        runner = self._factory.task_runner(provider, lambda: settings, data_dir=opened.data_dir)
        return DocumentRuntime(provider.name, runner, ServiceUsageMeter(usage, service))

    def _merged_lines(
        self,
        source: DocumentSource,
        previous: Sequence[DocumentUnit],
        result: TailoredCv,
        replace_all: bool,
    ) -> list[documents_repo.StoredUnit]:
        """The new version's lines: the regeneration merged with the user's edits (replacing
        everything keeps no earlier line, tombstones included), each with its flags, role and
        citations, under a fresh id."""
        merged = merge_regenerated(previous, result.units, replace_all)
        earlier = {unit.id: unit for unit in source.previous}
        by_label: dict[str, str] = {}
        for role in source.roles:
            by_label.setdefault(role_label(role), role.id)
        flags: dict[str, list[documents_repo.StoredFlag]] = {}
        for flag in result.flags:
            flags.setdefault(flag.unit_id, []).append(
                documents_repo.StoredFlag(flag.kind, flag.claim)
            )
        lines: list[documents_repo.StoredUnit] = []
        for position, unit in enumerate(merged):
            old = earlier.get(unit.unit_id)
            lines.append(
                documents_repo.StoredUnit(
                    id=self._ids.new(),
                    position=position,
                    section=unit.section,
                    role_id=old.role_id
                    if old
                    else (by_label.get(unit.role) if unit.role else None),
                    kind=unit.kind,
                    text=unit.text,
                    origin=unit.origin.value,
                    removed=unit.removed,
                    added_details=unit.added_details,
                    flags=old.flags if old else tuple(flags.get(unit.unit_id, ())),
                    refs=unit.fact_ids,
                )
            )
        return lines

    def _writer(
        self,
        application_id: str,
        version: documents_repo.NewVersion,
        lines: list[documents_repo.StoredUnit],
        fact_refs: Sequence[str],
    ) -> ResultWriter:
        expected = version.parent_version_id
        sent = ref_pairs(fact_refs)

        def write(conn: sqlite3.Connection) -> str:
            now = self._now()
            document = documents_repo.document_for_application(conn, application_id, self.KIND)
            if (document.current_version_id if document else None) != expected:
                raise VersionConflict()  # the document changed while the model was answering
            number = documents_repo.next_number(conn, document.id) if document else 1
            new = replace(version, number=number, now=now)
            if document is None:
                documents_repo.insert_document(
                    conn,
                    id=version.document_id,
                    application_id=application_id,
                    first_version_id=version.id,
                    now=now,
                    kind=self.KIND,
                )
            documents_repo.insert_version(conn, new)
            documents_repo.insert_sent_facts(conn, version.id, sent)
            citable = _citable(conn, version.id)
            for position, line in enumerate(lines):
                documents_repo.insert_unit(
                    conn, version.id, position, line, now=now, citable=citable
                )
            if document is not None:
                documents_repo.set_current_version(conn, document, version.id, now=now)
            return f"document_version:{version.id}"

        return write

    # --- editing: every change is a new draft version ---------------------------------------

    def _begin(self, conn: sqlite3.Connection, aid: str, version_id: str) -> DocumentDraft:
        """The document's current version, for a change made from ``version_id``
        (``VersionConflict`` when that is not the current one)."""
        if not is_safe_id(aid) or tailoring_repo.application_job_id(conn, aid) is None:
            raise ApplicationNotFound()
        document = documents_repo.document_for_application(conn, aid, self.KIND)
        if document is None or document.current_version_id != version_id:
            raise VersionConflict()
        parent = documents_repo.get_version(conn, document.id, version_id)
        if parent is None:
            raise VersionConflict()
        return DocumentDraft(
            document,
            parent,
            list(documents_repo.units(conn, version_id)),
            tuple(roles_repo.list_roles(conn)),
            template_of(parent.template_snapshot),
            self._name_sources(conn, aid),
        )

    def _name_sources(self, conn: sqlite3.Connection, aid: str) -> tuple[str, ...]:
        """Texts that may support a capitalised name in this kind of document, besides the cited
        facts. None for a CV; a letter's are the job's title and company."""
        return ()

    def _commit(
        self,
        conn: sqlite3.Connection,
        draft: DocumentDraft,
        lines: Sequence[documents_repo.StoredUnit],
    ) -> tuple[str, list[str]]:
        """Write ``lines`` as the next draft version (the parent's snapshot, angle, note-free and
        sent facts copied) and make it current. Returns its id and the new id of each line."""
        now = self._now()
        parent = draft.parent
        version_id = self._ids.new()
        documents_repo.insert_version(
            conn,
            documents_repo.NewVersion(
                id=version_id,
                document_id=draft.document.id,
                number=documents_repo.next_number(conn, draft.document.id),
                parent_version_id=parent.id,
                origin="user",
                template_snapshot=parent.template_snapshot,
                angle_text=parent.angle_text,
                note=None,
                provider=None,
                model=None,
                prompt_id=None,
                prompt_version=None,
                task_id=None,
                now=now,
                tone=parent.tone,
                length=parent.length,
            ),
        )
        documents_repo.copy_sent_facts(conn, parent.id, version_id)
        citable = _citable(conn, version_id)
        ids: list[str] = []
        for position, line in enumerate(lines):
            fresh = replace(line, id=self._ids.new())
            ids.append(fresh.id)
            documents_repo.insert_unit(conn, version_id, position, fresh, now=now, citable=citable)
        documents_repo.set_current_version(conn, draft.document, version_id, now=now)
        return version_id, ids

    @staticmethod
    def _index(draft: DocumentDraft, unit_id: str) -> int:
        for index, line in enumerate(draft.lines):
            if line.id == unit_id and not line.removed:
                return index
        raise UnitNotFound()

    def _comparison_facts(
        self, conn: sqlite3.Connection, draft: DocumentDraft
    ) -> tuple[AIFact, ...]:
        """The facts an edited line is checked against: the version's sent set, as worded then,
        and the facts the user saved from a document (they are theirs to cite)."""
        labels = {role.id: role_label(role) for role in draft.roles}
        found = {
            (row.number, row.version): row
            for row in (
                *documents_repo.sent_facts(conn, draft.parent.id),
                *documents_repo.facts_saved_from_documents(conn),
            )
        }
        return tuple(
            worded_then(
                row.number,
                row.version,
                FactContent(row.text, row.situation, row.action, row.result),
                labels.get(row.role_id or ""),
                facts_repo.skill_names(conn, row.fact_id),
                generalize=row.privacy == "generalize",
            )
            for row in found.values()
        )

    def _rechecked(
        self, conn: sqlite3.Connection, draft: DocumentDraft, line: documents_repo.StoredUnit
    ) -> tuple[documents_repo.StoredUnit, FactOffer | None]:
        """The line with its flags recomputed against the facts its citations name, and the
        offer to save it as a fact when it claims something those facts do not."""
        labels = {role.id: role_label(role) for role in draft.roles}
        sent = self._comparison_facts(conn, draft)
        skills = tuple(skill.name for skill in skills_repo.list_skills(conn))
        unit = GeneratedUnit(
            line.id,
            "structure" if line.kind == "structure" else "content",
            line.section,
            labels.get(line.role_id or ""),
            line.text,
            line.refs,
            line.added_details,
        )
        headings = draft.template.headings if draft.template else ()
        flags = check_units(
            [unit],
            sent,
            template_headings=headings,
            known_skills=skills,
            name_sources=draft.names,
        )
        checked = replace(line, flags=_flags_of([(flag.kind, flag.claim) for flag in flags]))
        claims: tuple[str, ...] = ()
        if unit.kind == "content" and unit.fact_ids:
            by_ref = {str(fact.ref): fact for fact in sent}
            cited = [by_ref[ref] for ref in dict.fromkeys(unit.fact_ids) if ref in by_ref]
            claims = tuple(
                claim.text
                for claim in new_claims(
                    unit.text,
                    [fact_text(fact) for fact in cited if not fact.generalize],
                    known_skills=skills,
                    added_details=unit.added_details,
                    name_hidden_sources=[fact_text(fact) for fact in cited if fact.generalize],
                    name_only_sources=draft.names,
                )
            )
        return checked, FactOffer(SAVE_AS_FACT, claims, line.id) if claims else None

    @staticmethod
    def _outcome(
        version_id: str, ids: Sequence[str], index: int, offer: FactOffer | None
    ) -> EditOutcome:
        unit_id = ids[index]
        return EditOutcome(
            version_id, unit_id, replace(offer, unit_id=unit_id) if offer is not None else None
        )

    def edit_unit(
        self,
        application_id: str,
        version_id: str,
        unit_id: str,
        text: str,
        *,
        fact_ids: Sequence[str] | None = None,
        added_details: Sequence[str] | None = None,
    ) -> EditOutcome:
        """Reword a line. It becomes the user's own, keeps its section and role, keeps its
        citations unless ``fact_ids`` replaces them and its added details unless
        ``added_details`` replaces them (``()`` clears), and is re-checked against the facts it
        cites and the sent set. ``UnitInvalid`` (also for references or details that are not
        well formed), ``UnitNotFound``, ``VersionConflict``."""
        text = self._text(text)
        wanted_ids = None if fact_ids is None else checked_ids(fact_ids)
        wanted_details = None if added_details is None else checked_details(added_details)
        with self._open().db.transaction() as conn:
            draft = self._begin(conn, application_id, version_id)
            index = self._index(draft, unit_id)
            line = draft.lines[index]
            edited = replace(
                line,
                text=text,
                origin="user",
                refs=line.refs if wanted_ids is None else wanted_ids,
                added_details=line.added_details if wanted_details is None else wanted_details,
                flags=(),
            )
            edited, offer = self._rechecked(conn, draft, edited)
            draft.lines[index] = edited
            new_version, ids = self._commit(conn, draft, draft.lines)
        return self._outcome(new_version, ids, index, offer)

    def remove_unit(self, application_id: str, version_id: str, unit_id: str) -> str:
        """Remove a line: it stays as a tombstone (hidden, ignored by approval, not brought back
        by a regeneration). Returns the new version's id."""
        with self._open().db.transaction() as conn:
            draft = self._begin(conn, application_id, version_id)
            index = self._index(draft, unit_id)
            draft.lines[index] = replace(draft.lines[index], removed=True)
            new_version, _ = self._commit(conn, draft, draft.lines)
        return new_version

    def resolve_flag_confirm(
        self, application_id: str, version_id: str, unit_id: str
    ) -> EditOutcome:
        """ "This is true: save it as a fact". In one transaction: a confirmed fact (source
        ``document``, the line's text and role) and the new version in which the line cites only
        that fact and has no flags. If the version cannot be written, no fact exists afterwards."""
        facts = self._facts()
        if facts is None:
            raise ApplicationNotFound()
        with self._open().db.transaction() as conn:
            draft = self._begin(conn, application_id, version_id)
            index = self._index(draft, unit_id)
            line = draft.lines[index]
            if line.kind == "structure":
                raise UnitInvalid("A heading can't be saved as a fact.")
            made = facts.create_in(
                conn, FactInput(text=line.text, role_id=line.role_id), source=SOURCE_DOCUMENT
            )
            draft.lines[index] = replace(
                line,
                origin="user",
                refs=(fact_ref_text(made.number, made.content_version),),
                added_details=(),
                flags=(),
            )
            new_version, ids = self._commit(conn, draft, draft.lines)
        return self._outcome(new_version, ids, index, None)

    # --- approving --------------------------------------------------------------------------

    def approve(self, application_id: str, version_id: str) -> None:
        """Approve the current draft when nothing needs the user's check, looking at the facts as
        they are now. ``VersionConflict`` for a stale or already approved version,
        ``NothingToApprove`` without a visible line, ``ApprovalBlocked`` while lines need a check
        (nothing changes)."""
        with self._open().db.transaction() as conn:
            draft = self._begin(conn, application_id, version_id)
            if draft.parent.status != DocumentStatus.DRAFT.value:
                raise VersionConflict()
            if all(line.removed for line in draft.lines):
                raise NothingToApprove()
            job_id = tailoring_repo.application_job_id(conn, application_id)
            facts = job_fit_repo.inputs(conn, str(job_id)).facts
            labels = {role.id: role_label(role) for role in draft.roles}
            units = [document_unit_of(line, labels) for line in draft.lines]
            blockers = blockers_of(draft.lines, units, facts)
            if blockers:
                raise ApprovalBlocked(blockers, self.NOUN)
            documents_repo.set_version_status(
                conn, draft.parent, DocumentStatus.APPROVED.value, now=self._now()
            )

    # --- reading ----------------------------------------------------------------------------


class DocumentService(DocumentBase):
    @staticmethod
    def _approved(source: DocumentSource) -> TemplateSpec:
        """The application's approved template, or ``GenerateNotReady`` saying what is missing:
        the summary, its requirements, a usable fact, the country, an approved template."""
        DocumentBase._require_inputs(source)
        if not source.country:
            raise GenerateNotReady("country", NO_COUNTRY)
        stored = source.template
        if stored is None or stored.status != "approved":
            raise GenerateNotReady("template", NO_TEMPLATE)
        try:
            return tailoring_repo.template_spec(stored.spec_json)
        except ValueError:
            raise GenerateNotReady("template", NO_TEMPLATE) from None

    # --- starting ---------------------------------------------------------------------------

    def plan_start(self, application_id: str, *, tier: Tier | None = None) -> SummaryPlan:
        """What a start would use. ``GenerateNotReady`` when the CV can't be made yet,
        ``ProviderNotConfigured`` without a usable provider, ``ValueError`` for a tier that is
        not ``fast`` or ``thorough``."""
        if tier not in TIERS:
            raise ValueError(BAD_TIER)
        self._approved(self._source(application_id, full=False))
        return self._plan(tier)

    def start_generate(
        self,
        application_id: str,
        *,
        note: str | None = None,
        replace_all: bool = False,
        confirm_replace: bool = False,
        tier: Tier | None = None,
    ) -> TaskRef:
        """Queue the generation. A task already queued or running is returned when it was asked
        the same (note and replace mode), else ``GenerateInProgress`` (nothing queued). Replacing
        everything, the user's edits included, needs ``confirm_replace`` (``ReplaceNotConfirmed``,
        nothing queued). A tier other than fast or thorough, or a note with control or bidi
        characters, is a ``ValueError`` (nothing queued). The budget gate is the caller's, before
        this."""
        if replace_all and not confirm_replace:
            raise ReplaceNotConfirmed()
        self.plan_start(application_id, tier=tier)
        cleaned = clean_note(note or "")
        if len(cleaned) > MAX_NOTE_CHARS:
            raise ValueError(NOTE_TOO_LONG)
        tasks = self._tasks()
        if tasks is None:
            raise ApplicationNotFound()
        ref = tasks.enqueue(
            TAILOR_CV,
            application_id,
            input_ref=request_text(application_id, tier, cleaned, replace_all),
        )
        if not ref.created and not self._same_request(tasks, ref.id, cleaned, replace_all):
            raise GenerateInProgress()
        return ref

    @staticmethod
    def _same_request(tasks: TaskService, task_id: str, note: str, replace_all: bool) -> bool:
        """Whether the active task was asked for this note and replace mode."""
        row = tasks.get(task_id)
        try:
            earlier = request_from(row.input_ref if row else None)
        except ApplicationNotFound:
            return False
        return (earlier.note, earlier.replace_all) == (note, replace_all)

    # --- running (inside a task) ------------------------------------------------------------

    async def run_tailor_cv(self, ctx: SummaryContext) -> ResultWriter:
        request = request_from(ctx.input_ref)
        await ctx.heartbeat(0)
        source = await asyncio.to_thread(self._source, request.application_id)
        try:
            template = self._approved(source)
        except GenerateNotReady:
            raise FitNotAvailable() from None
        runtime = await self._runtime()
        await ctx.heartbeat(10)
        labels = {role.id: role_label(role) for role in source.roles}
        previous = [document_unit_of(unit, labels) for unit in source.previous]
        kept = (
            [] if request.replace_all else [u for u in previous if u.origin == "user" or u.removed]
        )
        angle = self._chosen_angle(source)
        output = await run_tailor_cv(
            runtime.runner,
            TailoringJob(
                source.job_id,
                str(source.inputs.job["snapshot_text"]),
                source.inputs.requirements,
            ),
            source.inputs.facts,
            angle,
            template,
            request.note,
            kept,
            ctx.cancel_flag,
            meter=runtime.meter,
            tier=request.tier,
            task_id=ctx.task_id,
            known_skills=source.skills,
            new_id=self._ids.new,
        )
        ctx.check_cancelled()
        lines = self._merged_lines(source, previous, output.result, request.replace_all)
        version = documents_repo.NewVersion(
            id=self._ids.new(),
            document_id=source.document.id if source.document else self._ids.new(),
            number=0,
            parent_version_id=source.document.current_version_id if source.document else None,
            origin="ai",
            template_snapshot=json.dumps(template.model_dump(mode="json"), ensure_ascii=False),
            angle_text=angle_text_of(angle),
            note=request.note or None,
            provider=runtime.provider_name,
            model=output.model,
            prompt_id=output.prompt_id,
            prompt_version=output.prompt_version,
            task_id=ctx.task_id,
            now="",
        )
        return self._writer(request.application_id, version, lines, output.fact_refs)

    def add_unit(
        self,
        application_id: str,
        version_id: str,
        section: str,
        text: str,
        *,
        role: str | None = None,
        fact_ids: Sequence[str] = (),
        added_details: Sequence[str] = (),
    ) -> EditOutcome:
        """Add a line of the user's own right after the last line of its section (else at the
        end). Without citations it is ``unsupported`` until the user confirms it as a fact. A
        version holds at most ``MAX_VISIBLE_UNITS`` lines that are not removed."""
        text = self._text(text)
        ids, details = checked_ids(fact_ids), checked_details(added_details)
        with self._open().db.transaction() as conn:
            draft = self._begin(conn, application_id, version_id)
            if sum(1 for other in draft.lines if not other.removed) >= MAX_VISIBLE_UNITS:
                raise UnitInvalid(TOO_MANY_UNITS)
            if draft.template is None or section not in {
                spec.id.value for spec in draft.template.sections
            }:
                raise UnitInvalid("Choose one of the CV's sections.")
            role_id = self._role_id(draft, role)
            line = documents_repo.StoredUnit(
                id=self._ids.new(),
                position=0,
                section=section,
                role_id=role_id,
                kind="content",
                text=text,
                origin="user",
                removed=False,
                added_details=details,
                flags=(),
                refs=ids,
            )
            line, offer = self._rechecked(conn, draft, line)
            index = 1 + max(
                (i for i, other in enumerate(draft.lines) if other.section == section),
                default=len(draft.lines) - 1,
            )
            draft.lines.insert(index, line)
            new_version, new_ids = self._commit(conn, draft, draft.lines)
        return self._outcome(new_version, new_ids, index, offer)

    @staticmethod
    def _role_id(draft: DocumentDraft, label: str | None) -> str | None:
        if label is None:
            return None
        for role in draft.roles:
            if role_label(role) == label:
                return role.id
        raise UnitInvalid("Choose one of your roles, or none.")

    def view(self, application_id: str, version_id: str | None = None) -> DocumentView:
        """The CV as the screen shows it (the current version, or ``version_id`` read-only).
        Reads only: nothing is written, started or created."""
        if not is_safe_id(application_id):
            raise ApplicationNotFound()
        opened = self._open()
        details = SettingsService(opened.db, self._clock).load(PERSONAL_DETAILS).value
        with opened.db.read() as conn:
            job_id = tailoring_repo.application_job_id(conn, application_id)
            if job_id is None:
                raise ApplicationNotFound()
            task = task_state_of(tailoring_repo.latest_task(conn, f"{TAILOR_CV}:{application_id}"))
            document = documents_repo.document_for_application(conn, application_id)
            if document is None:
                return _empty_view(application_id, task, contact_block(details, None))
            versions = documents_repo.list_versions(conn, document.id)
            chosen = documents_repo.get_version(
                conn, document.id, version_id or document.current_version_id
            )
            if chosen is None:
                raise ApplicationNotFound()
            stored = documents_repo.units(conn, chosen.id)
            roles = {role.id: role for role in roles_repo.list_roles(conn)}
            facts = job_fit_repo.inputs(conn, job_id).facts
            wording = {ref: wording_of(conn, ref) for unit in stored for ref in unit.refs}
        return self._built(
            application_id,
            document.id,
            chosen,
            versions,
            stored,
            roles,
            facts,
            wording,
            task,
            contact=details,
        )

    def _built(
        self,
        application_id: str,
        document_id: str,
        chosen: documents_repo.StoredVersion,
        versions: Sequence[documents_repo.StoredVersion],
        stored: Sequence[documents_repo.StoredUnit],
        roles: dict[str, roles_repo.StoredRole],
        facts: Sequence[FactRecord],
        wording: dict[str, FactWording | None],
        task: TaskState,
        *,
        contact: PersonalDetails,
    ) -> DocumentView:
        template = template_of(chosen.template_snapshot)
        labels = {role_id: role_label(role) for role_id, role in roles.items()}
        visible = [unit for unit in stored if not unit.removed]
        views = tuple(
            UnitView(
                unit.id,
                unit.position,
                unit.section,
                labels.get(unit.role_id or ""),
                unit.role_id,
                unit.kind,
                unit.text,
                unit.origin,
                tuple(FlagView(flag.kind, flag.claim) for flag in unit.flags),
                unit.refs,
                tuple(w for ref in unit.refs if (w := wording.get(ref)) is not None),
                unit.added_details,
            )
            for unit in visible
        )
        documents = [document_unit_of(unit, labels) for unit in stored]
        periods = {
            labels[role_id]: RolePeriod(role.start_date, role.end_date)
            for role_id, role in roles.items()
        }
        master = build_master_cv(facts, periods)
        return DocumentView(
            application_id=application_id,
            document_id=document_id,
            version=version_info(chosen),
            versions=tuple(version_info(version) for version in versions),
            units=views,
            sections=sections_of(template, views, roles, master) if template else (),
            contact=contact_block(contact, template),
            blockers=blockers_of(stored, documents, facts),
            diff=cv_diff(master, documents),
            template=template,
            practice_notice=PRACTICE_NOTICE if provider_of(chosen, versions) == "fake" else None,
            task=task,
        )


def provider_of(
    chosen: documents_repo.StoredVersion, versions: Sequence[documents_repo.StoredVersion]
) -> str | None:
    """The provider that wrote the version's lines: its own, or for the user's edits (which
    carry none) that of the nearest earlier version a provider wrote."""
    by_id = {version.id: version for version in versions}
    version: documents_repo.StoredVersion | None = chosen
    for _ in range(len(by_id) + 1):
        if version is None or version.provider is not None:
            break
        version = by_id.get(version.parent_version_id or "")
    return version.provider if version else None


def angle_text_of(angle: ChosenAngle | None) -> str | None:
    if angle is None:
        return None
    return f"{angle.name}: {angle.description}" if angle.description else angle.name


def version_info(version: documents_repo.StoredVersion) -> VersionInfo:
    return VersionInfo(
        version.id,
        version.number,
        version.status,
        version.parent_version_id,
        version.origin,
        version.provider,
        version.note,
        version.created_at,
    )


def wording_of(conn: sqlite3.Connection, ref: str) -> FactWording | None:
    try:
        parsed = FactRef.parse(ref)
    except ValueError:
        return None
    stored = tailoring_repo.fact_wording(conn, parsed.number, parsed.version)
    return FactWording(stored.fact_id, stored.text) if stored and stored.text else None


def _empty_view(application_id: str, task: TaskState, contact: ContactBlock) -> DocumentView:
    return DocumentView(
        application_id=application_id,
        document_id=None,
        version=None,
        versions=(),
        units=(),
        sections=(),
        contact=contact,
        blockers=(),
        diff=(),
        template=None,
        practice_notice=None,
        task=task,
    )


__all__ = [
    "SAVE_AS_FACT",
    "TAILOR_CV_LABEL",
    "ApprovalBlocked",
    "DocumentBase",
    "DocumentService",
    "DocumentView",
    "EditOutcome",
    "FactOffer",
    "GenerateInProgress",
    "GenerateNotReady",
    "NothingToApprove",
    "ReplaceNotConfirmed",
    "UnitInvalid",
    "UnitNotFound",
]
