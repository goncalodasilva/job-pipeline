"""Reminder persistence (M5.7): the per-status rules, the state of each reminder the user acted
on, and the facts due reminders are computed from. Callers own the transaction. Reads write
nothing: a reminder that was never acted on has no row (ADR-013)."""

import sqlite3
from dataclasses import dataclass

from app.domain.errors import VersionConflict


@dataclass(frozen=True)
class StoredRule:
    status_key: str
    days: int
    enabled: bool
    version: int


@dataclass(frozen=True)
class StoredState:
    occurrence_key: str
    state: str
    snooze_until: str | None
    version: int


@dataclass(frozen=True)
class HistoryRow:
    id: str
    application_id: str
    from_status: str | None
    to_status: str
    changed_at: str


@dataclass(frozen=True)
class EventRow:
    id: str
    application_id: str
    type: str
    starts_at: str
    ends_at: str
    all_day: bool
    status: str
    thank_you_reminder: bool


_RULE = "SELECT status_key, days, enabled, version FROM reminder_rules"


def _rule(row: tuple[object, ...]) -> StoredRule:
    status_key, days, enabled, version = row
    return StoredRule(str(status_key), int(str(days)), bool(enabled), int(str(version)))


def list_rules(conn: sqlite3.Connection) -> list[StoredRule]:
    return [_rule(row) for row in conn.execute(f"{_RULE} ORDER BY status_key").fetchall()]


def get_rule(conn: sqlite3.Connection, status_key: str) -> StoredRule | None:
    row = conn.execute(f"{_RULE} WHERE status_key = ?", (status_key,)).fetchone()
    return None if row is None else _rule(row)


def insert_rule(
    conn: sqlite3.Connection, *, id: str, status_key: str, days: int, enabled: bool, now: str
) -> None:
    conn.execute(
        "INSERT INTO reminder_rules (id, status_key, days, enabled, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (id, status_key, days, int(enabled), now, now),
    )


def update_rule(
    conn: sqlite3.Connection,
    status_key: str,
    *,
    days: int,
    enabled: bool,
    now: str,
    expected_version: int,
) -> None:
    result = conn.execute(
        "UPDATE reminder_rules SET days = ?, enabled = ?, updated_at = ?, version = version + 1 "
        "WHERE status_key = ? AND version = ?",
        (days, int(enabled), now, status_key, expected_version),
    )
    if result.rowcount != 1:
        raise VersionConflict()


def delete_rule_for_status(conn: sqlite3.Connection, status_key: str) -> None:
    """Remove a status's own rule (it is configuration, not use of the status)."""
    conn.execute("DELETE FROM reminder_rules WHERE status_key = ?", (status_key,))


# --- states --------------------------------------------------------------------------------


def _state(row: tuple[object, ...]) -> StoredState:
    key, state, snooze_until, version = row
    return StoredState(
        str(key), str(state), None if snooze_until is None else str(snooze_until), int(str(version))
    )


_STATE = "SELECT occurrence_key, state, snooze_until, version FROM reminder_states"


def list_states(conn: sqlite3.Connection) -> list[StoredState]:
    return [_state(row) for row in conn.execute(f"{_STATE} ORDER BY occurrence_key").fetchall()]


def get_state(conn: sqlite3.Connection, key: str) -> StoredState | None:
    row = conn.execute(f"{_STATE} WHERE occurrence_key = ?", (key,)).fetchone()
    return None if row is None else _state(row)


def insert_state(
    conn: sqlite3.Connection,
    *,
    id: str,
    key: str,
    state: str,
    snooze_until: str | None,
    now: str,
) -> None:
    conn.execute(
        "INSERT INTO reminder_states (id, occurrence_key, state, snooze_until, created_at, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (id, key, state, snooze_until, now, now),
    )


def update_state(
    conn: sqlite3.Connection,
    key: str,
    *,
    state: str,
    snooze_until: str | None,
    now: str,
    expected_version: int,
) -> None:
    result = conn.execute(
        "UPDATE reminder_states SET state = ?, snooze_until = ?, updated_at = ?, "
        "version = version + 1 WHERE occurrence_key = ? AND version = ?",
        (state, snooze_until, now, key, expected_version),
    )
    if result.rowcount != 1:
        raise VersionConflict()


# --- the facts reminders are computed from -----------------------------------------------


def ruled_applications(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """``(application id, current status key)`` of every application whose current status has
    an enabled rule."""
    rows = conn.execute(
        "SELECT a.id, a.status_key FROM applications a "
        "JOIN reminder_rules r ON r.status_key = a.status_key AND r.enabled = 1 "
        "ORDER BY a.id"
    ).fetchall()
    return [(str(row[0]), str(row[1])) for row in rows]


def history_rows(conn: sqlite3.Connection) -> list[HistoryRow]:
    """The status history of those applications, oldest first."""
    rows = conn.execute(
        "SELECT h.id, h.application_id, h.from_status, h.to_status, h.changed_at "
        "FROM status_history h JOIN applications a ON a.id = h.application_id "
        "JOIN reminder_rules r ON r.status_key = a.status_key AND r.enabled = 1 "
        "ORDER BY h.application_id, h.changed_at, h.created_at, h.id"
    ).fetchall()
    return [HistoryRow(*row) for row in rows]


def event_rows(conn: sqlite3.Connection) -> list[EventRow]:
    """Scheduled events that can make a reminder: follow-ups and deadlines, and interviews
    that ask for a thank-you reminder."""
    rows = conn.execute(
        "SELECT id, application_id, type, starts_at, ends_at, all_day, status, "
        "thank_you_reminder FROM events WHERE status = 'scheduled' AND (type <> 'interview' "
        "OR thank_you_reminder = 1) ORDER BY id"
    ).fetchall()
    return [
        EventRow(
            str(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4]), bool(r[5]), str(r[6]), bool(r[7])
        )
        for r in rows
    ]
