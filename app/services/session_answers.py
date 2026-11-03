"""Assemble item-local answer sources and follow-ups; no providers or writes."""

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime
from typing import cast

from app.domain.errors import SessionEmpty, SessionNotFound, VersionConflict
from app.domain.fact_selection import AIFact, facts_for_ai, offered_skills
from app.domain.facts import FactStatus, Privacy
from app.modules.storage import (
    enrichment_repo,
    facts_repo,
    roles_repo,
    session_sources_repo,
    sessions_repo,
    skills_repo,
)
from app.services.session_backlog import FocusKind, SessionFocus, SessionItem, sources, target_of


def item_for(
    conn: sqlite3.Connection, answer: sessions_repo.StoredAnswer, now: datetime
) -> SessionItem:
    question = enrichment_repo.get_question(conn, answer.question_id)
    if question is None:
        raise SessionNotFound()
    focus = SessionFocus(
        cast(
            FocusKind,
            {"cluster": "cluster", "quality_hint": "fact", "capture_note": "capture_note"}[
                question.kind
            ],
        ),
        target_of(question),
    )
    parent = sessions_repo.get_answer(conn, answer.follow_up_of) if answer.follow_up_of else answer
    if parent is None:
        raise SessionNotFound()
    entries = sources(conn, focus, now, resolved_note_draft=parent.draft_fact_id)
    entry = next(
        (
            e
            for e in entries
            if e.item.kind == question.kind and e.item.target_key == target_of(question)
        ),
        None,
    )
    if entry is None:
        raise SessionEmpty()
    if answer.follow_up_of:
        return follow_up(conn, parent, entry)
    return entry


def follow_up(
    conn: sqlite3.Connection, parent: sessions_repo.StoredAnswer, entry: SessionItem
) -> SessionItem:
    draft = facts_repo.get_fact(conn, parent.draft_fact_id or "")
    if (
        draft is None
        or draft.status != FactStatus.DRAFT
        or draft.privacy == Privacy.NEVER_USE
        or draft.version != 1
    ):
        raise VersionConflict()
    answers = (parent.answer_text,) if parent.answer_text is not None else ()
    return replace(
        entry,
        follow_up_of=parent.id,
        personal_answers=answers,
        payload=json.dumps(
            {"item": json.loads(entry.payload), "missing": parent.missing, "answers": answers},
            ensure_ascii=False,
        ),
    )


@dataclass(frozen=True)
class AnswerSources:
    entry: SessionItem
    answer: sessions_repo.StoredAnswer
    own_answers: tuple[str, ...]
    target: AIFact | None
    note: str | None
    roles: dict[str, str]
    generalized_roles: frozenset[str]
    skills: tuple[str, ...]  # the whole list: the draft is checked against it
    offered_skills: tuple[str, ...]  # of usable facts only: the one the request names
    draft_id: str | None
    draft_version: int | None

    @property
    def stamp(self) -> str:
        return hashlib.sha256(repr(self).encode()).hexdigest()


def answer_sources(
    conn: sqlite3.Connection, row: sessions_repo.StoredAnswer, now: datetime
) -> AnswerSources:
    entry = item_for(conn, row, now)
    parent = sessions_repo.get_answer(conn, row.follow_up_of) if row.follow_up_of else None
    draft = facts_repo.get_fact(conn, parent.draft_fact_id or "") if parent else None
    if parent and (
        draft is None or draft.status != FactStatus.DRAFT or draft.privacy == Privacy.NEVER_USE
    ):
        raise VersionConflict()
    own = (*entry.personal_answers, *((row.answer_text,) if row.answer_text is not None else ()))
    selected = facts_for_ai(entry.facts) if entry.item.kind == "quality_hint" else ()
    target = selected[0] if selected else None
    note = json.loads(entry.payload).get("note") if entry.item.kind == "capture_note" else None
    if entry.follow_up_of and entry.item.kind == "capture_note":
        note = json.loads(entry.payload)["item"]["note"]
    bank = session_sources_repo.facts(conn)
    roles, generalized = {}, set()
    for role in roles_repo.list_roles(conn):
        linked = [f.record for f in bank if f.role_id == role.id]
        usable = facts_for_ai(linked)
        if linked and not usable:
            continue
        roles[role.id] = " at ".join(p for p in (role.title, role.employer) if p)
        if usable and all(f.generalize for f in usable):
            generalized.add(role.id)
    skills = tuple(s.name for s in skills_repo.list_skills(conn))
    return AnswerSources(
        entry,
        row,
        own,
        target,
        note,
        roles,
        frozenset(generalized),
        skills,
        offered_skills([f.record for f in bank], skills),
        draft.id if draft else None,
        draft.version if draft else None,
    )
