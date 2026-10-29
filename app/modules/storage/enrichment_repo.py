"""Per-target enrichment state. Callers own short write transactions and inject ids/times."""

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.domain.enrichment import ItemState
from app.domain.errors import VersionConflict


@dataclass(frozen=True)
class StoredQuestion:
    id: str
    kind: str
    cluster_key: str | None
    fact_id: str | None
    capture_note_id: str | None
    status: str
    cooldown_until: str | None
    later_until: str | None
    later_next_session: bool
    not_sure_count: int
    asked_count: int
    last_outcome: str | None
    last_asked_at: str | None
    earlier_wordings: tuple[str, ...]
    created_at: str
    updated_at: str
    version: int


_SELECT = (
    "SELECT id,kind,cluster_key,fact_id,capture_note_id,status,cooldown_until,later_until,"
    "later_next_session,not_sure_count,asked_count,last_outcome,last_asked_at,earlier_wordings,"
    "created_at,updated_at,version FROM enrichment_questions"
)


def _question(row: tuple[Any, ...]) -> StoredQuestion:
    values = list(row)
    values[8] = bool(values[8])
    values[13] = tuple(json.loads(str(values[13])))
    return StoredQuestion(*values)


def insert_question(
    conn: sqlite3.Connection,
    *,
    id: str,
    kind: str,
    now: str,
    cluster_key: str | None = None,
    fact_id: str | None = None,
    capture_note_id: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO enrichment_questions (id,kind,cluster_key,fact_id,capture_note_id,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
        (id, kind, cluster_key, fact_id, capture_note_id, now, now),
    )


def get_question(conn: sqlite3.Connection, id: str) -> StoredQuestion | None:
    row = conn.execute(_SELECT + " WHERE id=?", (id,)).fetchone()
    return _question(row) if row else None


def find_question(conn: sqlite3.Connection, kind: str, target: str) -> StoredQuestion | None:
    row = conn.execute(
        _SELECT + " WHERE kind=? AND (cluster_key=? OR fact_id=? OR capture_note_id=?)",
        (kind, target, target, target),
    ).fetchone()
    return _question(row) if row else None


def list_questions(conn: sqlite3.Connection) -> tuple[StoredQuestion, ...]:
    return tuple(_question(row) for row in conn.execute(_SELECT + " ORDER BY id"))


def update_question(
    conn: sqlite3.Connection,
    id: str,
    *,
    state: ItemState,
    now: str,
    expected_version: int,
) -> None:
    result = conn.execute(
        "UPDATE enrichment_questions SET status=?,cooldown_until=?,later_until=?,"
        "later_next_session=?,not_sure_count=?,asked_count=?,last_outcome=?,last_asked_at=?,"
        "earlier_wordings=?,updated_at=?,version=version+1 WHERE id=? AND version=?",
        (
            state.status,
            state.cooldown_until.isoformat() if state.cooldown_until else None,
            state.later_until.isoformat() if state.later_until else None,
            int(state.later_next_session),
            state.not_sure_count,
            state.asked_count,
            state.last_outcome,
            state.last_asked_at.isoformat() if state.last_asked_at else None,
            json.dumps(state.earlier_wordings, ensure_ascii=False),
            now,
            id,
            expected_version,
        ),
    )
    if result.rowcount != 1:
        raise VersionConflict()
