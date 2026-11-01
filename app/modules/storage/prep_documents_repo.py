"""Narrative card rows (M8.2 part B): prep documents, their versions, units, citations and sent
facts (0008 and 0009).

Callers own the transaction. A version and its units are written once and never changed (the
triggers of 0008 and 0009); only a document's current version and a version's status move, both
version-checked. Flags are stored as the JSON array ``[{"kind", "claim"}]``; a citation is the
pair (fact number, version) of a stored fact version. A unit is stored at ``position + 1``
(positions start at 1 in 0008); callers see positions from 0. Units and sent facts are read as
the ``documents_repo`` dataclasses, which hold the same fields.
"""

import json
import sqlite3
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from app.domain.errors import VersionConflict
from app.domain.facts import FactRef, fact_ref_text
from app.modules.storage.documents_repo import SentFact, StoredFlag, StoredUnit


@dataclass(frozen=True)
class StoredDocument:
    id: str
    current_version_id: str
    version: int


@dataclass(frozen=True)
class StoredVersion:
    id: str
    document_id: str
    number: int
    status: str
    origin: str
    angle_id: str | None
    angle_text: str
    parent_version_id: str | None
    reused_version_id: str | None
    provider: str | None
    created_at: str
    version: int


@dataclass(frozen=True)
class NewVersion:
    id: str
    document_id: str
    number: int
    origin: str
    parent_version_id: str | None
    angle_id: str | None
    angle_text: str
    reused_version_id: str | None
    provider: str | None
    model: str | None
    prompt_id: str | None
    prompt_version: str | None
    now: str


@dataclass(frozen=True)
class ReusableSource:
    """An approved version of another application's card: its id, the application's job title
    and company, and the version's number."""

    version_id: str
    title: str
    company: str
    number: int
    angle_id: str | None


# Literal SQL only (no string building).
_VERSION_SELECT = (
    "SELECT id, document_id, number, status, origin, angle_id, angle_text, parent_version_id, "
    "reused_version_id, provider, created_at, version FROM prep_document_versions "
)


def document(
    conn: sqlite3.Connection, application_id: str, kind: str, question_id: str | None = None
) -> StoredDocument | None:
    """The application's document of ``kind``; an adapted answer is one per question, so it is
    found by ``question_id`` too."""
    row = conn.execute(
        "SELECT id, current_version_id, version FROM prep_documents "
        "WHERE application_id = ? AND kind = ? AND question_id IS ?",
        (application_id, kind, question_id),
    ).fetchone()
    return StoredDocument(row[0], row[1], row[2]) if row else None


def insert_document(
    conn: sqlite3.Connection,
    *,
    id: str,
    application_id: str,
    kind: str,
    first_version_id: str,
    now: str,
    question_id: str | None = None,
) -> None:
    """The document, pointing at its first version. Insert the version in the same
    transaction: the schema checks the pointer at commit. An adapted answer names its
    question."""
    conn.execute(
        "INSERT INTO prep_documents (id, application_id, kind, question_id, current_version_id, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (id, application_id, kind, question_id, first_version_id, now, now),
    )


def set_current_version(
    conn: sqlite3.Connection, doc: StoredDocument, version_id: str, *, now: str
) -> None:
    result = conn.execute(
        "UPDATE prep_documents SET current_version_id = ?, updated_at = ?, version = version + 1 "
        "WHERE id = ? AND version = ?",
        (version_id, now, doc.id, doc.version),
    )
    if result.rowcount != 1:
        raise VersionConflict()


def next_number(conn: sqlite3.Connection, document_id: str) -> int:
    row = conn.execute(
        "SELECT coalesce(max(number), 0) + 1 FROM prep_document_versions WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    return int(row[0])


def insert_version(conn: sqlite3.Connection, new: NewVersion) -> None:
    conn.execute(
        "INSERT INTO prep_document_versions (id, document_id, number, origin, angle_id, "
        "angle_text, parent_version_id, reused_version_id, provider, model, prompt_id, "
        "prompt_version, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            new.id,
            new.document_id,
            new.number,
            new.origin,
            new.angle_id,
            new.angle_text,
            new.parent_version_id,
            new.reused_version_id,
            new.provider,
            new.model,
            new.prompt_id,
            new.prompt_version,
            new.now,
            new.now,
        ),
    )


def set_version_status(
    conn: sqlite3.Connection, version: StoredVersion, status: str, *, now: str
) -> None:
    result = conn.execute(
        "UPDATE prep_document_versions SET status = ?, updated_at = ?, version = version + 1 "
        "WHERE id = ? AND version = ?",
        (status, now, version.id, version.version),
    )
    if result.rowcount != 1:
        raise VersionConflict()


def approve_draft(conn: sqlite3.Connection, version_id: str, *, now: str) -> None:
    """Approve a version written in this transaction (code-built cards)."""
    result = conn.execute(
        "UPDATE prep_document_versions SET status = 'approved', updated_at = ?, "
        "version = version + 1 WHERE id = ? AND status = 'draft'",
        (now, version_id),
    )
    if result.rowcount != 1:
        raise VersionConflict()


def get_version(
    conn: sqlite3.Connection, document_id: str, version_id: str
) -> StoredVersion | None:
    row = conn.execute(
        _VERSION_SELECT + "WHERE document_id = ? AND id = ?", (document_id, version_id)
    ).fetchone()
    return StoredVersion(*row) if row else None


def list_versions(conn: sqlite3.Connection, document_id: str) -> tuple[StoredVersion, ...]:
    """Every version of the document, newest first."""
    rows = conn.execute(
        _VERSION_SELECT + "WHERE document_id = ? ORDER BY number DESC", (document_id,)
    )
    return tuple(StoredVersion(*row) for row in rows)


@dataclass(frozen=True)
class SourceVersion:
    """A version with its document's application and kind, and the model and prompt that wrote
    it (copied to a reused version)."""

    version: StoredVersion
    application_id: str
    kind: str
    model: str | None
    prompt_id: str | None
    prompt_version: str | None


def source_version(conn: sqlite3.Connection, version_id: str) -> SourceVersion | None:
    row = conn.execute(
        "SELECT d.application_id, d.kind, v.model, v.prompt_id, v.prompt_version FROM "
        "prep_document_versions v JOIN prep_documents d ON d.id = v.document_id WHERE v.id = ?",
        (version_id,),
    ).fetchone()
    found = conn.execute(_VERSION_SELECT + "WHERE id = ?", (version_id,)).fetchone()
    if row is None or found is None:
        return None
    return SourceVersion(StoredVersion(*found), row[0], row[1], row[2], row[3], row[4])


def reusable_sources(
    conn: sqlite3.Connection, application_id: str, kind: str, angle_id: str
) -> tuple[ReusableSource, ...]:
    """The newest approved version of each OTHER application's card of ``kind`` written with
    ``angle_id``, newest first."""
    rows = conn.execute(
        "SELECT v.id, coalesce(j.title, ''), coalesce(c.name, ''), v.number, v.angle_id "
        "FROM prep_document_versions v JOIN prep_documents d ON d.id = v.document_id "
        "JOIN applications a ON a.id = d.application_id JOIN jobs j ON j.id = a.job_id "
        "LEFT JOIN companies c ON c.id = j.company_id "
        "WHERE d.kind = ? AND d.application_id <> ? AND v.status = 'approved' AND v.angle_id = ? "
        "AND v.number = (SELECT max(w.number) FROM prep_document_versions w "
        "WHERE w.document_id = d.id AND w.status = 'approved') "
        "ORDER BY v.updated_at DESC, v.number DESC, v.id",
        (kind, application_id, angle_id),
    )
    return tuple(ReusableSource(*row) for row in rows)


def _flags(text: str) -> tuple[StoredFlag, ...]:
    return tuple(StoredFlag(str(item["kind"]), str(item["claim"])) for item in json.loads(text))


def units(conn: sqlite3.Connection, version_id: str) -> tuple[StoredUnit, ...]:
    """The version's units in order, tombstones included, each with its citations."""
    cited: dict[str, list[str]] = {}
    for unit_id, number, version in conn.execute(
        "SELECT c.unit_id, f.number, fv.version FROM prep_unit_facts c "
        "JOIN prep_units u ON u.id = c.unit_id "
        "JOIN facts f ON f.id = c.fact_id JOIN fact_versions fv ON fv.id = c.fact_version_id "
        "WHERE u.document_version_id = ? ORDER BY f.number",
        (version_id,),
    ):
        cited.setdefault(unit_id, []).append(fact_ref_text(number, version))
    rows = conn.execute(
        "SELECT id, position, section, kind, text, origin, removed, added_details, flags "
        "FROM prep_units WHERE document_version_id = ? ORDER BY position",
        (version_id,),
    )
    return tuple(
        StoredUnit(
            row[0],
            row[1] - 1,
            row[2],
            None,
            row[3],
            row[4],
            row[5],
            bool(row[6]),
            tuple(str(item) for item in json.loads(row[7])),
            _flags(row[8]),
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
    names a stored fact version in ``citable`` (the version's sent facts and the facts the person
    saved from a document; one row per fact). A ref naming no stored version, or a fact the
    request did not hold, has no row: its flag keeps the claim."""
    conn.execute(
        "INSERT INTO prep_units (id, document_version_id, position, kind, section, text, origin, "
        "added_details, flags, removed, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            unit.id,
            version_id,
            position + 1,
            unit.kind,
            unit.section,
            unit.text,
            unit.origin,
            json.dumps(list(unit.added_details), ensure_ascii=False),
            json.dumps(
                [{"kind": flag.kind, "claim": flag.claim} for flag in unit.flags],
                ensure_ascii=False,
            ),
            int(unit.removed),
            now,
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
            "INSERT INTO prep_unit_facts (unit_id, fact_id, fact_version_id, created_at, "
            "updated_at) SELECT ?, f.id, v.id, ?, ? FROM facts f "
            "JOIN fact_versions v ON v.fact_id = f.id WHERE f.number = ? AND v.version = ?",
            (unit.id, now, now, parsed.number, parsed.version),
        )
        if linked.rowcount:
            cited.add(parsed.number)


def insert_sent_facts(
    conn: sqlite3.Connection, version_id: str, pairs: Sequence[tuple[int, int]], *, now: str
) -> None:
    for number, version in pairs:
        conn.execute(
            "INSERT INTO prep_version_facts (version_id, fact_id, fact_version_id, created_at, "
            "updated_at) SELECT ?, f.id, v.id, ?, ? FROM facts f "
            "JOIN fact_versions v ON v.fact_id = f.id WHERE f.number = ? AND v.version = ?",
            (version_id, now, now, number, version),
        )


def copy_sent_facts(
    conn: sqlite3.Connection, from_version_id: str, to_version_id: str, *, now: str
) -> None:
    conn.execute(
        "INSERT INTO prep_version_facts (version_id, fact_id, fact_version_id, created_at, "
        "updated_at) SELECT ?, fact_id, fact_version_id, ?, ? FROM prep_version_facts "
        "WHERE version_id = ?",
        (to_version_id, now, now, from_version_id),
    )


def sent_facts(conn: sqlite3.Connection, version_id: str) -> tuple[SentFact, ...]:
    """The fact versions the version's request held, as they were worded then."""
    rows = conn.execute(
        "SELECT f.number, v.version, f.id, v.text, v.situation, v.action, v.result, f.role_id, "
        "f.privacy FROM prep_version_facts s JOIN facts f ON f.id = s.fact_id "
        "JOIN fact_versions v ON v.id = s.fact_version_id "
        "WHERE s.version_id = ? ORDER BY f.number",
        (version_id,),
    )
    return tuple(SentFact(*row) for row in rows)


@dataclass(frozen=True)
class JobPay:
    low: float | None
    high: float | None
    currency: str | None
    period: str | None


def job_pay(conn: sqlite3.Connection, application_id: str) -> JobPay | None:
    """The pay the posting states, as stored on the application's job."""
    row = conn.execute(
        "SELECT j.salary_min, j.salary_max, j.salary_currency, j.salary_period "
        "FROM applications a JOIN jobs j ON j.id = a.job_id WHERE a.id = ?",
        (application_id,),
    ).fetchone()
    return JobPay(*row) if row else None


def company_notes(conn: sqlite3.Connection, application_id: str) -> str:
    """The company's saved notes ("" when none): untrusted text of the person."""
    row = conn.execute(
        "SELECT c.notes FROM applications a JOIN jobs j ON j.id = a.job_id "
        "LEFT JOIN companies c ON c.id = j.company_id WHERE a.id = ?",
        (application_id,),
    ).fetchone()
    return str(row[0]) if row and row[0] else ""


def task_result_ref(conn: sqlite3.Connection, task_id: str) -> str | None:
    """The ``result_ref`` a finished task recorded, else None."""
    row = conn.execute("SELECT result_ref FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return str(row[0]) if row and row[0] is not None else None
