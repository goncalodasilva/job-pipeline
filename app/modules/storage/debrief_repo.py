"""Debrief persistence (M8.3 part A). Callers own the short transaction.

``debriefs`` rows are created finished (``done`` or ``dismissed``): there is no open row. The
state only ever moves forward and a next round's event belongs to the same application; the
tables hold no guard for either, so the service checks both before it writes.
"""

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class InterviewScope:
    """An interview event with the application and the status it is in now."""

    application_id: str
    event_id: str
    starts_at: str
    ends_at: str
    time_zone: str
    status: str
    application_version: int
    status_key: str
    status_label: str
    status_phase: str
    company_id: str | None
    company: str
    role: str


@dataclass(frozen=True)
class StoredDebrief:
    id: str
    application_id: str
    event_id: str
    state: str
    questions_text: str
    how_it_went: str
    experiences_text: str
    company_only: bool
    next_step: str | None
    next_event_id: str | None
    status_confirmed: bool
    version: int


@dataclass(frozen=True)
class DraftRow:
    fact_id: str
    status: str
    wording: str
    needs_check: bool


_DEBRIEF_COLUMNS = (
    "id,application_id,event_id,state,questions_text,how_it_went,experiences_text,company_only,"
    "next_step,next_event_id,status_confirmed,version"
)


def interview(
    conn: sqlite3.Connection, application_id: str, event_id: str
) -> InterviewScope | None:
    """The INTERVIEW event of this application, or None for any other id, an event of another
    application and an event of another kind."""
    row = conn.execute(
        "SELECT e.application_id,e.id,e.starts_at,e.ends_at,e.time_zone,e.status,a.version,"
        "a.status_key,s.label,s.phase,j.company_id,coalesce(c.name,''),coalesce(j.title,'') "
        "FROM events e JOIN applications a ON a.id=e.application_id "
        "JOIN application_statuses s ON s.key=a.status_key JOIN jobs j ON j.id=a.job_id "
        "LEFT JOIN companies c ON c.id=j.company_id "
        "WHERE e.application_id=? AND e.id=? AND e.type='interview'",
        (application_id, event_id),
    ).fetchone()
    return InterviewScope(*row) if row else None


def _debrief(row: tuple[object, ...]) -> StoredDebrief:
    (ident, aid, event, state, questions, how, experiences, only, step, nxt, confirmed, version) = (
        row
    )
    return StoredDebrief(
        str(ident),
        str(aid),
        str(event),
        str(state),
        str(questions),
        str(how),
        str(experiences),
        bool(only),
        None if step is None else str(step),
        None if nxt is None else str(nxt),
        bool(confirmed),
        int(str(version)),
    )


def get(conn: sqlite3.Connection, event_id: str) -> StoredDebrief | None:
    row = conn.execute(
        f"SELECT {_DEBRIEF_COLUMNS} FROM debriefs WHERE event_id=?",  # noqa: S608
        (event_id,),
    ).fetchone()
    return _debrief(row) if row else None


def get_by_id(conn: sqlite3.Connection, debrief_id: str) -> StoredDebrief | None:
    row = conn.execute(
        f"SELECT {_DEBRIEF_COLUMNS} FROM debriefs WHERE id=?",  # noqa: S608
        (debrief_id,),
    ).fetchone()
    return _debrief(row) if row else None


def insert(conn: sqlite3.Connection, debrief: StoredDebrief, *, now: str) -> None:
    conn.execute(
        "INSERT INTO debriefs(id,application_id,event_id,state,questions_text,how_it_went,"
        "experiences_text,company_only,next_step,next_event_id,status_confirmed,created_at,"
        "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            debrief.id,
            debrief.application_id,
            debrief.event_id,
            debrief.state,
            debrief.questions_text,
            debrief.how_it_went,
            debrief.experiences_text,
            int(debrief.company_only),
            debrief.next_step,
            debrief.next_event_id,
            int(debrief.status_confirmed),
            now,
            now,
        ),
    )


def mark_status_confirmed(
    conn: sqlite3.Connection, debrief_id: str, *, now: str, expected_version: int
) -> bool:
    """Record that the suggested status was confirmed; False when the debrief changed."""
    updated = conn.execute(
        "UPDATE debriefs SET status_confirmed=1,updated_at=?,version=version+1 "
        "WHERE id=? AND version=? AND state='done' AND status_confirmed=0",
        (now, debrief_id, expected_version),
    )
    return updated.rowcount == 1


def next_event_text(
    conn: sqlite3.Connection, application_id: str, event_id: str
) -> tuple[str, str] | None:
    """The start instant and zone of the next round's event, when it is this application's."""
    row = conn.execute(
        "SELECT starts_at,time_zone FROM events WHERE id=? AND application_id=?",
        (event_id, application_id),
    ).fetchone()
    return (str(row[0]), str(row[1])) if row else None


def drafts(conn: sqlite3.Connection, debrief_id: str) -> tuple[DraftRow, ...]:
    rows = conn.execute(
        "SELECT f.id,f.status,coalesce(v.text,''),coalesce(v.situation,''),"
        "coalesce(v.action,''),coalesce(v.result,''),v.needs_check "
        "FROM debrief_drafts d JOIN facts f ON f.id=d.fact_id "
        "JOIN fact_versions v ON v.id=f.current_version_id "
        "WHERE d.debrief_id=? ORDER BY f.number",
        (debrief_id,),
    ).fetchall()
    return tuple(
        DraftRow(
            str(fact_id),
            str(status),
            " ".join(p for p in (text, situation, action, result) if p),
            bool(needs_check) and status == "draft",
        )
        for fact_id, status, text, situation, action, result, needs_check in rows
    )


def has_drafts(conn: sqlite3.Connection, debrief_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM debrief_drafts WHERE debrief_id=? LIMIT 1", (debrief_id,)
        ).fetchone()
        is not None
    )


def insert_draft(
    conn: sqlite3.Connection, *, draft_id: str, debrief_id: str, fact_id: str, now: str
) -> None:
    conn.execute(
        "INSERT INTO debrief_drafts(id,debrief_id,fact_id,created_at,updated_at) VALUES(?,?,?,?,?)",
        (draft_id, debrief_id, fact_id, now, now),
    )


def latest_task(
    conn: sqlite3.Connection, debrief_id: str, task_type: str
) -> tuple[str, str, str | None] | None:
    """Id, status and error kind of the newest task of this type for the debrief."""
    row = conn.execute(
        "SELECT id,status,error FROM tasks WHERE type=? AND dedupe_key=? "
        "ORDER BY created_at DESC,rowid DESC LIMIT 1",
        (task_type, task_type + ":" + debrief_id),
    ).fetchone()
    return (str(row[0]), str(row[1]), None if row[2] is None else str(row[2])) if row else None
