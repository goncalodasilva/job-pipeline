"""Rows of ``usage_records``, ``budget_notices`` and ``provider_usage_snapshots`` (spec 3.7;
ADR-076 items 4, 5, 7; ADR-078). Callers own the transaction. Times are UTC ISO 8601 text
from the Clock; cycle bounds are UTC text, ``[start, end)``."""

import sqlite3
from dataclasses import dataclass
from datetime import date

# Literal SQL only (no string building).
_INSERT_RECORD = (
    "INSERT INTO usage_records (id, provider, service, task_type, tier, task_id, units, "
    "requested_at, success, input_tokens, output_tokens, created_at, updated_at, version) "
    "VALUES (?, ?, ?, ?, ?, ?, 1, ?, NULL, NULL, NULL, ?, ?, 1)"
)
_COMPLETE_RECORD = (
    "UPDATE usage_records SET success = ?, input_tokens = ?, output_tokens = ?, "
    "updated_at = ?, version = version + 1 WHERE id = ? AND success IS NULL"
)
_COUNT_REQUESTS = (
    "SELECT count(*) FROM usage_records WHERE service = ? AND requested_at >= ? "
    "AND requested_at < ? AND (? IS NULL OR tier = ?)"
)
_COUNT_UNFINISHED = (
    "SELECT count(*) FROM usage_records WHERE service = ? AND requested_at >= ? "
    "AND requested_at < ? AND success IS NULL"
)
_BREAKDOWN = (
    "SELECT tier, task_type, count(*), "
    "coalesce(sum(CASE WHEN success IS NULL THEN 1 ELSE 0 END), 0), "
    "coalesce(sum(input_tokens), 0), coalesce(sum(output_tokens), 0) "
    "FROM usage_records WHERE service = ? AND requested_at >= ? AND requested_at < ? "
    "GROUP BY tier, task_type ORDER BY tier, task_type"
)
_INSERT_NOTICE = (
    "INSERT INTO budget_notices (id, service, cycle_start, threshold, shown_at) "
    "VALUES (?, ?, ?, ?, ?) ON CONFLICT (service, cycle_start, threshold) DO NOTHING"
)
_SHOWN_THRESHOLDS = "SELECT threshold FROM budget_notices WHERE service = ? AND cycle_start = ?"
# An observation older than the stored one never replaces it.
_UPSERT_SNAPSHOT = (
    'INSERT INTO provider_usage_snapshots (id, service, "window", unit, used, limit_count, '
    "utilization, resets_at, observed_at, created_at, updated_at, version) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1) "
    'ON CONFLICT (service, "window") DO UPDATE SET unit = excluded.unit, '
    "used = excluded.used, limit_count = excluded.limit_count, "
    "utilization = excluded.utilization, resets_at = excluded.resets_at, "
    "observed_at = excluded.observed_at, updated_at = excluded.updated_at, "
    "version = provider_usage_snapshots.version + 1 "
    "WHERE excluded.observed_at >= provider_usage_snapshots.observed_at"
)
_LIST_SNAPSHOTS = (
    'SELECT service, "window", unit, used, limit_count, utilization, resets_at, observed_at, '
    'version FROM provider_usage_snapshots WHERE service = ? ORDER BY "window"'
)


@dataclass(frozen=True)
class BreakdownRow:
    tier: str | None
    task_type: str
    requests: int
    unfinished: int
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class StoredSnapshot:
    service: str
    window: str
    unit: str
    used: int | None
    limit_count: int | None
    utilization: float | None
    resets_at: str | None
    observed_at: str
    version: int


def insert_record(
    conn: sqlite3.Connection,
    *,
    record_id: str,
    service: str,
    tier: str,
    task_type: str,
    task_id: str | None,
    now: str,
) -> None:
    """Store an unfinished request; it counts toward the budget from now on.

    ``provider`` (from 0001) holds the service id too.
    """
    conn.execute(
        _INSERT_RECORD, (record_id, service, service, task_type, tier, task_id, now, now, now)
    )


def complete_record(
    conn: sqlite3.Connection,
    record_id: str,
    *,
    success: bool,
    input_tokens: int | None,
    output_tokens: int | None,
    now: str,
) -> bool:
    """Record the outcome of an unfinished request; False when it is unknown or finished."""
    cursor = conn.execute(
        _COMPLETE_RECORD, (int(success), input_tokens, output_tokens, now, record_id)
    )
    return cursor.rowcount == 1


def count_requests(
    conn: sqlite3.Connection, service: str, *, start: str, end: str, tier: str | None = None
) -> int:
    """Requests in ``[start, end)``, finished or not, optionally for one tier."""
    row = conn.execute(_COUNT_REQUESTS, (service, start, end, tier, tier)).fetchone()
    count: int = row[0]
    return count


def count_unfinished(conn: sqlite3.Connection, service: str, *, start: str, end: str) -> int:
    row = conn.execute(_COUNT_UNFINISHED, (service, start, end)).fetchone()
    count: int = row[0]
    return count


def breakdown(
    conn: sqlite3.Connection, service: str, *, start: str, end: str
) -> list[BreakdownRow]:
    rows = conn.execute(_BREAKDOWN, (service, start, end)).fetchall()
    return [BreakdownRow(*row) for row in rows]


def insert_notice(
    conn: sqlite3.Connection,
    *,
    notice_id: str,
    service: str,
    cycle_start: date,
    threshold: str,
    now: str,
) -> bool:
    """Record a shown warning; False when that service, cycle and threshold is stored."""
    cursor = conn.execute(
        _INSERT_NOTICE, (notice_id, service, cycle_start.isoformat(), threshold, now)
    )
    return cursor.rowcount == 1


def shown_thresholds(conn: sqlite3.Connection, service: str, cycle_start: date) -> frozenset[str]:
    rows = conn.execute(_SHOWN_THRESHOLDS, (service, cycle_start.isoformat())).fetchall()
    return frozenset(row[0] for row in rows)


def upsert_snapshot(
    conn: sqlite3.Connection,
    *,
    snapshot_id: str,
    service: str,
    window: str,
    unit: str,
    used: int | None,
    limit_count: int | None,
    utilization: float | None,
    resets_at: str | None,
    observed_at: str,
    now: str,
) -> bool:
    """Insert or update the (service, window) row in place; False when the stored
    observation is newer (``snapshot_id`` is used only for a new row)."""
    cursor = conn.execute(
        _UPSERT_SNAPSHOT,
        (
            snapshot_id,
            service,
            window,
            unit,
            used,
            limit_count,
            utilization,
            resets_at,
            observed_at,
            now,
            now,
        ),
    )
    return cursor.rowcount == 1


def list_snapshots(conn: sqlite3.Connection, service: str) -> list[StoredSnapshot]:
    rows = conn.execute(_LIST_SNAPSHOTS, (service,)).fetchall()
    return [StoredSnapshot(*row) for row in rows]
