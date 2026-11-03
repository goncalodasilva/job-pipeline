"""User-started enrichment questions and versioned outcomes (M7.4).

The gate precedes every enqueue. Task inputs hold ids and tier only; question snapshots
are written by ctx.complete, in the same transaction as task success.
"""

import asyncio
import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, tzinfo
from typing import Any, Final, cast

from app.domain.clock import Clock
from app.domain.enrichment import LaterChoice, Outcome, record_outcome
from app.domain.errors import (
    ProviderNotConfigured,
    SessionAlreadyOpen,
    SessionBusy,
    SessionEmpty,
    SessionNotFound,
    VersionConflict,
)
from app.domain.fact_selection import FactPromptBlock, fact_text, facts_for_ai, prompt_fact_block
from app.domain.facts import Privacy
from app.domain.honesty import new_claims
from app.domain.ids import IdFactory, is_safe_id
from app.domain.questions import fallback_question, question_problems
from app.domain.settings import SESSIONS, AIProviderSettings, Tier
from app.domain.structured_answer import resolve_structured_answer
from app.domain.tasks import TaskRef, dedupe_key
from app.domain.usage import SERVICE_BY_PROVIDER_KIND, ServiceId
from app.modules.ai import AIProviderFactory
from app.modules.storage import (
    capture_repo,
    enrichment_repo,
    sessions_repo,
    settings_repo,
    skills_repo,
    tasks_repo,
)
from app.services.budget_gate import BudgetGate, Refusal, Start
from app.services.facts import DraftInput, DraftOrigin, FactService
from app.services.job_summary import ResultWriter, SummaryContext, SummaryWorkspace
from app.services.session_answers import AnswerSources, answer_sources, follow_up, item_for
from app.services.session_backlog import (
    FocusKind,
    SessionFocus,
    SessionItem,
    backlog,
    sources,
    state_of,
    target_of,
)
from app.services.settings import SettingsService
from app.services.tasks import TaskService
from app.services.usage import Confirmation, UsageService
from app.services.usage_meter import ServiceUsageMeter

ENRICHMENT_QUESTION: Final = "enrichment_question"
QUESTION_LABEL: Final = "Ask a session question"
STRUCTURE_ANSWER: Final = "structure_answer"
ANSWER_LABEL: Final = "Structure a session answer"


@dataclass(frozen=True)
class SessionStart:
    session_id: str | None = None
    task: TaskRef | None = None
    refusal: Refusal | None = None


@dataclass(frozen=True)
class SessionView:
    session: sessions_repo.StoredSession
    answers: tuple[sessions_repo.StoredAnswer, ...]
    task: tasks_repo.TaskRow | None

    @property
    def practice(self) -> bool:
        return any(
            a.question_provider == "fake" or a.structure_provider == "fake" for a in self.answers
        )


def request_notice(length: int) -> str:
    return f"A {length}-question session uses up to {length * 2} requests."


def _session(
    conn: sqlite3.Connection, sid: str, version: int | None = None
) -> sessions_repo.StoredSession:
    row = sessions_repo.get_session(conn, sid) if is_safe_id(sid) else None
    if row is None:
        raise SessionNotFound()
    if version is not None and (row.version != version or row.state != "open"):
        raise VersionConflict()
    return row


def _active(conn: sqlite3.Connection, sid: str) -> tasks_repo.TaskRow | None:
    return next(
        (
            r
            for kind in (ENRICHMENT_QUESTION, STRUCTURE_ANSWER)
            if (r := tasks_repo.get_active(conn, dedupe_key(kind, "session:" + sid)))
        ),
        None,
    )


def _focus(row: sessions_repo.StoredSession) -> SessionFocus:
    return SessionFocus(cast(FocusKind, row.focus_kind), row.focus_ref)


def _ensure_question(conn: sqlite3.Connection, entry: SessionItem, ids: IdFactory, now: str) -> str:
    row = enrichment_repo.find_question(conn, entry.item.kind, entry.item.target_key)
    if row:
        return row.id
    qid = ids.new()
    enrichment_repo.insert_question(
        conn,
        id=qid,
        kind=entry.item.kind,
        now=now,
        cluster_key=entry.item.target_key if entry.item.kind == "cluster" else None,
        fact_id=entry.item.target_key if entry.item.kind == "quality_hint" else None,
        capture_note_id=entry.item.target_key if entry.item.kind == "capture_note" else None,
    )
    return qid


def _output_safe(question: str, why: str, entry: SessionItem, known: tuple[str, ...]) -> bool:
    facts = facts_for_ai(entry.facts)
    ordinary = tuple(fact_text(f) for f in facts if not f.generalize)
    hidden = tuple(fact_text(f) for f in facts if f.generalize)
    # Job demand supplies a topic, never personal employers or quantities. A controlled
    # skill cluster can name its skill without assuming the person has used it.
    topic = (
        (entry.item.target_key.removeprefix("skill:"),)
        if entry.item.kind == "cluster" and entry.item.target_key.startswith("skill:")
        else ()
    )
    note = (entry.payload,) if entry.item.kind == "capture_note" else ()
    allowed = (*ordinary, *topic, *note, *entry.personal_answers)
    return not question_problems(
        question, allowed, known, name_hidden_sources=hidden
    ) and not new_claims(why, allowed, known_skills=known, name_hidden_sources=hidden)


def _request_ref(sid: str, tier: Tier, entry: SessionItem) -> str:
    return json.dumps(
        {"session": sid, "tier": tier, "item": entry.identifier, "source": entry.source_stamp}
    )


def _parse_ref(value: str | None) -> tuple[str, Tier, str, str]:
    try:
        data = json.loads(value or "")
        sid, tier, item, source = data["session"], data["tier"], data["item"], data["source"]
        if (
            set(data) != {"session", "tier", "item", "source"}
            or not all(is_safe_id(v) for v in (sid, item, source))
            or tier not in ("fast", "thorough")
        ):
            raise ValueError
        return sid, cast(Tier, tier), item, source
    except (ValueError, KeyError, TypeError):
        raise SessionNotFound() from None


def _same_sources(
    conn: sqlite3.Connection, answer: sessions_repo.StoredAnswer, entry: SessionItem
) -> bool:
    task = tasks_repo.get(conn, answer.question_task_id) if answer.question_task_id else None
    if task is None:
        return False
    try:
        _, _, item, source = _parse_ref(task.input_ref)
    except SessionNotFound:
        return False
    return item == entry.identifier and source == entry.source_stamp


class SessionService:
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
        self._workspace, self._settings, self._tasks, self._usage, self._gate = (
            workspace,
            settings,
            tasks,
            usage,
            gate,
        )

    def _open(self) -> SummaryWorkspace:
        row = self._workspace()
        if row is None:
            raise SessionNotFound()
        return row

    def _provider(self) -> tuple[AIProviderSettings, ServiceId]:
        settings = self._settings()
        service = SERVICE_BY_PROVIDER_KIND.get(settings.kind or "") if settings else None
        if settings is None or service is None:
            raise ProviderNotConfigured()
        return settings, service

    def backlog(self, focus: SessionFocus | None = None) -> tuple[SessionItem, ...]:
        with self._open().db.read() as conn:
            return backlog(conn, focus or SessionFocus(), self._clock.now())

    def view(self, sid: str) -> SessionView:
        with self._open().db.read() as conn:
            answers = sessions_repo.list_answers(conn, sid)
            kind = (
                STRUCTURE_ANSWER
                if answers and answers[-1].answer_text is not None
                else ENRICHMENT_QUESTION
            )
            return SessionView(
                _session(conn, sid),
                answers,
                _active(conn, sid)
                or tasks_repo.get_latest(conn, dedupe_key(kind, "session:" + sid)),
            )

    def start(
        self,
        focus: SessionFocus,
        length: int,
        *,
        tier: Tier = "fast",
        confirmation: Confirmation | None = None,
    ) -> SessionStart:
        if type(length) is not int or length not in (3, 5) or tier not in ("fast", "thorough"):
            raise ValueError("Choose 3 or 5 questions and an available tier.")
        opened = self._open()
        with opened.db.read() as conn:
            existing = sessions_repo.open_session(conn)
            if existing:
                raise SessionAlreadyOpen(existing.id)
            entries = backlog(conn, focus, self._clock.now())
            if not entries:
                raise SessionEmpty()
            entry = entries[0]
            # Every write goes through this one connection (the data folder is locked to one
            # app), so an unchanged count means the snapshot read above is still current.
            seen_changes = conn.total_changes
        _, service = self._provider()
        checked = self._gate().check(service, tier=tier, confirmation=confirmation)
        if not isinstance(checked, Start):
            return SessionStart(refusal=checked)
        sid, now = self._ids.new(), self._clock.now().isoformat()
        tasks = self._tasks()
        if tasks is None:
            raise SessionNotFound()

        # Create the session and its task in one transaction, including concurrent-start checks.
        def eligible(conn: sqlite3.Connection) -> bool:
            existing = sessions_repo.open_session(conn)
            if existing:
                raise SessionAlreadyOpen(existing.id)
            if conn.total_changes != seen_changes:  # something was written since: look again
                entries = backlog(conn, focus, self._clock.now())
                if not entries or entries[0] != entry:
                    raise VersionConflict()
            sessions_repo.insert_session(
                conn,
                id=sid,
                focus_kind=focus.kind,
                focus_ref=focus.ref,
                job_id=focus.ref if focus.kind == "job" else None,
                length=length,
                now=now,
            )
            self.reset_suggestion(conn, now)
            return True

        task = tasks.enqueue_if(
            ENRICHMENT_QUESTION,
            "session:" + sid,
            input_ref=_request_ref(sid, tier, entry),
            eligible=eligible,
        )
        return SessionStart(sid, task)

    @staticmethod
    def reset_suggestion(conn: sqlite3.Connection, now: str) -> None:
        value = json.dumps({"last_reset": now})
        current = settings_repo.get_setting(conn, "session_suggestion")
        if current:
            if not settings_repo.update_setting(
                conn, current.key, value, expected_version=current.version, now=now
            ):
                raise VersionConflict()
        else:
            settings_repo.insert_setting(conn, "session_suggestion", value, now)

    def next(
        self,
        sid: str,
        *,
        expected_version: int,
        tier: Tier = "fast",
        confirmation: Confirmation | None = None,
    ) -> SessionStart:
        with self._open().db.read() as conn:
            row = _session(conn, sid, expected_version)
            active = _active(conn, sid)
            if active:
                return SessionStart(sid, TaskRef(active.id, False))
            entry = self._next_item(conn, row)
        _, service = self._provider()
        checked = self._gate().check(service, tier=tier, confirmation=confirmation)
        if not isinstance(checked, Start):
            return SessionStart(sid, refusal=checked)
        tasks = self._tasks()
        if tasks is None:
            raise SessionNotFound()

        def eligible(conn: sqlite3.Connection) -> bool:
            row = _session(conn, sid, expected_version)
            if not _active(conn, sid) and self._next_item(conn, row) != entry:
                raise VersionConflict()
            return True

        ref = tasks.enqueue_if(
            ENRICHMENT_QUESTION,
            "session:" + sid,
            input_ref=_request_ref(sid, tier, entry),
            eligible=eligible,
        )
        return SessionStart(sid, ref)

    def _next_item(self, conn: sqlite3.Connection, row: sessions_repo.StoredSession) -> SessionItem:
        answers = sessions_repo.list_answers(conn, row.id)
        if any(a.outcome == "pending" for a in answers):
            raise SessionBusy()
        if len(answers) >= row.length:
            raise SessionEmpty()
        for answer in answers:
            if (
                answer.outcome == "answered"
                and answer.missing
                and not answer.follow_up_of
                and not any(a.follow_up_of == answer.id for a in answers)
            ):
                try:
                    return follow_up(conn, answer, item_for(conn, answer, self._clock.now()))
                except (SessionEmpty, VersionConflict):
                    continue
        asked = {a.question_id for a in answers}
        exclude = frozenset(
            r.kind + ":" + target_of(r)
            for r in enrichment_repo.list_questions(conn)
            if r.id in asked
        )
        entries = backlog(conn, _focus(row), self._clock.now(), exclude=exclude)
        if not entries:
            raise SessionEmpty()
        return entries[0]

    def outcome(
        self,
        sid: str,
        aid: str,
        outcome: Outcome,
        *,
        expected_version: int,
        session_version: int,
        later_choice: LaterChoice | None = None,
        zone: tzinfo = UTC,
    ) -> None:
        if outcome not in ("skipped", "not_sure", "later"):
            raise ValueError("Choose a session outcome.")
        now = self._clock.now()
        default = (
            SettingsService(self._open().db, self._clock).load(SESSIONS).value.ask_later_default
        )
        choice = later_choice or default
        with self._open().db.transaction() as conn:
            _session(conn, sid, session_version)
            answer = sessions_repo.get_answer(conn, aid)
            if answer is None or answer.session_id != sid:
                raise SessionNotFound()
            if (
                answer.version != expected_version
                or answer.outcome != "pending"
                or _active(conn, sid)
            ):
                raise VersionConflict()
            question = enrichment_repo.get_question(conn, answer.question_id)
            if question is None:
                raise SessionNotFound()
            state = record_outcome(
                state_of(question), outcome, now, zone, choice, wording=answer.question
            )
            enrichment_repo.update_question(
                conn,
                question.id,
                state=state,
                now=now.isoformat(),
                expected_version=question.version,
            )
            sessions_repo.update_answer(
                conn,
                aid,
                answer_text=answer.answer_text,
                outcome=outcome,
                now=now.isoformat(),
                expected_version=expected_version,
                later_choice=str(choice) if outcome == "later" else None,
            )
            sessions_repo.touch_session(
                conn, sid, now=now.isoformat(), expected_version=session_version
            )

    def end(self, sid: str, *, expected_version: int) -> None:
        with self._open().db.transaction() as conn:
            _session(conn, sid, expected_version)
            active = _active(conn, sid)
            sessions_repo.end_session(
                conn, sid, now=self._clock.now().isoformat(), expected_version=expected_version
            )
        tasks = self._tasks()
        if active and tasks:
            tasks.request_cancel(active.id)

    def answer(
        self,
        sid: str,
        aid: str,
        text: str,
        *,
        expected_version: int,
        session_version: int,
        tier: Tier = "fast",
        confirmation: Confirmation | None = None,
    ) -> SessionStart:
        if (
            not text.strip()
            or len(text) > 4000
            or "\x00" in text
            or tier not in ("fast", "thorough")
        ):
            raise ValueError("Write an answer of up to 4,000 characters.")
        opened = self._open()
        with opened.db.read() as conn:
            row = _session(conn, sid)
            active = _active(conn, sid)
            answer = sessions_repo.get_answer(conn, aid)
            if row.state != "open":
                raise SessionBusy()
            if (
                active
                and active.type == STRUCTURE_ANSWER
                and answer
                and answer.session_id == sid
                and answer.outcome == "pending"
                and answer.answer_text == text
            ):
                return SessionStart(sid, TaskRef(active.id, False))
            _session(conn, sid, session_version)
            if active:
                raise SessionBusy()
            if answer is None or answer.session_id != sid:
                raise SessionNotFound()
            if answer.version != expected_version or answer.outcome != "pending":
                raise VersionConflict()
            initial = answer_sources(conn, answer, self._clock.now())
            if not _same_sources(conn, answer, initial.entry):
                raise VersionConflict()
        _, service = self._provider()
        checked = self._gate().check(service, tier=tier, confirmation=confirmation)
        if not isinstance(checked, Start):
            return SessionStart(sid, refusal=checked)
        tasks = self._tasks()
        if tasks is None:
            raise SessionNotFound()
        now = self._clock.now().isoformat()
        submitted = replace(answer, answer_text=text, version=answer.version + 1, updated_at=now)
        prepared = replace(
            initial, answer=submitted, own_answers=(*initial.entry.personal_answers, text)
        )

        def eligible(conn: sqlite3.Connection) -> bool:
            _session(conn, sid, row.version)
            if _active(conn, sid):
                raise VersionConflict()
            fresh = sessions_repo.get_answer(conn, aid)
            if fresh is None or answer_sources(conn, fresh, self._clock.now()) != initial:
                raise VersionConflict()
            sessions_repo.update_answer(
                conn,
                aid,
                answer_text=text,
                outcome="pending",
                now=now,
                expected_version=expected_version,
            )
            sessions_repo.touch_session(conn, sid, now=now, expected_version=session_version)
            return True

        task = tasks.enqueue_if(
            STRUCTURE_ANSWER,
            "session:" + sid,
            input_ref=json.dumps(
                {"session": sid, "answer": aid, "tier": tier, "source": prepared.stamp}
            ),
            eligible=eligible,
        )
        return SessionStart(sid, task)

    def _answer_input(
        self, sid: str, aid: str, stamp: str
    ) -> tuple[sessions_repo.StoredSession, AnswerSources]:
        with self._open().db.read() as conn:
            session = _session(conn, sid)
            answer = sessions_repo.get_answer(conn, aid)
            if (
                session.state != "open"
                or answer is None
                or answer.session_id != sid
                or answer.outcome != "pending"
                or answer.answer_text is None
            ):
                raise SessionBusy()
            data = answer_sources(conn, answer, self._clock.now())
            if data.stamp != stamp or not _same_sources(conn, answer, data.entry):
                raise VersionConflict()
            return session, data

    async def run_answer(self, ctx: SummaryContext) -> ResultWriter:
        try:
            ref = json.loads(ctx.input_ref or "")
            sid, aid, tier, stamp = ref["session"], ref["answer"], ref["tier"], ref["source"]
            if (
                set(ref) != {"session", "answer", "tier", "source"}
                or not all(is_safe_id(v) for v in (sid, aid, stamp))
                or tier not in ("fast", "thorough")
            ):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise SessionNotFound() from None
        sid, aid, stamp = str(sid), str(aid), str(stamp)
        await ctx.heartbeat(0)
        session, data = await asyncio.to_thread(self._answer_input, sid, aid, stamp)
        settings, service = await asyncio.to_thread(self._provider)
        opened, usage = self._open(), self._usage()
        if usage is None:
            raise SessionNotFound()
        try:
            provider = self._factory.ai_provider(settings, data_dir=opened.data_dir)
        except ValueError:
            raise ProviderNotConfigured() from None
        runner = self._factory.task_runner(provider, lambda: settings, data_dir=opened.data_dir)
        target_block = prompt_fact_block((data.target,) if data.target else ())
        generalized = target_block.generalize or bool(data.generalized_roles)
        inputs = {
            "question": data.answer.question,
            "answers": json.dumps(data.own_answers, ensure_ascii=False),
            "target": FactPromptBlock(
                json.dumps({"note": data.note, "fact": str(target_block)}, ensure_ascii=False),
                generalized,
            ),
            "choices": json.dumps(
                {
                    "roles": data.roles,
                    "generalized_roles": sorted(data.generalized_roles),
                    "skills": list(data.offered_skills),
                },
                ensure_ascii=False,
            ),
            "session_id": sid,
        }
        await ctx.heartbeat(10)
        result = await runner.run_task(
            STRUCTURE_ANSWER,
            inputs,
            ctx.cancel_flag,
            meter=ServiceUsageMeter(usage, service),
            tier=cast(Tier, tier),
            task_id=ctx.task_id,
        )
        ctx.check_cancelled()
        resolved = resolve_structured_answer(
            result.data,
            data.own_answers,
            note=data.note,
            target=data.target,
            roles=data.roles,
            generalized_roles=data.generalized_roles,
            known_skills=data.skills,
        )
        privacy = (
            Privacy.GENERALIZE
            if (data.target and data.target.generalize)
            or resolved.role_key in data.generalized_roles
            else Privacy.NORMAL
        )
        draft = DraftInput(
            type=resolved.type,
            text=resolved.content.text,
            situation=resolved.content.situation,
            action=resolved.content.action,
            result=resolved.content.result,
            role_key=resolved.role_key,
            skills=resolved.skills,
            original_answer="\n\n".join(data.own_answers),
            needs_check=resolved.needs_check,
            privacy=privacy,
        )
        origin = DraftOrigin(
            "session", provider.name, result.model, result.prompt_id, result.prompt_version
        )

        def write(conn: sqlite3.Connection) -> str:
            _session(conn, sid, session.version)
            fresh = sessions_repo.get_answer(conn, aid)
            if fresh is None or answer_sources(conn, fresh, self._clock.now()) != data:
                raise VersionConflict()
            facts = FactService(opened.db, self._clock, self._ids)
            roles = {k: k for k in data.roles}
            if data.draft_id:
                made = facts.revise_session_draft_in(
                    conn,
                    data.draft_id,
                    origin,
                    draft,
                    expected_version=cast(int, data.draft_version),
                    existing_roles=roles,
                )
            else:
                (made,) = facts.create_drafts_in(conn, origin, (draft,), existing_roles=roles)
            now = self._clock.now()
            if (
                data.entry.item.kind == "capture_note"
                and not data.entry.follow_up_of
                and not capture_repo.resolve_note(
                    conn, data.entry.item.target_key, fact_id=made.id, now=now.isoformat()
                )
            ):
                raise VersionConflict()
            question = enrichment_repo.get_question(conn, fresh.question_id)
            if question is None:
                raise SessionNotFound()
            enrichment_repo.update_question(
                conn,
                question.id,
                state=record_outcome(state_of(question), "answered", now, UTC),
                now=now.isoformat(),
                expected_version=question.version,
            )
            sessions_repo.update_answer(
                conn,
                aid,
                answer_text=fresh.answer_text,
                outcome="answered",
                now=now.isoformat(),
                expected_version=fresh.version,
                draft_fact_id=made.id,
                missing=resolved.missing,
                flagged_claims=tuple(asdict(f) for f in resolved.flags),
                structure_provider=provider.name,
                structure_model=result.model,
                structure_prompt_id=result.prompt_id,
                structure_prompt_version=result.prompt_version,
                structure_task_id=ctx.task_id,
            )
            sessions_repo.touch_session(
                conn, sid, now=now.isoformat(), expected_version=session.version
            )
            return "session_answer:" + aid

        return write

    def _question_input(
        self, sid: str, item_id: str, source_id: str
    ) -> tuple[
        sessions_repo.StoredSession,
        SessionItem,
        tuple[sessions_repo.StoredAnswer, ...],
        tuple[str, ...],
        tuple[str, ...],
    ]:
        with self._open().db.read() as conn:
            row = _session(conn, sid)
            if row.state != "open":
                raise SessionBusy()
            entry = self._next_item(conn, row)
            if entry.identifier != item_id or entry.source_stamp != source_id:
                raise VersionConflict()
            question = enrichment_repo.find_question(conn, entry.item.kind, entry.item.target_key)
            # Withdrawn target wording must not return through the conversation history.
            valid = {e.item.key: e for e in sources(conn, _focus(row), self._clock.now())}
            known = tuple(s.name for s in skills_repo.list_skills(conn))
            usable = {
                r.id: valid[r.kind + ":" + target_of(r)]
                for r in enrichment_repo.list_questions(conn)
                if r.kind + ":" + target_of(r) in valid
            }
            history = tuple(
                a
                for a in sessions_repo.list_answers(conn, sid)
                if a.question_id in usable
                and _same_sources(conn, a, usable[a.question_id])
                and not any(f.generalize for f in facts_for_ai(usable[a.question_id].facts))
                and _output_safe(a.question, "", usable[a.question_id], known)
            )
            previous = sessions_repo.not_sure_answers(conn, question.id) if question else ()
            earlier = tuple(
                wording
                for wording in (question.earlier_wordings if question else ())
                if any(a.question == wording and _same_sources(conn, a, entry) for a in previous)
                if _output_safe(wording, "", entry, known)
            )
            return row, entry, history, earlier, known

    async def run_question(self, ctx: SummaryContext) -> ResultWriter:
        sid, tier, item_id, source_id = _parse_ref(ctx.input_ref)
        await ctx.heartbeat(0)
        row, entry, history, earlier, known = await asyncio.to_thread(
            self._question_input, sid, item_id, source_id
        )
        settings, service = await asyncio.to_thread(self._provider)
        opened, usage = self._open(), self._usage()
        if usage is None:
            raise SessionNotFound()
        try:
            provider = self._factory.ai_provider(settings, data_dir=opened.data_dir)
        except ValueError:
            raise ProviderNotConfigured() from None
        runner = self._factory.task_runner(provider, lambda: settings, data_dir=opened.data_dir)
        facts = facts_for_ai(entry.facts)
        block = prompt_fact_block(facts)
        inputs: dict[str, Any] = {
            "item": entry.payload,
            "facts": FactPromptBlock(
                json.dumps({"facts": str(block)}, ensure_ascii=False), block.generalize
            ),
            "session_so_far": json.dumps(
                [
                    {"question": a.question, "answer": a.answer_text, "outcome": a.outcome}
                    for a in history
                ],
                ensure_ascii=False,
            ),
            "earlier_wordings": json.dumps(earlier, ensure_ascii=False),
            "session_id": sid,
        }
        await ctx.heartbeat(10)
        result = await runner.run_task(
            ENRICHMENT_QUESTION,
            inputs,
            ctx.cancel_flag,
            meter=ServiceUsageMeter(usage, service),
            tier=tier,
            task_id=ctx.task_id,
        )
        ctx.check_cancelled()
        question, why = str(result.data["question"]).strip(), str(result.data["why"]).strip()
        fallback = not _output_safe(question, why, entry, known)
        if fallback:
            question, why = fallback_question(entry.item.kind, entry.label)
        aid = self._ids.new()

        def write(conn: sqlite3.Connection) -> str:
            current = _session(conn, sid, row.version)
            # Re-read source privacy and versions before accepting a paid result.
            fresh = self._next_item(conn, current)
            if fresh != entry:
                raise VersionConflict()
            now = self._clock.now().isoformat()
            qid = _ensure_question(conn, entry, self._ids, now)
            sessions_repo.insert_answer(
                conn,
                id=aid,
                session_id=sid,
                position=len(sessions_repo.list_answers(conn, sid)) + 1,
                question_id=qid,
                question=question,
                why=why,
                now=now,
                question_provider=provider.name,
                question_model=result.model,
                question_prompt_id=result.prompt_id,
                question_prompt_version=result.prompt_version,
                question_task_id=ctx.task_id,
                follow_up_of=entry.follow_up_of,
                fallback=fallback,
            )
            sessions_repo.touch_session(conn, sid, now=now, expected_version=row.version)
            return "session_answer:" + aid

        return write


__all__ = ["SessionAlreadyOpen", "SessionEmpty", "SessionFocus", "SessionService"]
