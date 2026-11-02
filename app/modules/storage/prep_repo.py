"""Interview-prep persistence. Callers own the short transaction."""

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from app.domain.errors import VersionConflict
from app.domain.fit import Requirement
from app.domain.question_bank import BankQuestion, duplicate_question


@dataclass(frozen=True)
class PrepSource:
    application_id: str
    version: int
    title: str
    company_id: str | None
    company: str
    industry: str
    summary_id: str | None
    summary: str
    requirements: tuple[Requirement, ...]
    cv_version_id: str | None
    cv_text: str
    cv_source: str
    seniority: str


@dataclass(frozen=True)
class PrepItem:
    id: str
    question_id: str
    ready: bool
    version: int
    practiced: bool


def source(conn: sqlite3.Connection, aid: str) -> PrepSource | None:
    row = conn.execute(
        "SELECT a.version,j.id,coalesce(j.title,''),j.company_id,coalesce(c.name,''),"
        "coalesce(c.industry,''),coalesce(j.seniority,'') FROM applications a "
        "JOIN jobs j ON j.id=a.job_id "
        "LEFT JOIN companies c ON c.id=j.company_id WHERE a.id=?",
        (aid,),
    ).fetchone()
    if row is None:
        return None
    summary = conn.execute(
        "SELECT id,summary FROM job_summaries WHERE job_id=? ORDER BY created_at "
        "DESC,rowid DESC LIMIT 1",
        (row[1],),
    ).fetchone()
    requirements = tuple(
        Requirement(*r)
        for r in conn.execute(
            "SELECT id,kind,text FROM job_requirements WHERE summary_id=? ORDER BY position",
            (summary[0] if summary else None,),
        )
    )
    sent = conn.execute(
        "SELECT cv_version_id FROM application_submissions WHERE application_id=?", (aid,)
    ).fetchone()
    cv_id, cv_source = (sent[0] if sent else None), "sent"
    if cv_id is None:
        approved = conn.execute(
            "SELECT v.id FROM documents d JOIN document_versions v ON v.document_id=d.id "
            "WHERE d.application_id=? AND d.kind='cv' AND v.status IN ('approved','sent') "
            "ORDER BY v.number DESC LIMIT 1",
            (aid,),
        ).fetchone()
        cv_id, cv_source = (approved[0] if approved else None), "approved"
    # Current never-use choices apply even to a formerly sent CV. Headers/contact details
    # are code-built and are not document units, so no personal-details setting is read.
    cv_text = (
        "\n".join(
            r[0]
            for r in conn.execute(
                "SELECT u.text FROM document_units u WHERE u.document_version_id=? AND u.removed=0 "
                "AND u.kind='content' AND NOT EXISTS(SELECT 1 FROM "
                "document_unit_facts uf JOIN facts f "
                "ON f.id=uf.fact_id WHERE uf.document_unit_id=u.id AND "
                "f.privacy='never_use') ORDER BY u.position",
                (cv_id,),
            )
        )
        if cv_id
        else ""
    )
    return PrepSource(
        aid,
        row[0],
        row[2],
        row[3],
        row[4],
        row[5],
        summary[0] if summary else None,
        summary[1] if summary else "",
        requirements,
        cv_id,
        cv_text,
        cv_source if cv_id else "none",
        row[6],
    )


def questions(
    conn: sqlite3.Connection, *, query: str = "", question_type: str = "", question_id: str = ""
) -> tuple[BankQuestion, ...]:
    tags: dict[str, list[str]] = defaultdict(list)
    for qid, text in conn.execute(
        "SELECT question_id,text FROM bank_question_requirements WHERE (?='' OR question_id=?) "
        "ORDER BY question_id,text",
        (question_id, question_id),
    ):
        tags[qid].append(text)
    phrase = '"' + query.replace('"', '""') + '"'
    rows = conn.execute(
        "SELECT q.id,q.text,q.type,q.company_id,q.role_family,q.seniority,q.asked_count,q.origin,"
        "q.source_url,q.found_at,q.source_date,q.provider,q.model,"
        "q.prompt_id,q.prompt_version,q.version "
        "FROM bank_questions q WHERE (?='' OR q.type=?) AND (?='' OR q.id=?) AND (?='' OR q.id IN "
        "(SELECT id FROM bank_questions_fts WHERE bank_questions_fts MATCH ?)) "
        "ORDER BY q.created_at DESC, q.id",
        (question_type, question_type, question_id, question_id, query, phrase),
    )
    return tuple(BankQuestion(r[0], r[1], r[2], tuple(tags[r[0]]), *r[3:]) for r in rows)


def items(conn: sqlite3.Connection, aid: str) -> tuple[PrepItem, ...]:
    return tuple(
        PrepItem(r[0], r[1], bool(r[2]), r[3], bool(r[4]))
        for r in conn.execute(
            "SELECT p.id,p.question_id,p.ready,p.version,EXISTS(SELECT 1 FROM practice_attempts a "
            "WHERE a.prep_item_id=p.id) FROM prep_items p WHERE p.application_id=? ORDER BY p.id",
            (aid,),
        )
    )


def generated_here(conn: sqlite3.Connection, aid: str) -> frozenset[str]:
    return frozenset(
        r[0]
        for r in conn.execute(
            "SELECT question_id FROM bank_question_sightings "
            "WHERE application_id=? AND origin='generated'",
            (aid,),
        )
    )


def insert_question(
    conn: sqlite3.Connection,
    candidate: BankQuestion,
    *,
    sighting_id: str,
    now: str,
    application_id: str | None = None,
    task_id: str | None = None,
    event_id: str | None = None,
) -> BankQuestion:
    existing = duplicate_question(candidate, questions(conn))
    if existing is None:
        conn.execute(
            "INSERT INTO "
            "bank_questions(id,text,type,origin,company_id,role_family,seniority,asked_count,"
            "source_url,found_at,source_date,provider,model,prompt_id,prompt_version,created_at,updated_at)"
            " "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                candidate.id,
                candidate.text,
                candidate.type,
                candidate.origin,
                candidate.company_id,
                candidate.role_family,
                candidate.seniority,
                int(candidate.origin == "asked"),
                candidate.source_url,
                candidate.found_at,
                candidate.source_date,
                candidate.provider,
                candidate.model,
                candidate.prompt_id,
                candidate.prompt_version,
                now,
                now,
            ),
        )
        kept = candidate
    else:
        kept = existing
        updated = conn.execute(
            "UPDATE bank_questions SET "
            "asked_count=asked_count+?,version=version+1,updated_at=? WHERE id=? AND version=?",
            (int(candidate.origin == "asked"), now, kept.id, kept.version),
        )
        if updated.rowcount != 1:
            raise VersionConflict()
    old_tags = set(kept.requirements) if existing else set()
    for text in dict.fromkeys(candidate.requirements):
        if text not in old_tags:
            conn.execute(
                "INSERT INTO "
                "bank_question_requirements(question_id,text,created_at,updated_at)"
                " VALUES(?,?,?,?)",
                (kept.id, text, now, now),
            )
    conn.execute(
        "INSERT INTO "
        "bank_question_sightings(id,question_id,application_id,event_id,origin,source_url,"
        "source_date,found_at,provider,model,prompt_id,prompt_version,task_id,created_at,"
        "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            sighting_id,
            kept.id,
            application_id,
            event_id,
            candidate.origin,
            candidate.source_url,
            candidate.source_date,
            candidate.found_at or now,
            candidate.provider,
            candidate.model,
            candidate.prompt_id,
            candidate.prompt_version,
            task_id,
            now,
            now,
        ),
    )
    return kept


def add_item(
    conn: sqlite3.Connection, aid: str, question_id: str, *, item_id: str, now: str
) -> None:
    if (
        conn.execute(
            "SELECT 1 FROM prep_items WHERE application_id=? AND question_id=?", (aid, question_id)
        ).fetchone()
        is None
    ):
        conn.execute(
            "INSERT INTO "
            "prep_items(id,application_id,question_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?)",
            (item_id, aid, question_id, now, now),
        )


def remove_unpracticed_generated(conn: sqlite3.Connection, aid: str) -> None:
    conn.execute(
        "DELETE FROM prep_items WHERE application_id=? AND ready=0 AND NOT EXISTS "
        "(SELECT 1 FROM practice_attempts a WHERE a.prep_item_id=prep_items.id) "
        "AND question_id IN (SELECT id FROM bank_questions WHERE origin='generated')",
        (aid,),
    )


def changed(conn: sqlite3.Connection, aid: str, now: str, *, expected_version: int) -> None:
    updated = conn.execute(
        "UPDATE applications SET version=version+1,updated_at=? WHERE id=? AND version=?",
        (now, aid, expected_version),
    )
    if updated.rowcount != 1:
        raise VersionConflict()


def latest_task(
    conn: sqlite3.Connection, aid: str, task_type: str
) -> tuple[str, str, str | None] | None:
    row = conn.execute(
        "SELECT id,status,error FROM tasks WHERE type=? AND dedupe_key=? ORDER BY"
        " created_at DESC,rowid DESC LIMIT 1",
        (task_type, task_type + ":" + aid),
    ).fetchone()
    return (row[0], row[1], row[2]) if row else None


def mark_ready(
    conn: sqlite3.Connection,
    item_id: str,
    application_id: str,
    *,
    ready: bool,
    now: str,
    expected_version: int,
) -> None:
    updated = conn.execute(
        "UPDATE prep_items SET ready=?,updated_at=?,version=version+1 "
        "WHERE id=? AND application_id=? AND version=?",
        (int(ready), now, item_id, application_id, expected_version),
    )
    if updated.rowcount != 1:
        raise VersionConflict()


# --- prep state: what the board, the drawer and Today show (M8.2 part D) ------------------------

# Correlated columns over an ``applications`` row aliased ``a``; the one ``?`` is the instant
# "now". The board's card query embeds the same text, so it stays a single statement.
SUMMARY_COLUMNS = (
    "(SELECT count(*) FROM prep_items pi WHERE pi.application_id = a.id), "
    "(SELECT count(*) FROM prep_items pp WHERE pp.application_id = a.id "
    "AND EXISTS (SELECT 1 FROM practice_attempts pa WHERE pa.prep_item_id = pp.id)), "
    "EXISTS (SELECT 1 FROM events pe WHERE pe.application_id = a.id AND pe.status = 'scheduled' "
    "AND pe.type = 'interview' AND julianday(pe.ends_at) >= julianday(?)), "
    "EXISTS (SELECT 1 FROM application_statuses ps WHERE ps.key = a.status_key "
    "AND ps.phase = 'closed')"
)


@dataclass(frozen=True)
class PrepSummary:
    """Prep items, the items with at least one practice attempt, and whether a scheduled
    interview is not yet over; ``closed`` is true when the application's status is in the closed
    phase."""

    item_count: int
    practiced_count: int
    has_future_interview: bool
    closed: bool = False


def states(
    conn: sqlite3.Connection, application_ids: list[str], now: str
) -> dict[str, PrepSummary]:
    """A summary for each given id (zeros for an unknown one), in one SELECT; none for an empty
    list. ``now`` is an ISO UTC instant."""
    if not application_ids:
        return {}
    rows = conn.execute(
        f"SELECT a.id, {SUMMARY_COLUMNS} FROM applications a "  # noqa: S608 - fixed text
        "WHERE a.id IN (SELECT value FROM json_each(?))",
        (now, json.dumps(list(application_ids))),
    ).fetchall()
    found = {str(r[0]): PrepSummary(int(r[1]), int(r[2]), bool(r[3]), bool(r[4])) for r in rows}
    return {aid: found.get(aid, PrepSummary(0, 0, False)) for aid in application_ids}
