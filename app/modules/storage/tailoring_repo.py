"""Tailoring persistence (M6.4): version-checked updates. Callers own the write transaction."""

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.domain.errors import VersionConflict


class _Keep(Enum):
    """Marks a letter choice the caller leaves as it is (``None`` clears it)."""

    UNCHANGED = 0


KEEP = _Keep.UNCHANGED


def update_workspace(
    conn: sqlite3.Connection,
    workspace_id: str,
    *,
    cv_country: str | None,
    angle_id: str | None,
    letter_tone: str | _Keep | None = KEEP,
    letter_length: str | _Keep | None = KEEP,
    now: str,
    expected_version: int,
) -> None:
    """Set the CV country, angle and (unless left as ``KEEP``) the letter tone and length. The
    schema refuses a tone or length outside its closed sets (``sqlite3.IntegrityError``)."""
    result = conn.execute(
        "UPDATE workspaces SET cv_country = ?, angle_id = ?, "
        "letter_tone = CASE WHEN ? THEN letter_tone ELSE ? END, "
        "letter_length = CASE WHEN ? THEN letter_length ELSE ? END, "
        "updated_at = ?, version = version + 1 WHERE id = ? AND version = ?",
        (
            cv_country,
            angle_id,
            letter_tone is KEEP,
            None if letter_tone is KEEP else letter_tone,
            letter_length is KEEP,
            None if letter_length is KEEP else letter_length,
            now,
            workspace_id,
            expected_version,
        ),
    )
    if result.rowcount != 1:
        raise VersionConflict()


def set_current_version(
    conn: sqlite3.Connection,
    document_id: str,
    version_id: str,
    *,
    now: str,
    expected_version: int,
) -> None:
    """Point a document at one of its versions. The schema checks the version is its own, at
    commit (``sqlite3.IntegrityError``)."""
    result = conn.execute(
        "UPDATE documents SET current_version_id = ?, updated_at = ?, version = version + 1 "
        "WHERE id = ? AND version = ?",
        (version_id, now, document_id, expected_version),
    )
    if result.rowcount != 1:
        raise VersionConflict()


def set_version_status(
    conn: sqlite3.Connection,
    version_id: str,
    status: str,
    *,
    now: str,
    expected_version: int,
) -> None:
    """Move a version forward. The schema refuses any other change (``sqlite3.IntegrityError``)."""
    result = conn.execute(
        "UPDATE document_versions SET status = ?, updated_at = ?, version = version + 1 "
        "WHERE id = ? AND version = ?",
        (status, now, version_id, expected_version),
    )
    if result.rowcount != 1:
        raise VersionConflict()


# --- the Tailoring tab (M6.5): reads and append-only writes ------------------------------------

_REFS = "SELECT id, status, error, attempt FROM tasks WHERE dedupe_key = ? "
_LATEST_TASK = (
    _REFS + "ORDER BY CASE WHEN status IN ('queued', 'running') THEN 0 ELSE 1 END, "
    "updated_at DESC, created_at DESC, rowid DESC LIMIT 1"
)


@dataclass(frozen=True)
class StoredWorkspace:
    id: str
    version: int
    cv_country: str | None
    angle_id: str | None
    angle_name: str | None
    angle_description: str | None


@dataclass(frozen=True)
class StoredAngle:
    id: str
    name: str
    description: str


@dataclass(frozen=True)
class StoredResult:
    """A stored gap check or angle proposal: ``result`` is the checked JSON object."""

    id: str
    result: dict[str, Any]
    provider: str | None
    summary: str | None


@dataclass(frozen=True)
class StoredWording:
    fact_id: str
    text: str


def application_job_id(conn: sqlite3.Connection, application_id: str) -> str | None:
    row = conn.execute("SELECT job_id FROM applications WHERE id = ?", (application_id,)).fetchone()
    return str(row[0]) if row else None


def application_header(
    conn: sqlite3.Connection, application_id: str
) -> tuple[str, str, str] | None:
    """(job id, job title, company name or "") of an application."""
    row = conn.execute(
        "SELECT a.job_id, j.title, coalesce(c.name, '') FROM applications a "
        "JOIN jobs j ON j.id = a.job_id LEFT JOIN companies c ON c.id = j.company_id "
        "WHERE a.id = ?",
        (application_id,),
    ).fetchone()
    return (str(row[0]), str(row[1] or ""), str(row[2])) if row else None


def current_summary_text(conn: sqlite3.Connection, job_id: str) -> str | None:
    row = conn.execute(
        "SELECT summary FROM job_summaries WHERE job_id = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    return str(row[0]) if row else None


def workspace(conn: sqlite3.Connection, application_id: str) -> StoredWorkspace | None:
    row = conn.execute(
        "SELECT w.id, w.version, w.cv_country, w.angle_id, a.name, a.description "
        "FROM workspaces w LEFT JOIN angles a ON a.id = w.angle_id WHERE w.application_id = ?",
        (application_id,),
    ).fetchone()
    return StoredWorkspace(*row) if row else None


def get_angle(conn: sqlite3.Connection, angle_id: str) -> StoredAngle | None:
    row = conn.execute(
        "SELECT id, name, description FROM angles WHERE id = ?", (angle_id,)
    ).fetchone()
    return StoredAngle(*row) if row else None


def earlier_angles(
    conn: sqlite3.Connection, application_id: str, limit: int
) -> tuple[StoredAngle, ...]:
    """Angles used by the workspaces of other applications, most recently updated first."""
    rows = conn.execute(
        "SELECT a.id, a.name, a.description FROM workspaces w JOIN angles a ON a.id = w.angle_id "
        "WHERE w.application_id <> ? GROUP BY a.id ORDER BY max(w.updated_at) DESC, a.id LIMIT ?",
        (application_id, limit),
    )
    return tuple(StoredAngle(*row) for row in rows)


def insert_angle(
    conn: sqlite3.Connection, *, id: str, name: str, description: str, origin: str, now: str
) -> None:
    conn.execute(
        "INSERT INTO angles (id, name, description, origin, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (id, name, description, origin, now, now),
    )


def insert_workspace(
    conn: sqlite3.Connection, *, id: str, application_id: str, angle_id: str, now: str
) -> None:
    conn.execute(
        "INSERT INTO workspaces (id, application_id, angle_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (id, application_id, angle_id, now, now),
    )


def insert_gap_check(
    conn: sqlite3.Connection,
    *,
    id: str,
    application_id: str,
    summary: str,
    result: dict[str, Any],
    fact_versions: Sequence[tuple[int, int]],
    provider: str,
    model: str,
    prompt_id: str,
    prompt_version: str,
    task_id: str,
    now: str,
) -> None:
    """Append one gap check and the (fact number, version) pairs it sent."""
    conn.execute(
        "INSERT INTO gap_checks (id, application_id, summary, result, provider, model, "
        "prompt_id, prompt_version, task_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            id,
            application_id,
            summary,
            json.dumps(result, ensure_ascii=False),
            provider,
            model,
            prompt_id,
            prompt_version,
            task_id,
            now,
        ),
    )
    for number, version in fact_versions:
        conn.execute(
            "INSERT INTO gap_check_facts (gap_check_id, fact_id, fact_version_id) "
            "SELECT ?, f.id, v.id FROM facts f JOIN fact_versions v ON v.fact_id = f.id "
            "WHERE f.number = ? AND v.version = ?",
            (id, number, version),
        )


def insert_angle_proposal(
    conn: sqlite3.Connection,
    *,
    id: str,
    application_id: str,
    result: dict[str, Any],
    provider: str,
    model: str,
    prompt_id: str,
    prompt_version: str,
    task_id: str,
    now: str,
) -> None:
    conn.execute(
        "INSERT INTO angle_proposals (id, application_id, result, provider, model, prompt_id, "
        "prompt_version, task_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            id,
            application_id,
            json.dumps(result, ensure_ascii=False),
            provider,
            model,
            prompt_id,
            prompt_version,
            task_id,
            now,
        ),
    )


def latest_gap_check(conn: sqlite3.Connection, application_id: str) -> StoredResult | None:
    row = conn.execute(
        "SELECT id, result, provider, summary FROM gap_checks WHERE application_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (application_id,),
    ).fetchone()
    return StoredResult(row[0], json.loads(row[1]), row[2], row[3]) if row else None


def latest_angle_proposal(conn: sqlite3.Connection, application_id: str) -> StoredResult | None:
    row = conn.execute(
        "SELECT id, result, provider FROM angle_proposals WHERE application_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (application_id,),
    ).fetchone()
    if row is None or row[1] is None:
        return None
    return StoredResult(row[0], json.loads(row[1]), row[2], None)


def fact_wording(conn: sqlite3.Connection, number: int, version: int) -> StoredWording | None:
    """What fact ``number`` said at ``version``, in the user's own words."""
    row = conn.execute(
        "SELECT f.id, v.text, v.situation, v.action, v.result FROM facts f "
        "JOIN fact_versions v ON v.fact_id = f.id WHERE f.number = ? AND v.version = ?",
        (number, version),
    ).fetchone()
    if row is None:
        return None
    parts = [str(part).strip() for part in row[1:] if part and str(part).strip()]
    return StoredWording(str(row[0]), " ".join(parts))


def has_confirmed_fact_text(conn: sqlite3.Connection, text: str) -> bool:
    """Whether a confirmed fact's current version says exactly ``text``."""
    row = conn.execute(
        "SELECT 1 FROM facts f JOIN fact_versions v ON v.id = f.current_version_id "
        "WHERE f.status = 'confirmed' AND v.text = ? LIMIT 1",
        (text,),
    ).fetchone()
    return row is not None


def latest_task(
    conn: sqlite3.Connection, dedupe_key: str
) -> tuple[str, str, str | None, int] | None:
    """(id, status, error, attempt) of the active task, else the latest, for a dedupe key."""
    row = conn.execute(_LATEST_TASK, (dedupe_key,)).fetchone()
    return (row[0], row[1], row[2], row[3]) if row else None
