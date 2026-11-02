"""Company brief (M8.3 part B): a read-only view and an explicit, metered, web-searched start.

A brief is a few short sections, each with one source the search returned. Reads change
nothing and ask nothing. The search provider's storage rule decides where a result lives:
allowed, one row in ``company_briefs``; unclear or forbidden, nothing derived is written and the
result is held only in the bounded process-local store. Company text is untrusted data and is
never logged or put in a task row, usage row, event or error.
"""

import asyncio
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo

from app.domain.clock import Clock
from app.domain.company_brief import BriefSection
from app.domain.errors import ProviderNotConfigured, VersionConflict, format_day
from app.domain.ids import IdFactory, is_safe_id
from app.domain.settings import AIProviderSettings, Tier
from app.domain.tasks import TaskRef
from app.domain.usage import SERVICE_BY_PROVIDER_KIND
from app.modules.ai import AIProvider, AIProviderFactory
from app.modules.search import SearchProvider, TransientSearchResults
from app.modules.storage import company_briefs_repo as repo
from app.services.company_brief_ai import COMPANY_BRIEF, BriefContext, BriefOutput, company_brief
from app.services.job_summary import ResultWriter, SummaryContext, SummaryPlan, SummaryWorkspace
from app.services.tasks import TaskService
from app.services.usage import UsageService
from app.services.usage_meter import ServiceUsageMeter

BRIEF = COMPANY_BRIEF
BRIEF_LABEL = "Company brief"
UNCLEAR_STORAGE = "Not saved: this search provider's terms on keeping results are unclear"
FORBIDDEN_STORAGE = "Not saved: this search provider does not allow keeping results"
# The result_ref of a run that kept no section; the page says so in plain words.
NOTHING_KEPT = "none"


@dataclass(frozen=True)
class Brief:
    sections: tuple[BriefSection, ...]
    sources: tuple[str, ...]
    found_at: str


@dataclass(frozen=True)
class BriefView:
    company_id: str
    company: str
    version: int
    brief: Brief | None
    transient: Brief | None
    storage_notice: str
    search_provider: SearchProvider | None
    tasks: tuple[tuple[str, str, str | None], ...]
    found_day: str = ""  # the day of the brief on screen: the held one when there is one
    nothing_kept: bool = False


@dataclass(frozen=True)
class _Held:
    output: BriefOutput
    provider: str
    found_at: str
    storage_allowed: bool | None
    # name, website and industry the run was about; a held result is shown only while they hold.
    company_key: tuple[str, str, str]


class BriefService:
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
            raise LookupError
        return opened

    def _company(self, company_id: str) -> repo.CompanyFields:
        if not is_safe_id(company_id):
            raise LookupError
        with self._open().db.read() as conn:
            found = repo.company(conn, company_id)
        if found is None:
            raise LookupError
        return found

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
        except (LookupError, ProviderNotConfigured):
            return None

    def plan_start(self, company_id: str, *, tier: Tier | None = None) -> SummaryPlan | None:
        self._company(company_id)
        provider, settings = self._provider()
        if self._search(provider) is None:
            return None
        service = SERVICE_BY_PROVIDER_KIND.get(settings.kind or "")
        if service is None:
            raise ProviderNotConfigured()
        return SummaryPlan(service, tier or settings.task_tiers.get(BRIEF, "thorough"))

    def view(self, company_id: str) -> BriefView:
        company = self._company(company_id)
        with self._open().db.read() as conn:
            saved = repo.latest(conn, company_id)
            task = repo.latest_task(conn, BRIEF, company_id)
        brief = Brief(saved.sections, saved.sources, saved.found_at) if saved else None
        held = self._transient.get("brief:" + company_id)
        transient: Brief | None = None
        notice = ""
        # The newest brief is the one on screen. A held result older than a saved one (the
        # storage rule changed since) is not shown, and neither is one about a company whose
        # name, website or industry has changed since the run.
        if (
            isinstance(held, _Held)
            and held.company_key == (company.name, company.website, company.industry)
            and not (saved and saved.found_at >= held.found_at)
        ):
            transient = Brief(held.output.sections, held.output.sources, held.found_at)
            notice = UNCLEAR_STORAGE if held.storage_allowed is None else FORBIDDEN_STORAGE
        shown = transient or brief
        return BriefView(
            company_id,
            company.name,
            company.version,
            brief,
            transient,
            notice,
            self.search_info(),
            ((BRIEF, task.status, task.error),) if task else (),
            self._day(shown.found_at) if shown else "",
            bool(task and task.status == "succeeded" and task.result_ref == NOTHING_KEPT),
        )

    def _day(self, stamp: str) -> str:
        try:
            return format_day(datetime.fromisoformat(stamp).astimezone(self._tz).date())
        except ValueError:
            return ""

    def start(self, company_id: str, *, version: int, tier: Tier | None = None) -> TaskRef:
        if self._company(company_id).version != version:
            raise VersionConflict()
        if self.plan_start(company_id, tier=tier) is None:
            raise ProviderNotConfigured()
        tasks = self._tasks()
        if tasks is None:
            raise LookupError
        return tasks.enqueue(BRIEF, company_id, input_ref=company_id + "/" + (tier or "default"))

    async def run(self, ctx: SummaryContext) -> tuple[ResultWriter, _Held | None, str]:
        parts = (ctx.input_ref or "").split("/")
        if len(parts) != 2 or parts[1] not in {"default", "fast", "thorough"}:
            raise LookupError
        company_id, raw_tier = parts
        loaded = await asyncio.to_thread(self._company, company_id)
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
        policy = self._search(provider)
        if policy is None:
            raise ProviderNotConfigured()
        output = await company_brief(
            runner,
            BriefContext(company_id, loaded.name, loaded.website, loaded.industry),
            ctx.cancel_flag,
            meter=meter,
            tier=tier,
            task_id=ctx.task_id,
        )
        await ctx.heartbeat(80)
        now = self._clock.now().isoformat()
        kept = bool(output.sections)
        held = (
            _Held(
                output,
                provider.name,
                now,
                policy.storage_allowed,
                (loaded.name, loaded.website, loaded.industry),
            )
            if kept and policy.storage_allowed is not True
            else None
        )

        def write(conn: sqlite3.Connection) -> str:
            fresh = repo.company(conn, company_id)
            if fresh is None or (fresh.name, fresh.website, fresh.industry) != (
                loaded.name,
                loaded.website,
                loaded.industry,
            ):
                raise VersionConflict()
            if not kept:
                return NOTHING_KEPT
            if held is None:
                repo.insert(
                    conn,
                    brief_id=self._ids.new(),
                    company_id=company_id,
                    sections=output.sections,
                    sources=output.sources,
                    found_at=now,
                    provider=provider.name,
                    model=output.model,
                    prompt_id=output.prompt_id,
                    prompt_version=output.prompt_version,
                )
            return company_id

        return write, held, company_id

    def remember(self, company_id: str, held: _Held | None) -> None:
        if held is not None:
            self._transient.put("brief:" + company_id, held)
