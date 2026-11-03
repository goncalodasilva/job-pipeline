"""Metered, source-bound fit assessment using the one confirmed-fact selector (M4.5)."""

import asyncio
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from app.domain.clock import Clock
from app.domain.errors import FitNotAvailable, JobNotFound, ProviderNotConfigured
from app.domain.fact_selection import facts_for_ai, prompt_fact_block
from app.domain.fit import checked_conflicts, evaluate_matches, salary_below_minimum
from app.domain.ids import IdFactory, is_safe_id
from app.domain.settings import JOB_PREFERENCES, AIProviderSettings, Tier
from app.domain.tasks import TaskRef
from app.domain.tiers import task_tier
from app.domain.usage import SERVICE_BY_PROVIDER_KIND
from app.modules.ai import AIProviderFactory
from app.modules.storage import job_fit_repo
from app.services.budget_gate import BudgetGate, Start
from app.services.job_summary import ResultWriter, SummaryContext, SummaryPlan, SummaryWorkspace
from app.services.settings import SettingsService
from app.services.tasks import TaskService
from app.services.usage import UsageService
from app.services.usage_meter import ServiceUsageMeter

ASSESS_FIT: Final = "assess_fit"
FIT_LABEL: Final = "Assess fit"
NO_FACTS: Final = "Add confirmed facts to see how you match."
PRACTICE_NOTICE: Final = (
    "Practice data (fake provider). The posting and facts weren't read; "
    "this is a fictional example."
)


@dataclass(frozen=True)
class FitView:
    job_id: str
    band: str | None
    flagged: bool
    practice: bool
    active: bool
    task_status: str | None
    task_key: str | None
    error: str | None
    notice: str
    conflicts: tuple[dict[str, object], ...]


class JobFitService:
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
        gate: Callable[[], BudgetGate],
    ) -> None:
        self._factory, self._clock, self._ids = factory, clock, ids
        self._workspace, self._settings, self._tasks = workspace, settings, tasks
        self._usage, self._gate = usage, gate

    def _open(self) -> SummaryWorkspace:
        opened = self._workspace()
        if opened is None:
            raise JobNotFound()
        return opened

    def _inputs(self, job_id: str) -> job_fit_repo.FitInputs:
        if not is_safe_id(job_id):
            raise JobNotFound()
        with self._open().db.read() as conn:
            return job_fit_repo.inputs(conn, job_id)

    def plan_start(self, job_id: str, *, tier: Tier | None = None) -> SummaryPlan | None:
        source = self._inputs(job_id)
        if source.summary_id is None or not facts_for_ai(source.facts):
            return None
        settings = self._settings()
        service = SERVICE_BY_PROVIDER_KIND.get(settings.kind or "") if settings else None
        if settings is None or service is None:
            raise ProviderNotConfigured()
        return SummaryPlan(service, tier or task_tier(ASSESS_FIT, settings))

    def start(self, job_id: str, *, tier: Tier | None = None) -> TaskRef | None:
        if self.plan_start(job_id, tier=tier) is None:
            return None
        tasks = self._tasks()
        if tasks is None:
            raise JobNotFound()
        return tasks.enqueue(ASSESS_FIT, job_id, input_ref=job_id + (f"/{tier}" if tier else ""))

    def after_summary(self, job_id: str, *, unless_summary_id: str | None = None) -> None:
        source = self._inputs(job_id)
        if source.summary_id == unless_summary_id:
            return
        with self._open().db.read() as conn:
            assessed, _ = job_fit_repo.view_rows(conn, job_id, source.summary_id)
        if assessed is not None:
            return
        try:
            plan = self.plan_start(job_id)
            if plan is not None and isinstance(
                self._gate().check(plan.service, tier=plan.tier), Start
            ):
                tasks = self._tasks()
                if tasks is None:
                    return

                def eligible(conn: sqlite3.Connection) -> bool:
                    current = job_fit_repo.current_summary(conn, job_id)
                    assessed, _ = job_fit_repo.view_rows(conn, job_id, current)
                    return current == source.summary_id and assessed is None

                tasks.enqueue_if(ASSESS_FIT, job_id, input_ref=job_id, eligible=eligible)
        except ProviderNotConfigured:
            return

    async def run(self, ctx: SummaryContext) -> tuple[ResultWriter, str, str]:
        job_id, _, tier = (ctx.input_ref or "").partition("/")
        if tier not in ("", "fast", "thorough"):
            raise JobNotFound()
        await ctx.heartbeat(0)
        source = await asyncio.to_thread(self._inputs, job_id)
        facts = facts_for_ai(source.facts)
        if source.summary_id is None or not facts:
            raise FitNotAvailable()
        opened, usage = self._open(), self._usage()
        settings = await asyncio.to_thread(self._settings)
        if settings is None or usage is None:
            raise ProviderNotConfigured()
        service = SERVICE_BY_PROVIDER_KIND.get(settings.kind or "")
        if service is None:
            raise ProviderNotConfigured()
        preferences = await asyncio.to_thread(
            SettingsService(opened.db, self._clock).load, JOB_PREFERENCES
        )
        try:
            provider = self._factory.ai_provider(settings, data_dir=opened.data_dir)
        except ValueError:
            raise ProviderNotConfigured() from None
        runner = self._factory.task_runner(provider, lambda: settings, data_dir=opened.data_dir)
        await ctx.heartbeat(10)
        preferences_data = preferences.value.model_dump(mode="json") if preferences.value else {}
        for field in ("must_haves", "deal_breakers"):
            preferences_data[field] = [
                {"line": line, "text": text}
                for line, text in enumerate(preferences_data.get(field, ()), 1)
            ]
        result = await runner.run_task(
            ASSESS_FIT,
            {
                "job_context": json.dumps(
                    {
                        "posting": source.job["snapshot_text"],
                        "requirements": [
                            {"requirement_id": item.id, "kind": item.kind, "text": item.text}
                            for item in source.requirements
                        ],
                    },
                    ensure_ascii=False,
                ),
                "facts": prompt_fact_block(facts),
                "preferences": json.dumps(preferences_data, ensure_ascii=False),
                "job_id": job_id,
            },
            ctx.cancel_flag,
            meter=ServiceUsageMeter(usage, service),
            tier="fast" if tier == "fast" else "thorough" if tier == "thorough" else None,
            task_id=ctx.task_id,
        )
        ctx.check_cancelled()
        refs = tuple(str(fact.ref) for fact in facts)
        checked = evaluate_matches(source.requirements, result.data["requirements"], refs)
        conflicts = checked_conflicts(
            result.data["conflicts"], preferences.value, source.job["snapshot_text"]
        )
        salary = salary_below_minimum(
            source.job["salary_min"],
            source.job["salary_max"],
            source.job["salary_currency"],
            source.job["salary_period"],
            preferences.value.minimum_salary if preferences.value else None,
        )
        data = {
            **checked.result(),
            "conflicts": conflicts,
            "salary_conflict": salary,
            "preferences_version": preferences.version,
        }
        assessment_id = self._ids.new()
        summary_id = source.summary_id

        def write(conn: sqlite3.Connection) -> str:
            return job_fit_repo.store(
                conn,
                id=assessment_id,
                job_id=job_id,
                summary_id=summary_id,
                result=data,
                band=checked.band,
                flagged=bool(conflicts) or salary,
                fact_refs=refs,
                provider=provider.name,
                model=result.model,
                prompt_id=result.prompt_id,
                prompt_version=result.prompt_version,
                task_id=ctx.task_id,
                now=self._clock.now().isoformat(),
            )

        return write, job_id, summary_id

    def view(self, job_id: str) -> FitView:
        source = self._inputs(job_id)
        with self._open().db.read() as conn:
            row, task = job_fit_repo.view_rows(conn, job_id, source.summary_id)
        notice = (
            "Summarize the job first."
            if source.summary_id is None
            else (NO_FACTS if not facts_for_ai(source.facts) else "")
        )
        return FitView(
            job_id,
            row[0] if row else None,
            bool(row and row[1]),
            bool(row and row[3] == "fake"),
            bool(task and task[1] in ("queued", "running")),
            task[1] if task else None,
            f"{task[0]}:{task[3]}" if task else None,
            task[2] if task else None,
            notice,
            tuple(json.loads(row[2]).get("conflicts", ())) if row else (),
        )
