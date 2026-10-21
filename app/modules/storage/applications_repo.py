"""Applications persistence (M5.2). Callers own the write transaction and history write."""

import sqlite3
from dataclasses import dataclass

from app.domain.errors import VersionConflict


@dataclass(frozen=True)
class StoredApplication:
    id: str
    job_id: str
    status_key: str
    applied_at: str | None
    created_at: str
    updated_at: str
    version: int


def get_application(conn: sqlite3.Connection, application_id: str) -> StoredApplication | None:
    row = conn.execute(
        "SELECT id, job_id, status_key, applied_at, created_at, updated_at, version "
        "FROM applications WHERE id = ?",
        (application_id,),
    ).fetchone()
    return None if row is None else StoredApplication(*row)


def update_application(
    conn: sqlite3.Connection,
    application_id: str,
    *,
    status_key: str,
    applied_at: str | None,
    now: str,
    expected_version: int,
) -> None:
    """Keep the first applied date even when a later caller supplies None or another date."""
    result = conn.execute(
        "UPDATE applications SET status_key = ?, applied_at = COALESCE(applied_at, ?), "
        "updated_at = ?, version = version + 1 WHERE id = ? AND version = ?",
        (status_key, applied_at, now, application_id, expected_version),
    )
    if result.rowcount != 1:
        raise VersionConflict()


def get_application_for_job(conn: sqlite3.Connection, job_id: str) -> StoredApplication | None:
    row = conn.execute(
        "SELECT id, job_id, status_key, applied_at, created_at, updated_at, version "
        "FROM applications WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    return None if row is None else StoredApplication(*row)


def insert_application(
    conn: sqlite3.Connection, *, id: str, job_id: str, status_key: str, now: str
) -> None:
    conn.execute(
        "INSERT INTO applications (id, job_id, status_key, applied_at, created_at, updated_at) "
        "VALUES (?, ?, ?, NULL, ?, ?)",
        (id, job_id, status_key, now, now),
    )


def delete_application(conn: sqlite3.Connection, application_id: str) -> None:
    """Delete an application. Migration 0005 aborts this unless the application is untouched
    (``sqlite3.IntegrityError``); its sole creation history row goes with it."""
    conn.execute("DELETE FROM applications WHERE id = ?", (application_id,))


def touch_application(conn: sqlite3.Connection, application_id: str, now: str) -> None:
    """Record activity (a note or event) without a version change; never moves time back."""
    conn.execute(
        "UPDATE applications SET updated_at = MAX(updated_at, ?) WHERE id = ?",
        (now, application_id),
    )


def insert_history(
    conn: sqlite3.Connection,
    *,
    id: str,
    application_id: str,
    from_status: str | None,
    to_status: str,
    changed_at: str,
    source: str,
    now: str,
) -> None:
    conn.execute(
        "INSERT INTO status_history "
        "(id, application_id, from_status, to_status, changed_at, source, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (id, application_id, from_status, to_status, changed_at, source, now),
    )


def insert_note(
    conn: sqlite3.Connection, *, id: str, application_id: str, kind: str, text: str, now: str
) -> None:
    conn.execute(
        "INSERT INTO application_notes (id, application_id, kind, text, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (id, application_id, kind, text, now, now),
    )


def insert_event(
    conn: sqlite3.Connection,
    *,
    id: str,
    application_id: str,
    type: str,
    starts_at: str,
    ends_at: str,
    time_zone: str,
    now: str,
) -> None:
    conn.execute(
        "INSERT INTO events (id, application_id, type, starts_at, ends_at, time_zone, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (id, application_id, type, starts_at, ends_at, time_zone, now, now),
    )


# --- statuses ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredStatus:
    key: str
    label: str
    phase: str
    position: int
    built_in: bool
    version: int


def _status(row: tuple[object, ...]) -> StoredStatus:
    key, label, phase, position, built_in, version = row
    return StoredStatus(
        str(key), str(label), str(phase), int(str(position)), bool(built_in), int(str(version))
    )


def get_status(conn: sqlite3.Connection, key: str) -> StoredStatus | None:
    row = conn.execute(
        "SELECT key, label, phase, position, built_in, version FROM application_statuses "
        "WHERE key = ?",
        (key,),
    ).fetchone()
    return None if row is None else _status(row)


def list_statuses(conn: sqlite3.Connection) -> list[StoredStatus]:
    rows = conn.execute(
        "SELECT key, label, phase, position, built_in, version FROM application_statuses "
        "ORDER BY position, key"
    ).fetchall()
    return [_status(row) for row in rows]


def first_status_in_phase(conn: sqlite3.Connection, phase: str) -> StoredStatus | None:
    row = conn.execute(
        "SELECT key, label, phase, position, built_in, version FROM application_statuses "
        "WHERE phase = ? ORDER BY position, key LIMIT 1",
        (phase,),
    ).fetchone()
    return None if row is None else _status(row)


def statuses_in_phase(conn: sqlite3.Connection, phase: str) -> list[StoredStatus]:
    rows = conn.execute(
        "SELECT key, label, phase, position, built_in, version FROM application_statuses "
        "WHERE phase = ? ORDER BY position, key",
        (phase,),
    ).fetchall()
    return [_status(row) for row in rows]


def next_status_position(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM application_statuses").fetchone()
    return int(row[0])


def insert_status(
    conn: sqlite3.Connection, *, key: str, label: str, phase: str, position: int, now: str
) -> None:
    conn.execute(
        "INSERT INTO application_statuses "
        "(key, label, phase, position, built_in, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 0, ?, ?)",
        (key, label, phase, position, now, now),
    )


def rename_status(
    conn: sqlite3.Connection, key: str, *, label: str, now: str, expected_version: int
) -> None:
    result = conn.execute(
        "UPDATE application_statuses SET label = ?, updated_at = ?, version = version + 1 "
        "WHERE key = ? AND version = ?",
        (label, now, key, expected_version),
    )
    if result.rowcount != 1:
        raise VersionConflict()


def set_status_position(
    conn: sqlite3.Connection, key: str, *, position: int, now: str, expected_version: int
) -> None:
    result = conn.execute(
        "UPDATE application_statuses SET position = ?, updated_at = ?, version = version + 1 "
        "WHERE key = ? AND version = ?",
        (position, now, key, expected_version),
    )
    if result.rowcount != 1:
        raise VersionConflict()


def status_in_use(conn: sqlite3.Connection, key: str) -> bool:
    """Whether an application or its history names the status. A reminder rule does not count:
    it is configuration, removed with the status (``reminders_repo.delete_rule_for_status``)."""
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM applications WHERE status_key = ?) "
        "OR EXISTS (SELECT 1 FROM status_history WHERE to_status = ? OR from_status = ?)",
        (key, key, key),
    ).fetchone()
    return bool(row[0])


def delete_status(conn: sqlite3.Connection, key: str, *, expected_version: int) -> None:
    result = conn.execute(
        "DELETE FROM application_statuses WHERE key = ? AND version = ?", (key, expected_version)
    )
    if result.rowcount != 1:
        raise VersionConflict()


# --- board reads: one SELECT each (the service reads the board and the table in two) -----------


@dataclass(frozen=True)
class StatusCount:
    status: StoredStatus
    applications: int


@dataclass(frozen=True)
class CardRow:
    application_id: str
    job_id: str
    company: str
    role: str
    status_key: str
    updated_at: str
    next_event_at: str | None
    next_event_type: str | None = None
    next_event_zone: str | None = None
    version: int = 1


@dataclass(frozen=True)
class TableRow:
    application_id: str
    company: str
    role: str
    status_key: str
    applied_at: str | None = None
    updated_at: str = ""


def status_counts(conn: sqlite3.Connection) -> list[StatusCount]:
    """Every status in pipeline order (phase, then position) with its application count."""
    rows = conn.execute(
        "SELECT s.key, s.label, s.phase, s.position, s.built_in, s.version, "
        "(SELECT count(*) FROM applications a WHERE a.status_key = s.key) "
        "FROM application_statuses s "
        "ORDER BY CASE s.phase WHEN 'preparing' THEN 0 WHEN 'active' THEN 1 ELSE 2 END, "
        "s.position, s.key"
    ).fetchall()
    return [StatusCount(_status(row[:6]), int(row[6])) for row in rows]


def board_cards(conn: sqlite3.Connection, *, now: str, include_closed: bool) -> list[CardRow]:
    """Cards of preparing and active statuses, and of closed ones only when asked; each with
    its next scheduled event not yet over at ``now`` (its start, type and time zone)."""
    rows = conn.execute(
        "SELECT a.id, a.job_id, COALESCE(c.name, ''), j.title, a.status_key, a.updated_at, "
        "e.starts_at, e.type, e.time_zone, a.version "
        "FROM applications a JOIN application_statuses s ON s.key = a.status_key "
        "JOIN jobs j ON j.id = a.job_id LEFT JOIN companies c ON c.id = j.company_id "
        "LEFT JOIN events e ON e.id = (SELECT n.id FROM events n WHERE n.application_id = a.id "
        "AND n.status = 'scheduled' AND julianday(n.ends_at) >= julianday(?) "
        "ORDER BY julianday(n.starts_at), n.id LIMIT 1) "
        "WHERE s.phase <> 'closed' OR ? = 1",
        (now, 1 if include_closed else 0),
    ).fetchall()
    return [CardRow(*row) for row in rows]


def count_applications(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT count(*) FROM applications").fetchone()[0])


# One literal statement per sort and direction, each driven by its index (a temp b-tree may
# still be used for part of the ordering). Ties break on ids.
# Company: companies are read through their name index, and company-less jobs join the result
# as a second arm that sorts as an empty name (first ascending, last descending).
_PAGE_COMPANY_ASC = (
    "SELECT a.id, c.name, j.title, a.status_key, a.applied_at, a.updated_at, c.id "
    "FROM companies c "
    "CROSS JOIN jobs j ON j.company_id = c.id CROSS JOIN applications a ON a.job_id = j.id "
    "UNION ALL SELECT a.id, NULL, j.title, a.status_key, a.applied_at, a.updated_at, NULL "
    "FROM jobs j "
    "CROSS JOIN applications a ON a.job_id = j.id WHERE j.company_id IS NULL "
    "ORDER BY 2 COLLATE NOCASE, 7, 1 LIMIT ? OFFSET ?"
)
_PAGE_COMPANY_DESC = (
    "SELECT a.id, c.name, j.title, a.status_key, a.applied_at, a.updated_at, c.id "
    "FROM companies c "
    "CROSS JOIN jobs j ON j.company_id = c.id CROSS JOIN applications a ON a.job_id = j.id "
    "UNION ALL SELECT a.id, NULL, j.title, a.status_key, a.applied_at, a.updated_at, NULL "
    "FROM jobs j "
    "CROSS JOIN applications a ON a.job_id = j.id WHERE j.company_id IS NULL "
    "ORDER BY 2 COLLATE NOCASE DESC, 7 DESC, 1 DESC LIMIT ? OFFSET ?"
)
_PAGE_ROLE_ASC = (
    "SELECT a.id, COALESCE(c.name, ''), j.title, a.status_key, a.applied_at, a.updated_at "
    "FROM jobs j "
    "INDEXED BY jobs_title_application_sort CROSS JOIN applications a ON a.job_id = j.id "
    "LEFT JOIN companies c ON c.id = j.company_id "
    "ORDER BY j.title COLLATE NOCASE, j.id LIMIT ? OFFSET ?"
)
_PAGE_ROLE_DESC = (
    "SELECT a.id, COALESCE(c.name, ''), j.title, a.status_key, a.applied_at, a.updated_at "
    "FROM jobs j "
    "INDEXED BY jobs_title_application_sort CROSS JOIN applications a ON a.job_id = j.id "
    "LEFT JOIN companies c ON c.id = j.company_id "
    "ORDER BY j.title COLLATE NOCASE DESC, j.id DESC LIMIT ? OFFSET ?"
)
# Status: pipeline order (phase, then position), then the application's own activity order.
_PAGE_STATUS_ASC = (
    "SELECT a.id, COALESCE(c.name, ''), j.title, a.status_key, a.applied_at, a.updated_at "
    "FROM application_statuses s "
    "CROSS JOIN applications a ON a.status_key = s.key JOIN jobs j ON j.id = a.job_id "
    "LEFT JOIN companies c ON c.id = j.company_id "
    "ORDER BY CASE s.phase WHEN 'preparing' THEN 0 WHEN 'active' THEN 1 ELSE 2 END, "
    "s.position, s.key, a.updated_at, a.id LIMIT ? OFFSET ?"
)
_PAGE_STATUS_DESC = (
    "SELECT a.id, COALESCE(c.name, ''), j.title, a.status_key, a.applied_at, a.updated_at "
    "FROM application_statuses s "
    "CROSS JOIN applications a ON a.status_key = s.key JOIN jobs j ON j.id = a.job_id "
    "LEFT JOIN companies c ON c.id = j.company_id "
    "ORDER BY CASE s.phase WHEN 'preparing' THEN 0 WHEN 'active' THEN 1 ELSE 2 END DESC, "
    "s.position DESC, s.key DESC, a.updated_at DESC, a.id DESC LIMIT ? OFFSET ?"
)
# Applied: an application never applied for has no date and sorts as the smallest value.
_PAGE_APPLIED_ASC = (
    "SELECT a.id, COALESCE(c.name, ''), j.title, a.status_key, a.applied_at, a.updated_at "
    "FROM applications a "
    "JOIN jobs j ON j.id = a.job_id LEFT JOIN companies c ON c.id = j.company_id "
    "ORDER BY a.applied_at, a.id LIMIT ? OFFSET ?"
)
_PAGE_APPLIED_DESC = (
    "SELECT a.id, COALESCE(c.name, ''), j.title, a.status_key, a.applied_at, a.updated_at "
    "FROM applications a "
    "JOIN jobs j ON j.id = a.job_id LEFT JOIN companies c ON c.id = j.company_id "
    "ORDER BY a.applied_at DESC, a.id DESC LIMIT ? OFFSET ?"
)
_PAGE_ACTIVITY_ASC = (
    "SELECT a.id, COALESCE(c.name, ''), j.title, a.status_key, a.applied_at, a.updated_at "
    "FROM applications a "
    "JOIN jobs j ON j.id = a.job_id LEFT JOIN companies c ON c.id = j.company_id "
    "ORDER BY a.updated_at, a.id LIMIT ? OFFSET ?"
)
_PAGE_ACTIVITY_DESC = (
    "SELECT a.id, COALESCE(c.name, ''), j.title, a.status_key, a.applied_at, a.updated_at "
    "FROM applications a "
    "JOIN jobs j ON j.id = a.job_id LEFT JOIN companies c ON c.id = j.company_id "
    "ORDER BY a.updated_at DESC, a.id DESC LIMIT ? OFFSET ?"
)
_PAGES = {
    ("company", False): _PAGE_COMPANY_ASC,
    ("company", True): _PAGE_COMPANY_DESC,
    ("role", False): _PAGE_ROLE_ASC,
    ("role", True): _PAGE_ROLE_DESC,
    ("status", False): _PAGE_STATUS_ASC,
    ("status", True): _PAGE_STATUS_DESC,
    ("applied", False): _PAGE_APPLIED_ASC,
    ("applied", True): _PAGE_APPLIED_DESC,
    ("activity", False): _PAGE_ACTIVITY_ASC,
    ("activity", True): _PAGE_ACTIVITY_DESC,
}
TABLE_SORTS = ("company", "role", "status", "applied", "activity")


def table_page(
    conn: sqlite3.Connection, sort: str, *, descending: bool, limit: int, offset: int
) -> list[TableRow]:
    """One page of the table sorted by ``sort`` (one of ``TABLE_SORTS``); an unknown sort is
    a ``ValueError``. Company-less jobs are listed with an empty company."""
    statement = _PAGES.get((sort, descending))
    if statement is None:
        raise ValueError("unknown table sort")
    rows = conn.execute(statement, (limit, offset)).fetchall()
    return [TableRow(row[0], row[1] or "", row[2], row[3], row[4], row[5]) for row in rows]
