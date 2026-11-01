"""Practice answers and their feedback (M8.2 part A; AC-M8.4; spec 3.1 items 5 and 6; ADR-124).

An attempt is immutable: the answer and the CV sent are kept as they were. The task writes the
feedback in the transaction that marks it succeeded, and nowhere else. A claim the code finds
that is not in the fact bank becomes a needs-check draft fact only when the person ticks it,
all of the ticked claims in one transaction. Answer and feedback text never reach logs, task or
usage rows, events or error text.
"""

import asyncio
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final

from app.domain.answer_feedback import (
    MAX_ANSWER_CHARS,
    Feedback,
    current_cv_lines,
    fact_wordings,
    feedback_from_data,
    sentence_of,
)
from app.domain.clock import Clock
from app.domain.errors import ApplicationNotFound, ProviderNotConfigured, VersionConflict
from app.domain.fact_selection import FactRecord, facts_for_ai
from app.domain.facts import Privacy
from app.domain.ids import IdFactory, is_safe_id
from app.domain.question_bank import BankQuestion
from app.domain.settings import AIProviderSettings, Tier
from app.domain.tasks import TaskRef
from app.domain.usage import SERVICE_BY_PROVIDER_KIND
from app.modules.ai import AIProvider, AIProviderFactory
from app.modules.storage import job_fit_repo, practice_repo, prep_repo, skills_repo
from app.services.answer_ai import ANSWER_FEEDBACK, AnswerContext, answer_feedback
from app.services.facts import SOURCE_PRACTICE, DraftInput, DraftOrigin, FactService
from app.services.job_summary import ResultWriter, SummaryContext, SummaryPlan, SummaryWorkspace
from app.services.prep import PrepService
from app.services.tasks import TaskService
from app.services.usage import UsageService
from app.services.usage_meter import ServiceUsageMeter

MAX_CV_SNAPSHOT: Final = 100_000
_ACTIVE: Final = frozenset({"queued", "running", "cancelling"})
_TIERS: Final = frozenset({"default", "fast", "thorough"})
TOO_LONG: Final = "Your answer can be at most 4,000 characters."
BLANK: Final = "Type your answer first."
CHOOSE_CLAIMS: Final = "Choose one of the flagged claims."


class UnknownClaim(ValueError):
    """A ticked claim index that is not in the attempt's flagged list."""


@dataclass(frozen=True)
class AttemptView:
    """One attempt. ``feedback`` is None while the task runs or after it failed
    (``task_status``/``error`` say which); ``resolved`` maps a claim index to the draft fact
    it became."""

    id: str
    answer: str
    version: int
    feedback: Feedback | None
    resolved: Mapping[int, str]
    task_status: str | None
    error: str | None
    practice: bool
    sentences: Mapping[int, str]  # claim index -> the sentence a ticked claim saves as a draft

    @property
    def active(self) -> bool:
        return self.feedback is None and self.task_status in _ACTIVE


@dataclass(frozen=True)
class PracticeView:
    application_id: str
    question: BankQuestion
    version: int  # the prep item's; 0 while there is no item
    ready: bool
    attempts: tuple[AttemptView, ...]
    cv_source: str
    truncated: bool = False  # older tries exist than the ones shown
    fact_words: Mapping[str, str] = field(default_factory=dict)  # fact ref -> its short wording

    @property
    def active(self) -> bool:
        return any(attempt.active for attempt in self.attempts)


@dataclass(frozen=True)
class Submitted:
    attempt_id: str
    task: TaskRef


def normalize_answer(answer: str) -> str:
    """The answer as it is stored and shown again: line ends as LF (a browser posts CRLF), no
    NUL, nothing trimmed. Not checked for blank or long."""
    return answer.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")


def clean_answer(answer: str) -> str:
    """The answer as stored; ``ValueError`` when it is blank or over ``MAX_ANSWER_CHARS``."""
    text = normalize_answer(answer)
    if not text.strip():
        raise ValueError(BLANK)
    if len(text) > MAX_ANSWER_CHARS:
        raise ValueError(TOO_LONG)
    return text


def _resolved(stored: Mapping[str, Any]) -> dict[int, str]:
    raw = stored.get("resolved")
    if not isinstance(raw, dict):
        return {}
    return {int(key): str(value) for key, value in raw.items() if str(key).isdigit()}


class AnswerPracticeService:
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
        prep: PrepService,
    ) -> None:
        self._factory, self._clock, self._ids = factory, clock, ids
        self._workspace, self._settings, self._tasks, self._usage = (
            workspace,
            settings,
            tasks,
            usage,
        )
        self._prep = prep

    def _open(self) -> SummaryWorkspace:
        opened = self._workspace()
        if opened is None:
            raise ApplicationNotFound()
        return opened

    # --- reading ----------------------------------------------------------------------------

    def view(self, aid: str, qid: str) -> PracticeView:
        if not is_safe_id(aid) or not is_safe_id(qid):
            raise ApplicationNotFound()
        with self._open().db.read() as conn:
            source, question = self._question(conn, aid, qid)
            item = next((i for i in prep_repo.items(conn, aid) if i.question_id == qid), None)
            found = practice_repo.attempts(conn, item.id) if item else ()
            rows = found[: practice_repo.MAX_ATTEMPTS_SHOWN]
            tasks = {r.id: prep_repo.latest_task(conn, r.id, ANSWER_FEEDBACK) for r in rows}
            records = job_fit_repo.fact_records(conn) if rows else ()
        return PracticeView(
            aid,
            question,
            item.version if item else 0,
            item.ready if item else False,
            tuple(self._attempt(row, tasks[row.id]) for row in rows),
            source.cv_source,
            len(found) > len(rows),
            fact_wordings(facts_for_ai(records)),
        )

    def attempt_in(self, aid: str, qid: str, attempt_id: str) -> bool:
        """Whether the attempt belongs to this application's question, however old it is.
        ``ApplicationNotFound`` for a question outside the application's scope."""
        if not is_safe_id(aid) or not is_safe_id(qid):
            raise ApplicationNotFound()
        with self._open().db.read() as conn:
            self._question(conn, aid, qid)
            row = practice_repo.attempt(conn, attempt_id) if is_safe_id(attempt_id) else None
        return row is not None and (row.application_id, row.question_id) == (aid, qid)

    @staticmethod
    def _question(
        conn: sqlite3.Connection, aid: str, qid: str
    ) -> tuple[prep_repo.PrepSource, BankQuestion]:
        """The application and the question, when both exist and the question is in the
        application's scope (global, or of its company)."""
        source = prep_repo.source(conn, aid)
        if source is None:
            raise ApplicationNotFound()
        found = prep_repo.questions(conn, question_id=qid)
        if not found or (found[0].company_id not in (None, source.company_id)):
            raise ApplicationNotFound()
        return source, found[0]

    @staticmethod
    def _attempt(
        row: practice_repo.AttemptRow, task: tuple[str, str, str | None] | None
    ) -> AttemptView:
        feedback: Feedback | None = None
        resolved: dict[int, str] = {}
        sentences: dict[int, str] = {}
        if row.feedback:
            try:
                stored = json.loads(row.feedback)
                feedback, resolved = feedback_from_data(stored), _resolved(stored)
                sentences = {
                    i: sentence_of(row.answer_text, claim) or claim.text
                    for i, claim in enumerate(feedback.unsupported)
                }
            except ValueError:
                feedback = None
        return AttemptView(
            row.id,
            row.answer_text,
            row.version,
            feedback,
            resolved,
            task[1] if task else None,
            task[2] if task else None,
            row.provider == "fake",
            sentences,
        )

    # --- starting ---------------------------------------------------------------------------

    def plan_start(self, aid: str, qid: str, *, tier: Tier | None = None) -> SummaryPlan:
        """What a start would use. ``ApplicationNotFound`` for a question outside the
        application's scope, ``ProviderNotConfigured`` without a usable provider."""
        self.view_scope(aid, qid)
        settings = self._settings()
        service = SERVICE_BY_PROVIDER_KIND.get(settings.kind or "") if settings else None
        if settings is None or service is None:
            raise ProviderNotConfigured()
        return SummaryPlan(service, tier or settings.task_tiers.get(ANSWER_FEEDBACK, "thorough"))

    def view_scope(self, aid: str, qid: str) -> None:
        if not is_safe_id(aid) or not is_safe_id(qid):
            raise ApplicationNotFound()
        with self._open().db.read() as conn:
            self._question(conn, aid, qid)

    def submit(
        self, aid: str, qid: str, answer: str, *, version: int, tier: Tier | None = None
    ) -> Submitted:
        """Keep the answer and queue its feedback in one transaction; the caller has asked the
        budget gate. ``ValueError`` for a blank or over-long answer, ``VersionConflict`` for a
        stale item version. Nothing is stored or queued on any refusal."""
        text = clean_answer(answer)
        self.plan_start(aid, qid, tier=tier)
        tasks = self._tasks()
        if tasks is None:
            raise ApplicationNotFound()
        attempt_id = self._ids.new()

        def store(conn: sqlite3.Connection) -> bool:
            source, item = self._prep.ensure_item(conn, aid, qid, version=version)
            now = self._clock.now().isoformat()
            practice_repo.insert_attempt(
                conn,
                attempt_id=attempt_id,
                item_id=item.id,
                answer=text,
                cv_snapshot=source.cv_text.replace("\x00", "")[:MAX_CV_SNAPSHOT],
                now=now,
            )
            practice_repo.bump_item(conn, item.id, now, expected_version=item.version)
            prep_repo.changed(conn, aid, now, expected_version=source.version)
            return True

        ref = tasks.enqueue_if(
            ANSWER_FEEDBACK,
            attempt_id,
            input_ref=f"{attempt_id}/{tier or 'default'}",
            eligible=store,
        )
        if ref is None:
            raise VersionConflict()
        return Submitted(attempt_id, ref)

    # --- running (inside a task) ------------------------------------------------------------

    def _load_run(
        self, attempt_id: str
    ) -> tuple[practice_repo.AttemptRow, str, Sequence[FactRecord], tuple[str, ...]]:
        if not is_safe_id(attempt_id):
            raise ApplicationNotFound()
        with self._open().db.read() as conn:
            row = practice_repo.attempt(conn, attempt_id)
            if row is None:
                raise ApplicationNotFound()
            asked = prep_repo.questions(conn, question_id=row.question_id)
            if not asked:
                raise ApplicationNotFound()
            records = job_fit_repo.fact_records(conn)
            skills = tuple(s.name for s in skills_repo.list_skills(conn))
            source = prep_repo.source(conn, row.application_id)
        # The CV text is what was sent then, less any line the CV no longer holds: a fact
        # marked never use since must not be sent now.
        current = source.cv_text if source else ""
        fresh = replace(row, cv_snapshot=current_cv_lines(row.cv_snapshot, current))
        return fresh, asked[0].text, records, skills

    def _provider(self) -> tuple[AIProvider, AIProviderSettings]:
        settings = self._settings()
        if settings is None or settings.kind is None:
            raise ProviderNotConfigured()
        try:
            provider = self._factory.ai_provider(settings, data_dir=self._open().data_dir)
        except ValueError:
            raise ProviderNotConfigured() from None
        return provider, settings

    async def run(self, ctx: SummaryContext) -> ResultWriter:
        parts = (ctx.input_ref or "").split("/")
        if len(parts) != 2 or parts[1] not in _TIERS:
            raise ApplicationNotFound()
        attempt_id, raw_tier = parts
        row, question, records, skills = await asyncio.to_thread(self._load_run, attempt_id)
        await ctx.heartbeat(10)
        provider, settings = await asyncio.to_thread(self._provider)
        usage = self._usage()
        service = SERVICE_BY_PROVIDER_KIND.get(settings.kind or "")
        if usage is None or service is None:
            raise ProviderNotConfigured()
        runner = self._factory.task_runner(
            provider, lambda: settings, data_dir=self._open().data_dir
        )
        tier: Tier | None = (
            "fast" if raw_tier == "fast" else "thorough" if raw_tier == "thorough" else None
        )
        output = await answer_feedback(
            runner,
            AnswerContext(row.application_id, question, row.answer_text, row.cv_snapshot),
            records,
            ctx.cancel_flag,
            meter=ServiceUsageMeter(usage, service),
            tier=tier,
            task_id=ctx.task_id,
            known_skills=skills,
        )
        ctx.check_cancelled()
        await ctx.heartbeat(80)
        stored = json.dumps(output.feedback.to_data(), ensure_ascii=False)
        refs = json.dumps(list(output.fact_refs))
        now = self._clock.now().isoformat()

        def write(conn: sqlite3.Connection) -> str:
            practice_repo.write_feedback(
                conn,
                attempt_id,
                feedback=stored,
                fact_refs=refs,
                provider=provider.name,
                model=output.model,
                prompt_id=output.prompt_id,
                prompt_version=output.prompt_version,
                now=now,
            )
            return attempt_id

        return write

    # --- adding claims ----------------------------------------------------------------------

    def add_claims(self, attempt_id: str, indexes: Sequence[int], *, version: int) -> int:
        """Add each ticked claim of the feedback's ``unsupported`` list as a needs-check draft
        fact (never confirmed), in one transaction with the attempt's resolved map. A claim that
        is already added is skipped. Returns how many drafts were made. ``UnknownClaim`` (a
        ``ValueError``) for an index outside the list, ``VersionConflict`` for a stale attempt
        version, ``ApplicationNotFound`` for feedback that cannot be read."""
        if not is_safe_id(attempt_id):
            raise ApplicationNotFound()
        opened = self._open()
        with opened.db.transaction() as conn:
            row = practice_repo.attempt(conn, attempt_id)
            if row is None or not row.feedback:
                raise ApplicationNotFound()
            if row.version != version:
                raise VersionConflict()
            try:
                stored: dict[str, Any] = json.loads(row.feedback)
                feedback = feedback_from_data(stored)
            except ValueError:
                raise ApplicationNotFound() from None  # nothing readable to add claims from
            wanted = list(dict.fromkeys(indexes))
            if any(i < 0 or i >= len(feedback.unsupported) for i in wanted):
                raise UnknownClaim(CHOOSE_CLAIMS)
            done = _resolved(stored)
            todo = [i for i in wanted if i not in done]
            if not todo:
                return 0
            drafts = []
            for i in todo:
                claim = feedback.unsupported[i]
                sentence = sentence_of(row.answer_text, claim) or claim.text
                drafts.append(
                    DraftInput(
                        text=sentence,
                        original_answer=sentence,
                        needs_check=True,
                        privacy=Privacy.NORMAL,
                    )
                )
            made = FactService(opened.db, self._clock, self._ids).create_drafts_in(
                conn, DraftOrigin(SOURCE_PRACTICE), drafts
            )
            resolved = {**done, **{i: fact.id for i, fact in zip(todo, made, strict=True)}}
            stored["resolved"] = {str(i): fact_id for i, fact_id in sorted(resolved.items())}
            practice_repo.store_feedback(
                conn,
                attempt_id,
                json.dumps(stored, ensure_ascii=False),
                self._clock.now().isoformat(),
                expected_version=row.version,
            )
        return len(made)
