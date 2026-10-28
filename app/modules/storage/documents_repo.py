"""Tailored CV rows (M6.7): documents, their versions, units, citations and sent facts.

Callers own the transaction. A version and its units are written once and never changed (0006's
triggers); only a document's current version and a version's status move, both version-checked.
Flags are stored as the JSON array ``[{"kind", "claim"}]``; a citation is the pair (fact number,
version) of a stored fact version and has a row only when that version exists.
"""

import json
import sqlite3
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from app.domain.errors import VersionConflict
from app.domain.facts import FactRef, fact_ref_text


@dataclass(frozen=True)
class StoredDocument:
    id: str
    current_version_id: str
    version: int


@dataclass(frozen=True)
class StoredVersion:
    id: str
    number: int
    status: str
    parent_version_id: str | None
    origin: str
    template_snapshot: str | None
    angle_text: str | None
    note: str | None
    provider: str | None
    created_at: str
    version: int
    tone: str | None = None
    length: str | None = None


@dataclass(frozen=True)
class StoredFlag:
    kind: str
    claim: str


@dataclass(frozen=True)
class StoredUnit:
    """One line of a version. ``refs`` are the cited fact versions as ``F<number>@<version>``."""

    id: str
    position: int
    section: str
    role_id: str | None
    kind: str
    text: str
    origin: str
    removed: bool
    added_details: tuple[str, ...]
    flags: tuple[StoredFlag, ...]
    refs: tuple[str, ...]


@dataclass(frozen=True)
class SentFact:
    """A fact version a request held: its wording then, and the fact's role and privacy."""

    number: int
    version: int
    fact_id: str
    text: str | None
    situation: str | None
    action: str | None
    result: str | None
    role_id: str | None
    privacy: str


@dataclass(frozen=True)
class NewVersion:
    id: str
    document_id: str
    number: int
    parent_version_id: str | None
    origin: str
    template_snapshot: str | None
    angle_text: str | None
    note: str | None
    provider: str | None
    model: str | None
    prompt_id: str | None
    prompt_version: str | None
    task_id: str | None
    now: str
    tone: str | None = None
    length: str | None = None


# Literal SQL only (no string building).
_VERSION_SELECT = (
    "SELECT id, number, status, parent_version_id, origin, template_snapshot, angle_text, note, "
    "provider, created_at, version, tone, length FROM document_versions "
)


def document_for_application(
    conn: sqlite3.Connection, application_id: str, kind: str = "cv"
) -> StoredDocument | None:
    """The application's document of ``kind`` (``cv`` or ``cover_letter``)."""
    row = conn.execute(
        "SELECT id, current_version_id, version FROM documents "
        "WHERE application_id = ? AND kind = ?",
        (application_id, kind),
    ).fetchone()
    return StoredDocument(row[0], row[1], row[2]) if row else None


def insert_document(
    conn: sqlite3.Connection,
    *,
    id: str,
    application_id: str,
    first_version_id: str,
    now: str,
    kind: str = "cv",
) -> None:
    """The document of an application, pointing at its first version. Insert the version in the
    same transaction: the schema checks the pointer at commit."""
    conn.execute(
        "INSERT INTO documents (id, application_id, kind, current_version_id, created_at, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (id, application_id, kind, first_version_id, now, now),
    )


def set_current_version(
    conn: sqlite3.Connection, document: StoredDocument, version_id: str, *, now: str
) -> None:
    result = conn.execute(
        "UPDATE documents SET current_version_id = ?, updated_at = ?, version = version + 1 "
        "WHERE id = ? AND version = ?",
        (version_id, now, document.id, document.version),
    )
    if result.rowcount != 1:
        raise VersionConflict()


def next_number(conn: sqlite3.Connection, document_id: str) -> int:
    row = conn.execute(
        "SELECT coalesce(max(number), 0) + 1 FROM document_versions WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    return int(row[0])


def insert_version(conn: sqlite3.Connection, new: NewVersion) -> None:
    conn.execute(
        "INSERT INTO document_versions (id, document_id, number, origin, parent_version_id, "
        "template_snapshot, angle_text, tone, length, note, provider, model, prompt_id, "
        "prompt_version, task_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            new.id,
            new.document_id,
            new.number,
            new.origin,
            new.parent_version_id,
            new.template_snapshot,
            new.angle_text,
            new.tone,
            new.length,
            new.note,
            new.provider,
            new.model,
            new.prompt_id,
            new.prompt_version,
            new.task_id,
            new.now,
            new.now,
        ),
    )


def set_version_status(
    conn: sqlite3.Connection, version: StoredVersion, status: str, *, now: str
) -> None:
    result = conn.execute(
        "UPDATE document_versions SET status = ?, updated_at = ?, version = version + 1 "
        "WHERE id = ? AND version = ?",
        (status, now, version.id, version.version),
    )
    if result.rowcount != 1:
        raise VersionConflict()


def get_version(
    conn: sqlite3.Connection, document_id: str, version_id: str
) -> StoredVersion | None:
    row = conn.execute(
        _VERSION_SELECT + "WHERE document_id = ? AND id = ?",
        (document_id, version_id),
    ).fetchone()
    return StoredVersion(*row) if row else None


def list_versions(conn: sqlite3.Connection, document_id: str) -> tuple[StoredVersion, ...]:
    """Every version of the document, newest first."""
    rows = conn.execute(
        _VERSION_SELECT + "WHERE document_id = ? ORDER BY number DESC",
        (document_id,),
    )
    return tuple(StoredVersion(*row) for row in rows)


def _flags(text: str) -> tuple[StoredFlag, ...]:
    loaded = json.loads(text)
    return tuple(StoredFlag(str(item["kind"]), str(item["claim"])) for item in loaded)


def units(conn: sqlite3.Connection, version_id: str) -> tuple[StoredUnit, ...]:
    """The version's units in order, tombstones included, each with its citations."""
    cited: dict[str, list[str]] = {}
    for unit_id, number, version in conn.execute(
        "SELECT c.document_unit_id, f.number, fv.version FROM document_unit_facts c "
        "JOIN document_units u ON u.id = c.document_unit_id "
        "JOIN facts f ON f.id = c.fact_id JOIN fact_versions fv ON fv.id = c.fact_version_id "
        "WHERE u.document_version_id = ? ORDER BY f.number",
        (version_id,),
    ):
        cited.setdefault(unit_id, []).append(fact_ref_text(number, version))
    rows = conn.execute(
        "SELECT id, position, section, role_id, kind, text, origin, removed, added_details, flags "
        "FROM document_units WHERE document_version_id = ? ORDER BY position",
        (version_id,),
    )
    return tuple(
        StoredUnit(
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            row[6],
            bool(row[7]),
            tuple(str(item) for item in json.loads(row[8])),
            _flags(row[9]),
            tuple(cited.get(row[0], ())),
        )
        for row in rows
    )


def insert_unit(
    conn: sqlite3.Connection,
    version_id: str,
    position: int,
    unit: StoredUnit,
    *,
    now: str,
    citable: Collection[tuple[int, int]],
) -> None:
    """One unit of a draft version at ``position`` and a citation row for each of its refs that
    names a stored fact version in ``citable`` (the version's sent facts and the facts the user
    saved from a document; one row per fact). A ref naming no stored version, or a fact the
    request did not hold (never-use, draft, archived), has no row: its flag keeps the claim and
    the screen never shows that fact's text as the line's wording."""
    conn.execute(
        "INSERT INTO document_units (id, document_version_id, position, section, role_id, kind, "
        "text, origin, removed, added_details, flags, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            unit.id,
            version_id,
            position,
            unit.section,
            unit.role_id,
            unit.kind,
            unit.text,
            unit.origin,
            int(unit.removed),
            json.dumps(list(unit.added_details), ensure_ascii=False),
            json.dumps(
                [{"kind": flag.kind, "claim": flag.claim} for flag in unit.flags],
                ensure_ascii=False,
            ),
            now,
        ),
    )
    cited: set[int] = set()
    for ref in unit.refs:
        try:
            parsed = FactRef.parse(ref)
        except ValueError:
            continue
        if parsed.number in cited or (parsed.number, parsed.version) not in citable:
            continue
        linked = conn.execute(
            "INSERT INTO document_unit_facts (document_unit_id, fact_id, fact_version_id) "
            "SELECT ?, f.id, v.id FROM facts f JOIN fact_versions v ON v.fact_id = f.id "
            "WHERE f.number = ? AND v.version = ?",
            (unit.id, parsed.number, parsed.version),
        )
        if linked.rowcount:
            cited.add(parsed.number)


def insert_sent_facts(
    conn: sqlite3.Connection, version_id: str, pairs: Sequence[tuple[int, int]]
) -> None:
    for number, version in pairs:
        conn.execute(
            "INSERT INTO document_sent_facts (document_version_id, fact_id, fact_version_id) "
            "SELECT ?, f.id, v.id FROM facts f JOIN fact_versions v ON v.fact_id = f.id "
            "WHERE f.number = ? AND v.version = ?",
            (version_id, number, version),
        )


def copy_sent_facts(conn: sqlite3.Connection, from_version_id: str, to_version_id: str) -> None:
    conn.execute(
        "INSERT INTO document_sent_facts (document_version_id, fact_id, fact_version_id) "
        "SELECT ?, fact_id, fact_version_id FROM document_sent_facts WHERE document_version_id = ?",
        (to_version_id, from_version_id),
    )


def sent_facts(conn: sqlite3.Connection, version_id: str) -> tuple[SentFact, ...]:
    """The fact versions the version's request held, as they were worded then."""
    rows = conn.execute(
        "SELECT f.number, v.version, f.id, v.text, v.situation, v.action, v.result, f.role_id, "
        "f.privacy FROM document_sent_facts s JOIN facts f ON f.id = s.fact_id "
        "JOIN fact_versions v ON v.id = s.fact_version_id "
        "WHERE s.document_version_id = ? ORDER BY f.number",
        (version_id,),
    )
    return tuple(SentFact(*row) for row in rows)


def facts_saved_from_documents(conn: sqlite3.Connection) -> tuple[SentFact, ...]:
    """The fact versions the user saved from a document ("This is true: save it as a fact"):
    theirs to cite in any version, though no request held them."""
    rows = conn.execute(
        "SELECT f.number, v.version, f.id, v.text, v.situation, v.action, v.result, f.role_id, "
        "f.privacy FROM fact_versions v JOIN facts f ON f.id = v.fact_id "
        "WHERE v.source = 'document' ORDER BY f.number, v.version"
    )
    return tuple(SentFact(*row) for row in rows)
