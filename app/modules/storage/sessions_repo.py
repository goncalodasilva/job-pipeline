"""Sessions and question/answer snapshots. Callers own transactions and task completion."""

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.domain.errors import VersionConflict


@dataclass(frozen=True)
class StoredSession:
    id: str
    focus_kind: str
    focus_ref: str | None
    job_id: str | None
    length: int
    state: str
    started_at: str
    ended_at: str | None
    created_at: str
    updated_at: str
    version: int


@dataclass(frozen=True)
class StoredAnswer:
    id: str
    session_id: str
    position: int
    question_id: str
    follow_up_of: str | None
    question: str
    why: str
    question_provider: str | None
    question_model: str | None
    question_prompt_id: str | None
    question_prompt_version: str | None
    fallback: bool
    answer_text: str | None
    outcome: str
    later_choice: str | None
    draft_fact_id: str | None
    missing: tuple[str, ...]
    flagged_claims: tuple[Any, ...]
    structure_provider: str | None
    structure_model: str | None
    structure_prompt_id: str | None
    structure_prompt_version: str | None
    question_task_id: str | None
    structure_task_id: str | None
    created_at: str
    updated_at: str
    version: int


_SESSION = (
    "SELECT id,focus_kind,focus_ref,job_id,length,state,started_at,ended_at,created_at,"
    "updated_at,version FROM sessions"
)
_ANSWER = (
    "SELECT id,session_id,position,question_id,follow_up_of,question,why,question_provider,"
    "question_model,question_prompt_id,question_prompt_version,fallback,answer_text,outcome,"
    "later_choice,draft_fact_id,missing,flagged_claims,structure_provider,structure_model,"
    "structure_prompt_id,structure_prompt_version,question_task_id,structure_task_id,"
    "created_at,updated_at,version FROM session_answers"
)


def _answer(row: tuple[Any, ...]) -> StoredAnswer:
    values = list(row)
    values[11] = bool(values[11])
    values[16] = tuple(json.loads(values[16]))
    values[17] = tuple(json.loads(values[17]))
    return StoredAnswer(*values)


def insert_session(
    conn: sqlite3.Connection,
    *,
    id: str,
    focus_kind: str,
    length: int,
    now: str,
    focus_ref: str | None = None,
    job_id: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO sessions (id,focus_kind,focus_ref,job_id,length,started_at,created_at,"
        "updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (id, focus_kind, focus_ref, job_id, length, now, now, now),
    )


def get_session(conn: sqlite3.Connection, id: str) -> StoredSession | None:
    row = conn.execute(_SESSION + " WHERE id=?", (id,)).fetchone()
    return StoredSession(*row) if row else None


def open_session(conn: sqlite3.Connection) -> StoredSession | None:
    row = conn.execute(_SESSION + " WHERE state='open'").fetchone()
    return StoredSession(*row) if row else None


def end_session(
    conn: sqlite3.Connection,
    id: str,
    *,
    now: str,
    expected_version: int,
) -> None:
    result = conn.execute(
        "UPDATE sessions SET state='ended',ended_at=?,updated_at=?,version=version+1 "
        "WHERE id=? AND version=? AND state='open'",
        (now, now, id, expected_version),
    )
    if result.rowcount != 1:
        raise VersionConflict()


def insert_answer(
    conn: sqlite3.Connection,
    *,
    id: str,
    session_id: str,
    position: int,
    question_id: str,
    question: str,
    why: str,
    now: str,
    follow_up_of: str | None = None,
    question_provider: str | None = None,
    question_model: str | None = None,
    question_prompt_id: str | None = None,
    question_prompt_version: str | None = None,
    question_task_id: str | None = None,
    fallback: bool = False,
) -> None:
    conn.execute(
        "INSERT INTO session_answers (id,session_id,position,question_id,follow_up_of,"
        "question,why,question_provider,question_model,question_prompt_id,question_prompt_version,"
        "question_task_id,fallback,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            id,
            session_id,
            position,
            question_id,
            follow_up_of,
            question,
            why,
            question_provider,
            question_model,
            question_prompt_id,
            question_prompt_version,
            question_task_id,
            int(fallback),
            now,
            now,
        ),
    )


def get_answer(conn: sqlite3.Connection, id: str) -> StoredAnswer | None:
    row = conn.execute(_ANSWER + " WHERE id=?", (id,)).fetchone()
    return _answer(row) if row else None


def list_answers(conn: sqlite3.Connection, session_id: str) -> tuple[StoredAnswer, ...]:
    rows = conn.execute(_ANSWER + " WHERE session_id=? ORDER BY position", (session_id,))
    return tuple(_answer(row) for row in rows)


def update_answer(
    conn: sqlite3.Connection,
    id: str,
    *,
    answer_text: str | None,
    outcome: str,
    now: str,
    expected_version: int,
    later_choice: str | None = None,
    draft_fact_id: str | None = None,
    missing: Sequence[str] = (),
    flagged_claims: Sequence[Any] = (),
    structure_provider: str | None = None,
    structure_model: str | None = None,
    structure_prompt_id: str | None = None,
    structure_prompt_version: str | None = None,
    structure_task_id: str | None = None,
) -> None:
    result = conn.execute(
        "UPDATE session_answers SET answer_text=?,outcome=?,later_choice=?,draft_fact_id=?,"
        "missing=?,flagged_claims=?,structure_provider=?,structure_model=?,structure_prompt_id=?,"
        "structure_prompt_version=?,structure_task_id=?,updated_at=?,version=version+1 "
        "WHERE id=? AND version=?",
        (
            answer_text,
            outcome,
            later_choice,
            draft_fact_id,
            json.dumps(tuple(missing)),
            json.dumps(tuple(flagged_claims), ensure_ascii=False),
            structure_provider,
            structure_model,
            structure_prompt_id,
            structure_prompt_version,
            structure_task_id,
            now,
            id,
            expected_version,
        ),
    )
    if result.rowcount != 1:
        raise VersionConflict()
