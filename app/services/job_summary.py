"""Summarize stored job text through the metered runner; save results only at completion."""

import asyncio
import json
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from app.domain.clock import Clock
from app.domain.errors import JobNotFound, ProviderNotConfigured
from app.domain.ids import IdFactory, is_safe_id
from app.domain.job_summary import checked_summary
from app.domain.settings import AIProviderSettings, Tier
from app.domain.tasks import TaskRef
from app.domain.tiers import task_tier
from app.domain.usage import SERVICE_BY_PROVIDER_KIND, ServiceId
from app.modules.ai import AIProviderFactory, CancelToken
from app.modules.storage import Database, job_summary_repo
from app.services.tasks import TaskService
from app.services.usage import UsageService
from app.services.usage_meter import ServiceUsageMeter

SUMMARIZE_JOB: Final = "summarize_job"
SUMMARIZE_LABEL: Final = "Summarize job"
PRACTICE_NOTICE: Final = (
    "Practice data (fake provider). The posting wasn't read; this is a fictional example."
)
ResultWriter = Callable[[sqlite3.Connection], str]


class SummaryWorkspace(Protocol):
    @property
    def db(self) -> Database: ...
    @property
    def data_dir(self) -> Path: ...


class SummaryContext(Protocol):
    @property
    def task_id(self) -> str: ...
    @property
    def input_ref(self) -> str | None: ...
    @property
    def cancel_flag(self) -> CancelToken: ...
    def heartbeat(self, progress: int | None = None) -> Awaitable[None]: ...
    def check_cancelled(self) -> None: ...


@dataclass(frozen=True)
class SummaryPlan:
    service: ServiceId
    tier: Tier


@dataclass(frozen=True)
class SummaryView:
    job_id: str
    text: str
    practice: bool
    task_id: str | None
    active: bool
    error: str | None
    task_key: str | None
    task_status: str | None


class JobSummaryService:
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
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._ids = ids
        self._workspace = workspace
        self._settings = settings
        self._tasks = tasks
        self._usage = usage

    def _open(self) -> SummaryWorkspace:
        opened = self._workspace()
        if opened is None:
            raise JobNotFound()
        return opened

    def _job(self, job_id: str) -> dict[str, object]:
        if not is_safe_id(job_id):
            raise JobNotFound()
        with self._open().db.read() as conn:
            return job_summary_repo.job(conn, job_id)

    def _provider(self) -> tuple[AIProviderSettings, ServiceId]:
        settings = self._settings()
        service = SERVICE_BY_PROVIDER_KIND.get(settings.kind or "") if settings else None
        if settings is None or service is None:
            raise ProviderNotConfigured()
        return settings, service

    def plan_start(self, job_id: str, *, tier: Tier | None = None) -> SummaryPlan:
        self._job(job_id)
        settings, service = self._provider()
        return SummaryPlan(service, tier or task_tier(SUMMARIZE_JOB, settings))

    def start(self, job_id: str, *, tier: Tier | None = None) -> TaskRef:
        self.plan_start(job_id, tier=tier)
        tasks = self._tasks()
        if tasks is None:
            raise JobNotFound()
        return tasks.enqueue(SUMMARIZE_JOB, job_id, input_ref=job_id + (f"/{tier}" if tier else ""))

    async def run(self, ctx: SummaryContext) -> ResultWriter:
        job_id, _, tier = (ctx.input_ref or "").partition("/")
        if tier not in ("", "fast", "thorough"):
            raise JobNotFound()
        await ctx.heartbeat(0)
        current = await asyncio.to_thread(self._job, job_id)
        opened = self._open()
        settings, service = await asyncio.to_thread(self._provider)
        usage = self._usage()
        if usage is None:
            raise JobNotFound()
        try:
            provider = self._factory.ai_provider(settings, data_dir=opened.data_dir)
        except ValueError:
            raise ProviderNotConfigured() from None
        runner = self._factory.task_runner(provider, lambda: settings, data_dir=opened.data_dir)

        def lists() -> dict[str, dict[str, list[str]]]:
            with opened.db.read() as conn:
                return job_summary_repo.controlled(conn)

        controlled = await asyncio.to_thread(lists)
        await ctx.heartbeat(10)
        result = await runner.run_task(
            SUMMARIZE_JOB,
            {
                "posting_context": json.dumps(
                    {"job_text": current["snapshot_text"], "controlled_lists": controlled},
                    ensure_ascii=False,
                ),
                "job_id": job_id,
            },
            ctx.cancel_flag,
            meter=ServiceUsageMeter(usage, service),
            tier="fast" if tier == "fast" else "thorough" if tier == "thorough" else None,
            task_id=ctx.task_id,
        )
        ctx.check_cancelled()
        data = checked_summary(result.data, str(current["snapshot_text"]))
        summary_id = self._ids.new()

        def write(conn: sqlite3.Connection) -> str:
            return job_summary_repo.store(
                conn,
                job_id=job_id,
                task_id=ctx.task_id,
                data=data,
                summary_id=summary_id,
                provider=provider.name,
                model=result.model,
                prompt_id=result.prompt_id,
                prompt_version=result.prompt_version,
                ids=self._ids,
                now=self._clock.now().isoformat(),
            )

        return write

    def view(self, job_id: str) -> SummaryView:
        self._job(job_id)
        with self._open().db.read() as conn:
            summary, task = job_summary_repo.view_rows(conn, job_id)
        return SummaryView(
            job_id,
            summary[0] if summary else "",
            bool(summary and summary[1] == "fake"),
            task[0] if task else None,
            bool(task and task[1] in ("queued", "running")),
            task[2] if task else None,
            f"{task[0]}:{task[3]}" if task else None,
            task[1] if task else None,
        )
