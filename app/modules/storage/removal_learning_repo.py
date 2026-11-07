"""Removal evidence and rules, read and written within the caller's transaction."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import date

from app.domain.errors import VersionConflict
from app.domain.ids import is_safe_id
from app.domain.removal_learning import Condition, JobSignals, salary_condition
from app.domain.removal_reasons import PREFERENCE_FIELD_LABELS


@dataclass(frozen=True)
class Rule:
    id: str
    condition: Condition
    mode: str
    evidence_job_ids: tuple[str, ...]
    active: bool
    version: int


@dataclass(frozen=True)
class RemovalNote:
    job_id: str
    removal_version: int
    text: str


def removal_notes(conn: sqlite3.Connection) -> tuple[RemovalNote, ...]:
    rows = conn.execute(
        "SELECT r.job_id,r.job_version,r.text FROM job_removals r JOIN jobs j ON j.id=r.job_id "
        "WHERE j.triage_state='removed' AND j.merged_into_job_id IS NULL AND r.event='remove' "
        "AND r.reason IN ('other','not_interested') AND trim(r.text)<>'' "
        "AND r.job_version=(SELECT max(old.job_version) FROM job_removals old "
        "WHERE old.job_id=j.id) "
        "ORDER BY r.removed_at DESC,r.job_id LIMIT 100"
    )
    return tuple(
        RemovalNote(id, version, text[:500]) for id, version, text in rows if is_safe_id(id)
    )


def signals(
    conn: sqlite3.Connection, today: date, *, job_id: str | None = None
) -> tuple[JobSignals, ...]:
    rows = conn.execute(
        "WITH protected AS ("
        "SELECT m.id, coalesce(m.merged_into_job_id,m.id) AS root FROM jobs m "
        "WHERE m.triage_state='to_apply' OR EXISTS "
        "(SELECT 1 FROM applications a WHERE a.job_id=m.id)"
        ") SELECT j.id,j.company_id,j.seniority,j.industry,j.work_mode,j.locations,"
        "j.salary_max,j.salary_currency,j.salary_period,"
        "(j.id IN (SELECT id FROM protected) OR j.id IN (SELECT root FROM protected)),"
        "(j.merged_into_job_id IS NULL AND "
        "(j.triage_state IN ('new','seen') OR (j.triage_state='snoozed' AND j.snooze_until<=?))),"
        "r.reason,r.job_version FROM jobs j LEFT JOIN job_removals r ON "
        "j.triage_state='removed' AND j.merged_into_job_id IS NULL AND r.job_id=j.id "
        "AND r.job_version=(SELECT max(old.job_version) FROM job_removals old "
        "WHERE old.job_id=j.id) "
        "WHERE (j.merged_into_job_id IS NULL OR j.id IN (SELECT id FROM protected)) "
        "AND (? IS NULL OR j.id=?)",
        (today.isoformat(), job_id, job_id),
    )
    found = []
    for row in rows:
        (
            id,
            company,
            seniority,
            industry,
            mode,
            locations,
            maximum,
            currency,
            period,
            protected,
            in_queue,
            reason,
            removal_version,
        ) = row
        conditions = [
            Condition(field, value)
            for field, value in (
                ("company", company),
                ("seniority", seniority),
                ("industry", industry),
                ("work_mode", mode),
            )
            if value
        ]
        conditions.extend(
            Condition("location", location)
            for location in json.loads(locations)
            if isinstance(location, str) and location
        )
        if (salary := salary_condition(maximum, currency, period)) is not None:
            conditions.append(salary)
        found.append(
            JobSignals(
                id, tuple(conditions), reason, bool(protected), bool(in_queue), removal_version
            )
        )
    return tuple(found)


def rules(conn: sqlite3.Connection, *, active_only: bool = False) -> tuple[Rule, ...]:
    result = []
    for id, field, condition, mode, evidence, active, version in conn.execute(
        "SELECT id,field,condition,mode,evidence_job_ids,active,version FROM removal_rules "
        "WHERE (?=0 OR active=1) ORDER BY created_at,id",
        (active_only,),
    ):
        value = json.loads(condition).get("value")
        if is_safe_id(id) and field in PREFERENCE_FIELD_LABELS and isinstance(value, str) and value:
            ids = tuple(item for item in json.loads(evidence) if isinstance(item, str))
            result.append(Rule(id, Condition(field, value), mode, ids, bool(active), version))
    return tuple(result)


def insert_rule(
    conn: sqlite3.Connection,
    id: str,
    condition: Condition,
    mode: str,
    evidence: tuple[str, ...],
    now: str,
    *,
    origin: str = "pattern",
) -> None:
    conn.execute(
        "INSERT INTO removal_rules (id,field,condition,mode,evidence_job_ids,origin,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            id,
            condition.field,
            json.dumps({"value": condition.value}),
            mode,
            json.dumps(evidence),
            origin,
            now,
            now,
        ),
    )


def update_rule(
    conn: sqlite3.Connection, id: str, version: int, *, mode: str, active: bool, now: str
) -> None:
    updated = conn.execute(
        "UPDATE removal_rules SET mode=?,active=?,updated_at=?,version=version+1 "
        "WHERE id=? AND version=?",
        (mode, active, now, id, version),
    )
    if updated.rowcount != 1:
        raise VersionConflict()


def delete_rule(conn: sqlite3.Connection, id: str, version: int) -> None:
    if (
        conn.execute("DELETE FROM removal_rules WHERE id=? AND version=?", (id, version)).rowcount
        != 1
    ):
        raise VersionConflict()


def dismissals(conn: sqlite3.Connection) -> dict[str, int]:
    return dict(conn.execute("SELECT signature,evidence_count FROM removal_suggestion_dismissals"))


def dismiss(conn: sqlite3.Connection, signature: str, count: int, now: str) -> None:
    existing = conn.execute(
        "SELECT version FROM removal_suggestion_dismissals WHERE signature=?", (signature,)
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO removal_suggestion_dismissals (signature,evidence_count,created_at,"
            "updated_at) VALUES (?,?,?,?)",
            (signature, count, now, now),
        )
    elif (
        conn.execute(
            "UPDATE removal_suggestion_dismissals SET evidence_count=?,updated_at=?,"
            "version=version+1 WHERE signature=? AND version=?",
            (count, now, signature, existing[0]),
        ).rowcount
        != 1
    ):
        raise VersionConflict()


def company_name(conn: sqlite3.Connection, id: str) -> str | None:
    row = conn.execute("SELECT name FROM companies WHERE id=?", (id,)).fetchone()
    return row[0] if row else None


def evidence_titles(conn: sqlite3.Connection, ids: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    return tuple(
        conn.execute(
            "SELECT id,title FROM jobs WHERE id IN (SELECT value FROM json_each(?)) ORDER BY id",
            (json.dumps(ids),),
        )
    )


@dataclass(frozen=True)
class Theme:
    condition: Condition
    evidence: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class ThemeRun:
    themes: tuple[Theme, ...]
    provider: str
    model: str
    prompt_id: str
    prompt_version: str


def store_theme_run(conn: sqlite3.Connection, task_id: str, result: ThemeRun, now: str) -> str:
    payload = [
        {"field": item.condition.field, "value": item.condition.value, "evidence": item.evidence}
        for item in result.themes
    ]
    conn.execute(
        "INSERT INTO removal_theme_runs (task_id,themes,provider,model,prompt_id,prompt_version,"
        "created_at) VALUES (?,?,?,?,?,?,?)",
        (
            task_id,
            json.dumps(payload),
            result.provider,
            result.model,
            result.prompt_id,
            result.prompt_version,
            now,
        ),
    )
    return task_id


def latest_theme_run(conn: sqlite3.Connection) -> ThemeRun | None:
    row = conn.execute(
        "SELECT themes,provider,model,prompt_id,prompt_version FROM removal_theme_runs "
        "ORDER BY created_at DESC,rowid DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    themes = []
    for item in json.loads(row[0]):
        if not isinstance(item, dict):
            continue
        field, value, evidence = item.get("field"), item.get("value"), item.get("evidence")
        if (
            isinstance(field, str)
            and field in PREFERENCE_FIELD_LABELS
            and isinstance(value, str)
            and value
            and isinstance(evidence, list)
        ):
            checked = tuple(
                (pair[0], pair[1])
                for pair in evidence
                if isinstance(pair, list)
                and len(pair) == 2
                and is_safe_id(pair[0])
                and type(pair[1]) is int
                and pair[1] >= 1
            )
            if len(checked) == len(evidence):
                themes.append(Theme(Condition(field, value), checked))
    return ThemeRun(tuple(themes), row[1], row[2], row[3], row[4])


def grouping_task(conn: sqlite3.Connection, task_type: str) -> tuple[str, str | None] | None:
    row = conn.execute(
        "SELECT status,error FROM tasks WHERE type=? "
        "ORDER BY (status IN ('queued','running')) DESC,updated_at DESC,rowid DESC LIMIT 1",
        (task_type,),
    ).fetchone()
    return (row[0], row[1]) if row is not None else None
