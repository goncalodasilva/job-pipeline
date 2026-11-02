"""Narrative cards and adapted answers (M8.2 parts B and C; AC-M8.4 honesty rule for generated
prep text; spec 3.1, 3.2, 3.6; ADR-113 item 8; ADR-124 items 5, 7 and 8).

A card is a short answer the person gives in an interview. Six are written by ``draft_narrative``
from the confirmed facts (tell me about yourself, why this role, why this company, why you are
leaving, strengths, weaknesses); the seventh, the salary card, is built by code from the saved
minimum and the pay the posting states, with no AI request. Cards are stored in the prep tables
(0008, with the facts sent in 0009) and work as the CV and the cover letter do: every change is a
NEW draft version, a line is re-checked against the facts SENT for that version plus the facts
the person saved from documents, a removed line stays as a tombstone, confirming a flagged line
creates a confirmed fact (source ``document``) in the same transaction as the new version, and
approval re-runs the approval checks with the facts as they are now.

Generation is a background task (``draft_narrative:<application id>/<kind>``). Its whole result
is written by the writer ``ctx.complete`` runs in the transaction that marks the task succeeded,
or not at all. Narrative text never reaches logs, task or usage rows, events or error text.
Reuse copies another application's approved card as a new draft, only for the four kinds that
are not about one role or company and only with the same chosen angle; approval checks it again.

An adapted answer (part C, ``adapt_answer:<application id>/<question id>``) is the same kind of
document with kind ``adapted_answer`` and a ``question_id``: the person's best earlier practice
answer to a bank question, from ANOTHER application, rewritten from the confirmed facts for this
job. It is stored, edited, checked and approved exactly as a card is. The old answer is data: it
is never a source of names, and what the model copies from it that no sent fact states is
flagged and blocks approval. Neither the old nor the adapted text reaches logs, task or usage
rows, events or error text.
"""

import asyncio
import json
import sqlite3
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

from app.domain.clock import Clock
from app.domain.company_brief import BriefSection
from app.domain.documents import DocumentStatus, DocumentUnit, UnitOrigin
from app.domain.errors import (
    ApplicationNotFound,
    FitNotAvailable,
    InvalidModelOutput,
    ProviderNotConfigured,
    VersionConflict,
)
from app.domain.fact_selection import AIFact, FactRecord, fact_text, facts_for_ai, worded_then
from app.domain.facts import FactContent, fact_ref_text
from app.domain.honesty import GeneratedUnit, check_units, new_claims
from app.domain.ids import IdFactory, is_safe_id
from app.domain.narrative import (
    CARD_KINDS,
    MAX_NARRATIVE_CHARS,
    MAX_NARRATIVE_UNITS,
    NARRATIVE_SECTION,
    REUSABLE_KINDS,
)
from app.domain.salary_card import build_salary_card
from app.domain.settings import (
    JOB_PREFERENCES,
    AIProviderSettings,
    Tier,
    has_control_characters,
)
from app.domain.tasks import TaskRef
from app.domain.usage import SERVICE_BY_PROVIDER_KIND
from app.modules.ai import AIProviderFactory
from app.modules.storage import (
    documents_repo,
    facts_repo,
    job_fit_repo,
    practice_repo,
    prep_documents_repo,
    prep_repo,
    roles_repo,
    skills_repo,
    tailoring_repo,
)
from app.services.adapt_ai import ADAPT_ANSWER, run_adapt_answer
from app.services.document_views import Blocker, FlagView, blockers_of, role_label
from app.services.documents import (
    BAD_TIER,
    SAVE_AS_FACT,
    TIERS,
    ApprovalBlocked,
    DocumentRuntime,
    EditOutcome,
    FactOffer,
    GenerateNotReady,
    NothingToApprove,
    UnitInvalid,
    UnitNotFound,
    angle_text_of,
    checked_details,
    checked_ids,
    ref_pairs,
    task_state_of,
    wording_of,
)
from app.services.facts import SOURCE_DOCUMENT, FactInput, FactService
from app.services.job_summary import ResultWriter, SummaryContext, SummaryPlan, SummaryWorkspace
from app.services.narrative_ai import DRAFT_NARRATIVE, NarrativeOutput, run_draft_narrative
from app.services.settings import SettingsService
from app.services.tailoring import NO_FACTS, FactWording, TaskState
from app.services.tailoring_ai import ChosenAngle, TailoringJob
from app.services.tasks import TaskService
from app.services.usage import UsageService
from app.services.usage_meter import ServiceUsageMeter

SALARY: Final = "salary"
ALL_KINDS: Final = (*CARD_KINDS, SALARY)
LABELS: Final = {
    "tell_me_about_yourself": "Tell me about yourself",
    "why_this_role": "Why this role",
    "why_this_company": "Why this company",
    "why_leaving": "Why you are leaving",
    "strengths": "Strengths",
    "weaknesses": "Weaknesses",
    SALARY: "Salary",
}
DRAFT_NARRATIVE_LABEL: Final = "Draft narrative card"
ADAPTED: Final = "adapted_answer"
ADAPTED_LABEL: Final = "Adapted answer"
ADAPT_ANSWER_LABEL: Final = "Adapt answer"
NO_EARLIER_ANSWER: Final = (
    "There is no earlier answer to adapt. Practice this question in another application first."
)
CARD_PRACTICE_NOTICE: Final = (
    "Practice data (fake provider). This card is a fictional example, not a real answer."
)
NOUN: Final = "paragraph"
BAD_KIND: Final = "Choose one of the written cards."
NO_SALARY: Final = (
    "Save a minimum salary in Settings, or add the pay the posting states, to build this card."
)
REUSE_REFUSED: Final = "That card can't be copied here."
REUSE_SAME_ANGLE: Final = "A card can only be copied between applications with the same angle."
ASKED_REFUSED: Final = "This card is about one role or company, so it can't be copied."
_ACTIVE: Final = ("queued", "running")
_RESULT_PREFIX: Final = "prep_document_version:"
_DROPPED_KEY: Final = ";dropped="
_LEFT_OUT_WORDS: Final = {
    "too_many": f"past the limit of {MAX_NARRATIVE_UNITS} paragraphs",
    "too_long": f"past the {MAX_NARRATIVE_CHARS:,} character limit",
    "invalid": "that could not be used",
}


def _result_ref(version_id: str, dropped: Mapping[str, int]) -> str:
    """What a finished draft task records: the version it wrote and how many paragraphs code left
    out, by reason (counts only: never any text)."""
    kept = [f"{reason}:{count}" for reason, count in dropped.items() if count > 0]
    return _RESULT_PREFIX + version_id + (_DROPPED_KEY + ",".join(kept) if kept else "")


def _left_out(result_ref: str | None, version_id: str) -> str:
    """The plain-words count of paragraphs left out of the version the task ``result_ref`` names
    (``""`` when none, or when the card has moved on to another version)."""
    start = _RESULT_PREFIX + version_id + _DROPPED_KEY
    if not result_ref or not result_ref.startswith(start):
        return ""
    parts: list[str] = []
    total = 0
    for item in result_ref[len(start) :].split(","):
        reason, _, number = item.partition(":")
        if reason in _LEFT_OUT_WORDS and number.isdigit() and int(number) > 0:
            total += int(number)
            parts.append(f"{int(number)} {_LEFT_OUT_WORDS[reason]}")
    if not total:
        return ""
    noun = "paragraph was" if total == 1 else "paragraphs were"
    return f"{total} {noun} left out: {', '.join(parts)}."


class ReuseRefused(ValueError):
    """The card can't be copied here (nothing written); ``message`` says why."""

    def __init__(self, message: str = REUSE_REFUSED) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class CardVersion:
    id: str
    number: int
    status: str
    origin: str
    provider: str | None
    reused_version_id: str | None


@dataclass(frozen=True)
class CardUnit:
    id: str
    text: str
    origin: str
    flags: tuple[FlagView, ...]
    fact_ids: tuple[str, ...]
    added_details: tuple[str, ...]
    cited: tuple[FactWording, ...]


@dataclass(frozen=True)
class CardReuse:
    version_id: str
    label: str


@dataclass(frozen=True)
class CardView:
    kind: str
    label: str
    state: str  # none | running | failed | empty | draft | approved | recheck
    version: CardVersion | None
    units: tuple[CardUnit, ...]
    blockers: tuple[Blocker, ...]
    empty: bool
    approved: bool
    needs_recheck: bool
    task: TaskState
    reusable: tuple[CardReuse, ...]
    practice: bool
    removed_all: bool = False  # the version has paragraphs, and the person removed every one
    left_out: str = ""  # "2 paragraphs were left out: ...", else ""


@dataclass(frozen=True)
class CardEdit(EditOutcome):
    """An ``EditOutcome`` that also says whether the version it was made from was approved."""

    was_approved: bool = False


@dataclass(frozen=True)
class CardsView:
    application_id: str
    cards: tuple[CardView, ...]
    has_facts: bool
    approved_kinds: tuple[str, ...]
    angle_id: str | None
    angle_name: str | None


@dataclass(frozen=True)
class AdaptedView:
    """The practice screen's part about an earlier answer: the best earlier answer (None when
    there is none), whether it can be adapted now, and the adapted answer as a card."""

    application_id: str
    question_id: str
    earlier: practice_repo.EarlierAnswer | None
    can_adapt: bool
    answer: CardView
    has_facts: bool = True  # a usable confirmed fact exists


@dataclass(frozen=True)
class CardSource:
    """What a run reads: the job, the fact rows, the known skills, the chosen angle (and its
    id), the company's notes and the document's current version."""

    job: TailoringJob
    records: tuple[FactRecord, ...]
    skills: tuple[str, ...]
    angle: ChosenAngle | None
    angle_id: str | None
    notes: str
    current_version_id: str | None
    brief: tuple[BriefSection, ...] = ()


@dataclass(frozen=True)
class CardDraft:
    """What an edit starts from: the current version's lines and everything its re-check needs."""

    document: prep_documents_repo.StoredDocument
    parent: prep_documents_repo.StoredVersion
    lines: list[documents_repo.StoredUnit]
    roles: tuple[roles_repo.StoredRole, ...]
    names: tuple[str, ...]


def _kind(kind: str, *, salary: bool = False) -> str:
    if kind in CARD_KINDS or (salary and kind == SALARY):
        return kind
    raise ValueError(BAD_KIND)


def _text(text: str) -> str:
    """The text of a paragraph the person wrote, or ``UnitInvalid`` saying what to change."""
    if not text.strip():
        raise UnitInvalid(f"Write the {NOUN} before saving.")
    if len(text) > MAX_NARRATIVE_CHARS:
        raise UnitInvalid(f"Keep the {NOUN} to {MAX_NARRATIVE_CHARS} characters or fewer.")
    if has_control_characters(text) or any(unicodedata.category(char) == "Cf" for char in text):
        raise UnitInvalid("Use plain text on one line, without special characters.")
    return text


def _document_unit_of(unit: documents_repo.StoredUnit) -> DocumentUnit:
    """A stored unit as the honesty helpers read it (a code-built line counts as the person's
    own: nothing checks it against facts)."""
    return DocumentUnit(
        unit.id,
        "content",
        unit.section,
        None,
        unit.text,
        UnitOrigin.AI if unit.origin == "ai" else UnitOrigin.USER,
        unit.refs,
        unit.added_details,
        unit.removed,
    )


def _flags_of(flags: Sequence[tuple[str, str]]) -> tuple[documents_repo.StoredFlag, ...]:
    return tuple(documents_repo.StoredFlag(kind, claim) for kind, claim in flags)


def _citable(conn: sqlite3.Connection, version_id: str) -> frozenset[tuple[int, int]]:
    """The fact versions a unit of the version may cite with a stored citation: the ones its
    request held and the ones the person saved from a document."""
    return frozenset(
        (row.number, row.version)
        for row in (
            *prep_documents_repo.sent_facts(conn, version_id),
            *documents_repo.facts_saved_from_documents(conn),
        )
    )


def _provider_of(
    chosen: prep_documents_repo.StoredVersion,
    versions: Sequence[prep_documents_repo.StoredVersion],
) -> str | None:
    """The provider that wrote the version: its own, or for the person's edits (which carry
    none) that of the nearest earlier version a provider wrote."""
    by_id = {version.id: version for version in versions}
    version: prep_documents_repo.StoredVersion | None = chosen
    for _ in range(len(by_id) + 1):
        if version is None or version.provider is not None:
            break
        version = by_id.get(version.parent_version_id or "")
    return version.provider if version else None


class PrepDocumentService:
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

    # --- reading ----------------------------------------------------------------------------

    def view(self, application_id: str) -> CardsView:
        """All cards as the screen shows them. Reads only: nothing is written, started or
        created."""
        if not is_safe_id(application_id):
            raise ApplicationNotFound()
        with self._open().db.read() as conn:
            header = tailoring_repo.application_header(conn, application_id)
            if header is None:
                raise ApplicationNotFound()
            records = job_fit_repo.fact_records(conn)
            workspace = tailoring_repo.workspace(conn, application_id)
            angle_id = workspace.angle_id if workspace else None
            cards = tuple(
                self._card(conn, application_id, kind, records, angle_id) for kind in ALL_KINDS
            )
        return CardsView(
            application_id,
            cards,
            bool(facts_for_ai(records)),
            tuple(card.kind for card in cards if card.approved),
            angle_id,
            workspace.angle_name if workspace and angle_id else None,
        )

    def _card(
        self,
        conn: sqlite3.Connection,
        aid: str,
        kind: str,
        records: Sequence[FactRecord],
        angle_id: str | None,
    ) -> CardView:
        latest = (
            tailoring_repo.latest_task(conn, f"{DRAFT_NARRATIVE}:{aid}/{kind}")
            if kind != SALARY
            else None
        )
        reusable = self._reusable(conn, aid, kind, records, angle_id)
        document = prep_documents_repo.document(conn, aid, kind)
        return self._build_card(conn, kind, LABELS[kind], document, latest, records, reusable)

    @staticmethod
    def _build_card(
        conn: sqlite3.Connection,
        kind: str,
        label: str,
        document: prep_documents_repo.StoredDocument | None,
        latest: tuple[str, str, str | None, int] | None,
        records: Sequence[FactRecord],
        reusable: tuple[CardReuse, ...],
    ) -> CardView:
        """The view of one document (a card or an adapted answer) and its latest task."""
        has_facts = bool(facts_for_ai(records))
        task = task_state_of(latest)
        chosen = (
            prep_documents_repo.get_version(conn, document.id, document.current_version_id)
            if document
            else None
        )
        if document is None or chosen is None:
            if task.active:
                state = "running"
            elif task.status == "failed":
                state = "failed"
            else:
                state = "none"
            return CardView(
                kind, label, state, None, (), (), kind != SALARY and not has_facts,
                False, False, task, reusable, False,
            )  # fmt: skip
        versions = prep_documents_repo.list_versions(conn, document.id)
        stored = prep_documents_repo.units(conn, chosen.id)
        documents = [_document_unit_of(unit) for unit in stored]
        blockers = blockers_of(stored, documents, records)
        units = tuple(
            CardUnit(
                unit.id,
                unit.text,
                unit.origin,
                tuple(FlagView(flag.kind, flag.claim) for flag in unit.flags),
                unit.refs,
                unit.added_details,
                tuple(w for ref in unit.refs if (w := wording_of(conn, ref)) is not None),
            )
            for unit in stored
            if not unit.removed
        )
        approved_status = chosen.status == DocumentStatus.APPROVED.value
        if task.active:
            state = "running"
        elif not units:
            state = "empty"
        elif approved_status:
            state = "recheck" if blockers else "approved"
        else:
            state = "draft"
        provider = _provider_of(chosen, versions)
        left_out = (
            _left_out(prep_documents_repo.task_result_ref(conn, latest[0]), chosen.id)
            if latest and latest[1] == "succeeded"
            else ""
        )
        return CardView(
            kind,
            label,
            state,
            CardVersion(
                chosen.id, chosen.number, chosen.status, chosen.origin, provider,
                chosen.reused_version_id,
            ),
            units,
            blockers,
            not units,
            approved_status and not blockers and bool(units),
            approved_status and bool(blockers),
            task,
            reusable,
            provider == "fake",
            bool(stored) and not units,
            left_out,
        )  # fmt: skip

    def _reusable(
        self,
        conn: sqlite3.Connection,
        aid: str,
        kind: str,
        records: Sequence[FactRecord],
        angle_id: str | None,
    ) -> tuple[CardReuse, ...]:
        """The approved cards of other applications this one may copy, newest first."""
        if kind not in REUSABLE_KINDS or angle_id is None:
            return ()
        found: list[CardReuse] = []
        for source in prep_documents_repo.reusable_sources(conn, aid, kind, angle_id):
            if self._blockers(conn, source.version_id, records):
                continue
            label = " at ".join(part for part in (source.title, source.company) if part)
            found.append(CardReuse(source.version_id, f"{label or 'Another application'}"))
        return tuple(found)

    @staticmethod
    def _blockers(
        conn: sqlite3.Connection, version_id: str, records: Sequence[FactRecord]
    ) -> tuple[Blocker, ...]:
        stored = prep_documents_repo.units(conn, version_id)
        return blockers_of(stored, [_document_unit_of(unit) for unit in stored], records)

    # --- starting ---------------------------------------------------------------------------

    def _plan(self, tier: Tier | None, task: str = DRAFT_NARRATIVE) -> SummaryPlan:
        settings = self._settings()
        service = SERVICE_BY_PROVIDER_KIND.get(settings.kind or "") if settings else None
        if settings is None or service is None:
            raise ProviderNotConfigured()
        return SummaryPlan(service, tier or settings.task_tiers.get(task, "thorough"))

    def plan_start(
        self, application_id: str, kind: str, *, tier: Tier | None = None
    ) -> SummaryPlan:
        """What a start would use. ``ValueError`` for "salary", a word that is not a card or a
        tier that is not fast or thorough, ``GenerateNotReady`` (reason ``facts``) without a
        usable confirmed fact, ``ProviderNotConfigured`` without a usable provider."""
        _kind(kind)
        if tier not in TIERS:
            raise ValueError(BAD_TIER)
        if not is_safe_id(application_id):
            raise ApplicationNotFound()
        with self._open().db.read() as conn:
            if tailoring_repo.application_job_id(conn, application_id) is None:
                raise ApplicationNotFound()
            records = job_fit_repo.fact_records(conn)
        if not facts_for_ai(records):
            raise GenerateNotReady("facts", NO_FACTS)
        return self._plan(tier)

    def start_card(
        self,
        application_id: str,
        kind: str,
        *,
        version: str | None = None,
        tier: Tier | None = None,
    ) -> TaskRef:
        """Queue the drafting of one card. ``version`` is the card's current version as last
        seen (None while it has none), else ``VersionConflict`` (nothing queued). A task already
        queued or running for the card is returned. The budget gate is the caller's, before
        this."""
        self.plan_start(application_id, kind, tier=tier)
        tasks = self._tasks()
        if tasks is None:
            raise ApplicationNotFound()
        with self._open().db.read() as conn:
            document = prep_documents_repo.document(conn, application_id, kind)
        if (document.current_version_id if document else None) != (version or None):
            raise VersionConflict()
        asked = json.dumps({"application": application_id, "kind": kind, "tier": tier})
        return tasks.enqueue(DRAFT_NARRATIVE, f"{application_id}/{kind}", input_ref=asked)

    # --- adapting an earlier answer (part C) ------------------------------------------------

    @staticmethod
    def _scope(conn: sqlite3.Connection, aid: str, qid: str) -> str:
        """The text of the question when the application exists and the question is in its
        prep scope (global, or of its company); else ``ApplicationNotFound``."""
        if not is_safe_id(aid) or not is_safe_id(qid):
            raise ApplicationNotFound()
        source = prep_repo.source(conn, aid)
        found = prep_repo.questions(conn, question_id=qid)
        if source is None or not found or found[0].company_id not in (None, source.company_id):
            raise ApplicationNotFound()
        return found[0].text

    def adapted_view(self, application_id: str, question_id: str) -> AdaptedView:
        """The best earlier answer to the question (from another application), whether it can
        be adapted now, and the adapted answer as a card. Reads only: nothing is written,
        started or created."""
        with self._open().db.read() as conn:
            self._scope(conn, application_id, question_id)
            records = job_fit_repo.fact_records(conn)
            earlier = practice_repo.best_answer(conn, question_id, application_id)
            latest = tailoring_repo.latest_task(
                conn, f"{ADAPT_ANSWER}:{application_id}/{question_id}"
            )
            document = prep_documents_repo.document(conn, application_id, ADAPTED, question_id)
            answer = self._build_card(conn, ADAPTED, ADAPTED_LABEL, document, latest, records, ())
        has_facts = bool(facts_for_ai(records))
        can_adapt = earlier is not None and has_facts and not answer.approved
        return AdaptedView(application_id, question_id, earlier, can_adapt, answer, has_facts)

    def plan_adapt(
        self, application_id: str, question_id: str, *, tier: Tier | None = None
    ) -> SummaryPlan:
        """What an adaptation would use. ``ValueError`` for a tier that is not fast or thorough,
        ``GenerateNotReady`` (reason ``answer``) without an earlier answer in another
        application, (reason ``facts``) without a usable confirmed fact, ``ProviderNotConfigured``
        without a usable provider, ``ApplicationNotFound`` outside the application's scope."""
        if tier not in TIERS:
            raise ValueError(BAD_TIER)
        with self._open().db.read() as conn:
            self._scope(conn, application_id, question_id)
            earlier = practice_repo.best_answer(conn, question_id, application_id)
            records = job_fit_repo.fact_records(conn)
        if earlier is None:
            raise GenerateNotReady("answer", NO_EARLIER_ANSWER)
        if not facts_for_ai(records):
            raise GenerateNotReady("facts", NO_FACTS)
        return self._plan(tier, ADAPT_ANSWER)

    def start_adapt(
        self,
        application_id: str,
        question_id: str,
        *,
        version: str | None = None,
        tier: Tier | None = None,
    ) -> TaskRef:
        """Queue the adaptation of the earlier answer. ``version`` is the adapted answer's
        current version as last seen (None while it has none), else ``VersionConflict`` (nothing
        queued). A task already queued or running for the question is returned. The budget gate
        is the caller's, before this. The task holds ids and the tier, never any text."""
        self.plan_adapt(application_id, question_id, tier=tier)
        tasks = self._tasks()
        if tasks is None:
            raise ApplicationNotFound()
        with self._open().db.read() as conn:
            document = prep_documents_repo.document(conn, application_id, ADAPTED, question_id)
        if (document.current_version_id if document else None) != (version or None):
            raise VersionConflict()
        asked = json.dumps({"application": application_id, "question": question_id, "tier": tier})
        return tasks.enqueue(ADAPT_ANSWER, f"{application_id}/{question_id}", input_ref=asked)

    # --- running (inside a task) ------------------------------------------------------------

    @staticmethod
    def _request_from(text: str | None) -> tuple[str, str, Tier | None]:
        try:
            loaded: Any = json.loads(text or "")
            aid, kind, tier = loaded["application"], loaded["kind"], loaded["tier"]
            if not is_safe_id(aid) or kind not in CARD_KINDS or tier not in TIERS:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ApplicationNotFound() from None
        return aid, kind, tier

    def _source(self, aid: str, kind: str, question_id: str | None = None) -> CardSource:
        with self._open().db.read() as conn:
            inputs_job = tailoring_repo.application_header(conn, aid)
            if inputs_job is None:
                raise ApplicationNotFound()
            job_id, title, company = inputs_job
            inputs = job_fit_repo.inputs(conn, job_id)
            workspace = tailoring_repo.workspace(conn, aid)
            document = prep_documents_repo.document(conn, aid, kind, question_id)
            notes = prep_documents_repo.company_notes(conn, aid)
            brief = prep_documents_repo.company_brief_sections(conn, aid)
            skills = tuple(skill.name for skill in skills_repo.list_skills(conn))
        angle = (
            ChosenAngle(workspace.angle_name or "", workspace.angle_description or "")
            if workspace and workspace.angle_id
            else None
        )
        return CardSource(
            TailoringJob(
                job_id,
                str(inputs.job["snapshot_text"]),
                inputs.requirements,
                title,
                company,
            ),
            inputs.facts,
            skills,
            angle,
            workspace.angle_id if workspace else None,
            notes,
            document.current_version_id if document else None,
            brief,
        )

    async def _runtime(self) -> DocumentRuntime:
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

    async def run(self, ctx: SummaryContext) -> ResultWriter:
        aid, kind, tier = self._request_from(ctx.input_ref)
        await ctx.heartbeat(0)
        source = await asyncio.to_thread(self._source, aid, kind)
        if not facts_for_ai(source.records):
            raise FitNotAvailable()
        runtime = await self._runtime()
        await ctx.heartbeat(10)
        output = await run_draft_narrative(
            runtime.runner,
            aid,
            kind,
            source.job,
            source.records,
            source.angle,
            source.notes,
            ctx.cancel_flag,
            meter=runtime.meter,
            tier=tier,
            task_id=ctx.task_id,
            known_skills=source.skills,
            new_id=self._ids.new,
            brief=source.brief,
        )
        ctx.check_cancelled()
        version = self._generated_version(runtime, output, source)
        dropped: Counter[str] = Counter(str(item.reason) for item in output.result.dropped)
        return self._writer(
            aid,
            kind,
            version,
            self._lines_of(output),
            output.fact_refs,
            source.current_version_id,
            dropped,
        )

    @staticmethod
    def _adapt_request_from(text: str | None) -> tuple[str, str, Tier | None]:
        try:
            loaded: Any = json.loads(text or "")
            aid, qid, tier = loaded["application"], loaded["question"], loaded["tier"]
            if not is_safe_id(aid) or not is_safe_id(qid) or tier not in TIERS:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ApplicationNotFound() from None
        return aid, qid, tier

    def _adapt_source(self, aid: str, qid: str) -> tuple[CardSource, str, str]:
        """What an adaptation reads: the target application's source, the question's text and
        the best earlier answer as it is now."""
        source = self._source(aid, ADAPTED, qid)
        with self._open().db.read() as conn:
            question = self._scope(conn, aid, qid)
            earlier = practice_repo.best_answer(conn, qid, aid)
        if earlier is None:
            raise ApplicationNotFound()
        return source, question, earlier.text

    async def run_adapt(self, ctx: SummaryContext) -> ResultWriter:
        aid, qid, tier = self._adapt_request_from(ctx.input_ref)
        await ctx.heartbeat(0)
        source, question, old_answer = await asyncio.to_thread(self._adapt_source, aid, qid)
        if not facts_for_ai(source.records):
            raise FitNotAvailable()
        runtime = await self._runtime()
        await ctx.heartbeat(10)
        output = await run_adapt_answer(
            runtime.runner,
            aid,
            old_answer,
            question,
            source.job,
            source.records,
            source.angle,
            ctx.cancel_flag,
            meter=runtime.meter,
            tier=tier,
            task_id=ctx.task_id,
            known_skills=source.skills,
            new_id=self._ids.new,
        )
        ctx.check_cancelled()
        if not output.result.units:
            raise InvalidModelOutput()  # nothing usable came back: no empty answer is stored
        version = self._generated_version(runtime, output, source)
        dropped: Counter[str] = Counter(str(item.reason) for item in output.result.dropped)
        return self._writer(
            aid,
            ADAPTED,
            version,
            self._lines_of(output),
            output.fact_refs,
            source.current_version_id,
            dropped,
            question_id=qid,
        )

    @staticmethod
    def _lines_of(output: NarrativeOutput) -> list[documents_repo.StoredUnit]:
        flags: dict[str, list[documents_repo.StoredFlag]] = {}
        for flag in output.result.flags:
            flags.setdefault(flag.unit_id, []).append(
                documents_repo.StoredFlag(flag.kind, flag.claim)
            )
        return [
            documents_repo.StoredUnit(
                id=unit.unit_id,
                position=position,
                section=NARRATIVE_SECTION,
                role_id=None,
                kind="content",
                text=unit.text,
                origin="ai",
                removed=False,
                added_details=unit.added_details,
                flags=tuple(flags.get(unit.unit_id, ())),
                refs=unit.fact_ids,
            )
            for position, unit in enumerate(output.result.units)
        ]

    def _generated_version(
        self, runtime: DocumentRuntime, output: NarrativeOutput, source: CardSource
    ) -> prep_documents_repo.NewVersion:
        return prep_documents_repo.NewVersion(
            id=self._ids.new(),
            document_id=self._ids.new(),
            number=0,
            origin="generated",
            parent_version_id=source.current_version_id,
            angle_id=source.angle_id,
            angle_text=angle_text_of(source.angle) or "",
            reused_version_id=None,
            provider=runtime.provider_name,
            model=output.model,
            prompt_id=output.prompt_id,
            prompt_version=output.prompt_version,
            now="",
        )

    def _writer(
        self,
        aid: str,
        kind: str,
        version: prep_documents_repo.NewVersion,
        lines: list[documents_repo.StoredUnit],
        fact_refs: Sequence[str],
        expected: str | None,
        dropped: Mapping[str, int],
        *,
        question_id: str | None = None,
    ) -> ResultWriter:
        sent = ref_pairs(fact_refs)

        def write(conn: sqlite3.Connection) -> str:
            now = self._now()
            document = prep_documents_repo.document(conn, aid, kind, question_id)
            if (document.current_version_id if document else None) != expected:
                raise VersionConflict()  # the card changed while the model was answering
            document_id = document.id if document else version.document_id
            new = replace(
                version,
                document_id=document_id,
                number=prep_documents_repo.next_number(conn, document.id) if document else 1,
                now=now,
            )
            if document is None:
                prep_documents_repo.insert_document(
                    conn,
                    id=document_id,
                    application_id=aid,
                    kind=kind,
                    first_version_id=version.id,
                    now=now,
                    question_id=question_id,
                )
            prep_documents_repo.insert_version(conn, new)
            prep_documents_repo.insert_sent_facts(conn, version.id, sent, now=now)
            citable = _citable(conn, version.id)
            for position, line in enumerate(lines):
                prep_documents_repo.insert_unit(
                    conn, version.id, position, line, now=now, citable=citable
                )
            if document is not None:
                prep_documents_repo.set_current_version(conn, document, version.id, now=now)
            return _result_ref(version.id, dropped)

        return write

    # --- editing: every change is a new draft version ---------------------------------------

    def _begin(
        self,
        conn: sqlite3.Connection,
        aid: str,
        kind: str,
        version_id: str,
        question_id: str | None = None,
    ) -> CardDraft:
        """The card's current version, for a change made from ``version_id``
        (``VersionConflict`` when that is not the current one). An adapted answer names its
        question, which must be in the application's scope."""
        if not is_safe_id(aid):
            raise ApplicationNotFound()
        header = tailoring_repo.application_header(conn, aid)
        if header is None:
            raise ApplicationNotFound()
        if question_id is not None:
            self._scope(conn, aid, question_id)
        document = prep_documents_repo.document(conn, aid, kind, question_id)
        if document is None or document.current_version_id != version_id:
            raise VersionConflict()
        parent = prep_documents_repo.get_version(conn, document.id, version_id)
        if parent is None:
            raise VersionConflict()
        return CardDraft(
            document,
            parent,
            list(prep_documents_repo.units(conn, version_id)),
            tuple(roles_repo.list_roles(conn)),
            tuple(part for part in header[1:] if part),
        )

    def _commit(
        self,
        conn: sqlite3.Connection,
        draft: CardDraft,
        lines: Sequence[documents_repo.StoredUnit],
        *,
        by_code: bool = False,
    ) -> tuple[str, list[str]]:
        """Write ``lines`` as the next draft version (the parent's angle and sent facts copied)
        and make it current. Returns its id and the new id of each line. ``by_code`` (the salary
        card, which has nothing to check) writes it with origin ``code`` and approves it in the
        same transaction."""
        now = self._now()
        parent = draft.parent
        version_id = self._ids.new()
        prep_documents_repo.insert_version(
            conn,
            prep_documents_repo.NewVersion(
                id=version_id,
                document_id=draft.document.id,
                number=prep_documents_repo.next_number(conn, draft.document.id),
                origin="code" if by_code else "user",
                parent_version_id=parent.id,
                angle_id=parent.angle_id,
                angle_text=parent.angle_text,
                reused_version_id=None,
                provider=None,
                model=None,
                prompt_id=None,
                prompt_version=None,
                now=now,
            ),
        )
        prep_documents_repo.copy_sent_facts(conn, parent.id, version_id, now=now)
        citable = _citable(conn, version_id)
        ids: list[str] = []
        for position, line in enumerate(lines):
            fresh = replace(line, id=self._ids.new())
            ids.append(fresh.id)
            prep_documents_repo.insert_unit(
                conn, version_id, position, fresh, now=now, citable=citable
            )
        if by_code:
            prep_documents_repo.approve_draft(conn, version_id, now=now)
        prep_documents_repo.set_current_version(conn, draft.document, version_id, now=now)
        return version_id, ids

    @staticmethod
    def _index(draft: CardDraft, unit_id: str) -> int:
        for index, line in enumerate(draft.lines):
            if line.id == unit_id and not line.removed:
                return index
        raise UnitNotFound()

    @staticmethod
    def _comparison_facts(
        conn: sqlite3.Connection, version_id: str, roles: Sequence[roles_repo.StoredRole]
    ) -> tuple[AIFact, ...]:
        """The facts a line is checked against: the version's sent set, as worded then, and the
        facts the person saved from a document (they are theirs to cite)."""
        labels = {role.id: role_label(role) for role in roles}
        found = {
            (row.number, row.version): row
            for row in (
                *prep_documents_repo.sent_facts(conn, version_id),
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
        self, conn: sqlite3.Connection, draft: CardDraft, line: documents_repo.StoredUnit
    ) -> tuple[documents_repo.StoredUnit, FactOffer | None]:
        """The line with its flags recomputed against the facts its citations name, and the
        offer to save it as a fact when it claims something those facts do not."""
        return self._recheck(conn, draft.parent.id, draft.roles, draft.names, line)

    def _recheck(
        self,
        conn: sqlite3.Connection,
        version_id: str,
        roles: Sequence[roles_repo.StoredRole],
        names: Sequence[str],
        line: documents_repo.StoredUnit,
    ) -> tuple[documents_repo.StoredUnit, FactOffer | None]:
        """``_rechecked`` for the facts sent for ``version_id`` and the job title and company
        (``names``) of the application the line is in."""
        sent = self._comparison_facts(conn, version_id, roles)
        skills = tuple(skill.name for skill in skills_repo.list_skills(conn))
        unit = GeneratedUnit(
            line.id,
            "content",
            line.section,
            None,
            line.text,
            line.refs,
            line.added_details,
        )
        flags = check_units([unit], sent, known_skills=skills, name_sources=names)
        checked = replace(line, flags=_flags_of([(flag.kind, flag.claim) for flag in flags]))
        claims: tuple[str, ...] = ()
        if unit.fact_ids:
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
                    name_only_sources=names,
                )
            )
        return checked, FactOffer(SAVE_AS_FACT, claims, line.id) if claims else None

    @staticmethod
    def _outcome(
        version_id: str,
        ids: Sequence[str],
        index: int,
        offer: FactOffer | None,
        *,
        was_approved: bool,
    ) -> CardEdit:
        unit_id = ids[index]
        return CardEdit(
            version_id,
            unit_id,
            replace(offer, unit_id=unit_id) if offer is not None else None,
            was_approved,
        )

    def edit_unit(
        self,
        application_id: str,
        kind: str,
        version_id: str,
        unit_id: str,
        text: str,
        *,
        fact_ids: Sequence[str] | None = None,
        added_details: Sequence[str] | None = None,
    ) -> CardEdit:
        """Reword a paragraph. It becomes the person's own, keeps its citations unless
        ``fact_ids`` replaces them and its added details unless ``added_details`` replaces them
        (``()`` clears), and is re-checked against the facts sent for the version and the facts
        the person saved from documents. ``UnitInvalid``, ``UnitNotFound``,
        ``VersionConflict``."""
        _kind(kind)
        return self._edit(
            application_id, kind, None, version_id, unit_id, text, fact_ids, added_details
        )

    def _edit(
        self,
        application_id: str,
        kind: str,
        question_id: str | None,
        version_id: str,
        unit_id: str,
        text: str,
        fact_ids: Sequence[str] | None,
        added_details: Sequence[str] | None,
    ) -> CardEdit:
        text = _text(text)
        wanted_ids = None if fact_ids is None else checked_ids(fact_ids)
        wanted_details = None if added_details is None else checked_details(added_details)
        with self._open().db.transaction() as conn:
            draft = self._begin(conn, application_id, kind, version_id, question_id)
            was_approved = draft.parent.status == DocumentStatus.APPROVED.value
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
        return self._outcome(new_version, ids, index, offer, was_approved=was_approved)

    def remove_unit(self, application_id: str, kind: str, version_id: str, unit_id: str) -> str:
        """Remove a paragraph: it stays as a tombstone (hidden, ignored by approval). Returns
        the new version's id. Also for the salary card's lines."""
        return self.remove_unit_outcome(application_id, kind, version_id, unit_id).version_id

    def remove_unit_outcome(
        self, application_id: str, kind: str, version_id: str, unit_id: str
    ) -> CardEdit:
        """``remove_unit`` with the outcome: the new version and whether the version it was
        removed from was approved. On the salary card, which has nothing to check, the new
        version is written by code and approved in the same transaction (the card stays
        approved); on every other card it is a draft."""
        _kind(kind, salary=True)
        return self._remove(application_id, kind, None, version_id, unit_id)

    def _remove(
        self, application_id: str, kind: str, question_id: str | None, version_id: str, unit_id: str
    ) -> CardEdit:
        with self._open().db.transaction() as conn:
            draft = self._begin(conn, application_id, kind, version_id, question_id)
            was_approved = draft.parent.status == DocumentStatus.APPROVED.value
            index = self._index(draft, unit_id)
            draft.lines[index] = replace(draft.lines[index], removed=True)
            new_version, ids = self._commit(conn, draft, draft.lines, by_code=kind == SALARY)
        return self._outcome(new_version, ids, index, None, was_approved=was_approved)

    def resolve_flag_confirm(
        self, application_id: str, kind: str, version_id: str, unit_id: str
    ) -> CardEdit:
        """ "This is true: save it as a fact". In one transaction: a confirmed fact (source
        ``document``, the paragraph's text) and the new version in which the paragraph cites
        only that fact and has no flags. If the version cannot be written, no fact exists
        afterwards."""
        _kind(kind)
        return self._confirm(application_id, kind, None, version_id, unit_id)

    def _confirm(
        self, application_id: str, kind: str, question_id: str | None, version_id: str, unit_id: str
    ) -> CardEdit:
        facts = self._facts()
        if facts is None:
            raise ApplicationNotFound()
        with self._open().db.transaction() as conn:
            draft = self._begin(conn, application_id, kind, version_id, question_id)
            was_approved = draft.parent.status == DocumentStatus.APPROVED.value
            index = self._index(draft, unit_id)
            line = draft.lines[index]
            made = facts.create_in(conn, FactInput(text=line.text), source=SOURCE_DOCUMENT)
            draft.lines[index] = replace(
                line,
                origin="user",
                refs=(fact_ref_text(made.number, made.content_version),),
                added_details=(),
                flags=(),
            )
            new_version, ids = self._commit(conn, draft, draft.lines)
        return self._outcome(new_version, ids, index, None, was_approved=was_approved)

    # --- approving --------------------------------------------------------------------------

    def approve(self, application_id: str, kind: str, version_id: str) -> None:
        """Approve the current draft when nothing needs the person's check, looking at the
        facts as they are now. ``VersionConflict`` for a stale or already approved version,
        ``NothingToApprove`` without a visible paragraph, ``ApprovalBlocked`` while paragraphs
        need a check (nothing changes)."""
        _kind(kind)
        self._approve(application_id, kind, None, version_id)

    def _approve(
        self, application_id: str, kind: str, question_id: str | None, version_id: str
    ) -> None:
        with self._open().db.transaction() as conn:
            draft = self._begin(conn, application_id, kind, version_id, question_id)
            if draft.parent.status != DocumentStatus.DRAFT.value:
                raise VersionConflict()
            if all(line.removed for line in draft.lines):
                raise NothingToApprove()
            records = job_fit_repo.fact_records(conn)
            units = [_document_unit_of(line) for line in draft.lines]
            blockers = blockers_of(draft.lines, units, records)
            if blockers:
                raise ApprovalBlocked(blockers, NOUN)
            prep_documents_repo.set_version_status(
                conn, draft.parent, DocumentStatus.APPROVED.value, now=self._now()
            )

    # --- the adapted answer: the same changes, for one question -----------------------------

    def edit_adapted(
        self,
        application_id: str,
        question_id: str,
        version_id: str,
        unit_id: str,
        text: str,
        *,
        fact_ids: Sequence[str] | None = None,
        added_details: Sequence[str] | None = None,
    ) -> CardEdit:
        """``edit_unit`` for the adapted answer of a question."""
        return self._edit(
            application_id, ADAPTED, question_id, version_id, unit_id, text, fact_ids, added_details
        )

    def remove_adapted(
        self, application_id: str, question_id: str, version_id: str, unit_id: str
    ) -> CardEdit:
        """``remove_unit_outcome`` for the adapted answer of a question."""
        return self._remove(application_id, ADAPTED, question_id, version_id, unit_id)

    def confirm_adapted(
        self, application_id: str, question_id: str, version_id: str, unit_id: str
    ) -> CardEdit:
        """``resolve_flag_confirm`` for the adapted answer of a question."""
        return self._confirm(application_id, ADAPTED, question_id, version_id, unit_id)

    def approve_adapted(self, application_id: str, question_id: str, version_id: str) -> None:
        """``approve`` for the adapted answer of a question."""
        self._approve(application_id, ADAPTED, question_id, version_id)

    # --- the salary card: built in code -----------------------------------------------------

    def build_salary(self, application_id: str, *, version: str | None = None) -> str:
        """Build the salary card from the saved minimum and the pay the posting states, with no
        AI request, and return the id of its approved version. With the same lines as the
        current approved version nothing changes. ``VersionConflict`` for a stale ``version``,
        ``GenerateNotReady`` (reason ``salary``) when there is nothing to build from."""
        if not is_safe_id(application_id):
            raise ApplicationNotFound()
        opened = self._open()
        minimum = (
            preferences.minimum_salary
            if (preferences := SettingsService(opened.db, self._clock).load(JOB_PREFERENCES).value)
            else None
        )
        with opened.db.transaction() as conn:
            pay = prep_documents_repo.job_pay(conn, application_id)
            if pay is None:
                raise ApplicationNotFound()
            document = prep_documents_repo.document(conn, application_id, SALARY)
            current = document.current_version_id if document else None
            if current != (version or None):
                raise VersionConflict()
            lines = build_salary_card(
                minimum=minimum,
                low=pay.low,
                high=pay.high,
                currency=pay.currency,
                period=pay.period,
            )
            if not lines:
                raise GenerateNotReady("salary", NO_SALARY)
            if document is not None and current is not None:
                earlier = prep_documents_repo.get_version(conn, document.id, current)
                visible = [
                    u.text for u in prep_documents_repo.units(conn, current) if not u.removed
                ]
                if earlier is not None and earlier.status == "approved" and visible == list(lines):
                    return current
            return self._write_salary(conn, application_id, document, current, lines)

    def _write_salary(
        self,
        conn: sqlite3.Connection,
        aid: str,
        document: prep_documents_repo.StoredDocument | None,
        parent: str | None,
        lines: Sequence[str],
    ) -> str:
        now = self._now()
        version_id = self._ids.new()
        document_id = document.id if document else self._ids.new()
        if document is None:
            prep_documents_repo.insert_document(
                conn,
                id=document_id,
                application_id=aid,
                kind=SALARY,
                first_version_id=version_id,
                now=now,
            )
        prep_documents_repo.insert_version(
            conn,
            prep_documents_repo.NewVersion(
                id=version_id,
                document_id=document_id,
                number=prep_documents_repo.next_number(conn, document_id) if document else 1,
                origin="code",
                parent_version_id=parent,
                angle_id=None,
                angle_text="",
                reused_version_id=None,
                provider=None,
                model=None,
                prompt_id=None,
                prompt_version=None,
                now=now,
            ),
        )
        for position, text in enumerate(lines):
            prep_documents_repo.insert_unit(
                conn,
                version_id,
                position,
                documents_repo.StoredUnit(
                    id=self._ids.new(),
                    position=position,
                    section=NARRATIVE_SECTION,
                    role_id=None,
                    kind="content",
                    text=text,
                    origin="code",
                    removed=False,
                    added_details=(),
                    flags=(),
                    refs=(),
                ),
                now=now,
                citable=frozenset(),
            )
        prep_documents_repo.approve_draft(conn, version_id, now=now)
        if document is not None:
            prep_documents_repo.set_current_version(conn, document, version_id, now=now)
        return version_id

    # --- reuse ------------------------------------------------------------------------------

    def use_card(
        self,
        application_id: str,
        kind: str,
        source_version_id: str,
        *,
        version: str | None = None,
    ) -> str:
        """Copy another application's APPROVED card into a new DRAFT version here and return its
        id. ``ReuseRefused`` (nothing written) unless the kind is one of the four reusable
        kinds, the source is an approved version of the same kind of another application, has no
        blocker now, and both applications chose the same angle. ``VersionConflict`` for a stale
        ``version``. Approval checks the copy again."""
        if not is_safe_id(application_id):
            raise ApplicationNotFound()
        if kind not in REUSABLE_KINDS:
            raise ReuseRefused(ASKED_REFUSED)
        with self._open().db.transaction() as conn:
            if tailoring_repo.application_job_id(conn, application_id) is None:
                raise ApplicationNotFound()
            document = prep_documents_repo.document(conn, application_id, kind)
            source = (
                prep_documents_repo.source_version(conn, source_version_id)
                if is_safe_id(source_version_id)
                else None
            )
            if (
                source is None
                or source.kind != kind
                or source.application_id == application_id
                or source.version.status != DocumentStatus.APPROVED.value
            ):
                raise ReuseRefused()
            workspace = tailoring_repo.workspace(conn, application_id)
            angle_id = workspace.angle_id if workspace else None
            if angle_id is None or source.version.angle_id != angle_id:
                raise ReuseRefused(REUSE_SAME_ANGLE)
            records = job_fit_repo.fact_records(conn)
            if self._blockers(conn, source.version.id, records):
                raise ReuseRefused()
            if (document.current_version_id if document else None) != (version or None):
                raise VersionConflict()
            return self._write_copy(conn, application_id, kind, document, source)

    def _write_copy(
        self,
        conn: sqlite3.Connection,
        aid: str,
        kind: str,
        document: prep_documents_repo.StoredDocument | None,
        source: prep_documents_repo.SourceVersion,
    ) -> str:
        now = self._now()
        version_id = self._ids.new()
        document_id = document.id if document else self._ids.new()
        if document is None:
            prep_documents_repo.insert_document(
                conn,
                id=document_id,
                application_id=aid,
                kind=kind,
                first_version_id=version_id,
                now=now,
            )
        prep_documents_repo.insert_version(
            conn,
            prep_documents_repo.NewVersion(
                id=version_id,
                document_id=document_id,
                number=prep_documents_repo.next_number(conn, document_id) if document else 1,
                origin="reused",
                parent_version_id=document.current_version_id if document else None,
                angle_id=source.version.angle_id,
                angle_text=source.version.angle_text,
                reused_version_id=source.version.id,
                provider=source.version.provider,
                model=source.model,
                prompt_id=source.prompt_id,
                prompt_version=source.prompt_version,
                now=now,
            ),
        )
        prep_documents_repo.copy_sent_facts(conn, source.version.id, version_id, now=now)
        citable = _citable(conn, version_id)
        # Every copied line is checked as an edit is, against the facts sent for the source and
        # the job title and company of THIS application: a name the source card used for its own
        # company is not supported here, so it is flagged and blocks approval.
        roles = tuple(roles_repo.list_roles(conn))
        header = tailoring_repo.application_header(conn, aid)
        names = tuple(part for part in header[1:] if part) if header else ()
        position = 0
        for line in prep_documents_repo.units(conn, source.version.id):
            if line.removed:
                continue
            checked, _ = self._recheck(
                conn, version_id, roles, names, replace(line, id=self._ids.new(), flags=())
            )
            prep_documents_repo.insert_unit(
                conn, version_id, position, checked, now=now, citable=citable
            )
            position += 1
        if document is not None:
            prep_documents_repo.set_current_version(conn, document, version_id, now=now)
        return version_id


__all__ = [
    "ADAPTED",
    "ADAPT_ANSWER_LABEL",
    "ALL_KINDS",
    "DRAFT_NARRATIVE_LABEL",
    "LABELS",
    "SALARY",
    "AdaptedView",
    "CardEdit",
    "CardReuse",
    "CardUnit",
    "CardVersion",
    "CardView",
    "CardsView",
    "PrepDocumentService",
    "ReuseRefused",
]
