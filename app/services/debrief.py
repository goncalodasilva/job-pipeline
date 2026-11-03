"""The debrief after an interview (M8.3 part A; AC-M8.5; spec 3.1 and 3.3; ADR-124 items 7, 9, 11).

Save is one transaction: the asked questions join the question bank (counted, never classified
by an AI), an optional next round becomes an interview event, and the debrief row is made
``done``. A saved debrief is never edited. Draft facts are a separate, explicit, metered
background task (``debrief_facts``): one ``structure_answer`` request per experience paragraph,
all written together with the task's success. A suggested status change is applied only when
the person confirms it.

"How it went" is stored on this computer and shown escaped; no AI path ever reads it. No
debrief text reaches logs, task or usage rows, events or error text.
"""

import asyncio
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Final
from zoneinfo import ZoneInfo

from app.domain.clock import Clock
from app.domain.debrief import (
    NEXT_STEPS,
    STATUS_BY_STEP,
    check_how_it_went,
    parse_asked_questions,
    split_experiences,
    suggest_status,
)
from app.domain.errors import (
    ApplicationNotFound,
    EventTimeInvalid,
    ProviderNotConfigured,
    VersionConflict,
    format_day_time,
)
from app.domain.fact_selection import offered_skills
from app.domain.ids import IdFactory, is_safe_id
from app.domain.question_bank import BankQuestion
from app.domain.settings import AIProviderSettings, Tier
from app.domain.tasks import TaskRef
from app.domain.usage import SERVICE_BY_PROVIDER_KIND, ServiceId
from app.modules.ai import AIProvider, AIProviderFactory
from app.modules.storage import (
    applications_repo,
    debrief_repo,
    job_fit_repo,
    prep_repo,
    skills_repo,
)
from app.modules.storage.debrief_repo import DraftRow
from app.services.applications import ApplicationService
from app.services.debrief_ai import STRUCTURE_ANSWER, ParagraphDraft, structure_paragraph
from app.services.facts import SOURCE_DEBRIEF, DraftOrigin, FactService
from app.services.job_summary import ResultWriter, SummaryContext, SummaryWorkspace
from app.services.tasks import TaskService
from app.services.usage import UsageService
from app.services.usage_meter import ServiceUsageMeter

DEBRIEF_FACTS: Final = "debrief_facts"
DEBRIEF_FACTS_LABEL: Final = "Debrief facts"
_ACTIVE: Final = frozenset({"queued", "running", "cancelling"})
_TIERS: Final = frozenset({"default", "fast", "thorough"})
_DATE: Final = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME: Final = re.compile(r"\d{2}:\d{2}")
CHOOSE_NEXT_STEP: Final = "Choose one of the listed steps."
NEXT_NEEDS_BOTH: Final = "Give both the date and the time of the next round, or leave both empty."
DATE_INVALID: Final = "Use a date that exists, as year-month-day."
TIME_INVALID: Final = "Use a time of day, as hours:minutes."
ZONE_NEEDED: Final = "Choose a time zone for the next round."
DATE_NEEDS_NEXT_ROUND: Final = (
    "Choose Next round to add a date and time, or clear the date and time."
)
TIER_FALLBACK: Final[Tier] = "fast"


class DebriefInvalid(ValueError):
    """Input the debrief refuses, one message per form field (never the text itself)."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("invalid debrief")
        self.errors = errors


def normalize_text(text: str) -> str:
    """Text as it is checked, stored and shown again: line ends as LF, no NUL, tabs as spaces,
    nothing trimmed."""
    return text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")


@dataclass(frozen=True)
class DebriefEntry:
    """What the Save form posts, as strings."""

    questions: str = ""
    how_it_went: str = ""
    experiences: str = ""
    company_only: bool = False
    next_step: str = ""
    next_date: str = ""
    next_time: str = ""
    next_zone: str = ""

    def normalized(self) -> "DebriefEntry":
        return DebriefEntry(
            normalize_text(self.questions),
            normalize_text(self.how_it_went),
            normalize_text(self.experiences),
            self.company_only,
            normalize_text(self.next_step),
            normalize_text(self.next_date),
            normalize_text(self.next_time),
            normalize_text(self.next_zone),
        )


@dataclass(frozen=True)
class _Checked:
    questions: tuple[str, ...]
    how_it_went: str
    experiences: tuple[str, ...]
    company_only: bool
    next_step: str | None
    wall: datetime | None
    zone: str


@dataclass(frozen=True)
class StatusSuggestion:
    key: str
    label: str


@dataclass(frozen=True)
class NextRound:
    local_text: str
    instant: str
    zone: str


@dataclass(frozen=True)
class DebriefView:
    """One interview's debrief. ``version`` is the debrief's (0 while it has no row) and
    ``application_version`` the application's, which a status confirmation checks."""

    application_id: str
    event_id: str
    company: str
    role: str
    when: str
    instant: str
    zone: str
    version: int
    state: str  # "open" (no row), "done" or "dismissed"
    questions: str
    how_it_went: str
    experiences: str
    company_only: bool
    next_step: str | None
    next_round: NextRound | None
    drafts: tuple[DraftRow, ...]
    paragraphs: int
    task_status: str | None
    task_error: str | None
    application_version: int
    suggestion: StatusSuggestion | None
    moved_to: str | None  # the label of the status the confirmed suggestion moved to

    @property
    def saved(self) -> bool:
        return self.state == "done"

    @property
    def active(self) -> bool:
        return not self.drafts and self.task_status in _ACTIVE

    @property
    def failed(self) -> bool:
        return (
            self.saved
            and not self.drafts
            and self.task_status is not None
            and self.task_status not in _ACTIVE
            and self.task_status != "succeeded"
        )

    @property
    def can_draft(self) -> bool:
        return self.saved and self.paragraphs > 0 and not self.drafts and not self.active


@dataclass(frozen=True)
class DraftPlan:
    service: ServiceId
    tier: Tier
    requests: int  # one request per experience paragraph


def _paragraphs(text: str) -> tuple[str, ...]:
    try:
        return split_experiences(text)
    except ValueError:
        return ()


class DebriefService:
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
        applications: Callable[[], ApplicationService],
    ) -> None:
        self._factory, self._clock, self._ids = factory, clock, ids
        self._workspace, self._settings, self._tasks, self._usage = (
            workspace,
            settings,
            tasks,
            usage,
        )
        self._applications = applications

    def _open(self) -> SummaryWorkspace:
        opened = self._workspace()
        if opened is None:
            raise ApplicationNotFound()
        return opened

    @staticmethod
    def _scope(
        conn: sqlite3.Connection, aid: str, event_id: str, *, writable: bool = False
    ) -> debrief_repo.InterviewScope:
        """The interview of this application. ``writable``: a cancelled interview is refused
        too (Save and Not needed), with the same ``ApplicationNotFound``."""
        scope = (
            debrief_repo.interview(conn, aid, event_id)
            if is_safe_id(aid) and is_safe_id(event_id)
            else None
        )
        if scope is None or (writable and scope.status != "scheduled"):
            raise ApplicationNotFound()
        return scope

    # --- reading ----------------------------------------------------------------------------

    def view(self, aid: str, event_id: str) -> DebriefView:
        """Reads only. ``ApplicationNotFound`` for an unknown or unsafe id, an event of another
        application and an event that is not an interview."""
        with self._open().db.read() as conn:
            scope = self._scope(conn, aid, event_id)
            stored = debrief_repo.get(conn, event_id)
            drafts = debrief_repo.drafts(conn, stored.id) if stored else ()
            task = debrief_repo.latest_task(conn, stored.id, DEBRIEF_FACTS) if stored else None
            next_round = self._next_round(conn, stored)
            target = (
                suggest_status(stored.next_step, scope.status_key, scope.status_phase)
                if stored and stored.state == "done" and not stored.status_confirmed
                else None
            )
            suggestion = None
            if target is not None and (status := applications_repo.get_status(conn, target)):
                suggestion = StatusSuggestion(status.key, status.label)
            moved = None
            if stored and stored.status_confirmed and stored.next_step in STATUS_BY_STEP:
                done = applications_repo.get_status(conn, STATUS_BY_STEP[stored.next_step])
                moved = done.label if done else None
        local = datetime.fromisoformat(scope.starts_at).astimezone(ZoneInfo(scope.time_zone))
        return DebriefView(
            aid,
            event_id,
            scope.company or "No company",
            scope.role,
            format_day_time(local),
            local.isoformat(),
            scope.time_zone,
            stored.version if stored else 0,
            stored.state if stored else "open",
            stored.questions_text if stored else "",
            stored.how_it_went if stored else "",
            stored.experiences_text if stored else "",
            stored.company_only if stored else False,
            stored.next_step if stored else None,
            next_round,
            drafts,
            len(_paragraphs(stored.experiences_text)) if stored else 0,
            task[1] if task else None,
            task[2] if task else None,
            scope.application_version,
            suggestion,
            moved,
        )

    @staticmethod
    def _next_round(
        conn: sqlite3.Connection, stored: debrief_repo.StoredDebrief | None
    ) -> NextRound | None:
        if stored is None or stored.next_event_id is None:
            return None
        found = debrief_repo.next_event_text(conn, stored.application_id, stored.next_event_id)
        if found is None:
            return None
        starts_at, zone = found
        local = datetime.fromisoformat(starts_at).astimezone(ZoneInfo(zone))
        return NextRound(format_day_time(local), local.isoformat(), zone)

    # --- Save and Not needed ----------------------------------------------------------------

    @staticmethod
    def check(entry: DebriefEntry) -> _Checked:
        """The entry as it is kept. ``DebriefInvalid`` with a message per field; nothing here
        reads or writes storage."""
        found = entry.normalized()
        errors: dict[str, str] = {}
        questions: tuple[str, ...] = ()
        experiences: tuple[str, ...] = ()
        try:
            questions = parse_asked_questions(found.questions)
        except ValueError as error:
            errors["questions"] = str(error)
        try:
            experiences = split_experiences(found.experiences)
        except ValueError as error:
            errors["experiences"] = str(error)
        how_it_went = ""
        try:
            how_it_went = check_how_it_went(found.how_it_went)
        except ValueError as error:
            errors["how_it_went"] = str(error)
        step = found.next_step
        if step and step not in NEXT_STEPS:
            errors["next_step"] = CHOOSE_NEXT_STEP
        wall = None
        if step == "next_round" and (found.next_date or found.next_time):
            wall = DebriefService._wall(found, errors)
        elif step != "next_round" and (found.next_date or found.next_time):
            errors.setdefault("next_step", DATE_NEEDS_NEXT_ROUND)
        if errors:
            raise DebriefInvalid(errors)
        return _Checked(
            questions,
            how_it_went,
            experiences,
            found.company_only,
            step or None,
            wall,
            found.next_zone,
        )

    @staticmethod
    def _wall(found: DebriefEntry, errors: dict[str, str]) -> datetime | None:
        if not (found.next_date and found.next_time):
            errors["next_time" if found.next_date else "next_date"] = NEXT_NEEDS_BOTH
            return None
        day: date | None = None
        clock: time | None = None
        try:
            if not _DATE.fullmatch(found.next_date):
                raise ValueError
            day = date.fromisoformat(found.next_date)
        except ValueError:
            errors["next_date"] = DATE_INVALID
        try:
            if not _TIME.fullmatch(found.next_time):
                raise ValueError
            clock = time.fromisoformat(found.next_time)
        except ValueError:
            errors["next_time"] = TIME_INVALID
        if not found.next_zone:
            errors["next_zone"] = ZONE_NEEDED
        return datetime.combine(day, clock) if day and clock else None

    def save(self, aid: str, event_id: str, entry: DebriefEntry, *, version: int) -> None:
        """Keep the debrief. ``DebriefInvalid`` for a refused input, ``VersionConflict`` for a
        stale version or a debrief that is already saved or dismissed. Nothing is stored on
        any refusal."""
        opened = self._open()
        with opened.db.read() as conn:
            self._scope(conn, aid, event_id, writable=True)
        checked = self.check(entry)
        now = self._clock.now().astimezone(UTC).isoformat()
        with opened.db.transaction() as conn:
            scope = self._scope(conn, aid, event_id, writable=True)
            if debrief_repo.get(conn, event_id) is not None or version != 0:
                raise VersionConflict()
            for text in checked.questions:
                prep_repo.insert_question(
                    conn,
                    BankQuestion(
                        self._ids.new(),
                        text,
                        "behavioral",
                        company_id=scope.company_id if checked.company_only else None,
                        origin="asked",
                    ),
                    sighting_id=self._ids.new(),
                    now=now,
                    application_id=aid,
                    event_id=event_id,
                )
            next_event_id = None
            if checked.wall is not None:
                try:
                    next_event_id = self._applications().add_interview_date_in(
                        conn, aid, starts_at=checked.wall, time_zone=checked.zone
                    )
                except EventTimeInvalid as error:
                    field = "next_zone" if error.field == "time_zone" else "next_time"
                    raise DebriefInvalid({field: error.user_message}) from None
            debrief_repo.insert(
                conn,
                debrief_repo.StoredDebrief(
                    self._ids.new(),
                    aid,
                    event_id,
                    "done",
                    "\n".join(checked.questions),
                    checked.how_it_went,
                    "\n\n".join(checked.experiences),
                    checked.company_only and scope.company_id is not None,
                    checked.next_step,
                    next_event_id,
                    False,
                    1,
                ),
                now=now,
            )

    def dismiss(self, aid: str, event_id: str, *, version: int) -> None:
        """Mark the debrief not needed: a ``dismissed`` row with no text. ``VersionConflict``
        when the debrief already has a row or the version is stale."""
        now = self._clock.now().astimezone(UTC).isoformat()
        with self._open().db.transaction() as conn:
            self._scope(conn, aid, event_id, writable=True)
            if debrief_repo.get(conn, event_id) is not None or version != 0:
                raise VersionConflict()
            debrief_repo.insert(
                conn,
                debrief_repo.StoredDebrief(
                    self._ids.new(),
                    aid,
                    event_id,
                    "dismissed",
                    "",
                    "",
                    "",
                    False,
                    None,
                    None,
                    False,
                    1,
                ),
                now=now,
            )

    # --- the suggested status ---------------------------------------------------------------

    def confirm_status(self, aid: str, event_id: str, *, version: int) -> str:
        """Move the application to the suggested status (a drawer move, so it is recorded like
        any other) and remember that it was confirmed, in one transaction: either both are kept
        or neither. Returns the status label. ``VersionConflict`` when the application's version
        is stale, nothing is suggested or the debrief changed meanwhile."""
        shown = self.view(aid, event_id)
        if shown.application_version != version or shown.suggestion is None:
            raise VersionConflict()
        suggestion = shown.suggestion
        now = self._clock.now().astimezone(UTC).isoformat()
        with self._open().db.transaction() as conn:
            stored = debrief_repo.get(conn, event_id)
            if stored is None or stored.version != shown.version:
                raise VersionConflict()
            self._applications().move_in(
                conn, aid, suggestion.key, expected_version=version, source="drawer"
            )
            if not debrief_repo.mark_status_confirmed(
                conn, stored.id, now=now, expected_version=stored.version
            ):
                raise VersionConflict()
        return suggestion.label

    # --- draft facts ------------------------------------------------------------------------

    def _provider(self) -> tuple[AIProvider, AIProviderSettings]:
        settings = self._settings()
        if settings is None or settings.kind is None:
            raise ProviderNotConfigured()
        try:
            provider = self._factory.ai_provider(settings, data_dir=self._open().data_dir)
        except ValueError:
            raise ProviderNotConfigured() from None
        return provider, settings

    def plan_start(
        self, aid: str, event_id: str, *, version: int | None = None, tier: Tier | None = None
    ) -> DraftPlan:
        """What starting the draft facts would use. ``ApplicationNotFound`` for an unknown
        interview, ``VersionConflict`` when the debrief is not saved, has no experiences, has
        drafts, has a task running or the version is stale, ``ProviderNotConfigured`` without a
        usable provider."""
        shown = self.view(aid, event_id)
        if (version is not None and shown.version != version) or not shown.can_draft:
            raise VersionConflict()
        settings = self._settings()
        service = SERVICE_BY_PROVIDER_KIND.get(settings.kind or "") if settings else None
        if settings is None or service is None:
            raise ProviderNotConfigured()
        # Each paragraph is a structure_answer request, so that task's tier is the one used.
        planned = tier or settings.task_tiers.get(STRUCTURE_ANSWER, TIER_FALLBACK)
        return DraftPlan(service, planned, shown.paragraphs)

    def start_drafts(
        self, aid: str, event_id: str, *, version: int, tier: Tier | None = None
    ) -> TaskRef:
        """Queue the draft facts; the caller has asked the budget gate. Nothing is stored or
        queued on any refusal (see ``plan_start``)."""
        self.plan_start(aid, event_id, version=version, tier=tier)
        tasks = self._tasks()
        if tasks is None:
            raise ApplicationNotFound()

        def eligible(conn: sqlite3.Connection) -> bool:
            stored = debrief_repo.get(conn, event_id)
            return (
                stored is not None
                and stored.version == version
                and stored.state == "done"
                and not debrief_repo.has_drafts(conn, stored.id)
            )

        stored_id = self._stored_id(aid, event_id)
        ref = tasks.enqueue_if(
            DEBRIEF_FACTS,
            stored_id,
            input_ref=f"{stored_id}/{tier or 'default'}",
            eligible=eligible,
        )
        if ref is None:
            raise VersionConflict()
        return ref

    def _stored_id(self, aid: str, event_id: str) -> str:
        with self._open().db.read() as conn:
            self._scope(conn, aid, event_id)
            stored = debrief_repo.get(conn, event_id)
        if stored is None:
            raise VersionConflict()
        return stored.id

    # --- running (inside a task) ------------------------------------------------------------

    def _load_run(
        self, debrief_id: str
    ) -> tuple[debrief_repo.StoredDebrief, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        with self._open().db.read() as conn:
            stored = debrief_repo.get_by_id(conn, debrief_id)
            if stored is None:
                raise ApplicationNotFound()
            if stored.state != "done" or debrief_repo.has_drafts(conn, debrief_id):
                raise VersionConflict()
            skills = tuple(s.name for s in skills_repo.list_skills(conn))
            offered = offered_skills(job_fit_repo.fact_records(conn), skills)
        paragraphs = _paragraphs(stored.experiences_text)
        if not paragraphs:
            raise VersionConflict()
        return stored, paragraphs, skills, offered

    async def run(self, ctx: SummaryContext) -> ResultWriter:
        parts = (ctx.input_ref or "").split("/")
        if len(parts) != 2 or not is_safe_id(parts[0]) or parts[1] not in _TIERS:
            raise ApplicationNotFound()
        debrief_id, raw_tier = parts
        stored, paragraphs, skills, offered = await asyncio.to_thread(self._load_run, debrief_id)
        await ctx.heartbeat(5)
        provider, settings = await asyncio.to_thread(self._provider)
        usage = self._usage()
        service = SERVICE_BY_PROVIDER_KIND.get(settings.kind or "")
        if usage is None or service is None:
            raise ProviderNotConfigured()
        opened = self._open()
        runner = self._factory.task_runner(provider, lambda: settings, data_dir=opened.data_dir)
        tier: Tier | None = (
            "fast" if raw_tier == "fast" else "thorough" if raw_tier == "thorough" else None
        )
        made: list[ParagraphDraft] = []
        for index, paragraph in enumerate(paragraphs):
            made.append(
                await structure_paragraph(
                    runner,
                    paragraph,
                    debrief_id=debrief_id,
                    known_skills=skills,
                    offered_skills=offered,
                    cancel=ctx.cancel_flag,
                    meter=ServiceUsageMeter(usage, service),
                    tier=tier,
                    task_id=ctx.task_id,
                )
            )
            ctx.check_cancelled()
            await ctx.heartbeat(5 + 85 * (index + 1) // len(paragraphs))
        now = self._clock.now().astimezone(UTC).isoformat()

        def write(conn: sqlite3.Connection) -> str:
            fresh = debrief_repo.get_by_id(conn, debrief_id)
            if (
                fresh is None
                or fresh.state != "done"
                or fresh.experiences_text != stored.experiences_text
                or debrief_repo.has_drafts(conn, debrief_id)
            ):
                raise VersionConflict()
            facts = FactService(opened.db, self._clock, self._ids)
            for item in made:
                origin = DraftOrigin(
                    SOURCE_DEBRIEF,
                    provider.name,
                    item.model,
                    item.prompt_id,
                    item.prompt_version,
                )
                (fact,) = facts.create_drafts_in(conn, origin, (item.draft,))
                debrief_repo.insert_draft(
                    conn,
                    draft_id=self._ids.new(),
                    debrief_id=debrief_id,
                    fact_id=fact.id,
                    now=now,
                )
            return "debrief:" + debrief_id

        return write
