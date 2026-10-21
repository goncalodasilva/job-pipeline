"""Detail reads and versioned event/note writes. The service owns transactions."""

import sqlite3
from typing import Any

from app.domain.errors import VersionConflict


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def header(conn: sqlite3.Connection, application_id: str) -> dict[str, Any] | None:
    rows = _rows(
        conn.execute(
            "SELECT a.*, j.title, j.company_id, c.name AS company, s.label, s.phase "
            "FROM applications a JOIN jobs j ON j.id=a.job_id "
            "JOIN application_statuses s ON s.key=a.status_key "
            "LEFT JOIN companies c ON c.id=j.company_id WHERE a.id=?",
            (application_id,),
        )
    )
    return rows[0] if rows else None


def notes(conn: sqlite3.Connection, application_id: str) -> list[dict[str, Any]]:
    return _rows(
        conn.execute(
            "SELECT * FROM application_notes WHERE application_id=? "
            "ORDER BY updated_at DESC, id DESC",
            (application_id,),
        )
    )


def events(conn: sqlite3.Connection, application_id: str) -> list[dict[str, Any]]:
    return _rows(
        conn.execute(
            "SELECT * FROM events WHERE application_id=? ORDER BY updated_at DESC, id DESC",
            (application_id,),
        )
    )


def event(conn: sqlite3.Connection, application_id: str, event_id: str) -> dict[str, Any] | None:
    rows = _rows(
        conn.execute(
            "SELECT * FROM events WHERE application_id=? AND id=?", (application_id, event_id)
        )
    )
    return rows[0] if rows else None


def history(conn: sqlite3.Connection, application_id: str) -> list[dict[str, Any]]:
    return _rows(
        conn.execute(
            "SELECT h.*, f.label AS from_label, t.label AS to_label FROM status_history h "
            "LEFT JOIN application_statuses f ON f.key=h.from_status "
            "JOIN application_statuses t ON t.key=h.to_status WHERE h.application_id=? "
            "ORDER BY h.changed_at DESC, h.id DESC",
            (application_id,),
        )
    )


def interactions(conn: sqlite3.Connection, application_id: str) -> list[dict[str, Any]]:
    return _rows(
        conn.execute(
            "SELECT i.*, c.name AS contact_name FROM interactions i "
            "LEFT JOIN contacts c ON c.id=i.contact_id WHERE i.application_id=? "
            "ORDER BY i.happened_at DESC, i.id DESC",
            (application_id,),
        )
    )


def update_note(
    conn: sqlite3.Connection,
    application_id: str,
    note_id: str,
    *,
    text: str,
    version: int,
    now: str,
) -> None:
    result = conn.execute(
        "UPDATE application_notes SET text=?, updated_at=?, version=version+1 "
        "WHERE id=? AND application_id=? AND version=?",
        (text, now, note_id, application_id, version),
    )
    if result.rowcount != 1:
        raise VersionConflict()


EVENT_COLUMNS = (
    "type",
    "starts_at",
    "ends_at",
    "time_zone",
    "all_day",
    "round",
    "format",
    "interviewers",
    "location",
    "details",
    "thank_you_reminder",
)


def save_event(
    conn: sqlite3.Connection,
    application_id: str,
    event_id: str,
    values: dict[str, Any],
    *,
    version: int | None,
    now: str,
) -> None:
    args = [values[key] for key in EVENT_COLUMNS]
    if version is None:
        conn.execute(
            "INSERT INTO events (id, application_id, type, starts_at, ends_at, time_zone, "
            "all_day, round, format, interviewers, location, details, thank_you_reminder, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [event_id, application_id, *args, now, now],
        )
    else:
        result = conn.execute(
            "UPDATE events SET type=?, starts_at=?, ends_at=?, time_zone=?, all_day=?, "
            "round=?, format=?, interviewers=?, location=?, details=?, thank_you_reminder=?, "
            "updated_at=?, version=version+1 WHERE id=? AND application_id=? AND version=?",
            [*args, now, event_id, application_id, version],
        )
        if result.rowcount != 1:
            raise VersionConflict()


def cancel_event(
    conn: sqlite3.Connection, application_id: str, event_id: str, *, version: int, now: str
) -> None:
    result = conn.execute(
        "UPDATE events SET status='cancelled', updated_at=?, version=version+1 "
        "WHERE application_id=? AND id=? AND version=?",
        (now, application_id, event_id, version),
    )
    if result.rowcount != 1:
        raise VersionConflict()
