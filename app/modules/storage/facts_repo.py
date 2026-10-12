"""Rows of ``facts``, ``fact_versions``, ``fact_events`` and ``fact_skills``, and the library
search over ``facts_fts`` (migration 0003; spec 3.2, 3.5; ADR-090). Callers own the
transaction. Versions and events are only ever inserted; a fact changes through one
conditional update that bumps ``version`` and ``updated_at``. Times are UTC ISO 8601 text
from the Clock."""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

# Literal SQL only (no string building); status sets go in as a JSON array.
# ADR-090: the number is picked inside the write transaction, never before it.
_NEXT_NUMBER = "SELECT coalesce(max(number), 0) + 1 FROM facts"
_INSERT_FACT = (
    "INSERT INTO facts (id, number, type, status, role_id, privacy, tags, start_date, "
    "end_date, current_version_id, merged_into_fact_id, cv_import_id, created_at, updated_at, "
    "version) VALUES (?, ?, ?, ?, ?, 'normal', ?, ?, ?, ?, NULL, ?, ?, ?, 1)"
)
_SELECT_FACT = (
    "SELECT f.id, f.number, f.type, f.status, f.role_id, f.privacy, f.tags, f.start_date, "
    "f.end_date, f.merged_into_fact_id, f.cv_import_id, f.created_at, f.updated_at, f.version, "
    "v.id, v.fact_id, v.version, v.text, v.situation, v.action, v.result, v.source, "
    "v.original_answer, v.provider, v.model, v.prompt_id, v.prompt_version, v.needs_check, "
    "v.created_at "
    "FROM facts AS f JOIN fact_versions AS v ON v.id = f.current_version_id WHERE f.id = ?"
)
_UPDATE_FACT = (
    "UPDATE facts SET type = ?, status = ?, role_id = ?, privacy = ?, tags = ?, start_date = ?, "
    "end_date = ?, current_version_id = ?, merged_into_fact_id = ?, updated_at = ?, "
    "version = version + 1 WHERE id = ? AND version = ?"
)
_INSERT_VERSION = (
    "INSERT INTO fact_versions (id, fact_id, version, text, situation, action, result, source, "
    "original_answer, provider, model, prompt_id, prompt_version, needs_check, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
# ADR-090 item 3: history is ordered by time, then by insertion.
_LIST_VERSIONS = (
    "SELECT id, fact_id, version, text, situation, action, result, source, original_answer, "
    "provider, model, prompt_id, prompt_version, needs_check, created_at "
    "FROM fact_versions WHERE fact_id = ? ORDER BY created_at, rowid"
)
_INSERT_EVENT = (
    "INSERT INTO fact_events (id, fact_id, type, fact_version_id, detail, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)
_LATEST_MERGE = (
    "SELECT id, fact_version_id, detail FROM fact_events WHERE fact_id = ? AND type = 'merged' "
    "AND json_extract(detail, '$.draft_id') = ? ORDER BY created_at DESC, rowid DESC LIMIT 1"
)
# Events are only ever inserted, so a later row is a later event even when the times tie.
_DETAILS_CHANGED_AFTER = (
    "SELECT EXISTS (SELECT 1 FROM fact_events WHERE fact_id = ? AND type = 'details_changed' "
    "AND rowid > (SELECT rowid FROM fact_events WHERE id = ?))"
)
# The first version a provider made: where an AI-made fact came from (ADR-089 item 8).
_ORIGIN = (
    "SELECT provider, model FROM fact_versions WHERE fact_id = ? AND provider IS NOT NULL "
    "ORDER BY created_at, rowid LIMIT 1"
)
# The CV line (or the user's own answer) a fact was first made from: the earliest version
# that keeps one, whatever was reworded since.
_SOURCE_QUOTE = (
    "SELECT original_answer FROM fact_versions WHERE fact_id = ? "
    "AND original_answer IS NOT NULL ORDER BY created_at, rowid LIMIT 1"
)
_COUNT_BY_STATUS = "SELECT status, count(*) FROM facts GROUP BY status"
_IDS_WITH_STATUS = "SELECT id FROM facts WHERE status = ? ORDER BY number"
_SKILL_IDS = "SELECT skill_id FROM fact_skills WHERE fact_id = ?"
_SKILL_NAMES = (
    "SELECT s.name FROM fact_skills AS fs JOIN skills AS s ON s.id = fs.skill_id "
    "WHERE fs.fact_id = ? ORDER BY s.normalized_name"
)
_LINK_SKILL = "INSERT INTO fact_skills (fact_id, skill_id) VALUES (?, ?) ON CONFLICT DO NOTHING"
_UNLINK_SKILL = "DELETE FROM fact_skills WHERE fact_id = ? AND skill_id = ?"
# The library: every filter is optional except the statuses. The search entry's rowid is the
# fact's number. Most recently changed first; the number makes the order total.
_COUNT = (
    "SELECT count(*) FROM facts AS f WHERE f.status IN (SELECT value FROM json_each(?)) "
    "AND (? IS NULL OR f.role_id = ?) "
    "AND (? IS NULL OR EXISTS (SELECT 1 FROM fact_skills AS fs "
    "WHERE fs.fact_id = f.id AND fs.skill_id = ?))"
)
_COUNT_MATCHING = (
    "SELECT count(*) FROM facts AS f WHERE f.status IN (SELECT value FROM json_each(?)) "
    "AND (? IS NULL OR f.role_id = ?) "
    "AND (? IS NULL OR EXISTS (SELECT 1 FROM fact_skills AS fs "
    "WHERE fs.fact_id = f.id AND fs.skill_id = ?)) "
    "AND f.number IN (SELECT rowid FROM facts_fts WHERE facts_fts MATCH ?)"
)
_SEARCH = (
    "SELECT f.id FROM facts AS f WHERE f.status IN (SELECT value FROM json_each(?)) "
    "AND (? IS NULL OR f.role_id = ?) "
    "AND (? IS NULL OR EXISTS (SELECT 1 FROM fact_skills AS fs "
    "WHERE fs.fact_id = f.id AND fs.skill_id = ?)) "
    "ORDER BY f.updated_at DESC, f.number DESC LIMIT ? OFFSET ?"
)
_SEARCH_MATCHING = (
    "SELECT f.id FROM facts AS f WHERE f.status IN (SELECT value FROM json_each(?)) "
    "AND (? IS NULL OR f.role_id = ?) "
    "AND (? IS NULL OR EXISTS (SELECT 1 FROM fact_skills AS fs "
    "WHERE fs.fact_id = f.id AND fs.skill_id = ?)) "
    "AND f.number IN (SELECT rowid FROM facts_fts WHERE facts_fts MATCH ?) "
    "ORDER BY f.updated_at DESC, f.number DESC LIMIT ? OFFSET ?"
)


@dataclass(frozen=True)
class StoredVersion:
    id: str
    fact_id: str
    version: int
    text: str | None
    situation: str | None
    action: str | None
    result: str | None
    source: str
    original_answer: str | None
    provider: str | None
    model: str | None
    prompt_id: str | None
    prompt_version: str | None
    needs_check: bool
    created_at: str


@dataclass(frozen=True)
class StoredFact:
    """A ``facts`` row with its current version."""

    id: str
    number: int
    type: str
    status: str
    role_id: str | None
    privacy: str
    tags: str  # a JSON array
    start_date: str | None
    end_date: str | None
    merged_into_fact_id: str | None
    cv_import_id: str | None
    created_at: str
    updated_at: str
    version: int
    current: StoredVersion


@dataclass(frozen=True)
class StoredMerge:
    """The ``merged`` event a merge left on its target."""

    event_id: str
    fact_version_id: str | None
    detail: str | None


def _version(row: Sequence[Any]) -> StoredVersion:
    (
        version_id,
        fact_id,
        version,
        text,
        situation,
        action,
        result,
        source,
        original_answer,
        provider,
        model,
        prompt_id,
        prompt_version,
        needs_check,
        created_at,
    ) = row
    return StoredVersion(
        id=version_id,
        fact_id=fact_id,
        version=version,
        text=text,
        situation=situation,
        action=action,
        result=result,
        source=source,
        original_answer=original_answer,
        provider=provider,
        model=model,
        prompt_id=prompt_id,
        prompt_version=prompt_version,
        needs_check=bool(needs_check),
        created_at=created_at,
    )


def _fact(row: Sequence[Any]) -> StoredFact:
    (
        fact_id,
        number,
        type,
        status,
        role_id,
        privacy,
        tags,
        start_date,
        end_date,
        merged_into_fact_id,
        cv_import_id,
        created_at,
        updated_at,
        version,
    ) = row[:14]
    return StoredFact(
        id=fact_id,
        number=number,
        type=type,
        status=status,
        role_id=role_id,
        privacy=privacy,
        tags=tags,
        start_date=start_date,
        end_date=end_date,
        merged_into_fact_id=merged_into_fact_id,
        cv_import_id=cv_import_id,
        created_at=created_at,
        updated_at=updated_at,
        version=version,
        current=_version(row[14:]),
    )


def next_number(conn: sqlite3.Connection) -> int:
    """The number the next fact gets. Call inside the write transaction that inserts it."""
    number: int = conn.execute(_NEXT_NUMBER).fetchone()[0]
    return number


def insert_fact(
    conn: sqlite3.Connection,
    *,
    fact_id: str,
    number: int,
    type: str,
    status: str,
    role_id: str | None,
    tags: str,
    start_date: str | None,
    end_date: str | None,
    current_version_id: str,
    cv_import_id: str | None,
    now: str,
) -> None:
    """Store a fact that points at a version the same transaction must still insert (the
    key is checked at commit)."""
    conn.execute(
        _INSERT_FACT,
        (
            fact_id,
            number,
            type,
            status,
            role_id,
            tags,
            start_date,
            end_date,
            current_version_id,
            cv_import_id,
            now,
            now,
        ),
    )


def get_fact(conn: sqlite3.Connection, fact_id: str) -> StoredFact | None:
    row = conn.execute(_SELECT_FACT, (fact_id,)).fetchone()
    if row is None:
        return None
    return _fact(row)


def update_fact(
    conn: sqlite3.Connection,
    fact_id: str,
    *,
    expected_version: int,
    type: str,
    status: str,
    role_id: str | None,
    privacy: str,
    tags: str,
    start_date: str | None,
    end_date: str | None,
    current_version_id: str,
    merged_into_fact_id: str | None,
    now: str,
) -> bool:
    """Write the fact's whole changeable state; False when the row is missing or stale."""
    cursor = conn.execute(
        _UPDATE_FACT,
        (
            type,
            status,
            role_id,
            privacy,
            tags,
            start_date,
            end_date,
            current_version_id,
            merged_into_fact_id,
            now,
            fact_id,
            expected_version,
        ),
    )
    return cursor.rowcount == 1


def insert_version(conn: sqlite3.Connection, version: StoredVersion) -> None:
    conn.execute(
        _INSERT_VERSION,
        (
            version.id,
            version.fact_id,
            version.version,
            version.text,
            version.situation,
            version.action,
            version.result,
            version.source,
            version.original_answer,
            version.provider,
            version.model,
            version.prompt_id,
            version.prompt_version,
            int(version.needs_check),
            version.created_at,
        ),
    )


def list_versions(conn: sqlite3.Connection, fact_id: str) -> list[StoredVersion]:
    """A fact's versions, oldest first."""
    return [_version(row) for row in conn.execute(_LIST_VERSIONS, (fact_id,)).fetchall()]


def insert_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    fact_id: str,
    type: str,
    fact_version_id: str | None,
    detail: str | None,
    now: str,
) -> None:
    conn.execute(_INSERT_EVENT, (event_id, fact_id, type, fact_version_id, detail, now))


def latest_merge(conn: sqlite3.Connection, target_id: str, draft_id: str) -> StoredMerge | None:
    """The newest ``merged`` event on ``target_id`` that names ``draft_id``."""
    row = conn.execute(_LATEST_MERGE, (target_id, draft_id)).fetchone()
    return None if row is None else StoredMerge(*row)


def details_changed_after(conn: sqlite3.Connection, fact_id: str, event_id: str) -> bool:
    """Whether the fact has a ``details_changed`` event newer than the event ``event_id``."""
    return bool(conn.execute(_DETAILS_CHANGED_AFTER, (fact_id, event_id)).fetchone()[0])


def origin(conn: sqlite3.Connection, fact_id: str) -> tuple[str, str | None] | None:
    """The provider and model of the fact's earliest version a provider made; None when the
    user wrote every version."""
    row = conn.execute(_ORIGIN, (fact_id,)).fetchone()
    return None if row is None else (row[0], row[1])


def source_quote(conn: sqlite3.Connection, fact_id: str) -> str | None:
    """The ``original_answer`` of the fact's earliest version that has one, else None."""
    row = conn.execute(_SOURCE_QUOTE, (fact_id,)).fetchone()
    return None if row is None else str(row[0])


def count_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    """How many facts have each status; a status no fact has is left out."""
    return {str(row[0]): int(row[1]) for row in conn.execute(_COUNT_BY_STATUS).fetchall()}


def ids_with_status(conn: sqlite3.Connection, status: str) -> list[str]:
    """The ids of the facts with this status, in the order they were made."""
    return [str(row[0]) for row in conn.execute(_IDS_WITH_STATUS, (status,)).fetchall()]


def skill_ids(conn: sqlite3.Connection, fact_id: str) -> frozenset[str]:
    return frozenset(row[0] for row in conn.execute(_SKILL_IDS, (fact_id,)).fetchall())


def skill_names(conn: sqlite3.Connection, fact_id: str) -> tuple[str, ...]:
    """The names of a fact's skills, in alphabetical order."""
    return tuple(row[0] for row in conn.execute(_SKILL_NAMES, (fact_id,)).fetchall())


def link_skill(conn: sqlite3.Connection, fact_id: str, skill_id: str) -> None:
    conn.execute(_LINK_SKILL, (fact_id, skill_id))


def unlink_skill(conn: sqlite3.Connection, fact_id: str, skill_id: str) -> None:
    conn.execute(_UNLINK_SKILL, (fact_id, skill_id))


def search(
    conn: sqlite3.Connection,
    *,
    statuses: str,
    role_id: str | None,
    skill_id: str | None,
    match: str | None,
    limit: int,
    offset: int,
) -> tuple[list[str], int]:
    """One page of fact ids and the number of matches on all pages. ``statuses`` is a JSON
    array; ``match`` is an FTS5 query of quoted tokens (``app.domain.fts``) or None."""
    filters: tuple[object, ...] = (statuses, role_id, role_id, skill_id, skill_id)
    if match is None:
        total = conn.execute(_COUNT, filters).fetchone()[0]
        rows = conn.execute(_SEARCH, (*filters, limit, offset)).fetchall()
    else:
        total = conn.execute(_COUNT_MATCHING, (*filters, match)).fetchone()[0]
        rows = conn.execute(_SEARCH_MATCHING, (*filters, match, limit, offset)).fetchall()
    return [row[0] for row in rows], int(total)
