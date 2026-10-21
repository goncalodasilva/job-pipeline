"""Contacts, their links to applications, interactions and a company's history (M5.6).

Callers own the transaction. Times are UTC ISO 8601 text from the Clock. Literal SQL only; no
deletes (contacts are never deleted in M5, only unlinked).
"""

import sqlite3
from collections.abc import Mapping
from typing import Any

from app.domain.errors import VersionConflict

_KEYS = (
    "id",
    "company_id",
    "name",
    "role",
    "email",
    "phone",
    "relationship",
    "notes",
    "updated_at",
    "version",
)


def _contact(row: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip(_KEYS, row, strict=True))


def get(conn: sqlite3.Connection, contact_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, company_id, name, role, email, phone, relationship, notes, updated_at, "
        "version FROM contacts WHERE id = ?",
        (contact_id,),
    ).fetchone()
    return None if row is None else _contact(row)


def for_company(conn: sqlite3.Connection, company_id: str) -> list[dict[str, Any]]:
    """The company's contacts, each with ``last_contact`` (the latest interaction's stored
    time, or an empty string) and ``applications`` (the ones it is linked to, by title)."""
    contacts = [
        _contact(row)
        for row in conn.execute(
            "SELECT id, company_id, name, role, email, phone, relationship, notes, updated_at, "
            "version FROM contacts WHERE company_id = ?",
            (company_id,),
        )
    ]
    last = dict(
        conn.execute(
            "SELECT i.contact_id, MAX(i.happened_at) FROM interactions i "
            "JOIN contacts c ON c.id = i.contact_id WHERE c.company_id = ? GROUP BY i.contact_id",
            (company_id,),
        ).fetchall()
    )
    linked: dict[str, list[dict[str, str]]] = {}
    for contact_id, application_id, title in conn.execute(
        "SELECT ac.contact_id, a.id, j.title FROM application_contacts ac "
        "JOIN contacts c ON c.id = ac.contact_id JOIN applications a ON a.id = ac.application_id "
        "JOIN jobs j ON j.id = a.job_id WHERE c.company_id = ? "
        "ORDER BY j.title COLLATE NOCASE, a.id",
        (company_id,),
    ):
        linked.setdefault(contact_id, []).append({"id": application_id, "title": title})
    for contact in contacts:
        contact["last_contact"] = last.get(contact["id"]) or ""
        contact["applications"] = linked.get(contact["id"], [])
    return contacts


def linked_ids(conn: sqlite3.Connection, application_id: str) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT contact_id FROM application_contacts WHERE application_id = ?",
            (application_id,),
        )
    ]


def insert(
    conn: sqlite3.Connection, *, id: str, company_id: str, values: Mapping[str, str], now: str
) -> None:
    conn.execute(
        "INSERT INTO contacts (id, company_id, name, role, email, phone, relationship, notes, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            id,
            company_id,
            values["name"],
            values["role"],
            values["email"],
            values["phone"],
            values["relationship"],
            values["notes"],
            now,
            now,
        ),
    )


def update(
    conn: sqlite3.Connection,
    contact_id: str,
    *,
    values: Mapping[str, str],
    expected_version: int,
    now: str,
) -> None:
    """Write the six fields if the contact still has ``expected_version``; otherwise
    ``VersionConflict`` and nothing is written."""
    result = conn.execute(
        "UPDATE contacts SET name = ?, role = ?, email = ?, phone = ?, relationship = ?, "
        "notes = ?, updated_at = ?, version = version + 1 WHERE id = ? AND version = ?",
        (
            values["name"],
            values["role"],
            values["email"],
            values["phone"],
            values["relationship"],
            values["notes"],
            now,
            contact_id,
            expected_version,
        ),
    )
    if result.rowcount != 1:
        raise VersionConflict()


def link(conn: sqlite3.Connection, application_id: str, contact_id: str, now: str) -> bool:
    """Link the contact (a no-op when already linked). True when a link was made."""
    result = conn.execute(
        "INSERT OR IGNORE INTO application_contacts (application_id, contact_id, created_at, "
        "updated_at) VALUES (?, ?, ?, ?)",
        (application_id, contact_id, now, now),
    )
    return result.rowcount == 1


def unlink(conn: sqlite3.Connection, application_id: str, contact_id: str) -> None:
    """Remove the link only; the contact stays."""
    conn.execute(
        "DELETE FROM application_contacts WHERE application_id = ? AND contact_id = ?",
        (application_id, contact_id),
    )


def insert_interaction(
    conn: sqlite3.Connection,
    *,
    id: str,
    contact_id: str | None,
    application_id: str,
    happened_at: str,
    channel: str,
    note: str,
    now: str,
) -> None:
    conn.execute(
        "INSERT INTO interactions (id, contact_id, application_id, happened_at, channel, note, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (id, contact_id, application_id, happened_at, channel, note, now, now),
    )


def history(conn: sqlite3.Connection, company_id: str) -> list[dict[str, Any]]:
    """Every application for a job at the company with its status label and phase."""
    return [
        {
            "id": id_,
            "title": title,
            "applied_at": applied_at,
            "created_at": created_at,
            "label": label,
            "phase": phase,
        }
        for id_, title, applied_at, created_at, label, phase in conn.execute(
            "SELECT a.id, j.title, a.applied_at, a.created_at, s.label, s.phase "
            "FROM applications a JOIN jobs j ON j.id = a.job_id "
            "JOIN application_statuses s ON s.key = a.status_key WHERE j.company_id = ?",
            (company_id,),
        )
    ]


def job_has_contact_links(conn: sqlite3.Connection, job_id: str) -> bool:
    """Whether a contact is tied to the job's application (linked or logged in an interaction),
    which keeps the job's company from changing (migration 0005)."""
    return (
        conn.execute(
            "SELECT 1 FROM applications a JOIN application_contact_links l "
            "ON l.application_id = a.id WHERE a.job_id = ? LIMIT 1",
            (job_id,),
        ).fetchone()
        is not None
    )
