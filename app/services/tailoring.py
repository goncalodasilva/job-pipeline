"""The workspace's Tailoring tab, steps 1 and 2 (M6.5): the gap check and the angle.

Starts go through the same task machinery and budget gate as the fit (``job_fit.py``): the
route asks the gate, this service enqueues one task per application and step (dedupe
``gap_check:<application id>`` / ``propose_angles:<application id>``). The tasks read the
job's current summary requirements, the stored fact rows and, for angles, the angles other
applications chose; ``tailoring_ai`` calls the model once and resolves the answer with the
pure rules. The result is written by the task's writer, in the transaction that marks the
task succeeded, and nowhere else. Personal details and the photo are never read here, so no
request can hold them. Reads never write, start work or create a workspace.
"""

import asyncio
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from app.domain.angles import AnglesResult
from app.domain.clock import Clock
from app.domain.errors import (
    ApplicationNotFound,
    FitNotAvailable,
    ProviderNotConfigured,
    VersionConflict,
)
from app.domain.fact_selection import facts_for_ai
from app.domain.facts import FactRef
from app.domain.gap_check import GapCheckResult
from app.domain.ids import IdFactory, is_safe_id
from app.domain.locales import COUNTRIES
from app.domain.settings import JOB_PREFERENCES, AIProviderSettings, Tier
from app.domain.tasks import TaskRef
from app.domain.usage import SERVICE_BY_PROVIDER_KIND
from app.modules.ai import AIProviderFactory, AITaskRunner
from app.modules.storage import job_fit_repo, skills_repo, tailoring_repo
from app.services.job_summary import ResultWriter, SummaryContext, SummaryPlan, SummaryWorkspace
from app.services.settings import SettingsService
from app.services.tailoring_ai import (
    GAP_CHECK,
    MAX_EARLIER_ANGLES,
    PROPOSE_ANGLES,
    EarlierAngle,
    TailoringJob,
    run_gap_check,
    run_propose_angles,
)
from app.services.tasks import TaskService
from app.services.usage import UsageService
from app.services.usage_meter import ServiceUsageMeter

GAP_CHECK_LABEL: Final = "Check gaps"
PROPOSE_ANGLES_LABEL: Final = "Propose angles"
STEP_TITLES: Final = {1: "Gap check", 2: "Angle", 3: "CV", 4: "Cover letter", 5: "Submit"}
FIRST_LATER_STEP: Final = 3
NO_SUMMARY: Final = "Summarize the job first."
NO_REQUIREMENTS: Final = (
    "This job's summary has no requirements to check. Review the job summary first."
)
NO_FACTS: Final = "Add confirmed facts first, so there is something to check against."
KIND_LABELS: Final = {"must_have": "Must-have", "key": "Key requirement", "nice": "Nice to have"}
MAX_ANGLE_NAME: Final = 120
MAX_ANGLE_DESCRIPTION: Final = 2000
NAME_FIELD: Final = "name"
DESCRIPTION_FIELD: Final = "description"
COUNTRY_FIELD: Final = "cv_country"
COUNTRY_INVALID: Final = "Choose a country from the list."
_COUNTRY_CODES: Final = frozenset(code for code, _ in COUNTRIES)
_ATTENTION: Final = ("failed", "interrupted")
_ACTIVE: Final = ("queued", "running")


class AngleInvalid(ValueError):
    """The angle can't be saved: ``message`` says what to change, beside the form field
    ``field`` (``name`` or ``description``)."""

    def __init__(self, message: str, field: str = NAME_FIELD) -> None:
        super().__init__(message)
        self.message = message
        self.field = field


class CountryInvalid(ValueError):
    """The CV country is not one of the listed ISO 3166-1 codes (nothing stored)."""

    def __init__(self) -> None:
        super().__init__(COUNTRY_INVALID)
        self.message = COUNTRY_INVALID
        self.field = COUNTRY_FIELD


@dataclass(frozen=True)
class FactWording:
    id: str
    text: str


@dataclass(frozen=True)
class RequirementRow:
    id: str
    kind: str
    text: str
    verdict: str
    wording: tuple[FactWording, ...]

    @property
    def is_gap(self) -> bool:
        return self.verdict != "met"


@dataclass(frozen=True)
class FramingRow:
    text: str
    flagged: bool
    wording: tuple[FactWording, ...]


@dataclass(frozen=True)
class AngleRow:
    name: str
    description: str
    rationale: str
    reuse_angle_id: str
    flagged: bool
    wording: tuple[FactWording, ...]


@dataclass(frozen=True)
class StepState:
    step: int
    title: str
    state: str


@dataclass(frozen=True)
class TaskState:
    """The latest task of a step: running, or how it ended."""

    active: bool = False
    status: str | None = None
    error: str | None = None
    key: str | None = None

    @property
    def attention(self) -> bool:
        return self.status in _ATTENTION


@dataclass(frozen=True)
class TailoringView:
    application_id: str
    job_id: str
    title: str
    company: str
    step: int
    steps: tuple[StepState, ...]
    notice: str  # "" when both steps can start
    notice_link: str  # "job" or "facts" with a notice, else ""
    gap_task: TaskState
    gap_stored: bool
    gap_stale: bool
    gap_practice: bool
    requirements: tuple[RequirementRow, ...]
    gap_message: str | None
    framing: tuple[FramingRow, ...]
    angle_task: TaskState
    angles_stored: bool
    angles_practice: bool
    angles: tuple[AngleRow, ...]
    chosen_angle: str | None
    workspace_version: int
    cv_country: str  # the workspace's, else the preference's (never copied); "" when neither


@dataclass(frozen=True)
class _Source:
    job_id: str
    inputs: job_fit_repo.FitInputs
    summary: str | None
    known_skills: tuple[str, ...]
    earlier: tuple[tailoring_repo.StoredAngle, ...]


@dataclass(frozen=True)
class _Run:
    aid: str
    tier: Tier | None
    source: _Source
    runner: AITaskRunner
    provider: str
    meter: ServiceUsageMeter


def _fact_versions(refs: Sequence[str]) -> tuple[tuple[int, int], ...]:
    parsed = [FactRef.parse(ref) for ref in refs]
    return tuple((ref.number, ref.version) for ref in parsed)


def _clean(value: str) -> str:
    """Text as it is stored: no NUL, line ends as LF (a browser posts CRLF), trimmed."""
    return value.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").strip()


class TailoringService:
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
        self._factory, self._clock, self._ids = factory, clock, ids
        self._workspace, self._settings, self._tasks, self._usage = (
            workspace,
            settings,
            tasks,
            usage,
        )

    def _open(self) -> SummaryWorkspace:
        opened = self._workspace()
        if opened is None:
            raise ApplicationNotFound()
        return opened

    def _source(self, aid: str, *, full: bool = True) -> _Source:
        """What the job and the facts say; ``full`` also reads what only a run needs (the
        known skills and the angles of other applications)."""
        if not is_safe_id(aid):
            raise ApplicationNotFound()
        with self._open().db.read() as conn:
            job_id = tailoring_repo.application_job_id(conn, aid)
            if job_id is None:
                raise ApplicationNotFound()
            inputs = job_fit_repo.inputs(conn, job_id)
            return _Source(
                job_id,
                inputs,
                tailoring_repo.current_summary_text(conn, job_id),
                tuple(skill.name for skill in skills_repo.list_skills(conn)) if full else (),
                tailoring_repo.earlier_angles(conn, aid, MAX_EARLIER_ANGLES) if full else (),
            )

    @staticmethod
    def _notice(source: _Source) -> tuple[str, str]:
        if source.inputs.summary_id is None:
            return NO_SUMMARY, "job"
        if not source.inputs.requirements:
            return NO_REQUIREMENTS, "job"
        if not facts_for_ai(source.inputs.facts):
            return NO_FACTS, "facts"
        return "", ""

    # --- starting ---------------------------------------------------------------------------

    def plan_start(
        self, aid: str, task_type: str, *, tier: Tier | None = None
    ) -> SummaryPlan | None:
        """What a start would use, or None when the step is not ready (no current summary or
        no requirements, no usable confirmed fact). ``ProviderNotConfigured`` without a usable
        provider."""
        if self._notice(self._source(aid, full=False))[0]:
            return None
        settings = self._settings()
        service = SERVICE_BY_PROVIDER_KIND.get(settings.kind or "") if settings else None
        if settings is None or service is None:
            raise ProviderNotConfigured()
        return SummaryPlan(service, tier or settings.task_tiers.get(task_type, "thorough"))

    def _start(self, aid: str, task_type: str, tier: Tier | None) -> TaskRef | None:
        if self.plan_start(aid, task_type, tier=tier) is None:
            return None
        tasks = self._tasks()
        if tasks is None:
            raise ApplicationNotFound()
        return tasks.enqueue(task_type, aid, input_ref=aid + (f"/{tier}" if tier else ""))

    def start_gap_check(self, application_id: str, *, tier: Tier | None = None) -> TaskRef | None:
        return self._start(application_id, GAP_CHECK, tier)

    def start_angles(self, application_id: str, *, tier: Tier | None = None) -> TaskRef | None:
        return self._start(application_id, PROPOSE_ANGLES, tier)

    # --- running (inside a task) ------------------------------------------------------------

    async def _prepare(self, ctx: SummaryContext) -> _Run:
        aid, _, tier = (ctx.input_ref or "").partition("/")
        if tier not in ("", "fast", "thorough"):
            raise ApplicationNotFound()
        await ctx.heartbeat(0)
        source = await asyncio.to_thread(self._source, aid)
        if self._notice(source)[0]:
            raise FitNotAvailable()
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
        await ctx.heartbeat(10)
        meter = ServiceUsageMeter(usage, service)
        chosen: Tier | None = "fast" if tier == "fast" else "thorough" if tier else None
        return _Run(aid, chosen, source, runner, provider.name, meter)

    @staticmethod
    def _job(source: _Source) -> TailoringJob:
        return TailoringJob(
            source.job_id,
            str(source.inputs.job["snapshot_text"]),
            source.inputs.requirements,
        )

    async def run_gap_check(self, ctx: SummaryContext) -> ResultWriter:
        run = await self._prepare(ctx)
        aid, source, provider = run.aid, run.source, run.provider
        output = await run_gap_check(
            run.runner,
            self._job(source),
            source.inputs.facts,
            ctx.cancel_flag,
            meter=run.meter,
            tier=run.tier,
            task_id=ctx.task_id,
            known_skills=source.known_skills,
        )
        ctx.check_cancelled()
        result: GapCheckResult = output.result
        gap_id, summary = self._ids.new(), source.summary or ""

        def write(conn: sqlite3.Connection) -> str:
            tailoring_repo.insert_gap_check(
                conn,
                id=gap_id,
                application_id=aid,
                summary=summary,
                result=result.to_data(),
                fact_versions=_fact_versions(output.fact_refs),
                provider=provider,
                model=output.model,
                prompt_id=output.prompt_id,
                prompt_version=output.prompt_version,
                task_id=ctx.task_id,
                now=self._clock.now().isoformat(),
            )
            return f"gap_check:{gap_id}"

        return write

    async def run_angles(self, ctx: SummaryContext) -> ResultWriter:
        run = await self._prepare(ctx)
        aid, source, provider = run.aid, run.source, run.provider
        output = await run_propose_angles(
            run.runner,
            self._job(source),
            source.inputs.facts,
            tuple(EarlierAngle(a.id, a.name, a.description) for a in source.earlier),
            ctx.cancel_flag,
            meter=run.meter,
            tier=run.tier,
            task_id=ctx.task_id,
            known_skills=source.known_skills,
        )
        ctx.check_cancelled()
        result: AnglesResult = output.result
        proposal_id = self._ids.new()

        def write(conn: sqlite3.Connection) -> str:
            tailoring_repo.insert_angle_proposal(
                conn,
                id=proposal_id,
                application_id=aid,
                result=result.to_data(),
                provider=provider,
                model=output.model,
                prompt_id=output.prompt_id,
                prompt_version=output.prompt_version,
                task_id=ctx.task_id,
                now=self._clock.now().isoformat(),
            )
            return f"angle_proposal:{proposal_id}"

        return write

    # --- saving the angle -------------------------------------------------------------------

    def save_angle(
        self,
        application_id: str,
        *,
        name: str,
        description: str,
        version: int,
        reuse_angle_id: str | None = None,
    ) -> None:
        """Point the application's workspace at the angle. The workspace row is created by the
        first save (``version`` 0). An earlier angle saved unchanged is reused as it is; a
        proposal saved unchanged is written as ``ai``; anything else is the user's own.
        ``AngleInvalid`` (nothing stored), ``VersionConflict`` for a stale ``version``."""
        if not is_safe_id(application_id):
            raise ApplicationNotFound()
        name, description = _clean(name), _clean(description)
        if not name:
            raise AngleInvalid("Give the angle a name.")
        if len(name) > MAX_ANGLE_NAME:
            raise AngleInvalid(f"Keep the name to {MAX_ANGLE_NAME} characters or fewer.")
        if len(description) > MAX_ANGLE_DESCRIPTION:
            raise AngleInvalid(
                f"Keep the description to {MAX_ANGLE_DESCRIPTION} characters or fewer.",
                DESCRIPTION_FIELD,
            )
        now = self._clock.now().isoformat()
        with self._open().db.transaction() as conn:
            if tailoring_repo.application_job_id(conn, application_id) is None:
                raise ApplicationNotFound()
            current = tailoring_repo.workspace(conn, application_id)
            if (current.version if current else 0) != version:
                raise VersionConflict()
            angle_id = self._angle_for(
                conn, application_id, name, description, reuse_angle_id or None, current, now
            )
            if current is None:
                tailoring_repo.insert_workspace(
                    conn,
                    id=self._ids.new(),
                    application_id=application_id,
                    angle_id=angle_id,
                    now=now,
                )
            else:
                tailoring_repo.update_workspace(
                    conn,
                    current.id,
                    cv_country=current.cv_country,
                    angle_id=angle_id,
                    now=now,
                    expected_version=current.version,
                )

    def _angle_for(
        self,
        conn: sqlite3.Connection,
        application_id: str,
        name: str,
        description: str,
        reuse_id: str | None,
        current: tailoring_repo.StoredWorkspace | None,
        now: str,
    ) -> str:
        """The id of the angle to point at: an existing one saved unchanged, else a new row."""
        if reuse_id is not None:
            earlier = tailoring_repo.get_angle(conn, reuse_id) if is_safe_id(reuse_id) else None
            if earlier is None:
                raise AngleInvalid("That earlier angle is no longer available.")
            if (_clean(earlier.name), _clean(earlier.description)) == (name, description):
                return earlier.id
        if (
            current
            and current.angle_id
            and (_clean(current.angle_name or ""), _clean(current.angle_description or ""))
            == (name, description)
        ):
            return current.angle_id
        proposal = tailoring_repo.latest_angle_proposal(conn, application_id)
        proposed = proposal is not None and any(
            (_clean(str(item.get("name", ""))), _clean(str(item.get("description", ""))))
            == (name, description)
            for item in proposal.result.get("angles", ())
        )
        angle_id = self._ids.new()
        tailoring_repo.insert_angle(
            conn,
            id=angle_id,
            name=name,
            description=description,
            origin="ai" if proposed and reuse_id is None else "user",
            now=now,
        )
        return angle_id

    # --- the CV country ---------------------------------------------------------------------

    def save_country(self, application_id: str, *, cv_country: str, version: int) -> None:
        """Store the CV country on the application's workspace, creating the row (no angle) when
        there is none (``version`` 0); the angle and letter choices stay as they are.
        ``CountryInvalid`` (nothing stored), ``ApplicationNotFound``, ``VersionConflict``.

        Creating the workspace row counts as changing the application: a database guard then
        refuses to delete it with "Back to queue" (``ApplicationService.undo_apply``). The
        country form says so in a hint (ADR-106 item 6)."""
        if not is_safe_id(application_id):
            raise ApplicationNotFound()
        if cv_country not in _COUNTRY_CODES:
            raise CountryInvalid()
        now = self._clock.now().isoformat()
        with self._open().db.transaction() as conn:
            if tailoring_repo.application_job_id(conn, application_id) is None:
                raise ApplicationNotFound()
            current = tailoring_repo.workspace(conn, application_id)
            if (current.version if current else 0) != version:
                raise VersionConflict()
            if current is None:
                tailoring_repo.insert_workspace(
                    conn,
                    id=self._ids.new(),
                    application_id=application_id,
                    angle_id=None,
                    cv_country=cv_country,
                    now=now,
                )
            else:
                tailoring_repo.update_workspace(
                    conn,
                    current.id,
                    cv_country=cv_country,
                    angle_id=current.angle_id,
                    now=now,
                    expected_version=current.version,
                )

    # --- reading ----------------------------------------------------------------------------

    def has_confirmed_fact(self, text: str) -> bool:
        """Whether a confirmed fact already says exactly ``text`` (a double submit adds no
        second one)."""
        with self._open().db.read() as conn:
            return tailoring_repo.has_confirmed_fact_text(conn, _clean(text))

    def resume_step(self, application_id: str) -> int:
        """1 until a gap check is stored, then 2 (also once an angle is saved)."""
        return self.view(application_id, None).step

    def view(self, application_id: str, step: int | None) -> TailoringView:
        """What the tab shows for ``step`` (None: the derived resume step). Reads only."""
        source = self._source(application_id, full=False)
        opened = self._open()
        with opened.db.read() as conn:
            header = tailoring_repo.application_header(conn, application_id)
            if header is None:
                raise ApplicationNotFound()
            gap = tailoring_repo.latest_gap_check(conn, application_id)
            proposal = tailoring_repo.latest_angle_proposal(conn, application_id)
            workspace = tailoring_repo.workspace(conn, application_id)
            gap_task = _task_state(
                tailoring_repo.latest_task(conn, f"{GAP_CHECK}:{application_id}")
            )
            angle_task = _task_state(
                tailoring_repo.latest_task(conn, f"{PROPOSE_ANGLES}:{application_id}")
            )
            requirements = _requirement_rows(conn, gap)
            framing = _framing_rows(conn, gap)
            angles = _angle_rows(conn, proposal)
        preferred = SettingsService(opened.db, self._clock).load(JOB_PREFERENCES).value
        notice, notice_link = self._notice(source)
        chosen = workspace.angle_name if workspace and workspace.angle_id else None
        job_id, title, company = header
        steps = _step_states(
            gap_stored=gap is not None,
            gap_task=gap_task,
            proposed=proposal is not None,
            chosen=chosen is not None,
            angle_task=angle_task,
        )
        resume = 2 if gap is not None or chosen is not None else 1
        return TailoringView(
            application_id=application_id,
            job_id=job_id,
            title=title,
            company=company,
            step=step if step is not None else resume,
            steps=steps,
            notice=notice,
            notice_link=notice_link,
            gap_task=gap_task,
            gap_stored=gap is not None,
            gap_stale=gap is not None
            and source.summary is not None
            and gap.summary != source.summary,
            gap_practice=bool(gap and gap.provider == "fake"),
            requirements=requirements,
            gap_message=(gap.result.get("message") if gap else None),
            framing=framing,
            angle_task=angle_task,
            angles_stored=proposal is not None,
            angles_practice=bool(proposal and proposal.provider == "fake"),
            angles=angles,
            chosen_angle=chosen,
            workspace_version=workspace.version if workspace else 0,
            cv_country=(workspace.cv_country if workspace else None)
            or (preferred.cv_country if preferred else ""),
        )


def _task_state(row: tuple[str, str, str | None, int] | None) -> TaskState:
    if row is None:
        return TaskState()
    return TaskState(row[1] in _ACTIVE, row[1], row[2], f"{row[0]}:{row[3]}")


def _step_states(
    *,
    gap_stored: bool,
    gap_task: TaskState,
    proposed: bool,
    chosen: bool,
    angle_task: TaskState,
) -> tuple[StepState, ...]:
    def words(task: TaskState, done: bool, choice: bool = False) -> str:
        if task.active:
            return "Running"
        if task.attention:
            return "Needs attention"
        if done:
            return "Done"
        return "Needs your choice" if choice else "Not started"

    states = {1: words(gap_task, gap_stored), 2: words(angle_task, chosen, proposed)}
    return tuple(
        StepState(step, title, states.get(step, "Coming later"))
        for step, title in STEP_TITLES.items()
    )


def _wording(conn: sqlite3.Connection, refs: Sequence[object]) -> tuple[FactWording, ...]:
    """What the cited fact versions said, in the user's own words (unreadable ids skipped)."""
    found: list[FactWording] = []
    for ref in refs:
        try:
            parsed = FactRef.parse(str(ref))
        except ValueError:
            continue
        stored = tailoring_repo.fact_wording(conn, parsed.number, parsed.version)
        if stored is not None and stored.text:
            found.append(FactWording(stored.fact_id, stored.text))
    return tuple(found)


def _requirement_rows(
    conn: sqlite3.Connection, gap: tailoring_repo.StoredResult | None
) -> tuple[RequirementRow, ...]:
    if gap is None:
        return ()
    return tuple(
        RequirementRow(
            str(item["requirement_id"]),
            KIND_LABELS.get(str(item.get("kind")), "Requirement"),
            str(item.get("text", "")),
            str(item.get("verdict", "missing")),
            _wording(conn, item.get("fact_ids", ())),
        )
        for item in gap.result.get("requirements", ())
    )


def _framing_rows(
    conn: sqlite3.Connection, gap: tailoring_repo.StoredResult | None
) -> tuple[FramingRow, ...]:
    if gap is None:
        return ()
    return tuple(
        FramingRow(
            str(item.get("text", "")),
            bool(item.get("claims")),
            _wording(conn, item.get("fact_ids", ())),
        )
        for item in gap.result.get("framing_ideas", ())
    )


def _angle_rows(
    conn: sqlite3.Connection, proposal: tailoring_repo.StoredResult | None
) -> tuple[AngleRow, ...]:
    if proposal is None:
        return ()
    return tuple(
        AngleRow(
            str(item.get("name", "")),
            str(item.get("description", "")),
            str(item.get("rationale", "")),
            str(item.get("reuse_angle_id") or ""),
            bool(item.get("claims")),
            _wording(conn, item.get("fact_ids", ())),
        )
        for item in proposal.result.get("angles", ())
    )


__all__ = [
    "GAP_CHECK_LABEL",
    "PROPOSE_ANGLES_LABEL",
    "AngleInvalid",
    "CountryInvalid",
    "TailoringService",
    "TailoringView",
]
