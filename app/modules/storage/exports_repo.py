"""Export and submission rows. Callers own the transaction; no filesystem access here."""

import sqlite3
from dataclasses import dataclass

from app.modules.storage import documents_repo


@dataclass(frozen=True)
class StoredExport:
    id: str
    document_version_id: str
    format: str
    stored_name: str
    sha256: str
    size_bytes: int
    pages: int | None
    kind: str
    number: int


def listing(conn: sqlite3.Connection, aid: str) -> tuple[StoredExport, ...]:
    rows = conn.execute(
        "SELECT e.id,e.document_version_id,e.format,e.stored_name,e.sha256,e.size_bytes,"
        "e.pages,d.kind,v.number FROM document_exports e "
        "JOIN document_versions v ON v.id=e.document_version_id "
        "JOIN documents d ON d.id=v.document_id WHERE d.application_id=? "
        "ORDER BY d.kind,e.format,v.number",
        (aid,),
    ).fetchall()
    return tuple(StoredExport(*row) for row in rows)


def insert(conn: sqlite3.Connection, row: StoredExport, now: str) -> None:
    conn.execute(
        "INSERT INTO document_exports "
        "(id,document_version_id,format,stored_name,sha256,size_bytes,pages,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            row.id,
            row.document_version_id,
            row.format,
            row.stored_name,
            row.sha256,
            row.size_bytes,
            row.pages,
            now,
        ),
    )


def freeze(conn: sqlite3.Connection, aid: str, now: str) -> None:
    """Prefer the latest exported approved version, then latest approved, for each kind."""
    selected: list[str | None] = []
    for kind in ("cv", "cover_letter"):
        document = documents_repo.document_for_application(conn, aid, kind)
        chosen = None
        if document:
            row = conn.execute(
                "SELECT v.id FROM document_versions v WHERE v.document_id=? "
                "AND v.status IN ('approved','sent') ORDER BY "
                "EXISTS (SELECT 1 FROM document_exports e WHERE e.document_version_id=v.id) "
                "DESC, v.number DESC LIMIT 1",
                (document.id,),
            ).fetchone()
            if row:
                chosen = str(row[0])
                version = documents_repo.get_version(conn, document.id, chosen)
                if version and version.status == "approved":
                    documents_repo.set_version_status(conn, version, "sent", now=now)
        selected.append(chosen)
    conn.execute(
        "INSERT INTO application_submissions "
        "(application_id,frozen_at,cv_version_id,letter_version_id,created_at) VALUES (?,?,?,?,?)",
        (aid, now, *selected, now),
    )


def posting(conn: sqlite3.Connection, aid: str) -> str | None:
    row = conn.execute(
        "SELECT s.url FROM job_sources s JOIN applications a ON a.job_id=s.job_id "
        "WHERE a.id=? AND (s.url LIKE 'https://%' OR s.url LIKE 'http://%') "
        "ORDER BY s.created_at,s.id LIMIT 1",
        (aid,),
    ).fetchone()
    return str(row[0]) if row else None
