"""Rows of the ``refine_sessions`` table (migration 0003; ADR-090 item 10). Callers own the
transaction. Times are UTC ISO 8601 text from the Clock.

One conversation per row; at most one is open per fact (a unique partial index). ``turns``,
``suggestion``, ``flagged_claims`` and ``acknowledged_claims`` are JSON text the service
writes. Every change is a conditional update from the expected ``version`` while the
conversation is open, and bumps ``version``.
"""

import sqlite3
from dataclasses import dataclass

# Literal SQL only (no string building).
_SELECT_BY_ID = (
    "SELECT id, fact_id, base_version_id, state, answer_rounds, turns, suggestion, "
    "flagged_claims, acknowledged_claims, provider, model, prompt_id, prompt_version, task_id, "
    "version FROM refine_sessions WHERE id = ?"
)
_SELECT_OPEN = (
    "SELECT id, fact_id, base_version_id, state, answer_rounds, turns, suggestion, "
    "flagged_claims, acknowledged_claims, provider, model, prompt_id, prompt_version, task_id, "
    "version FROM refine_sessions WHERE fact_id = ? AND state = 'open'"
)
_INSERT = (
    "INSERT INTO refine_sessions (id, fact_id, base_version_id, state, created_at, updated_at, "
    "version) VALUES (?, ?, ?, 'open', ?, ?, 1)"
)
_SET_ANSWERS = (
    "UPDATE refine_sessions SET turns = ?, answer_rounds = ?, updated_at = ?, "
    "version = version + 1 WHERE id = ? AND state = 'open' AND version = ?"
)
_SET_TURN_RESULT = (
    "UPDATE refine_sessions SET turns = ?, suggestion = ?, flagged_claims = ?, provider = ?, "
    "model = ?, prompt_id = ?, prompt_version = ?, task_id = ?, updated_at = ?, "
    "version = version + 1 WHERE id = ? AND state = 'open' AND version = ?"
)
_CLOSE = (
    "UPDATE refine_sessions SET state = ?, acknowledged_claims = ?, updated_at = ?, "
    "version = version + 1 WHERE id = ? AND state = 'open' AND version = ?"
)


@dataclass(frozen=True)
class StoredSession:
    id: str
    fact_id: str
    base_version_id: str
    state: str
    answer_rounds: int
    turns: str  # a JSON array
    suggestion: str | None  # a JSON object, or NULL
    flagged_claims: str  # a JSON array
    acknowledged_claims: str  # a JSON array
    provider: str | None
    model: str | None
    prompt_id: str | None
    prompt_version: str | None
    task_id: str | None
    version: int


@dataclass(frozen=True)
class TurnResult:
    """What one ended turn stores: the conversation with its new questions, the suggestion
    (JSON or None), the claims the honesty check flagged and who answered."""

    turns: str
    suggestion: str | None
    flagged_claims: str
    provider: str
    model: str
    prompt_id: str
    prompt_version: str
    task_id: str


def get_session(conn: sqlite3.Connection, session_id: str) -> StoredSession | None:
    row = conn.execute(_SELECT_BY_ID, (session_id,)).fetchone()
    return None if row is None else StoredSession(*row)


def get_open(conn: sqlite3.Connection, fact_id: str) -> StoredSession | None:
    """The fact's open conversation, if it has one."""
    row = conn.execute(_SELECT_OPEN, (fact_id,)).fetchone()
    return None if row is None else StoredSession(*row)


def insert_session(
    conn: sqlite3.Connection, *, session_id: str, fact_id: str, base_version_id: str, now: str
) -> None:
    conn.execute(_INSERT, (session_id, fact_id, base_version_id, now, now))


def set_answers(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    turns: str,
    answer_rounds: int,
    expected_version: int,
    now: str,
) -> bool:
    cursor = conn.execute(_SET_ANSWERS, (turns, answer_rounds, now, session_id, expected_version))
    return cursor.rowcount == 1


def set_turn_result(
    conn: sqlite3.Connection,
    session_id: str,
    result: TurnResult,
    *,
    expected_version: int,
    now: str,
) -> bool:
    cursor = conn.execute(
        _SET_TURN_RESULT,
        (
            result.turns,
            result.suggestion,
            result.flagged_claims,
            result.provider,
            result.model,
            result.prompt_id,
            result.prompt_version,
            result.task_id,
            now,
            session_id,
            expected_version,
        ),
    )
    return cursor.rowcount == 1


def close_session(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    state: str,
    acknowledged_claims: str,
    expected_version: int,
    now: str,
) -> bool:
    """Close an open conversation as ``accepted`` or ``discarded``."""
    cursor = conn.execute(_CLOSE, (state, acknowledged_claims, now, session_id, expected_version))
    return cursor.rowcount == 1
