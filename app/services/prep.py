"""Interview preparation: read-only views and explicit, metered question starts."""

import asyncio
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from urllib.parse import urlsplit

from app.domain.clock import Clock
from app.domain.coverage import CoverageSkill
from app.domain.errors import (
    ApplicationNotFound,
    ProviderNotConfigured,
    VersionConflict,
    format_day,
)
from app.domain.fit import Requirement
from app.domain.ids import IdFactory, is_safe_id
from app.domain.question_bank import (
    QUESTION_TYPES,
    BankQuestion,
    BankSelection,
    QuestionDraft,
    rank_questions,
    role_family,
)
from app.domain.settings import AIProviderSettings, Tier
from app.domain.tasks import TaskRef
from app.domain.usage import SERVICE_BY_PROVIDER_KIND
from app.modules.ai import AIProvider, AIProviderFactory
from app.modules.search import SearchProvider, TransientSearchResults
from app.modules.storage import prep_repo, skills_repo
from app.services.job_summary import ResultWriter, SummaryContext, SummaryPlan, SummaryWorkspace
from app.services.prep_ai import (
    CompanyContext,
    InterviewContext,
    QuestionOutput,
    interview_questions,
    interview_web_questions,
)
from app.services.tasks import TaskService
from app.services.usage import UsageService
from app.services.usage_meter import ServiceUsageMeter

QUESTIONS = "interview_questions"
WEB_QUESTIONS = "interview_web_questions"
UNCLEAR_STORAGE = "Not saved: this search provider's terms on keeping results are unclear"
FORBIDDEN_STORAGE = "Not saved: this search provider does not allow keeping results"


@dataclass(frozen=True)
class QuestionRow:
    question: BankQuestion
    item_id: str | None
    version: int
    ready: bool
    practiced: bool


@dataclass(frozen=True)
class PrepView:
    application_id: str
    version: int
    title: str
    company: str
    questions: tuple[QuestionRow, ...]
    already_prepared: tuple[QuestionRow, ...]
    bank: tuple[BankQuestion, ...]
    uncovered_count: int
    cv_source: str
    has_summary: bool
    tasks: tuple[tuple[str, str, str | None], ...]
    search_provider: SearchProvider | None
    transient: tuple[BankQuestion, ...]
    storage_notice: str
    source_labels: Mapping[str, str]
    item_count: int = 0

    @property
    def has_items(self) -> bool:
        return self.item_count > 0


@dataclass(frozen=True)
class _Loaded:
    source: prep_repo.PrepSource
    bank: tuple[BankQuestion, ...]
    items: tuple[prep_repo.PrepItem, ...]
    skills: tuple[CoverageSkill, ...]
    generated_here: frozenset[str]

    def selection(self, regenerate: bool = False) -> BankSelection:
        protected = {i.question_id for i in self.items if i.ready or i.practiced}
        excluded = (self.generated_here | {i.question_id for i in self.items}) - protected
        bank = tuple(
            q
            for q in self.bank
            if not (regenerate and q.origin == "generated" and q.id in excluded)
        )
        return rank_questions(
            bank,
            self.source.requirements,
            skills=self.skills,
            company_id=self.source.company_id,
            title=self.source.title,
            ready_ids={i.question_id for i in self.items if i.ready},
        )


@dataclass(frozen=True)
class _Transient:
    output: QuestionOutput
    provider: str
    found_at: str
    storage_allowed: bool | None


class PrepService:
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
        search: Callable[[AIProvider], SearchProvider | None],
        search_settings: Callable[[AIProviderSettings], SearchProvider | None],
        transient: TransientSearchResults,
        tz: tzinfo = UTC,
    ) -> None:
        self._factory, self._clock, self._ids = factory, clock, ids
        self._workspace, self._settings, self._tasks, self._usage = (
            workspace,
            settings,
            tasks,
            usage,
        )
        self._search, self._transient = search, transient
        self._search_settings = search_settings
        self._tz = tz

    def _open(self) -> SummaryWorkspace:
        opened = self._workspace()
        if opened is None:
            raise ApplicationNotFound()
        return opened

    def _load_in(self, conn: sqlite3.Connection, aid: str) -> _Loaded:
        source = prep_repo.source(conn, aid)
        if source is None:
            raise ApplicationNotFound()
        return _Loaded(
            source,
            prep_repo.questions(conn),
            prep_repo.items(conn, aid),
            tuple(
                CoverageSkill(s.name, tuple(json.loads(s.aliases)))
                for s in skills_repo.list_skills(conn)
            ),
            prep_repo.generated_here(conn, aid),
        )

    def _load(self, aid: str) -> _Loaded:
        if not is_safe_id(aid):
            raise ApplicationNotFound()
        with self._open().db.read() as conn:
            return self._load_in(conn, aid)

    def _provider(self) -> tuple[AIProvider, AIProviderSettings]:
        settings = self._settings()
        if settings is None or settings.kind is None:
            raise ProviderNotConfigured()
        try:
            provider = self._factory.ai_provider(settings, data_dir=self._open().data_dir)
        except ValueError:
            raise ProviderNotConfigured() from None
        return provider, settings

    def search_info(self) -> SearchProvider | None:
        try:
            return self._search(self._provider()[0])
        except (ApplicationNotFound, ProviderNotConfigured):
            return None

    def settings_search_info(self) -> SearchProvider | None:
        settings = self._settings()
        return self._search_settings(settings) if settings else None

    def plan_start(
        self, aid: str, *, web: bool = False, action: str = "generate", tier: Tier | None = None
    ) -> SummaryPlan | None:
        loaded = self._load(aid)
        if web and not loaded.source.company:
            return None
        if not web and not self._needs_request(loaded, action):
            return None
        provider, settings = self._provider()
        if web and self._search(provider) is None:
            return None
        service = SERVICE_BY_PROVIDER_KIND.get(settings.kind or "")
        if service is None:
            raise ProviderNotConfigured()
        task = WEB_QUESTIONS if web else QUESTIONS
        return SummaryPlan(service, tier or settings.task_tiers.get(task, "thorough"))

    @staticmethod
    def _key_requirements(loaded: _Loaded) -> tuple[Requirement, ...]:
        return tuple(r for r in loaded.source.requirements if r.kind in {"must_have", "key"})

    def _needs_request(self, loaded: _Loaded, action: str) -> bool:
        """Generate and Regenerate need uncovered key requirements; Show more a summary only."""
        if not loaded.source.summary_id:
            return False
        if action == "more":
            return bool(self._key_requirements(loaded))
        return bool(loaded.selection(action == "regenerate").uncovered)

    def view(self, aid: str, *, query: str = "", question_type: str = "") -> PrepView:
        loaded = self._load(aid)
        selection, items = loaded.selection(), {i.question_id: i for i in loaded.items}
        by_id = {q.id: q for q in loaded.bank}
        ordered = [r.question for r in selection.ranked]
        ordered = [q for q in ordered if q.id in items] if loaded.items else ordered[:10]
        ordered += [
            by_id[i.question_id]
            for i in loaded.items
            if i.question_id in by_id
            and not i.ready
            and i.question_id not in {q.id for q in ordered}
        ]

        def row(q: BankQuestion) -> QuestionRow:
            item = items.get(q.id)
            return QuestionRow(
                q,
                item.id if item else None,
                item.version if item else 0,
                item.ready if item else False,
                item.practiced if item else False,
            )

        checked_type = question_type if question_type in QUESTION_TYPES else ""
        with self._open().db.read() as conn:
            bank = tuple(
                q
                for q in prep_repo.questions(conn, query=query, question_type=checked_type)
                if q.company_id is None or q.company_id == loaded.source.company_id
            )[:20]
            task_rows = tuple(
                t
                for kind in (QUESTIONS, WEB_QUESTIONS)
                if (t := prep_repo.latest_task(conn, aid, kind)) is not None
            )
        transient = self._transient.get("questions:" + aid)
        ephemeral: tuple[BankQuestion, ...] = ()
        notice = ""
        if isinstance(transient, _Transient):
            ephemeral = tuple(
                self._candidate(
                    draft,
                    f"transient-{i}",
                    loaded.source,
                    transient.output,
                    transient.provider,
                    transient.found_at,
                    web=True,
                )
                for i, draft in enumerate(transient.output.questions)
            )
            notice = UNCLEAR_STORAGE if transient.storage_allowed is None else FORBIDDEN_STORAGE
        return PrepView(
            aid,
            loaded.source.version,
            loaded.source.title,
            loaded.source.company,
            tuple(row(q) for q in ordered if not checked_type or q.type == checked_type),
            tuple(
                row(r.question)
                for r in selection.already_prepared
                if not checked_type or r.question.type == checked_type
            ),
            bank,
            len(selection.uncovered),
            loaded.source.cv_source,
            loaded.source.summary_id is not None,
            task_rows,
            self.search_info(),
            ephemeral,
            notice,
            {q.id: self._source_label(q) for q in (*loaded.bank, *ephemeral) if q.source_url},
            len(loaded.items),
        )

    def _source_label(self, q: BankQuestion) -> str:
        host = urlsplit(q.source_url or "").hostname or "source"
        try:
            found = format_day(datetime.fromisoformat(q.found_at or "").astimezone(self._tz).date())
        except ValueError:
            found = "date unavailable"
        return f"Reported online · {host} · found {found}"

    def start_questions(
        self,
        aid: str,
        *,
        version: int,
        action: str = "generate",
        confirmed: bool = False,
        tier: Tier | None = None,
    ) -> TaskRef | None:
        if action not in {"generate", "more", "regenerate"}:
            raise ValueError("Unknown question action.")
        if action == "regenerate" and not confirmed:
            raise ValueError("Confirm regeneration first.")
        loaded = self._load(aid)
        if loaded.source.version != version:
            raise VersionConflict()
        if not self._needs_request(loaded, action):
            with self._open().db.transaction() as conn:
                fresh = self._load_in(conn, aid)
                if fresh.source.version != version:
                    raise VersionConflict()
                before = conn.total_changes
                if action == "regenerate":
                    prep_repo.remove_unpracticed_generated(conn, aid)
                self._reuse(conn, fresh, regenerate=action == "regenerate")
                if conn.total_changes != before:
                    prep_repo.changed(
                        conn,
                        aid,
                        self._clock.now().isoformat(),
                        expected_version=fresh.source.version,
                    )
            return None
        self.plan_start(aid, action=action, tier=tier)
        tasks = self._tasks()
        if tasks is None:
            raise ApplicationNotFound()
        return tasks.enqueue(
            QUESTIONS, aid, input_ref=aid + "/" + action + "/" + (tier or "default")
        )

    def start_web(self, aid: str, *, version: int, tier: Tier | None = None) -> TaskRef:
        if self._load(aid).source.version != version:
            raise VersionConflict()
        if self.plan_start(aid, web=True, tier=tier) is None:
            raise ProviderNotConfigured()
        tasks = self._tasks()
        if tasks is None:
            raise ApplicationNotFound()
        return tasks.enqueue(WEB_QUESTIONS, aid, input_ref=aid + "/web/" + (tier or "default"))

    def _reuse(
        self, conn: sqlite3.Connection, loaded: _Loaded, *, regenerate: bool = False
    ) -> None:
        now = self._clock.now().isoformat()
        for ranked in loaded.selection(regenerate).ranked[:10]:
            prep_repo.add_item(
                conn,
                loaded.source.application_id,
                ranked.question.id,
                item_id=self._ids.new(),
                now=now,
            )

    def add_bank_question(
        self,
        text: str,
        question_type: str,
        *,
        requirements: tuple[str, ...] = (),
        company_id: str | None = None,
    ) -> BankQuestion:
        if not text.strip() or len(text) > 1000 or question_type not in QUESTION_TYPES:
            raise ValueError("Invalid bank question.")
        question = BankQuestion(
            self._ids.new(),
            text.strip(),
            question_type,
            requirements,
            company_id=company_id,
            origin="user",
        )
        with self._open().db.transaction() as conn:
            return prep_repo.insert_question(
                conn, question, sighting_id=self._ids.new(), now=self._clock.now().isoformat()
            )

    def mark_ready(self, aid: str, qid: str, *, ready: bool, version: int) -> None:
        self._load(aid)
        with self._open().db.transaction() as conn:
            loaded = self._load_in(conn, aid)
            question = next(
                (
                    q
                    for q in loaded.bank
                    if q.id == qid
                    and (q.company_id is None or q.company_id == loaded.source.company_id)
                ),
                None,
            )
            if question is None:
                raise ApplicationNotFound()
            item = next((i for i in loaded.items if i.question_id == qid), None)
            if (item.version if item else 0) != version:
                raise VersionConflict()
            now = self._clock.now().isoformat()
            if item is None:
                if not loaded.items:
                    # The first item turns the shown reuse candidates into items, so marking
                    # one ready doesn't hide the others.
                    self._reuse(conn, loaded)
                prep_repo.add_item(conn, aid, qid, item_id=self._ids.new(), now=now)
                item = next(i for i in prep_repo.items(conn, aid) if i.question_id == qid)
            prep_repo.mark_ready(
                conn, item.id, aid, ready=ready, now=now, expected_version=item.version
            )
            prep_repo.changed(conn, aid, now, expected_version=loaded.source.version)

    @staticmethod
    def _candidate(
        draft: QuestionDraft,
        id: str,
        source: prep_repo.PrepSource,
        output: QuestionOutput,
        provider: str,
        now: str,
        *,
        web: bool,
    ) -> BankQuestion:
        return BankQuestion(
            id,
            draft.text,
            draft.type,
            draft.requirements,
            company_id=source.company_id if draft.company_specific else None,
            role_family=role_family(source.title),
            seniority=source.seniority,
            origin="reported_online" if web else "generated",
            source_url=draft.source_url,
            source_date=draft.source_date,
            found_at=now if web else None,
            provider=provider,
            model=output.model,
            prompt_id=output.prompt_id,
            prompt_version=output.prompt_version,
        )

    async def run(
        self, ctx: SummaryContext, *, web: bool = False
    ) -> tuple[ResultWriter, _Transient | None, str]:
        parts = (ctx.input_ref or "").split("/")
        if (
            len(parts) != 3
            or parts[1] not in ({"web"} if web else {"generate", "more", "regenerate"})
            or parts[2] not in {"default", "fast", "thorough"}
        ):
            raise ApplicationNotFound()
        aid, action, raw_tier = parts
        loaded = await asyncio.to_thread(self._load, aid)
        await ctx.heartbeat(10)
        provider, settings = await asyncio.to_thread(self._provider)
        usage = self._usage()
        service = SERVICE_BY_PROVIDER_KIND.get(settings.kind or "")
        if usage is None or service is None:
            raise ProviderNotConfigured()
        runner = self._factory.task_runner(
            provider, lambda: settings, data_dir=self._open().data_dir
        )
        meter = ServiceUsageMeter(usage, service)
        tier: Tier | None = (
            "fast" if raw_tier == "fast" else "thorough" if raw_tier == "thorough" else None
        )
        policy = self._search(provider) if web else None
        if web and policy is None:
            raise ProviderNotConfigured()
        output: QuestionOutput | None
        if web:
            output = await interview_web_questions(
                runner,
                CompanyContext(
                    aid, loaded.source.company, loaded.source.title, loaded.source.industry
                ),
                ctx.cancel_flag,
                meter=meter,
                tier=tier,
                task_id=ctx.task_id,
            )
        else:
            selected = loaded.selection(action == "regenerate")
            texts = {q.id: q.text for q in loaded.bank}
            shown = (
                tuple(texts[i.question_id] for i in loaded.items if i.question_id in texts)
                if action == "more"
                else ()
            )
            output = await interview_questions(
                runner,
                InterviewContext(
                    aid,
                    loaded.source.summary,
                    (selected.uncovered or self._key_requirements(loaded))
                    if action == "more"
                    else selected.uncovered,
                    loaded.source.cv_text,
                    shown,
                ),
                ctx.cancel_flag,
                meter=meter,
                count=5 if action == "more" else 10,
                ask_count=0 if action == "more" else 3,
                known_skills=tuple(s.name for s in loaded.skills),
                tier=tier,
                task_id=ctx.task_id,
            )
        await ctx.heartbeat(80)
        now = self._clock.now().isoformat()
        ephemeral = (
            _Transient(output, provider.name, now, policy.storage_allowed)
            if output is not None and policy is not None and policy.storage_allowed is not True
            else None
        )

        def write(conn: sqlite3.Connection) -> str:
            fresh = self._load_in(conn, aid)
            if (
                fresh.source.summary_id,
                fresh.source.cv_version_id,
                fresh.source.cv_text,
                fresh.source.company,
                fresh.source.title,
                fresh.source.industry,
                fresh.source.seniority,
            ) != (
                loaded.source.summary_id,
                loaded.source.cv_version_id,
                loaded.source.cv_text,
                loaded.source.company,
                loaded.source.title,
                loaded.source.industry,
                loaded.source.seniority,
            ):
                raise VersionConflict()
            if ephemeral is not None:
                return aid
            before = conn.total_changes
            if action == "regenerate":
                prep_repo.remove_unpracticed_generated(conn, aid)
            if not web:
                self._reuse(conn, fresh, regenerate=action == "regenerate")
            if output is not None:
                for draft in output.questions:
                    if draft.company_specific and not fresh.source.company_id:
                        continue  # nothing to scope it to: it would become a global question
                    candidate = self._candidate(
                        draft, self._ids.new(), fresh.source, output, provider.name, now, web=web
                    )
                    question = prep_repo.insert_question(
                        conn,
                        candidate,
                        sighting_id=self._ids.new(),
                        now=now,
                        application_id=aid,
                        task_id=ctx.task_id,
                    )
                    prep_repo.add_item(conn, aid, question.id, item_id=self._ids.new(), now=now)
            if conn.total_changes != before:
                prep_repo.changed(conn, aid, now, expected_version=fresh.source.version)
            return aid

        return write, ephemeral, aid

    def remember(self, aid: str, result: _Transient | None) -> None:
        if result is not None:
            self._transient.put("questions:" + aid, result)
