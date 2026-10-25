"""Tailoring persistence (M6.4): version-checked updates. Callers own the write transaction."""

import sqlite3
from enum import Enum

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
