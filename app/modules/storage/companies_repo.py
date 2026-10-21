"""Reads and versioned writes of the ``companies`` table and the jobs at each company (M4.10).

Callers own the transaction. Times are UTC ISO 8601 text from the Clock. Jobs merged into
another card (duplicates) are not counted or listed. Literal SQL only.
"""

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from app.domain.errors import VersionConflict
from app.modules.storage import job_summary_repo

_LISTING = (
    "SELECT c.id, c.name, c.updated_at, "
    "COALESCE(SUM(CASE WHEN j.triage_state <> 'removed' THEN 1 END), 0), "
    "COALESCE(SUM(CASE WHEN j.triage_state = 'to_apply' AND NOT EXISTS ("
    "SELECT 1 FROM applications a JOIN application_statuses s ON s.key = a.status_key "
    "WHERE a.job_id = j.id AND s.phase <> 'preparing') THEN 1 END), 0), "
    "MAX(j.updated_at) "
    "FROM companies c LEFT JOIN jobs j ON j.company_id = c.id AND j.merged_into_job_id IS NULL "
    "GROUP BY c.id"
)


def listing(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every company with its counts and the time of its last activity: the latest of its own
    update and any of its jobs' (a job is updated when it is found, triaged or corrected).
    The counts come from the jobs' triage, never from a stored number."""
    return [
        {
            "id": id_,
            "name": name,
            "open_roles": open_roles,
            "to_apply": to_apply,
            "last_activity": max(updated, job_activity or updated),
        }
        for id_, name, updated, open_roles, to_apply, job_activity in conn.execute(_LISTING)
    ]


def _places(raw: Any) -> list[str]:
    """The stored place names; a value that is not a JSON list (corrupt data) is no places."""
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [p for p in loaded if isinstance(p, str)] if isinstance(loaded, list) else []


def get(conn: sqlite3.Connection, company_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, name, website, industry, size, locations, notes, updated_at, version, name_key "
        "FROM companies WHERE id = ?",
        (company_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "name": row[1],
        "website": row[2],
        "industry": row[3],
        "size": row[4],
        "locations": _places(row[5]),
        "notes": row[6],
        "updated_at": row[7],
        "version": row[8],
        "name_key": row[9],
    }


def jobs(conn: sqlite3.Connection, company_id: str) -> list[dict[str, Any]]:
    """Every job at the company, newest found first (removed ones too). ``moved_on`` is the
    label of the status of a To apply job whose application has left Preparing."""
    return [
        {
            "id": id_,
            "title": title,
            "triage_state": state,
            "snooze_until": until,
            "moved_on": label if state == "to_apply" else None,
            "application_id": application_id,
        }
        for id_, title, state, until, label, application_id in conn.execute(
            "SELECT j.id, j.title, j.triage_state, j.snooze_until, "
            "CASE WHEN s.phase <> 'preparing' THEN s.label END, a.id "
            "FROM jobs j LEFT JOIN applications a ON a.job_id = j.id "
            "LEFT JOIN application_statuses s ON s.key = a.status_key "
            "WHERE j.company_id = ? AND j.merged_into_job_id IS NULL "
            "ORDER BY j.found_at DESC, j.id",
            (company_id,),
        )
    ]


def industries(conn: sqlite3.Connection) -> list[str]:
    return sorted(job_summary_repo.controlled(conn).get("industry", {}))


def name_taken(conn: sqlite3.Connection, name_key: str, company_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM companies WHERE name_key = ? AND id <> ?", (name_key, company_id)
    ).fetchone()
    return row is not None


def update(
    conn: sqlite3.Connection,
    company_id: str,
    *,
    expected_version: int,
    values: Mapping[str, Any],
    name_key: str,
    now: str,
) -> None:
    """Write the checked ``values`` (every column of the row, already merged) if the company
    still has ``expected_version``; ``VersionConflict`` (and nothing written) when it doesn't."""
    cursor = conn.execute(
        "UPDATE companies SET name = ?, name_key = ?, website = ?, industry = ?, size = ?, "
        "locations = ?, notes = ?, updated_at = ?, version = version + 1 "
        "WHERE id = ? AND version = ?",
        (
            values["name"],
            name_key,
            values["website"],
            values["industry"],
            values["size"],
            json.dumps(values["locations"], ensure_ascii=False),
            values["notes"],
            now,
            company_id,
            expected_version,
        ),
    )
    if cursor.rowcount != 1:
        raise VersionConflict()
