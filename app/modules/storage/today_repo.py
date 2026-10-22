"""Bounded read-only Today queries; callers supply the clock's UTC bounds and local date."""

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class InterviewRow:
    id: str
    application_id: str
    starts_at: str
    time_zone: str
    all_day: bool
    role: str
    company: str | None


@dataclass(frozen=True)
class JobRow:
    id: str
    role: str
    company: str | None


@dataclass(frozen=True)
class TodayRows:
    interviews: tuple[InterviewRow, ...]
    unseen: int
    strong: tuple[JobRow, ...]
    drafts: int
    statuses: tuple[tuple[str, int], ...]


def read(conn: sqlite3.Connection, *, now: str, until: str, today: str) -> TodayRows:
    interviews = conn.execute(
        "SELECT e.id, e.application_id, e.starts_at, e.time_zone, e.all_day, j.title, c.name "
        "FROM events e JOIN applications a ON a.id=e.application_id JOIN jobs j ON j.id=a.job_id "
        "LEFT JOIN companies c ON c.id=j.company_id WHERE e.status='scheduled' "
        "AND e.type='interview' AND e.starts_at>=? AND e.starts_at<? ORDER BY e.starts_at,e.id",
        (now, until),
    ).fetchall()
    unseen = int(
        conn.execute(
            "SELECT count(*) FROM jobs j WHERE j.merged_into_job_id IS NULL "
            "AND (j.triage_state='new' OR (j.triage_state='snoozed' AND j.snooze_until<=?))",
            (today,),
        ).fetchone()[0]
    )
    strong = conn.execute(
        "SELECT j.id,j.title,c.name FROM jobs j LEFT JOIN companies c ON c.id=j.company_id "
        "WHERE j.merged_into_job_id IS NULL AND (j.triage_state='new' OR "
        "(j.triage_state='snoozed' AND j.snooze_until<=?)) "
        "AND j.fit_band='strong' ORDER BY j.found_at DESC,j.id LIMIT 3",
        (today,),
    ).fetchall()
    drafts = int(conn.execute("SELECT count(*) FROM facts WHERE status='draft'").fetchone()[0])
    statuses = conn.execute(
        "SELECT s.label,count(a.id) FROM application_statuses s JOIN applications a "
        "ON a.status_key=s.key GROUP BY s.key ORDER BY CASE s.phase WHEN 'preparing' "
        "THEN 0 WHEN 'active' THEN 1 ELSE 2 END,s.position,s.key"
    ).fetchall()
    return TodayRows(
        tuple(
            InterviewRow(i, a, s, z, bool(day), role, company)
            for i, a, s, z, day, role, company in interviews
        ),
        unseen,
        tuple(JobRow(*r) for r in strong),
        drafts,
        tuple((str(label), int(count)) for label, count in statuses),
    )
