"""Practice attempts: an immutable answer with the CV sent, and its feedback written once.
Callers own the short transaction."""

import sqlite3
from dataclasses import dataclass

from app.domain.errors import VersionConflict

MAX_ATTEMPTS_SHOWN = 20


@dataclass(frozen=True)
class AttemptRow:
    id: str
    prep_item_id: str
    question_id: str
    application_id: str
    answer_text: str
    cv_snapshot: str
    feedback: str | None
    version: int
    provider: str | None


_COLUMNS = (
    "a.id,a.prep_item_id,p.question_id,p.application_id,a.answer_text,a.cv_snapshot,a.feedback,"
    "a.version,a.provider"
)
_FROM = "FROM practice_attempts a JOIN prep_items p ON p.id=a.prep_item_id"


def insert_attempt(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    item_id: str,
    answer: str,
    cv_snapshot: str,
    now: str,
) -> None:
    conn.execute(
        "INSERT INTO practice_attempts(id,prep_item_id,answer_text,cv_snapshot,created_at,"
        "updated_at) VALUES(?,?,?,?,?,?)",
        (attempt_id, item_id, answer, cv_snapshot, now, now),
    )


def bump_item(conn: sqlite3.Connection, item_id: str, now: str, *, expected_version: int) -> None:
    updated = conn.execute(
        "UPDATE prep_items SET updated_at=?,version=version+1 WHERE id=? AND version=?",
        (now, item_id, expected_version),
    )
    if updated.rowcount != 1:
        raise VersionConflict()


def attempts(conn: sqlite3.Connection, item_id: str) -> tuple[AttemptRow, ...]:
    """The newest attempts of one item first: up to one more than ``MAX_ATTEMPTS_SHOWN``, so
    that the caller can tell that older ones exist."""
    return tuple(
        AttemptRow(*row)
        for row in conn.execute(
            f"SELECT {_COLUMNS} {_FROM} WHERE a.prep_item_id=? "
            "ORDER BY a.created_at DESC,a.id DESC LIMIT ?",
            (item_id, MAX_ATTEMPTS_SHOWN + 1),
        )
    )


def attempt(conn: sqlite3.Connection, attempt_id: str) -> AttemptRow | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} {_FROM} WHERE a.id=?",
        (attempt_id,),
    ).fetchone()
    return AttemptRow(*row) if row else None


def write_feedback(
    conn: sqlite3.Connection,
    attempt_id: str,
    *,
    feedback: str,
    fact_refs: str,
    provider: str,
    model: str,
    prompt_id: str,
    prompt_version: str,
    now: str,
) -> None:
    """Feedback is written once, by the task that succeeds."""
    updated = conn.execute(
        "UPDATE practice_attempts SET feedback=?,fact_refs=?,provider=?,model=?,prompt_id=?,"
        "prompt_version=?,updated_at=?,version=version+1 WHERE id=? AND feedback IS NULL",
        (feedback, fact_refs, provider, model, prompt_id, prompt_version, now, attempt_id),
    )
    if updated.rowcount != 1:
        raise VersionConflict()


def store_feedback(
    conn: sqlite3.Connection, attempt_id: str, feedback: str, now: str, *, expected_version: int
) -> None:
    """Replace the stored feedback JSON (the resolved-claims map); the version is checked."""
    updated = conn.execute(
        "UPDATE practice_attempts SET feedback=?,updated_at=?,version=version+1 "
        "WHERE id=? AND version=? AND feedback IS NOT NULL",
        (feedback, now, attempt_id, expected_version),
    )
    if updated.rowcount != 1:
        raise VersionConflict()
