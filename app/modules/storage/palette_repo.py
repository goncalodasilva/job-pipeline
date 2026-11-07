"""Reads for the command palette (M9.5 part A): jobs, applications and facts through the search
indexes, company names and the titles of recent items. Read only; literal SQL only.

``match`` is an FTS5 query of quoted tokens (``app.domain.fts.fts_query``), never raw user text.
"""

import sqlite3

from app.domain.palette import PaletteItem, shortened

_JOBS = (
    "SELECT j.id, j.title, c.name FROM jobs AS j LEFT JOIN companies AS c ON c.id = j.company_id "
    "WHERE j.merged_into_job_id IS NULL "
    "AND j.id IN (SELECT job_id FROM jobs_fts WHERE jobs_fts MATCH ?) "
    "ORDER BY j.found_at DESC, j.id LIMIT ?"
)
# An application is found by its job's title and company only, not by the job's text.
_APPLICATIONS = (
    "SELECT a.id, j.title, c.name FROM applications AS a JOIN jobs AS j ON j.id = a.job_id "
    "LEFT JOIN companies AS c ON c.id = j.company_id "
    "WHERE j.id IN (SELECT job_id FROM jobs_fts WHERE jobs_fts MATCH ?) "
    "ORDER BY a.updated_at DESC, a.id LIMIT ?"
)
# A fact's title is its text, or for a story its first part that has words.
_FACTS = (
    "SELECT f.id, COALESCE(NULLIF(trim(v.text), ''), NULLIF(trim(v.situation), ''), "
    "NULLIF(trim(v.action), ''), NULLIF(trim(v.result), ''), '') "
    "FROM facts AS f JOIN fact_versions AS v "
    "ON v.id = f.current_version_id "
    "WHERE f.status <> 'discarded' "
    "AND f.number IN (SELECT rowid FROM facts_fts WHERE facts_fts MATCH ?) "
    "ORDER BY f.updated_at DESC, f.number DESC LIMIT ?"
)
_COMPANIES = "SELECT id, name FROM companies ORDER BY name COLLATE NOCASE, id"
_JOB = (
    "SELECT j.title, c.name FROM jobs AS j LEFT JOIN companies AS c ON c.id = j.company_id "
    "WHERE j.id = ?"
)
_COMPANY = "SELECT name FROM companies WHERE id = ?"
_APPLICATION = (
    "SELECT j.title, c.name FROM applications AS a JOIN jobs AS j ON j.id = a.job_id "
    "LEFT JOIN companies AS c ON c.id = j.company_id WHERE a.id = ?"
)
_FACT = (
    "SELECT COALESCE(NULLIF(trim(v.text), ''), NULLIF(trim(v.situation), ''), "
    "NULLIF(trim(v.action), ''), NULLIF(trim(v.result), ''), '') "
    "FROM facts AS f JOIN fact_versions AS v "
    "ON v.id = f.current_version_id WHERE f.id = ?"
)


def jobs(conn: sqlite3.Connection, match: str, limit: int) -> list[PaletteItem]:
    rows = conn.execute(_JOBS, (match, limit)).fetchall()
    return [
        PaletteItem("job", str(i), str(title), str(company or "")) for i, title, company in rows
    ]


def applications(conn: sqlite3.Connection, match: str, limit: int) -> list[PaletteItem]:
    narrowed = "{title company} : (" + match + ")"
    rows = conn.execute(_APPLICATIONS, (narrowed, limit)).fetchall()
    return [
        PaletteItem("application", str(i), str(title), str(company or ""))
        for i, title, company in rows
    ]


def facts(conn: sqlite3.Connection, match: str, limit: int) -> list[PaletteItem]:
    rows = conn.execute(_FACTS, (match, limit)).fetchall()
    return [PaletteItem("fact", str(i), shortened(str(text))) for i, text in rows]


def companies(conn: sqlite3.Connection) -> list[PaletteItem]:
    """Every company, by name; the caller picks the ones the typed words match."""
    return [PaletteItem("company", str(i), str(name)) for i, name in conn.execute(_COMPANIES)]


def recent_item(conn: sqlite3.Connection, kind: str, item_id: str) -> PaletteItem | None:
    """The item of a recent page, or ``None`` when no such row exists."""
    if kind == "company":
        row = conn.execute(_COMPANY, (item_id,)).fetchone()
        return None if row is None else PaletteItem(kind, item_id, str(row[0]))
    if kind == "fact":
        row = conn.execute(_FACT, (item_id,)).fetchone()
        return None if row is None else PaletteItem(kind, item_id, shortened(str(row[0])))
    row = conn.execute(_JOB if kind == "job" else _APPLICATION, (item_id,)).fetchone()
    return None if row is None else PaletteItem(kind, item_id, str(row[0]), str(row[1] or ""))
