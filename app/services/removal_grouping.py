"""Explicit, metered grouping of the current free-text removal notes."""

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import tzinfo

from app.domain.clock import Clock
from app.domain.errors import JobNotFound, ProviderNotConfigured, VersionConflict
from app.domain.jobs import local_today
from app.domain.removal_learning import JobSignals, resolve_removal_themes
from app.domain.settings import AIProviderSettings, Tier
from app.domain.tasks import GROUP_REMOVAL_REASONS, TaskRef
from app.domain.tiers import task_tier
from app.domain.usage import SERVICE_BY_PROVIDER_KIND
from app.modules.ai import AIProviderFactory
from app.modules.storage import removal_learning_repo as repo
from app.services.job_summary import ResultWriter, SummaryContext, SummaryPlan, SummaryWorkspace
from app.services.tasks import TaskService
from app.services.usage import UsageService
from app.services.usage_meter import ServiceUsageMeter

GROUP_LABEL = "Group removal notes"


@dataclass(frozen=True)
class GroupingView:
    available: bool = False
    input_version: str = ""
    status: str = ""
    error: str | None = None
    practice: bool = False

    @property
    def active(self) -> bool:
        return self.status in ("queued", "running")


def _notes_version(notes: tuple[repo.RemovalNote, ...]) -> str:
    refs = [(note.job_id, note.removal_version) for note in notes]
    return hashlib.sha256(json.dumps(refs).encode()).hexdigest()


class RemovalGrouping:
    def __init__(
        self,
        *,
        factory: AIProviderFactory,
        clock: Clock,
        zone: tzinfo,
        workspace: Callable[[], SummaryWorkspace | None],
        settings: Callable[[], AIProviderSettings | None],
        tasks: Callable[[], TaskService | None],
        usage: Callable[[], UsageService | None],
    ) -> None:
        self._factory, self._clock, self._zone = factory, clock, zone
        self._workspace, self._settings, self._tasks, self._usage = (
            workspace,
            settings,
            tasks,
            usage,
        )

    def view(self) -> GroupingView:
        opened = self._workspace()
        if opened is None:
            return GroupingView()
        with opened.db.read() as conn:
            notes = repo.removal_notes(conn)
            status, error = repo.grouping_task(conn, GROUP_REMOVAL_REASONS) or ("", None)
            result = repo.latest_theme_run(conn)
        return GroupingView(
            len(notes) >= 2,
            _notes_version(notes),
            status,
            error,
            bool(result and result.provider == "fake"),
        )

    def plan(self, tier: Tier | None = None) -> SummaryPlan:
        settings = self._settings()
        service = SERVICE_BY_PROVIDER_KIND.get(settings.kind or "") if settings else None
        if settings is None or service is None:
            raise ProviderNotConfigured()
        return SummaryPlan(service, tier or task_tier(GROUP_REMOVAL_REASONS, settings))

    def start(self, plan: SummaryPlan, input_version: str) -> TaskRef:
        tasks = self._tasks()
        if tasks is None:
            raise JobNotFound()
        view = self.view()
        if not view.available or view.input_version != input_version:
            raise VersionConflict()
        return tasks.enqueue(
            GROUP_REMOVAL_REASONS, "inbox", input_ref=f"{plan.service}/{plan.tier}/{input_version}"
        )

    def _inputs(
        self,
        opened: SummaryWorkspace,
        version: str,
    ) -> tuple[tuple[repo.RemovalNote, ...], tuple[JobSignals, ...]]:
        with opened.db.read() as conn:
            notes = repo.removal_notes(conn)
            if len(notes) < 2 or _notes_version(notes) != version:
                raise VersionConflict()
            jobs = repo.signals(conn, local_today(self._clock.now(), self._zone))
        return notes, jobs

    async def run(self, ctx: SummaryContext) -> ResultWriter:
        service, _, rest = (ctx.input_ref or "").partition("/")
        tier, _, version = rest.partition("/")
        if tier not in ("fast", "thorough"):
            raise VersionConflict()
        chosen: Tier = "fast" if tier == "fast" else "thorough"
        opened, usage = self._workspace(), self._usage()
        settings = await asyncio.to_thread(self._settings)
        if opened is None or usage is None or settings is None:
            raise ProviderNotConfigured()
        if SERVICE_BY_PROVIDER_KIND.get(settings.kind or "") != service:
            raise ProviderNotConfigured()
        await ctx.heartbeat(0)
        notes, jobs = await asyncio.to_thread(self._inputs, opened, version)
        try:
            provider = self._factory.ai_provider(settings, data_dir=opened.data_dir)
        except ValueError:
            raise ProviderNotConfigured() from None
        runner = self._factory.task_runner(provider, lambda: settings, data_dir=opened.data_dir)
        await ctx.heartbeat(10)
        result = await runner.run_task(
            GROUP_REMOVAL_REASONS,
            {
                "reasons": json.dumps(
                    [{"job_id": note.job_id, "text": note.text} for note in notes],
                    ensure_ascii=False,
                )
            },
            ctx.cancel_flag,
            meter=ServiceUsageMeter(usage, service),
            tier=chosen,
            task_id=ctx.task_id,
        )
        ctx.check_cancelled()
        versions = {note.job_id: note.removal_version for note in notes}
        checked = resolve_removal_themes(result.data["themes"], frozenset(versions), jobs)
        output = repo.ThemeRun(
            tuple(
                repo.Theme(condition, tuple((id, versions[id]) for id in ids))
                for condition, ids in checked
            ),
            provider.name,
            result.model,
            result.prompt_id,
            result.prompt_version,
        )

        def write(conn: sqlite3.Connection) -> str:
            return repo.store_theme_run(conn, ctx.task_id, output, self._clock.now().isoformat())

        return write
