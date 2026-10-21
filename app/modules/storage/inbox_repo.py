"""Bounded inbox reads. SQL fragments and column names are code-owned; values are bound."""

import sqlite3
from datetime import date

from app.domain.fts import fts_query
from app.domain.inbox import InboxFilters, InboxGroup, InboxPage

# A To apply job whose application has moved on from Preparing is on the Board, not here.
# A job with no application (marked before applications existed) still counts.
_MOVED_ON = (
    "EXISTS (SELECT 1 FROM applications a JOIN application_statuses s ON s.key=a.status_key "
    "WHERE a.job_id=j.id AND s.phase<>'preparing')"
)


def _where(filters: InboxFilters, today: date) -> tuple[str, list[object]]:
    clauses = ["j.merged_into_job_id IS NULL"]
    params: list[object] = []
    if filters.view == "queue":
        clauses.append(
            "(j.triage_state IN ('new','seen') OR (j.triage_state='snoozed' AND j.snooze_until<=?))"
        )
        params.append(today.isoformat())
    elif filters.view == "snoozed":
        clauses.append("j.triage_state='snoozed' AND j.snooze_until>?")
        params.append(today.isoformat())
    elif filters.view == "to_apply":
        clauses.append(f"j.triage_state='to_apply' AND NOT {_MOVED_ON}")
    else:
        clauses.append("j.triage_state=?")
        params.append(filters.view)
    for column, value in (
        ("fit_band", filters.band),
        ("work_mode", filters.work_mode),
        ("industry", filters.industry),
        ("seniority", filters.seniority),
        ("company_id", filters.company),
        ("deal_breaker", filters.flagged),
    ):
        if value is not None:
            clauses.append(f"j.{column}=?")
            params.append(value)
    match = fts_query(filters.search)
    if match is not None:
        clauses.append("j.id IN (SELECT job_id FROM jobs_fts WHERE jobs_fts MATCH ?)")
        params.append(match)
    return " AND ".join(clauses), params


def page_statement(filters: InboxFilters, today: date, page: int) -> tuple[str, list[object]]:
    where, params = _where(filters, today)
    order = {
        "newest": "j.found_at DESC",
        "posting_age": "j.posted_date IS NULL,j.posted_date DESC,j.found_at DESC",
        "band": (
            "CASE j.fit_band WHEN 'strong' THEN 0 WHEN 'possible' THEN 1 "
            "WHEN 'stretch' THEN 2 ELSE 3 END,j.found_at DESC"
        ),
    }[filters.sort]
    # Every queue sort keeps New (including due snoozes) before Seen.
    if filters.view == "queue":
        order = "CASE WHEN j.triage_state='seen' THEN 1 ELSE 0 END," + order
    statement = (
        "SELECT j.id,j.title,j.company_id,c.name AS company,j.industry,"
        "j.work_mode,j.seniority,j.fit_band,j.deal_breaker,j.posted_date,j.found_at,"
        "j.snooze_until,j.version,CASE WHEN j.triage_state='snoozed' AND "
        "j.snooze_until<=? THEN 'new' ELSE j.triage_state END AS state "
        "FROM jobs j LEFT JOIN companies c ON c.id=j.company_id WHERE "
        + where
        + " ORDER BY "
        + order
        + ",j.id LIMIT 51 OFFSET ?"
    )
    return statement, [today.isoformat(), *params, (page - 1) * 50]


def page(conn: sqlite3.Connection, filters: InboxFilters, today: date, number: int) -> InboxPage:
    where, params = _where(filters, today)
    total = int(conn.execute("SELECT count(*) FROM jobs j WHERE " + where, params).fetchone()[0])  # noqa: S608
    sql, params = page_statement(filters, today, number)
    cursor = conn.execute(sql, params)
    columns = [c[0] for c in cursor.description]
    rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    return InboxPage(tuple(rows[:50]), total, number, rows[50] if len(rows) > 50 else None)


def groups(
    conn: sqlite3.Connection, filters: InboxFilters, today: date, by: str
) -> tuple[InboxGroup, ...]:
    column = {
        "company": "j.company_id",
        "industry": "j.industry",
        "band": "j.fit_band",
        "work_mode": "j.work_mode",
        "seniority": "j.seniority",
    }[by]
    label = "c.name" if by == "company" else column
    where, params = _where(filters, today)
    sql = (
        f"SELECT {column},{label},count(*) FROM jobs j "  # noqa: S608
        "LEFT JOIN companies c ON c.id=j.company_id "
        f"WHERE {where} GROUP BY {column} "
        f"ORDER BY {label} IS NULL,{label} COLLATE NOCASE,{column}"
    )
    return tuple(
        InboxGroup(row[0], row[1] or "Not stated", int(row[2])) for row in conn.execute(sql, params)
    )


def explain(conn: sqlite3.Connection, filters: InboxFilters, today: date) -> tuple[str, ...]:
    sql, params = page_statement(filters, today, 1)
    return tuple(str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params))
