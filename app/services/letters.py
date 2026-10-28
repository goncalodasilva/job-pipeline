"""The cover letter (M6.9; AC-M6.1 to AC-M6.4; spec 3.1, 3.2, 3.6; ADR-106 items 5, 8, 9, 11, 12).

A letter is a second document of the application, stored like the CV (the same tables, versions,
lines, citations and sent facts, under kind ``cover_letter``) and edited, checked, merged and
approved by the same code (``DocumentBase``). What differs:

- the model writes only the body paragraphs (section ``body``, no role, no template);
- the greeting, the opening line that names the job and company, the closing and the name are
  built by code at view time, from the job and the saved details. They are never stored in a
  paragraph and never sent to a request;
- the job's title and company may support capitalised names in a paragraph, and nothing else;
- tone and length are the person's choices, checked against fixed sets, stored on the version and
  sent as words;
- a letter needs neither a CV country nor a template nor a CV, and it is optional.
"""

import asyncio
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar, Final

from app.domain.cover_letter import LENGTHS, LETTER_SECTION, MAX_PARAGRAPH_TEXT, TONES
from app.domain.errors import ApplicationNotFound, FitNotAvailable
from app.domain.ids import is_safe_id
from app.domain.settings import PERSONAL_DETAILS, Tier
from app.domain.tasks import TaskRef
from app.modules.storage import (
    documents_repo,
    job_fit_repo,
    roles_repo,
    tailoring_repo,
)
from app.services.document_views import (
    Blocker,
    FlagView,
    UnitView,
    VersionInfo,
    blockers_of,
    role_label,
)
from app.services.documents import (
    BAD_TIER,
    NOTE_TOO_LONG,
    TIERS,
    DocumentBase,
    DocumentDraft,
    EditOutcome,
    GenerateInProgress,
    GenerateNotReady,
    ReplaceNotConfirmed,
    UnitInvalid,
    angle_text_of,
    checked_details,
    checked_ids,
    clean_note,
    document_unit_of,
    provider_of,
    request_from,
    request_text,
    task_state_of,
    version_info,
    wording_of,
)
from app.services.job_summary import ResultWriter, SummaryContext, SummaryPlan
from app.services.settings import SettingsService
from app.services.tailoring import TaskState
from app.services.tailoring_ai import (
    DRAFT_COVER_LETTER,
    MAX_NOTE_CHARS,
    TailoringJob,
    run_draft_cover_letter,
)
from app.services.tasks import TaskService

DRAFT_COVER_LETTER_LABEL: Final = "Draft cover letter"
BAD_TONE: Final = "Choose a warm, formal or direct tone."
BAD_LENGTH: Final = "Choose a short, medium or long letter."
LETTER_IN_PROGRESS: Final = "A letter is already being drafted. Wait for it to finish."
LETTER_PRACTICE_NOTICE: Final = (
    "Practice data (fake provider). This letter is a fictional example, not a real cover letter."
)
DEFAULT_TONE: Final = "warm"
DEFAULT_LENGTH: Final = "medium"
MAX_VISIBLE_PARAGRAPHS: Final = 30
TOO_MANY_PARAGRAPHS: Final = (
    f"A letter can have at most {MAX_VISIBLE_PARAGRAPHS} paragraphs. "
    "Remove one before adding another."
)
GREETING: Final = "Dear hiring team,"
CLOSING: Final = "Kind regards,"


@dataclass(frozen=True)
class LetterFrame:
    """The parts of the letter that code writes: never stored in a paragraph, never sent to the
    model, and not editable on the page. ``name`` is the saved name ("" when none)."""

    greeting: str
    opening: str
    closing: str
    name: str


@dataclass(frozen=True)
class LetterView:
    """The letter as the step shows it."""

    application_id: str
    document_id: str | None
    version: VersionInfo | None
    versions: tuple[VersionInfo, ...]
    units: tuple[UnitView, ...]
    blockers: tuple[Blocker, ...]
    practice_notice: str | None
    task: TaskState
    tone: str | None
    length: str | None
    job_title: str
    company: str
    frame: LetterFrame


def opening_line(title: str, company: str) -> str:
    """One sentence that names the job's title and company, built from the stored job."""
    role = f"the {title} role" if title else "the role"
    return f"I am writing to apply for {role}{f' at {company}' if company else ''}."


class LetterService(DocumentBase):
    KIND: ClassVar[str] = "cover_letter"
    NOUN: ClassVar[str] = "paragraph"
    MAX_TEXT: ClassVar[int] = MAX_PARAGRAPH_TEXT
    TASK: ClassVar[str] = DRAFT_COVER_LETTER

    def _name_sources(self, conn: sqlite3.Connection, aid: str) -> tuple[str, ...]:
        header = tailoring_repo.application_header(conn, aid)
        return tuple(part for part in header[1:] if part) if header else ()

    # --- starting ---------------------------------------------------------------------------

    def plan_start(self, application_id: str, *, tier: Tier | None = None) -> SummaryPlan:
        """What a start would use. ``GenerateNotReady`` (reason ``summary``, ``requirements`` or
        ``facts``) when the letter can't be drafted yet, ``ProviderNotConfigured`` without a
        usable provider, ``ValueError`` for a tier that is not ``fast`` or ``thorough``."""
        if tier not in TIERS:
            raise ValueError(BAD_TIER)
        self._require_inputs(self._source(application_id, full=False))
        return self._plan(tier)

    def _choices(
        self, application_id: str, tone: str | None, length: str | None
    ) -> tuple[str, str]:
        """The tone and length to use: the ones asked for, else those of the current version, else
        warm and medium."""
        with self._open().db.read() as conn:
            document = documents_repo.document_for_application(conn, application_id, self.KIND)
            current = (
                documents_repo.get_version(conn, document.id, document.current_version_id)
                if document
                else None
            )
        return (
            tone or (current.tone if current and current.tone else DEFAULT_TONE),
            length or (current.length if current and current.length else DEFAULT_LENGTH),
        )

    def start_generate(
        self,
        application_id: str,
        *,
        tone: str | None = None,
        length: str | None = None,
        note: str | None = None,
        replace_all: bool = False,
        confirm_replace: bool = False,
        tier: Tier | None = None,
    ) -> TaskRef:
        """Queue the drafting. A task already queued or running is returned when it was asked the
        same (tone, length, note and replace mode), else ``GenerateInProgress`` (nothing queued).
        Replacing everything, the user's edits included, needs ``confirm_replace``
        (``ReplaceNotConfirmed``, nothing queued). A tone or length outside the sets, a tier other
        than fast or thorough, or a note with control or bidi characters is a ``ValueError``
        (nothing queued). The budget gate is the caller's, before this."""
        if replace_all and not confirm_replace:
            raise ReplaceNotConfirmed()
        if tone is not None and tone not in TONES:
            raise ValueError(BAD_TONE)
        if length is not None and length not in LENGTHS:
            raise ValueError(BAD_LENGTH)
        self.plan_start(application_id, tier=tier)
        cleaned = clean_note(note or "")
        if len(cleaned) > MAX_NOTE_CHARS:
            raise ValueError(NOTE_TOO_LONG)
        chosen_tone, chosen_length = self._choices(application_id, tone, length)
        tasks = self._tasks()
        if tasks is None:
            raise ApplicationNotFound()
        ref = tasks.enqueue(
            DRAFT_COVER_LETTER,
            application_id,
            input_ref=request_text(
                application_id,
                tier,
                cleaned,
                replace_all,
                chosen_tone,
                chosen_length,
            ),
        )
        asked = (cleaned, replace_all, chosen_tone, chosen_length)
        if not ref.created and not self._same_request(tasks, ref.id, asked):
            raise GenerateInProgress(LETTER_IN_PROGRESS)
        return ref

    @staticmethod
    def _same_request(tasks: TaskService, task_id: str, asked: tuple[str, bool, str, str]) -> bool:
        """Whether the active task was asked for this note, replace mode, tone and length."""
        row = tasks.get(task_id)
        try:
            earlier = request_from(row.input_ref if row else None)
        except ApplicationNotFound:
            return False
        return (earlier.note, earlier.replace_all, earlier.tone, earlier.length) == asked

    # --- running (inside a task) ------------------------------------------------------------

    def _header(self, application_id: str) -> tuple[str, str]:
        with self._open().db.read() as conn:
            header = tailoring_repo.application_header(conn, application_id)
        if header is None:
            raise ApplicationNotFound()
        return header[1], header[2]

    async def run_draft_cover_letter(self, ctx: SummaryContext) -> ResultWriter:
        request = request_from(ctx.input_ref)
        await ctx.heartbeat(0)
        source = await asyncio.to_thread(self._source, request.application_id)
        try:
            self._require_inputs(source)
        except GenerateNotReady:
            raise FitNotAvailable() from None
        title, company = await asyncio.to_thread(self._header, request.application_id)
        runtime = await self._runtime()
        await ctx.heartbeat(10)
        labels = {role.id: role_label(role) for role in source.roles}
        previous = [document_unit_of(unit, labels) for unit in source.previous]
        kept = (
            [] if request.replace_all else [u for u in previous if u.origin == "user" or u.removed]
        )
        angle = self._chosen_angle(source)
        # A stored value outside the sets (a damaged task row) falls back to the defaults.
        tone = request.tone if request.tone in TONES else DEFAULT_TONE
        length = request.length if request.length in LENGTHS else DEFAULT_LENGTH
        output = await run_draft_cover_letter(
            runtime.runner,
            TailoringJob(
                source.job_id,
                str(source.inputs.job["snapshot_text"]),
                source.inputs.requirements,
                title,
                company,
            ),
            source.inputs.facts,
            angle,
            tone,
            length,
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
            template_snapshot=None,
            angle_text=angle_text_of(angle),
            note=request.note or None,
            provider=runtime.provider_name,
            model=output.model,
            prompt_id=output.prompt_id,
            prompt_version=output.prompt_version,
            task_id=ctx.task_id,
            now="",
            tone=tone,
            length=length,
        )
        return self._writer(request.application_id, version, lines, output.fact_refs)

    # --- editing ----------------------------------------------------------------------------

    def add_unit(
        self,
        application_id: str,
        version_id: str,
        text: str,
        *,
        fact_ids: Sequence[str] = (),
        added_details: Sequence[str] = (),
    ) -> EditOutcome:
        """Add a paragraph of the user's own after the last one. Without citations it is
        ``unsupported`` until the user confirms it as a fact. A version holds at most
        ``MAX_VISIBLE_PARAGRAPHS`` paragraphs that are not removed."""
        text = self._text(text)
        ids, details = checked_ids(fact_ids), checked_details(added_details)
        with self._open().db.transaction() as conn:
            draft = self._begin(conn, application_id, version_id)
            if self._visible(draft) >= MAX_VISIBLE_PARAGRAPHS:
                raise UnitInvalid(TOO_MANY_PARAGRAPHS)
            line = documents_repo.StoredUnit(
                id=self._ids.new(),
                position=0,
                section=LETTER_SECTION,
                role_id=None,
                kind="content",
                text=text,
                origin="user",
                removed=False,
                added_details=details,
                flags=(),
                refs=ids,
            )
            line, offer = self._rechecked(conn, draft, line)
            draft.lines.append(line)
            new_version, new_ids = self._commit(conn, draft, draft.lines)
        return self._outcome(new_version, new_ids, len(draft.lines) - 1, offer)

    @staticmethod
    def _visible(draft: DocumentDraft) -> int:
        return sum(1 for other in draft.lines if not other.removed)

    # --- reading ----------------------------------------------------------------------------

    def view(self, application_id: str, version_id: str | None = None) -> LetterView:
        """The letter as the step shows it (the current version, or ``version_id`` read-only).
        Reads only: nothing is written, started or created."""
        if not is_safe_id(application_id):
            raise ApplicationNotFound()
        opened = self._open()
        details = SettingsService(opened.db, self._clock).load(PERSONAL_DETAILS).value
        with opened.db.read() as conn:
            header = tailoring_repo.application_header(conn, application_id)
            if header is None:
                raise ApplicationNotFound()
            job_id, title, company = header
            task = task_state_of(
                tailoring_repo.latest_task(conn, f"{DRAFT_COVER_LETTER}:{application_id}")
            )
            document = documents_repo.document_for_application(conn, application_id, self.KIND)
            frame = LetterFrame(GREETING, opening_line(title, company), CLOSING, details.name or "")
            if document is None:
                return LetterView(
                    application_id=application_id,
                    document_id=None,
                    version=None,
                    versions=(),
                    units=(),
                    blockers=(),
                    practice_notice=None,
                    task=task,
                    tone=None,
                    length=None,
                    job_title=title,
                    company=company,
                    frame=frame,
                )
            versions = documents_repo.list_versions(conn, document.id)
            chosen = documents_repo.get_version(
                conn, document.id, version_id or document.current_version_id
            )
            if chosen is None:
                raise ApplicationNotFound()
            stored = documents_repo.units(conn, chosen.id)
            labels = {role.id: role_label(role) for role in roles_repo.list_roles(conn)}
            facts = job_fit_repo.inputs(conn, job_id).facts
            wording = {ref: wording_of(conn, ref) for unit in stored for ref in unit.refs}
        units = tuple(
            UnitView(
                unit.id,
                unit.position,
                unit.section,
                None,
                None,
                unit.kind,
                unit.text,
                unit.origin,
                tuple(FlagView(flag.kind, flag.claim) for flag in unit.flags),
                unit.refs,
                tuple(w for ref in unit.refs if (w := wording.get(ref)) is not None),
                unit.added_details,
            )
            for unit in stored
            if not unit.removed
        )
        documents = [document_unit_of(unit, labels) for unit in stored]
        return LetterView(
            application_id=application_id,
            document_id=document.id,
            version=version_info(chosen),
            versions=tuple(version_info(version) for version in versions),
            units=units,
            blockers=blockers_of(stored, documents, facts),
            practice_notice=LETTER_PRACTICE_NOTICE
            if provider_of(chosen, versions) == "fake"
            else None,
            task=task,
            tone=chosen.tone,
            length=chosen.length,
            job_title=title,
            company=company,
            frame=frame,
        )


__all__ = [
    "BAD_LENGTH",
    "BAD_TONE",
    "DRAFT_COVER_LETTER_LABEL",
    "LENGTHS",
    "TONES",
    "LetterFrame",
    "LetterService",
    "LetterView",
    "opening_line",
]
