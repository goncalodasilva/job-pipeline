"""Indexed read models for current citations, frozen submissions and waiting drafts."""

import json
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class FactUse:
    fact_id: str
    application_id: str
    role: str
    company: str | None
    sent: bool
    differs: bool


@dataclass(frozen=True)
class SentDocument:
    kind: str
    number: int
    frozen_at: str
    facts: tuple[tuple[str, int, int], ...]
    files: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class DocumentDraft:
    application_id: str
    kind: str
    role: str
    company: str | None


def used_in(conn: sqlite3.Connection, fact_ids: tuple[str, ...]) -> tuple[FactUse, ...]:
    """One indexed query for a Library page or an editor, deduplicated per application.

    Only visible units in a current or actually frozen version count; generation input sets
    and superseded drafts do not. A sent citation remains visible after a later edit.
    """
    rows = conn.execute(
        "SELECT uf.fact_id,a.id,j.title,c.name,"
        "max(v.id=s.cv_version_id OR v.id=s.letter_version_id),"
        "max(CASE WHEN v.id=s.cv_version_id OR v.id=s.letter_version_id "
        "THEN uf.fact_version_id<>f.current_version_id ELSE 0 END) "
        "FROM document_unit_facts uf INDEXED BY document_unit_facts_fact_id "
        "JOIN document_units u ON u.id=uf.document_unit_id "
        "JOIN document_versions v ON v.id=u.document_version_id "
        "JOIN documents d ON d.id=v.document_id "
        "JOIN applications a ON a.id=d.application_id JOIN jobs j ON j.id=a.job_id "
        "LEFT JOIN companies c ON c.id=j.company_id "
        "LEFT JOIN application_submissions s ON s.application_id=a.id "
        "JOIN facts f ON f.id=uf.fact_id "
        "WHERE uf.fact_id IN (SELECT value FROM json_each(?)) AND u.removed=0 "
        "AND (v.id=d.current_version_id OR v.id=s.cv_version_id OR v.id=s.letter_version_id) "
        "GROUP BY uf.fact_id,a.id ORDER BY uf.fact_id,c.name,j.title,a.id",
        (json.dumps(fact_ids),),
    ).fetchall()
    return tuple(FactUse(f, a, r, c, bool(sent), bool(diff)) for f, a, r, c, sent, diff in rows)


def sent_documents(conn: sqlite3.Connection, aid: str) -> tuple[SentDocument, ...]:
    """One query starting at the submission primary key; exports and units use their indexes."""
    rows = conn.execute(
        "SELECT d.kind,v.number,s.frozen_at,"
        "(SELECT json_group_array(json_array(x.fact_id,x.number,x.version)) FROM "
        "(SELECT DISTINCT f.id AS fact_id,f.number,fv.version "
        "FROM document_units u JOIN document_unit_facts uf ON uf.document_unit_id=u.id "
        "JOIN facts f ON f.id=uf.fact_id JOIN fact_versions fv ON fv.id=uf.fact_version_id "
        "WHERE u.document_version_id=v.id AND u.removed=0 ORDER BY f.number,fv.version) x),"
        "(SELECT json_group_array(json_array(e.id,e.format)) FROM document_exports e "
        "WHERE e.document_version_id=v.id ORDER BY e.format) "
        "FROM application_submissions s JOIN document_versions v "
        "ON v.id=s.cv_version_id OR v.id=s.letter_version_id "
        "JOIN documents d ON d.id=v.document_id WHERE s.application_id=? "
        "ORDER BY CASE d.kind WHEN 'cv' THEN 0 ELSE 1 END",
        (aid,),
    ).fetchall()
    return tuple(
        SentDocument(
            kind,
            number,
            frozen_at,
            tuple((str(f), int(n), int(v)) for f, n, v in json.loads(facts)),
            tuple((str(i), str(fmt)) for i, fmt in json.loads(files)),
        )
        for kind, number, frozen_at, facts, files in rows
    )


def document_drafts(conn: sqlite3.Connection) -> tuple[DocumentDraft, ...]:
    rows = conn.execute(
        "SELECT d.application_id,d.kind,j.title,c.name FROM documents d "
        "JOIN document_versions v ON v.id=d.current_version_id "
        "JOIN applications a ON a.id=d.application_id JOIN jobs j ON j.id=a.job_id "
        "LEFT JOIN companies c ON c.id=j.company_id WHERE v.status='draft' "
        "ORDER BY d.updated_at DESC,d.application_id,d.kind"
    ).fetchall()
    return tuple(DocumentDraft(*row) for row in rows)
