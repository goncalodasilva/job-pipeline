"""Refine with AI (M3.8; AC-M3.4, AC-M3.5; spec 3.1 items 1 and 5, 3.3; ADR-089 items 5, 6
and 8, ADR-090 item 10).

A conversation about one fact lives in a ``refine_sessions`` row: at most one is open per
fact. Each turn is the background task ``refine_fact`` (dedupe target: the fact's id,
input: the conversation's id), counted through the usage meter after the route asked the
budget gate. A turn sends the fact (a draft or a confirmed fact, never a "never use" one)
with the usable confirmed facts as context, all through ``facts_for_ai``; the fact's wording
and the user's answers are delimited as untrusted. What it gets back (questions, a
suggestion, the claims the honesty check flagged and who answered) is written to the
conversation only, in the task's result transaction: a turn never changes a fact.

Start queues the first turn only; after it the conversation goes on by answers, at most
three, each to the questions of the turn before. Accept saves the wording the user posts as
the fact's next version (source ``refine``, with the suggestion's provider, model and
prompt), and only when every claim that wording makes beyond the fact bank is acknowledged
or edited out: the server checks the posted wording again, against the sources of the
suggestion's request, whatever was flagged before. A "generalize names" fact sent supports
numbers and skills there, never its names. Accept is refused when the fact changed since
the suggestion was made, or a fact sent became "never use". The acknowledged claims are
kept with the closed conversation as the user's assertion. Discard closes it and leaves the
fact as it was; both cancel a turn still on its way.

The fact's wording and the answers are personal: none is logged, stored in a task row or put
in an error message (spec 2.10). Everything here blocks except ``run``.
"""

import asyncio
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, Protocol

from app.domain.clock import Clock
from app.domain.errors import (
    FactInputInvalid,
    FactNotFound,
    ProviderNotConfigured,
    RefineAnswersUsed,
    RefineBusy,
    RefineClaimsUnacknowledged,
    RefineContextChanged,
    RefineNeverUse,
    RefineNoSuggestion,
    RefineNotAvailable,
    RefineNotOpen,
    RefineStopping,
    RefineSuggestionStale,
    RefineTurnRunning,
    VersionConflict,
)
from app.domain.fact_selection import (
    AIFact,
    FactRecord,
    fact_label,
    fact_text,
    facts_for_ai,
    prompt_fact_block,
    refine_target,
)
from app.domain.facts import FactContent, FactStatus, FactType, Privacy, clean_part
from app.domain.honesty import Claim, new_claims
from app.domain.ids import IdFactory, is_safe_id
from app.domain.settings import AIProviderSettings, Tier
from app.domain.tasks import TaskRef, dedupe_key
from app.domain.usage import SERVICE_BY_PROVIDER_KIND, ServiceId
from app.modules.ai import AIProviderFactory, CancelToken
from app.modules.storage import (
    Database,
    facts_repo,
    refine_repo,
    roles_repo,
    skills_repo,
    tasks_repo,
)
from app.services.fact_history import PartView, diff_parts
from app.services.facts import SOURCE_REFINE, DraftOrigin, FactService, FactView
from app.services.tasks import TaskService
from app.services.usage import UsageService
from app.services.usage_meter import ServiceUsageMeter

REFINE_FACT: Final = "refine_fact"  # the task type, the AI task and the prompt id
REFINE_FACT_LABEL: Final = "Refine with AI"
MAX_ANSWER_ROUNDS: Final = 3
MAX_ANSWER_CHARS: Final = 2_000
FAST: Final[Tier] = "fast"
STATE_OPEN: Final = "open"
STATE_ACCEPTED: Final = "accepted"
STATE_DISCARDED: Final = "discarded"
MSG_ANSWER_NEEDED: Final = "Write an answer before sending it."
MSG_ANSWER_TOO_LONG: Final = f"Keep the answer under {MAX_ANSWER_CHARS:,} characters."
MSG_WORDING_NEEDED: Final = "Write the wording before accepting it."

_SEPARATOR: Final = "/"  # never part of an id
_RESULT_PREFIX: Final = "refine_session"
_PARTS: Final = ("text", "situation", "action", "result")

_log = logging.getLogger(__name__)

ResultWriter = Callable[[sqlite3.Connection], str]
"""Stores a turn's result on the connection of the task's result transaction and returns
the ``result_ref``; it must not commit."""


class OpenWorkspace(Protocol):
    @property
    def data_dir(self) -> Path: ...

    @property
    def db(self) -> Database: ...


class TurnContext(Protocol):
    """The part of the runner's ``TaskContext`` a turn uses."""

    @property
    def task_id(self) -> str: ...

    @property
    def input_ref(self) -> str | None: ...

    @property
    def cancel_flag(self) -> CancelToken: ...

    def heartbeat(self, progress: int | None = None) -> Awaitable[None]: ...

    def check_cancelled(self) -> None: ...


@dataclass(frozen=True)
class RefineTurn:
    """One round of the conversation: the questions the AI asked, and the user's answer.
    ``fact_numbers`` are the facts its request sent; ``generalized`` those of them sent
    marked "generalize names"."""

    questions: tuple[str, ...]
    answer: str | None = None
    fact_numbers: tuple[int, ...] = ()
    generalized: tuple[int, ...] = ()


@dataclass(frozen=True)
class StartPlan:
    """What a turn will be counted on: the budget gate is asked about exactly this."""

    service: ServiceId
    tier: Tier


@dataclass(frozen=True)
class RefineView:
    """An open conversation as the page shows it (M3.9 builds the panel on this)."""

    fact_id: str
    questions: tuple[str, ...]
    suggestion: FactContent | None
    flagged: tuple[str, ...]
    answers_left: int
    practice: bool


@dataclass(frozen=True)
class RefineClaim:
    """A claim of the suggestion that isn't in the fact bank (M3.9). ``edits_out``: taking
    its text out of the wording drops it (a detail the model reported adding drops only
    when the suggestion wrote it as reported); otherwise only ticking it will do."""

    text: str
    edits_out: bool


PHASE_CLOSED: Final = "closed"
PHASE_WAITING: Final = "waiting"
PHASE_FAILED: Final = "failed"
PHASE_ANSWERED: Final = "answered"


@dataclass(frozen=True)
class RefinePanel:
    """The Refine with AI panel of a fact (M3.9). ``phase``: no open conversation
    (closed), a turn queued or running (waiting), the last turn ended without an answer
    (failed: an answer's or the first turn failed or was cancelled) or the AI answered.
    ``blocked`` is why a conversation can't start (never use, archived). ``changes`` is the
    suggestion as a word diff against the fact's current wording; ``unchanged`` when it
    marks nothing."""

    fact_id: str
    version: int
    story: bool
    phase: str
    blocked: str | None
    view: RefineView | None
    changes: tuple[PartView, ...]
    unchanged: bool
    claims: tuple[RefineClaim, ...]
    stale: bool = False
    """The suggestion was made for an earlier version of the fact: Accept would be refused."""
    retry_start: bool = False
    """The first turn failed: Start may queue it again."""


@dataclass(frozen=True)
class _Context:
    """What one turn or one Accept is checked against: the fact as sent (and the id of its
    current version), the confirmed facts sent with it, and the controlled skills list."""

    target: AIFact
    context: tuple[AIFact, ...]
    known_skills: tuple[str, ...]
    version_id: str


# --- the prompt's inputs and the honesty check's sources (pure) -------------------------------


def _conversation(turns: Sequence[RefineTurn]) -> str:
    blocks = []
    for turn in turns:
        lines = [f"Question: {question}" for question in turn.questions]
        lines.append(f"Answer: {turn.answer}" if turn.answer else "Answer: (not answered)")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def refine_prompt_inputs(
    *, fact: AIFact, context: Sequence[AIFact], turns: Sequence[RefineTurn]
) -> Mapping[str, object]:
    """The ``refine_fact`` prompt's inputs: the fact to refine (its wording untrusted: a
    draft can come from a CV), the confirmed facts sent as context through the code-owned
    block (with the generalize instruction once, for them or for the fact itself; their
    text is untrusted too, as it can come from a CV), and the conversation so far
    (untrusted), shown once a turn has asked something."""
    return {
        "fact_id": str(fact.ref),
        "fact_label": fact_label(fact),
        "fact": fact_text(fact),
        "facts": prompt_fact_block(context, also_generalize=fact.generalize),
        "has_turns": bool(turns),
        "conversation": _conversation(turns),
    }


def _fact_sources(fact: AIFact) -> list[str | None]:
    return [*(getattr(fact.content, part) for part in _PARTS), fact.role, *fact.skills]


def allowed_sources(
    target: AIFact, context: Iterable[AIFact], turns: Iterable[RefineTurn]
) -> list[str | None]:
    """What a suggestion may say (ADR-089 item 5): the fact, its role and skills, the
    user's own answers and the confirmed facts sent. Never the AI's own questions. The
    confirmed facts marked "generalize names" are ``name_hidden_sources`` instead."""
    sources = _fact_sources(target)
    for fact in context:
        if not fact.generalize:
            sources += _fact_sources(fact)
    return sources + [turn.answer for turn in turns]


def name_hidden_sources(context: Iterable[AIFact]) -> list[str | None]:
    """The confirmed facts sent that are marked "generalize names": a suggestion may use
    their numbers and skills, but not their names (ADR-089 item 6)."""
    return [source for fact in context if fact.generalize for source in _fact_sources(fact)]


def wording_of(content: FactContent) -> str:
    """A wording's parts as one text to check, one part per line."""
    return "\n".join(part for part in (getattr(content, name) for name in _PARTS) if part)


def _folded(text: str) -> str:
    return " ".join(text.casefold().split())


def details_still_said(details: Iterable[str], *, suggestion: str, wording: str) -> tuple[str, ...]:
    """The details the model reported adding that the posted wording may still say. A
    detail is taken as edited out only when the suggestion wrote it as it was reported and
    the wording no longer does; any other is kept, to be acknowledged."""
    said, shown = _folded(suggestion), _folded(wording)
    return tuple(
        detail
        for detail in details
        if not (_folded(detail) in said and _folded(detail) not in shown)
    )


# --- stored JSON --------------------------------------------------------------------------------


def _turns(raw: str) -> list[RefineTurn]:
    return [
        RefineTurn(
            questions=tuple(item.get("questions", ())),
            answer=item.get("answer"),
            fact_numbers=tuple(item.get("fact_numbers", ())),
            generalized=tuple(item.get("generalized", ())),
        )
        for item in json.loads(raw)
    ]


def _turns_json(turns: Sequence[RefineTurn]) -> str:
    items = [
        {
            "questions": list(turn.questions),
            "answer": turn.answer,
            "fact_numbers": list(turn.fact_numbers),
            "generalized": list(turn.generalized),
        }
        for turn in turns
    ]
    return json.dumps(items, ensure_ascii=False)


def _claims_json(claims: Iterable[Claim]) -> str:
    return json.dumps(
        [{"kind": claim.kind, "text": claim.text} for claim in claims], ensure_ascii=False
    )


def _stored_claims(raw: str) -> list[dict[str, str]]:
    return [item for item in json.loads(raw) if isinstance(item, dict)]


def _suggestion(raw: str | None) -> FactContent | None:
    if raw is None:
        return None
    data = json.loads(raw)
    return FactContent(*(data.get(part) for part in _PARTS))


def _content(data: Mapping[str, Any] | None) -> FactContent | None:
    if data is None:
        return None
    content = FactContent(*(clean_part(data.get(part)) for part in _PARTS))
    return content if any(content.normalized()) else None


def _parse_input(input_ref: str | None) -> tuple[str, Tier | None]:
    session_id, _, tier = (input_ref or "").partition(_SEPARATOR)
    if not is_safe_id(session_id) or tier not in ("", FAST):
        raise RefineNotOpen(detail="refine_fact: the input names no conversation")
    return session_id, FAST if tier else None


def tier_from_form(form: Mapping[str, Sequence[str]]) -> Tier | None:
    """``fast`` when the form asks for one run on Fast (the budget dialog's offer); None,
    the prompt's own tier, for anything else."""
    return FAST if tuple(form.get("tier", ())) == (FAST,) else None


# --- reading the fact bank ----------------------------------------------------------------------


def _role_labels(conn: sqlite3.Connection) -> dict[str, str]:
    labels = {}
    for role in roles_repo.list_roles(conn):
        parts = [part for part in (role.title.strip(), role.employer.strip()) if part]
        labels[role.id] = " at ".join(parts)
    return labels


def _record(
    conn: sqlite3.Connection, row: facts_repo.StoredFact, roles: Mapping[str, str]
) -> FactRecord:
    current = row.current
    return FactRecord(
        number=row.number,
        version=current.version,
        is_current=True,
        status=FactStatus(row.status),
        privacy=Privacy(row.privacy),
        content=FactContent(current.text, current.situation, current.action, current.result),
        role=roles.get(row.role_id or "") or None,
        skills=facts_repo.skill_names(conn, row.id),
    )


def _refinable(row: facts_repo.StoredFact) -> None:
    """Refuse a fact that may not be refined, with its reason: never use (ADR-089 item 6),
    or discarded or archived."""
    if row.privacy == Privacy.NEVER_USE:
        raise RefineNeverUse(detail=f"fact {row.id} is never use")
    if row.status not in (FactStatus.DRAFT, FactStatus.CONFIRMED):
        raise RefineNotAvailable(detail=f"fact {row.id} is {row.status}")


def _fact_row(conn: sqlite3.Connection, fact_id: str) -> facts_repo.StoredFact:
    row = facts_repo.get_fact(conn, fact_id) if is_safe_id(fact_id) else None
    if row is None:
        raise FactNotFound(detail="no such fact")
    return row


def _bank_context(conn: sqlite3.Connection, fact_id: str) -> _Context:
    """The fact to refine and its context, read on ``conn``: every other fact goes through
    ``facts_for_ai`` (confirmed, current, not never use). Raises like ``_refinable``."""
    roles = _role_labels(conn)
    records: list[FactRecord] = []
    target_record: FactRecord | None = None
    version_id = ""
    for row in facts_repo.list_facts(conn):
        if row.id == fact_id:
            _refinable(row)
            target_record = _record(conn, row, roles)
            version_id = row.current.id
        else:
            records.append(_record(conn, row, roles))
    target = None if target_record is None else refine_target(target_record)
    if target is None:
        raise FactNotFound(detail="no such fact")
    skills = tuple(skill.name for skill in skills_repo.list_skills(conn))
    return _Context(target, facts_for_ai(records), skills, version_id)


def _open_session(conn: sqlite3.Connection, fact_id: str) -> refine_repo.StoredSession:
    session = refine_repo.get_open(conn, fact_id)
    if session is None:
        raise RefineNotOpen(detail="no open conversation")
    return session


def _active_turn(conn: sqlite3.Connection, fact_id: str) -> str | None:
    """The id of the fact's turn still queued or running, if any."""
    active = tasks_repo.get_active(conn, dedupe_key(REFINE_FACT, fact_id))
    return None if active is None else active.id


def _turn_active(conn: sqlite3.Connection, fact_id: str) -> bool:
    return _active_turn(conn, fact_id) is not None


def _startable(conn: sqlite3.Connection, fact_id: str, session: refine_repo.StoredSession) -> None:
    """Start continues an open conversation only while its first turn hasn't run: once
    the AI has asked, the conversation goes on by answers. Raises ``RefineContextChanged``,
    ``RefineBusy`` (a later turn is on its way) or ``VersionConflict`` (the page that
    offered Start is out of date)."""
    _history_allowed(conn, session)
    if _turns(session.turns):
        if _turn_active(conn, fact_id):
            raise RefineTurnRunning(detail="a turn is on its way")
        raise VersionConflict(detail="the conversation already had a turn")


def _history_allowed(conn: sqlite3.Connection, session: refine_repo.StoredSession) -> None:
    """Old AI questions can quote context that a later privacy change forbids sending."""
    sent = {number for turn in _turns(session.turns) for number in turn.fact_numbers}
    if any(
        row.number in sent and row.privacy == Privacy.NEVER_USE
        for row in facts_repo.list_facts(conn)
    ):
        raise RefineContextChanged(detail="previously sent context is now never use")


def _names_still_allowed(
    conn: sqlite3.Connection, session: refine_repo.StoredSession, target_number: int
) -> None:
    """A suggestion may use the names of the facts sent with it, unless they were sent
    marked "generalize names". A fact switched to "generalize names" since it was sent would
    still lend its names to Accept through the stored sources, so Accept is refused (the
    fact being refined keeps its own wording's names)."""
    turns = _turns(session.turns)
    named = {number for turn in turns for number in turn.fact_numbers} - {target_number}
    named -= {number for turn in turns for number in turn.generalized}
    if any(
        row.number in named and row.privacy == Privacy.GENERALIZE
        for row in facts_repo.list_facts(conn)
    ):
        raise RefineContextChanged(detail="previously sent context is now generalize names")


class FactRefineService:
    def __init__(
        self,
        *,
        factory: AIProviderFactory,
        clock: Clock,
        ids: IdFactory,
        workspace: Callable[[], OpenWorkspace | None],
        settings: Callable[[], AIProviderSettings | None],
        tasks: Callable[[], TaskService | None],
        usage: Callable[[], UsageService | None],
    ) -> None:
        """``workspace``, ``settings`` (blocking: a database read), ``tasks`` and ``usage``
        answer None while no workspace is open."""
        self._factory = factory
        self._clock = clock
        self._ids = ids
        self._workspace = workspace
        self._settings = settings
        self._tasks = tasks
        self._usage = usage

    # --- for routes (blocking) ------------------------------------------------------------------

    def plan_start(self, fact_id: str, *, tier: Tier | None = None) -> StartPlan:
        """What a start would be counted on, for the budget gate. Raises ``FactNotFound``,
        ``RefineNeverUse``, ``RefineNotAvailable``, ``RefineContextChanged``, ``RefineBusy``,
        ``VersionConflict`` or ``ProviderNotConfigured``. Writes nothing."""
        with self._db().read() as conn:
            _refinable(_fact_row(conn, fact_id))
            session = refine_repo.get_open(conn, fact_id)
            if session is not None:
                _startable(conn, fact_id, session)
            elif _turn_active(conn, fact_id):
                raise RefineStopping(detail="the previous conversation's task is still active")
        return self._plan(tier)

    def start(self, fact_id: str, *, tier: Tier | None = None) -> TaskRef:
        """Open a conversation about the fact (or continue its open one whose first turn
        hasn't run) and queue a turn; a turn already queued or running is returned instead.
        Raises as ``plan_start``."""
        now = self._now()
        with self._db().transaction() as conn:
            row = _fact_row(conn, fact_id)
            _refinable(row)
            session = refine_repo.get_open(conn, fact_id)
            if session is None:
                if _turn_active(conn, fact_id):
                    raise RefineStopping(detail="the previous conversation's task is still active")
                session_id = self._ids.new()
                refine_repo.insert_session(
                    conn,
                    session_id=session_id,
                    fact_id=fact_id,
                    base_version_id=row.current.id,
                    now=now,
                )
            else:
                _startable(conn, fact_id, session)
                session_id = session.id
        return self._enqueue(fact_id, session_id, tier)

    def plan_answer(self, fact_id: str, answer: str, *, tier: Tier | None = None) -> StartPlan:
        """What the turn an answer starts would be counted on. Raises ``FactInputInvalid``
        (blank or too long), ``FactNotFound``, ``RefineNeverUse``, ``RefineNotAvailable``,
        ``RefineNotOpen``, ``RefineBusy``, ``RefineAnswersUsed`` or
        ``ProviderNotConfigured``. Writes nothing."""
        _checked_answer(answer)
        with self._db().read() as conn:
            self._answerable(conn, fact_id)
        return self._plan(tier)

    def answer(self, fact_id: str, answer: str, *, tier: Tier | None = None) -> TaskRef:
        """Add the user's answer to the open conversation and queue the next turn. Raises
        as ``plan_answer``."""
        text = _checked_answer(answer)
        now = self._now()
        with self._db().transaction() as conn:
            session = self._answerable(conn, fact_id)
            turns = _turns(session.turns)
            asked = turns[-1]  # _answerable: the last turn asked and is unanswered
            turns[-1] = replace(asked, answer=text)
            if not refine_repo.set_answers(
                conn,
                session.id,
                turns=_turns_json(turns),
                answer_rounds=session.answer_rounds + 1,
                expected_version=session.version,
                now=now,
            ):
                raise VersionConflict(detail="the conversation changed while answering")
        return self._enqueue(fact_id, session.id, tier)

    def accept(
        self,
        fact_id: str,
        wording: FactContent,
        *,
        expected_version: int,
        acknowledged: Iterable[str] = (),
    ) -> FactView:
        """Save ``wording`` as the fact's next version and close the conversation as
        accepted, in one transaction; a turn still queued or running is cancelled. The
        wording is checked here against the fact bank: every claim it makes beyond it must
        be in ``acknowledged`` (the claim's text), or ``RefineClaimsUnacknowledged`` names
        those that aren't and nothing is written. Also raises ``FactNotFound``,
        ``RefineNeverUse``, ``RefineNotAvailable``, ``RefineNotOpen``,
        ``RefineContextChanged`` (a fact sent became "never use"), ``RefineNoSuggestion``,
        ``VersionConflict`` (a stale ``expected_version``, or a suggestion made for an
        earlier version of the fact) or ``FactInputInvalid``."""
        content = FactContent(*(clean_part(getattr(wording, part)) for part in _PARTS))
        db = self._db()
        facts = FactService(db, self._clock, self._ids)
        asserted = frozenset(acknowledged)
        now = self._now()
        with db.transaction() as conn:
            row = _fact_row(conn, fact_id)
            _refinable(row)
            session = _open_session(conn, fact_id)
            _history_allowed(conn, session)
            _names_still_allowed(conn, session, row.number)
            suggestion = _suggestion(session.suggestion)
            if suggestion is None:
                raise RefineNoSuggestion(detail="nothing to accept")
            if row.version != expected_version:
                raise VersionConflict(detail=f"fact {fact_id} changed elsewhere")
            if not any(content.normalized()):
                raise FactInputInvalid("text", MSG_WORDING_NEEDED)
            snapshot = _snapshot(session)
            if snapshot.get("fact_version_id") != row.current.id:
                raise RefineSuggestionStale(detail="the suggestion was made for an earlier version")
            claims = _claims_in(session, snapshot, suggestion, content)
            missing = tuple(claim.text for claim in claims if claim.text not in asserted)
            if missing:
                raise RefineClaimsUnacknowledged(missing, detail=f"{len(missing)} claim(s)")
            origin = DraftOrigin(
                source=SOURCE_REFINE,
                provider=session.provider,
                model=session.model,
                prompt_id=session.prompt_id,
                prompt_version=session.prompt_version,
            )
            view = facts.save_refined_in(
                conn,
                fact_id,
                content,
                expected_version=expected_version,
                origin=origin,
                detail={"refine_session_id": session.id},
            )
            _close(conn, session, STATE_ACCEPTED, _claims_json(claims), now)
            active = _active_turn(conn, fact_id)
        self._cancel(active)
        _log.info("Refine %s accepted (%d claim(s) asserted)", session.id, len(claims))
        return view

    def discard(self, fact_id: str) -> None:
        """Close the open conversation; the fact is untouched. A turn still queued or
        running is cancelled. Raises ``FactNotFound``, ``RefineNotOpen`` or
        ``VersionConflict``."""
        with self._db().transaction() as conn:
            _fact_row(conn, fact_id)
            session = _open_session(conn, fact_id)
            _close(conn, session, STATE_DISCARDED, "[]", self._now())
            active = _active_turn(conn, fact_id)
        self._cancel(active)
        _log.info("Refine %s discarded", session.id)

    def view(self, fact_id: str) -> RefineView | None:
        """The fact's open conversation, or None. Reads only."""
        with self._db().read() as conn:
            session = refine_repo.get_open(conn, fact_id) if is_safe_id(fact_id) else None
        return None if session is None else _view_of(fact_id, session)

    def panel(self, fact_id: str) -> RefinePanel:
        """What the fact's Refine with AI panel shows. Reads only. Raises ``FactNotFound``."""
        with self._db().read() as conn:
            row = _fact_row(conn, fact_id)
            session = refine_repo.get_open(conn, fact_id)
            waiting = session is not None and _turn_active(conn, fact_id)
        blocked: str | None = None
        try:
            _refinable(row)
        except (RefineNeverUse, RefineNotAvailable) as error:
            blocked = error.user_message
        view = None if session is None else _view_of(fact_id, session)
        suggestion = None if view is None else view.suggestion
        current = row.current
        changes, note = (
            diff_parts(
                FactContent(current.text, current.situation, current.action, current.result),
                suggestion,
            )
            if suggestion is not None
            else ((), None)
        )
        phase = _phase(session, waiting=waiting)
        return RefinePanel(
            fact_id=fact_id,
            version=row.version,
            story=row.type == FactType.STORY,
            phase=phase,
            stale=session is not None
            and suggestion is not None
            and _suggested_for(session) != current.id,
            retry_start=phase == PHASE_FAILED and session is not None and not _turns(session.turns),
            blocked=blocked,
            view=view,
            changes=changes,
            unchanged=note is not None,
            claims=() if session is None else _panel_claims(session, suggestion),
        )

    # --- for the task handler -------------------------------------------------------------------

    async def run(self, ctx: TurnContext) -> ResultWriter:
        """Make one turn's AI call and return the writer for the task's result transaction.
        Nothing is written here. Raises ``RefineNotOpen`` when the conversation is gone,
        ``RefineNeverUse`` or ``RefineNotAvailable`` (before any request) when the fact
        may no longer be refined; provider errors, ``InvalidModelOutput`` and the cancel
        signal propagate."""
        session_id, tier = _parse_input(ctx.input_ref)
        await ctx.heartbeat(0)
        workspace = self._open()
        session, bank = await asyncio.to_thread(self._turn_input, session_id)
        settings, service = await asyncio.to_thread(self._provider)
        usage = self._usage()
        if usage is None:
            raise RefineNotOpen(detail="no workspace is open")
        try:
            provider = self._factory.ai_provider(settings, data_dir=workspace.data_dir)
        except ValueError:  # the saved provider is no longer on offer
            raise ProviderNotConfigured(detail="the saved provider isn't on offer") from None
        runner = self._factory.task_runner(provider, lambda: settings, data_dir=workspace.data_dir)
        turns = _turns(session.turns)
        await ctx.heartbeat(10)
        result = await runner.run_task(
            REFINE_FACT,
            refine_prompt_inputs(fact=bank.target, context=bank.context, turns=turns),
            ctx.cancel_flag,
            meter=ServiceUsageMeter(usage, service),
            tier=tier,
            task_id=ctx.task_id,
        )
        ctx.check_cancelled()
        data = result.data
        suggestion = _content(data["suggestion"])
        claims: tuple[Claim, ...] = ()
        if suggestion is not None:
            claims = new_claims(
                wording_of(suggestion),
                allowed_sources(bank.target, bank.context, turns),
                known_skills=bank.known_skills,
                added_details=data["added_details"],
                name_hidden_sources=name_hidden_sources(bank.context),
            )
        questions = tuple(str(question).strip() for question in data["questions"])
        stored = refine_repo.TurnResult(
            turns=_turns_json(
                [
                    *turns,
                    RefineTurn(
                        questions,
                        fact_numbers=tuple(
                            fact.ref.number for fact in (bank.target, *bank.context)
                        ),
                        generalized=tuple(
                            fact.ref.number for fact in bank.context if fact.generalize
                        ),
                    ),
                ]
            ),
            suggestion=None if suggestion is None else _suggestion_json(suggestion, bank, turns),
            flagged_claims=_claims_json(claims),
            provider=provider.name,
            model=result.model,
            prompt_id=result.prompt_id,
            prompt_version=result.prompt_version,
            task_id=ctx.task_id,
        )
        _log.info(
            "Refine %s: %d question(s), suggestion: %s, %d claim(s) flagged",
            session_id,
            len(questions),
            suggestion is not None,
            len(claims),
        )
        return self._writer(session_id, session.version, stored)

    # --- helpers --------------------------------------------------------------------------------

    def _now(self) -> str:
        return self._clock.now().isoformat()

    def _open(self) -> OpenWorkspace:
        workspace = self._workspace()
        if workspace is None:  # setup sends every request to the guide until one is open
            raise RuntimeError("no workspace is open")
        return workspace

    def _db(self) -> Database:
        return self._open().db

    def _provider(self) -> tuple[AIProviderSettings, ServiceId]:
        """The saved provider and the usage service its calls are counted on: the same one
        for the budget gate and the meter."""
        settings = self._settings()
        service = SERVICE_BY_PROVIDER_KIND.get(settings.kind or "") if settings else None
        if settings is None or service is None:
            raise ProviderNotConfigured(detail="no provider is saved")
        return settings, service

    def _plan(self, tier: Tier | None) -> StartPlan:
        _settings, service = self._provider()
        return StartPlan(service, tier if tier is not None else FAST)

    def _enqueue(self, fact_id: str, session_id: str, tier: Tier | None) -> TaskRef:
        tasks = self._tasks()
        if tasks is None:
            raise RefineNotOpen(detail="no workspace is open")
        input_ref = session_id if tier is None else f"{session_id}{_SEPARATOR}{tier}"
        ref = tasks.enqueue(REFINE_FACT, fact_id, input_ref=input_ref)
        _log.info("Refine %s: task %s (new: %s)", session_id, ref.id, ref.created)
        return ref

    def _answerable(self, conn: sqlite3.Connection, fact_id: str) -> refine_repo.StoredSession:
        """The open conversation an answer can go to now: the fact may be refined, no turn
        is queued or running or waiting to be started, the last turn asked something that
        isn't answered yet, and answers are left."""
        _refinable(_fact_row(conn, fact_id))
        session = _open_session(conn, fact_id)
        _history_allowed(conn, session)
        turns = _turns(session.turns)
        last = turns[-1] if turns else None
        if _turn_active(conn, fact_id):
            raise RefineTurnRunning(detail="a turn is on its way")
        if last is None or last.answer is not None:
            raise RefineBusy(detail="no turn waits for an answer")
        if not last.questions:  # nothing was asked, so there is nothing to answer
            raise RefineBusy(detail="the last turn asked no question")
        if session.answer_rounds >= MAX_ANSWER_ROUNDS:
            raise RefineAnswersUsed(detail="three answers given")
        return session

    def _turn_input(self, session_id: str) -> tuple[refine_repo.StoredSession, _Context]:
        with self._db().read() as conn:
            session = refine_repo.get_session(conn, session_id)
            if session is None or session.state != STATE_OPEN:
                raise RefineNotOpen(detail="the conversation is closed")
            _history_allowed(conn, session)
            return session, _bank_context(conn, session.fact_id)

    def _cancel(self, task_id: str | None) -> None:
        """Ask the runner to cancel the turn ``task_id`` names, if any (after the write
        that closed its conversation)."""
        tasks = self._tasks()
        if task_id is not None and tasks is not None:
            tasks.request_cancel(task_id)

    def _writer(
        self, session_id: str, expected_version: int, stored: refine_repo.TurnResult
    ) -> ResultWriter:
        def write(conn: sqlite3.Connection) -> str:
            now = self._clock.now().isoformat()
            if not refine_repo.set_turn_result(
                conn, session_id, stored, expected_version=expected_version, now=now
            ):
                # Closed, or answered again, while this turn ran.
                raise RefineNotOpen(detail="the conversation changed while the turn ran")
            return f"{_RESULT_PREFIX}:{session_id}"

        return write


def _suggestion_json(content: FactContent, bank: _Context, turns: Sequence[RefineTurn]) -> str:
    """The suggestion, with what Accept checks it against: the sources of its request and
    the id of the fact's version it was made for."""
    data: dict[str, object] = {part: getattr(content, part) for part in _PARTS}
    sources = allowed_sources(bank.target, bank.context, turns)
    data["allowed_sources"] = [source for source in sources if source]
    data["name_hidden_sources"] = [s for s in name_hidden_sources(bank.context) if s]
    data["known_skills"] = list(bank.known_skills)
    data["fact_version_id"] = bank.version_id
    return json.dumps(data, ensure_ascii=False)


def _snapshot(session: refine_repo.StoredSession) -> dict[str, Any]:
    """What the stored suggestion was checked against. Raises ``RefineNoSuggestion`` when
    the suggestion has none."""
    snapshot = json.loads(session.suggestion or "{}")
    if not isinstance(snapshot, dict) or "allowed_sources" not in snapshot:
        raise RefineNoSuggestion(detail="the suggestion has no source snapshot")
    return snapshot


def _claims_in(
    session: refine_repo.StoredSession,
    snapshot: Mapping[str, Any],
    suggestion: FactContent,
    content: FactContent,
) -> tuple[Claim, ...]:
    """What the posted wording says beyond the sources sent with the suggestion, with the
    details the model reported adding that the wording may still say."""
    wording = wording_of(content)
    reported = [
        item["text"]
        for item in _stored_claims(session.flagged_claims)
        if item.get("kind") == "added_detail"
    ]
    return new_claims(
        wording,
        snapshot["allowed_sources"],
        known_skills=snapshot["known_skills"],
        added_details=details_still_said(
            reported, suggestion=wording_of(suggestion), wording=wording
        ),
        name_hidden_sources=snapshot.get("name_hidden_sources", ()),
    )


def _view_of(fact_id: str, session: refine_repo.StoredSession) -> RefineView:
    turns = _turns(session.turns)
    return RefineView(
        fact_id=fact_id,
        questions=turns[-1].questions if turns and turns[-1].answer is None else (),
        suggestion=_suggestion(session.suggestion),
        flagged=tuple(item["text"] for item in _stored_claims(session.flagged_claims)),
        answers_left=MAX_ANSWER_ROUNDS - session.answer_rounds,
        practice=session.provider == "fake",
    )


def _suggested_for(session: refine_repo.StoredSession) -> str | None:
    """The id of the fact's version the stored suggestion was made for."""
    snapshot = json.loads(session.suggestion or "{}")
    found = snapshot.get("fact_version_id") if isinstance(snapshot, dict) else None
    return found if isinstance(found, str) else None


def _phase(session: refine_repo.StoredSession | None, *, waiting: bool) -> str:
    """Closed, waiting, failed (no turn ran, or the turn after the last answer ended without
    an answer) or answered."""
    if session is None:
        return PHASE_CLOSED
    if waiting:
        return PHASE_WAITING
    turns = _turns(session.turns)
    if not turns or turns[-1].answer is not None:
        return PHASE_FAILED
    return PHASE_ANSWERED


def _panel_claims(
    session: refine_repo.StoredSession, suggestion: FactContent | None
) -> tuple[RefineClaim, ...]:
    """The flagged claims, each with whether editing its text out of the wording drops it,
    as ``details_still_said`` reads a reported detail; any other claim is the wording's own
    text."""
    said = _folded(wording_of(suggestion)) if suggestion is not None else ""
    return tuple(
        RefineClaim(
            text=item["text"],
            edits_out=item.get("kind") != "added_detail" or _folded(item["text"]) in said,
        )
        for item in _stored_claims(session.flagged_claims)
    )


def _close(
    conn: sqlite3.Connection,
    session: refine_repo.StoredSession,
    state: str,
    acknowledged_claims: str,
    now: str,
) -> None:
    """Close the conversation as it was read; raises ``VersionConflict`` (and the caller's
    transaction rolls back) when it changed since."""
    if not refine_repo.close_session(
        conn,
        session.id,
        state=state,
        acknowledged_claims=acknowledged_claims,
        expected_version=session.version,
        now=now,
    ):
        raise VersionConflict(detail="the conversation changed")


def _checked_answer(answer: str) -> str:
    text = clean_part(answer)
    if text is None:
        raise FactInputInvalid("answer", MSG_ANSWER_NEEDED)
    if len(text) > MAX_ANSWER_CHARS:
        raise FactInputInvalid("answer", MSG_ANSWER_TOO_LONG)
    return text


__all__ = [
    "MAX_ANSWER_ROUNDS",
    "REFINE_FACT",
    "REFINE_FACT_LABEL",
    "FactRefineService",
    "RefineTurn",
    "RefineView",
    "StartPlan",
    "allowed_sources",
    "details_still_said",
    "name_hidden_sources",
    "refine_prompt_inputs",
    "tier_from_form",
    "wording_of",
]
