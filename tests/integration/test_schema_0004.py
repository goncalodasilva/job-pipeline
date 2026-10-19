"""Migration 0004, jobs (M4.2; groundwork for AC-M4.2 to AC-M4.9; spec 2.7; ADR-027, ADR-064,
ADR-098 items 3, 5, 6, 8).

0004 creates every table M4 needs, because a committed migration is never edited:
``companies``, ``jobs`` (filter columns, triage state with the previous state kept for undo,
snooze date, removal reason, user-corrected field names, merge link), ``job_sources``,
append-only ``job_summaries`` and ``job_assessments`` (results are new rows, never overwrites,
spec 5 M2 rule 3), ``job_requirements``, ``controlled_values`` (seeded in the migration),
``field_value_queue``, ``job_pair_dismissals``, ``saved_views`` and the FTS5 table ``jobs_fts``
(title, company name, latest summary and snapshot, kept in sync by triggers; ``job_id`` is
stored for joins and is not searchable, as ``facts_fts.fact_id`` in 0003).

Tests check behavior through SQL and the schema through PRAGMAs, never through index or
trigger names. The stale-version test goes through ``app.modules.storage.jobs_repo``. Everyone
and everything here is fictional.
"""

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations, load_migrations
from tests.fakes.clock import FrozenClock

pytestmark = pytest.mark.integration

START = datetime(2026, 10, 19, 9, 15, 0, tzinfo=UTC)
STAMP = "2026-10-19T09:15:00+00:00"
LATER = "2026-10-19T10:00:00+00:00"
LATEST = "2026-10-19T11:00:00+00:00"

MIGRATION_HASHES = Path(__file__).parents[1] / "fixtures" / "migration_hashes.json"

TRIAGE_STATES = ("new", "seen", "snoozed", "removed", "to_apply")
FIT_BANDS = ("strong", "possible", "stretch")
REMOVAL_REASONS = (
    "too_junior",
    "too_senior",
    "location",
    "salary",
    "industry",
    "company",
    "not_interested",
    "other",
)
SOURCE_KINDS = ("fetched", "pasted", "link")
REQUIREMENT_KINDS = ("must_have", "key", "nice")
EMPLOYMENT_TYPES = {"full_time", "part_time", "contract", "temporary", "freelance", "internship"}
WORK_MODES = {"remote", "hybrid", "on_site"}  # app.domain.settings.WorkMode

MUTABLE_TABLES = ("companies", "jobs", "controlled_values", "field_value_queue", "saved_views")

EXPECTED_COLUMNS: dict[str, set[str]] = {
    "companies": {
        "id",
        "name",
        "name_key",
        "website",
        "industry",
        "size",
        "locations",
        "notes",
        "created_at",
        "updated_at",
        "version",
    },
    "jobs": {
        "id",
        "company_id",
        "title",
        "snapshot_text",
        "captured_at",
        "industry",
        "department",
        "work_mode",
        "locations",
        "employment_type",
        "seniority",
        "experience_min",
        "experience_max",
        "experience_text",
        "salary_min",
        "salary_max",
        "salary_currency",
        "salary_period",
        "posted_date",
        "fit_band",
        "deal_breaker",
        "triage_state",
        "previous_state",
        "snooze_until",
        "removal_reason",
        "removal_text",
        "notes",
        "user_corrected",
        "merged_into_job_id",
        "found_at",
        "created_at",
        "updated_at",
        "version",
    },
    "job_sources": {"id", "job_id", "url", "canonical_url", "kind", "found_at", "created_at"},
    "job_summaries": {
        "id",
        "job_id",
        "summary",
        "fields_json",
        "provider",
        "model",
        "prompt_id",
        "prompt_version",
        "task_id",
        "created_at",
    },
    "job_assessments": {
        "id",
        "job_id",
        "summary_id",
        "result_json",
        "fit_band",
        "deal_breaker",
        "fact_refs_json",
        "provider",
        "model",
        "prompt_id",
        "prompt_version",
        "task_id",
        "created_at",
    },
    "job_requirements": {"id", "summary_id", "position", "kind", "text"},
    "controlled_values": {
        "id",
        "field",
        "value",
        "aliases",
        "created_at",
        "updated_at",
        "version",
    },
    "field_value_queue": {
        "id",
        "field",
        "value",
        "status",
        "created_at",
        "updated_at",
        "version",
    },
    "job_pair_dismissals": {"job_a_id", "job_b_id", "created_at"},
    "saved_views": {"id", "name", "filters_json", "created_at", "updated_at", "version"},
}


# --- helpers ----------------------------------------------------------------


def _migration_0004() -> Path:
    found = [m.path for m in load_migrations(MIGRATIONS_DIR) if m.version == 4]
    assert found, "migration 0004 is missing from the migrations folder"
    return found[0]


def _folder_up_to(tmp_path: Path, version: int) -> Path:
    folder = tmp_path / f"migrations-up-to-{version:04d}"
    folder.mkdir()
    for migration in load_migrations(MIGRATIONS_DIR):
        if migration.version <= version:
            shutil.copy(migration.path, folder / migration.path.name)
    return folder


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    _migration_0004()
    database = Database.open(tmp_path / "data" / "app.db")
    try:
        apply_migrations(database, MIGRATIONS_DIR, tmp_path / "backups", FrozenClock(START))
        yield database
    finally:
        database.close()


def columns(conn: sqlite3.Connection, table: str) -> dict[str, dict[str, object]]:
    return {
        row[1]: {"type": row[2].upper(), "notnull": row[3], "default": row[4]}
        for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    }


def index_columns(conn: sqlite3.Connection, table: str) -> list[list[str]]:
    """The key columns of each full (non-partial) index on ``table``."""
    found = []
    for row in conn.execute(f'PRAGMA index_list("{table}")').fetchall():
        if row[4]:
            continue
        found.append(
            [info[2] for info in conn.execute(f'PRAGMA index_info("{row[1]}")').fetchall()]
        )
    return found


def insert(conn: sqlite3.Connection, table: str, row: dict[str, object]) -> None:
    names = ", ".join(f'"{name}"' for name in row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})", tuple(row.values()))  # noqa: S608


def count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # noqa: S608


def company_row(
    company_id: str = "comp-0001", name: str = "Larkspur Orchards"
) -> dict[str, object]:
    return {
        "id": company_id,
        "name": name,
        "name_key": name.casefold(),
        "created_at": STAMP,
        "updated_at": STAMP,
    }


def job_row(job_id: str = "job-0001", **extra: object) -> dict[str, object]:
    return {
        "id": job_id,
        "title": "Greenhouse Technician",
        "snapshot_text": "Tend the tomato vines and calibrate the irrigation timers.",
        "captured_at": STAMP,
        "found_at": STAMP,
        "created_at": STAMP,
        "updated_at": STAMP,
        **extra,
    }


def task_row(task_id: str = "task-0001", target: str = "job-0001") -> dict[str, object]:
    return {
        "id": task_id,
        "type": "summarize_job",
        "status": "succeeded",
        "dedupe_key": f"summarize_job:{target}:{task_id}",
        "created_at": STAMP,
        "updated_at": STAMP,
    }


def summary_row(
    summary_id: str = "sum-0001", job_id: str = "job-0001", **extra: object
) -> dict[str, object]:
    return {
        "id": summary_id,
        "job_id": job_id,
        "summary": "Seasonal greenhouse role looking after tomato crops.",
        "fields_json": json.dumps({"title": "Greenhouse Technician", "work_mode": "onsite"}),
        "provider": "fake",
        "model": "fake-fast",
        "prompt_id": "summarize_job",
        "prompt_version": "1",
        "task_id": "task-0001",
        "created_at": STAMP,
        **extra,
    }


def assessment_row(assessment_id: str = "fit-0001", **extra: object) -> dict[str, object]:
    return {
        "id": assessment_id,
        "job_id": "job-0001",
        "summary_id": "sum-0001",
        "result_json": json.dumps({"requirements": []}),
        "fit_band": "possible",
        "deal_breaker": 0,
        "fact_refs_json": json.dumps(["F1@1"]),
        "provider": "fake",
        "model": "fake-thorough",
        "prompt_id": "assess_fit",
        "prompt_version": "1",
        "task_id": "task-0001",
        "created_at": STAMP,
        **extra,
    }


def seed(db: Database) -> None:
    """comp-0001 (Larkspur Orchards), job-0001 at that company, task-0001 and summary
    sum-0001 of job-0001."""
    with db.transaction() as conn:
        insert(conn, "companies", company_row())
        insert(conn, "jobs", job_row(company_id="comp-0001"))
        insert(conn, "tasks", task_row())
        insert(conn, "job_summaries", summary_row())


def put(db: Database, table: str, **extra: object) -> None:
    """Insert one more valid row into ``table`` (after ``seed``), with ``extra`` overriding
    columns, in one transaction."""
    rows: dict[str, Callable[[], dict[str, object]]] = {
        "companies": lambda: company_row("comp-0002", "Bramblewood Ferries"),
        "jobs": lambda: job_row("job-0002"),
        "job_sources": lambda: {
            "id": "src-0001",
            "job_id": "job-0001",
            "url": "https://jobs.example.test/larkspur/greenhouse?ref=feed",
            "canonical_url": "https://jobs.example.test/larkspur/greenhouse",
            "kind": "fetched",
            "found_at": STAMP,
            "created_at": STAMP,
        },
        "job_summaries": lambda: summary_row("sum-0002"),
        "job_assessments": lambda: assessment_row(),
        "job_requirements": lambda: {
            "id": "req-0001",
            "summary_id": "sum-0001",
            "position": 1,
            "kind": "must_have",
            "text": "Two seasons of greenhouse work",
        },
        "controlled_values": lambda: {
            "id": "cv-test-0001",
            "field": "industry",
            "value": "Fictional horticulture",
            "aliases": "[]",
            "created_at": STAMP,
            "updated_at": STAMP,
        },
        "field_value_queue": lambda: {
            "id": "fvq-0001",
            "field": "industry",
            "value": "Fictional orchard tourism",
            "created_at": STAMP,
            "updated_at": STAMP,
        },
        "saved_views": lambda: {
            "id": "view-0001",
            "name": "Remote strong fits",
            "filters_json": json.dumps({"work_mode": ["remote"], "fit_band": ["strong"]}),
            "created_at": STAMP,
            "updated_at": STAMP,
        },
    }
    with db.transaction() as conn:
        insert(conn, table, {**rows[table](), **extra})


def refused(db: Database, table: str, **extra: object) -> None:
    before = count(db.connection, table)
    with pytest.raises(sqlite3.IntegrityError):
        put(db, table, **extra)
    assert count(db.connection, table) == before, f"a refused {table} row was stored"


def search(db: Database, word: str) -> set[str]:
    """Ids of the jobs whose search entry holds ``word`` (quoted, one literal token)."""
    quoted = '"' + word.replace('"', '""') + '"'
    rows = db.connection.execute(
        "SELECT job_id FROM jobs_fts WHERE jobs_fts MATCH ?", (quoted,)
    ).fetchall()
    return {row[0] for row in rows}


def controlled(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute("SELECT field, value FROM controlled_values ORDER BY field, value")
    return [(row[0], row[1]) for row in rows.fetchall()]


# --- upgrade from 0003 ------------------------------------------------------


def _seed_0003(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO settings (key, value, created_at, updated_at) VALUES ('ui.theme', ?, ?, ?)",
        ('"dark"', STAMP, STAMP),
    )
    insert(
        conn,
        "tasks",
        {
            "id": "task-0001",
            "type": "extract_cv",
            "status": "succeeded",
            "dedupe_key": "cv_import:imp-0001",
            "created_at": STAMP,
            "updated_at": STAMP,
        },
    )
    insert(
        conn,
        "roles",
        {
            "id": "role-0001",
            "employer": "Northwind Logistics",
            "title": "Operations Analyst",
            "start_date": "2019-03",
            "end_date": "2022-11",
            "status": "confirmed",
            "created_at": STAMP,
            "updated_at": STAMP,
        },
    )
    insert(
        conn,
        "skills",
        {
            "id": "skill-0001",
            "name": "Forecasting",
            "normalized_name": "forecasting",
            "created_at": STAMP,
            "updated_at": STAMP,
        },
    )
    # facts.current_version_id is deferred: the fact first, then its version.
    insert(
        conn,
        "facts",
        {
            "id": "fact-0001",
            "number": 1,
            "type": "fact",
            "status": "confirmed",
            "privacy": "normal",
            "role_id": "role-0001",
            "current_version_id": "ver-0001",
            "created_at": STAMP,
            "updated_at": STAMP,
        },
    )
    insert(
        conn,
        "fact_versions",
        {
            "id": "ver-0001",
            "fact_id": "fact-0001",
            "version": 1,
            "text": "Cut the invoice backlog by a third",
            "source": "manual",
            "created_at": STAMP,
        },
    )
    insert(conn, "fact_skills", {"fact_id": "fact-0001", "skill_id": "skill-0001"})
    insert(
        conn,
        "capture_notes",
        {
            "id": "note-0001",
            "text": "Remember the warehouse relabelling weekend",
            "created_at": STAMP,
            "updated_at": STAMP,
        },
    )


def test_0004_upgrade_keeps_0003_rows_and_backs_up(tmp_path: Path) -> None:
    _migration_0004()
    backups = tmp_path / "backups"
    with Database.open(tmp_path / "data" / "app.db") as database:
        apply_migrations(database, _folder_up_to(tmp_path, 3), backups, FrozenClock(START))
        with database.transaction() as conn:
            _seed_0003(conn)
        tables_at_0003 = [
            row[0]
            for row in database.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%' AND name <> 'schema_migrations'"
                " AND name NOT LIKE 'facts_fts_%' ORDER BY name"
            ).fetchall()
        ]
        before = {
            table: sorted(database.connection.execute(f"SELECT * FROM {table}").fetchall())  # noqa: S608
            for table in tables_at_0003
        }
        for table in ("settings", "tasks", "roles", "facts", "fact_versions", "facts_fts"):
            assert before[table], f"seed a row in {table}"

        later = FrozenClock(datetime(2026, 10, 20, 8, 0, 0, tzinfo=UTC))
        result = apply_migrations(database, MIGRATIONS_DIR, backups, later)
        conn = database.connection

        assert 4 in result.applied
        assert result.backup is not None
        assert result.backup.name == "app-20261020T080000Z-before-0004.db"
        assert result.backup.is_file()

        for table, rows in before.items():
            after = sorted(conn.execute(f"SELECT * FROM {table}").fetchall())  # noqa: S608
            assert after == rows, f"{table} rows changed during the upgrade"

        # Every new table starts empty except the seeded controlled values.
        for table in EXPECTED_COLUMNS:
            if table != "controlled_values":
                assert count(conn, table) == 0, f"0004 must not seed {table}"
        assert count(conn, "jobs_fts") == 0
        assert count(conn, "controlled_values") > 0

    with sqlite3.connect(result.backup) as copy:
        assert [r[0] for r in copy.execute("SELECT version FROM schema_migrations")] == [1, 2, 3]
        assert copy.execute("SELECT count(*) FROM facts").fetchone()[0] == 1
        names = {r[0] for r in copy.execute("SELECT name FROM sqlite_master")}
        assert "jobs" not in names


def test_0004_adds_exactly_the_jobs_tables(tmp_path: Path) -> None:
    _migration_0004()
    with Database.open(tmp_path / "data" / "app.db") as database:
        apply_migrations(
            database, _folder_up_to(tmp_path, 3), tmp_path / "backups", FrozenClock(START)
        )
        query = (
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            " AND name NOT LIKE '%_fts_%'"
        )
        at_0003 = {row[0] for row in database.connection.execute(query).fetchall()}
        apply_migrations(
            database, _folder_up_to(tmp_path, 4), tmp_path / "backups", FrozenClock(START)
        )
        rows = database.connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts_%'"
        ).fetchall()
    added = {row[0] for row in rows} - at_0003
    assert added == set(EXPECTED_COLUMNS) | {"jobs_fts"}
    fts_sql = next(row[1] for row in rows if row[0] == "jobs_fts")
    assert "fts5" in fts_sql.lower()


# --- shape ------------------------------------------------------------------


@pytest.mark.parametrize("table", sorted(EXPECTED_COLUMNS))
def test_0004_table_has_exactly_the_columns_later_tasks_rely_on(db: Database, table: str) -> None:
    assert set(columns(db.connection, table)) == EXPECTED_COLUMNS[table]


@pytest.mark.parametrize("table", MUTABLE_TABLES)
def test_0004_mutable_tables_have_created_updated_version(db: Database, table: str) -> None:
    found = columns(db.connection, table)
    for name in ("created_at", "updated_at", "version"):
        assert found[name]["notnull"] == 1, f"{table}.{name} must be NOT NULL"
    assert found["version"]["type"] == "INTEGER"
    assert str(found["version"]["default"]) == "1"


def test_new_job_gets_the_documented_defaults(db: Database) -> None:
    seed(db)
    row = db.connection.execute(
        "SELECT triage_state, previous_state, snooze_until, deal_breaker, fit_band,"
        " user_corrected, merged_into_job_id, version FROM jobs WHERE id = 'job-0001'"
    ).fetchone()
    assert tuple(row) == ("new", None, None, 0, None, "[]", None, 1)


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("jobs", "triage_state"),
        ("jobs", "found_at"),
        ("jobs", "fit_band"),
        ("jobs", "work_mode"),
        ("jobs", "industry"),
        ("jobs", "seniority"),
        ("jobs", "company_id"),
        ("jobs", "posted_date"),
        ("jobs", "deal_breaker"),
        ("jobs", "employment_type"),
        ("job_sources", "job_id"),
        ("job_summaries", "job_id"),
        ("job_assessments", "job_id"),
        ("job_requirements", "summary_id"),
    ],
    ids=lambda value: str(value),
)
def test_0004_inbox_filter_and_sort_columns_are_indexed(
    db: Database, table: str, column: str
) -> None:
    # A light check; full EXPLAIN QUERY PLAN tests belong to M4.6.
    found = index_columns(db.connection, table)
    assert any(column in keys for keys in found), f"no full index on {table} uses {column}"


def test_0004_inbox_queue_index_leads_with_state_then_found_at(db: Database) -> None:
    found = index_columns(db.connection, "jobs")
    assert any(keys[:2] == ["triage_state", "found_at"] for keys in found), found


# --- checks -----------------------------------------------------------------

REFUSED: list[tuple[str, dict[str, object]]] = [
    ("jobs", {"triage_state": "archived"}),
    ("jobs", {"triage_state": "NEW"}),
    ("jobs", {"triage_state": ""}),
    ("jobs", {"triage_state": None}),
    ("jobs", {"previous_state": "snoozed"}),
    ("jobs", {"previous_state": "removed"}),
    ("jobs", {"previous_state": "to_apply"}),
    ("jobs", {"previous_state": "maybe"}),
    ("jobs", {"fit_band": "excellent"}),
    ("jobs", {"fit_band": "Strong"}),
    ("jobs", {"fit_band": ""}),
    ("jobs", {"removal_reason": "boring"}),
    ("jobs", {"removal_reason": "Salary"}),
    ("jobs", {"removal_reason": ""}),
    ("jobs", {"deal_breaker": 2}),
    ("jobs", {"deal_breaker": -1}),
    ("jobs", {"deal_breaker": "yes"}),
    ("jobs", {"deal_breaker": 0.5}),
    ("jobs", {"deal_breaker": None}),
    ("jobs", {"snooze_until": "2026-10-26T00:00:00+00:00"}),
    ("jobs", {"snooze_until": "26/10/2026"}),
    ("jobs", {"snooze_until": "2026-10"}),
    ("jobs", {"snooze_until": ""}),
    ("jobs", {"company_id": "comp-9999"}),
    ("jobs", {"merged_into_job_id": "job-9999"}),
    ("job_sources", {"kind": "scraped"}),
    ("job_sources", {"kind": "Fetched"}),
    ("job_sources", {"kind": None}),
    ("job_sources", {"job_id": "job-9999"}),
    ("job_requirements", {"kind": "optional"}),
    ("job_requirements", {"kind": "must-have"}),
    ("job_requirements", {"kind": None}),
    ("job_requirements", {"summary_id": "sum-9999"}),
    ("job_assessments", {"fit_band": "excellent"}),
    ("job_assessments", {"deal_breaker": 2}),
    ("job_summaries", {"job_id": "job-9999"}),
    # Dates, amounts and ranges.
    ("jobs", {"posted_date": "2026/10/19"}),
    ("jobs", {"posted_date": "19-10-2026"}),
    ("jobs", {"posted_date": "2026-10-19T00:00:00+00:00"}),
    ("jobs", {"salary_min": "lots"}),
    ("jobs", {"salary_max": "50k"}),
    ("jobs", {"salary_min": -1}),
    ("jobs", {"experience_min": 1.5}),
    ("jobs", {"experience_max": "three"}),
    ("jobs", {"experience_min": 5, "experience_max": 2}),
    ("jobs", {"salary_min": 60000, "salary_max": 50000}),
    # JSON shapes.
    ("jobs", {"locations": "Larkspur Vale"}),
    ("jobs", {"locations": '{"city": "Larkspur Vale"}'}),
    ("jobs", {"locations": None}),
    ("companies", {"locations": "Larkspur Vale"}),
    ("companies", {"locations": "{}"}),
    ("job_summaries", {"fields_json": "[]"}),
    ("job_summaries", {"fields_json": "not json"}),
    ("job_assessments", {"result_json": "[]"}),
    ("job_assessments", {"result_json": "not json"}),
    ("job_assessments", {"fact_refs_json": "{}"}),
    ("job_assessments", {"fact_refs_json": "F1@1"}),
    ("controlled_values", {"aliases": "{}"}),
    ("controlled_values", {"aliases": "remote"}),
    ("controlled_values", {"aliases": None}),
    ("saved_views", {"filters_json": "[]"}),
    ("saved_views", {"filters_json": "not json"}),
    ("saved_views", {"name": " "}),
    ("field_value_queue", {"status": "done"}),
    ("field_value_queue", {"status": "Pending"}),
    # Links are http or https only, so none can become a javascript: or file: link.
    ("companies", {"website": "javascript:alert(1)"}),
    ("companies", {"website": "file:///etc/hosts"}),
    ("companies", {"website": "ftp://larkspur.example.test"}),
    ("job_sources", {"url": "javascript:alert(1)", "canonical_url": "javascript:alert(1)"}),
    ("job_sources", {"url": "file:///etc/hosts", "canonical_url": "file:///etc/hosts"}),
    ("job_sources", {"canonical_url": "javascript:alert(1)"}),
    # A fetched page or a link has a URL; the canonical form is there exactly when the URL is.
    ("job_sources", {"url": None, "canonical_url": None}),
    ("job_sources", {"kind": "link", "url": None, "canonical_url": None}),
    ("job_sources", {"url": None}),
    ("job_sources", {"canonical_url": None}),
]


@pytest.mark.parametrize(
    ("table", "extra"),
    REFUSED,
    ids=[f"{table}.{next(iter(extra))}={next(iter(extra.values()))!r}" for table, extra in REFUSED],
)
def test_job_checks_reject_unknown_state_band_reason_and_kind(
    db: Database, table: str, extra: dict[str, object]
) -> None:
    seed(db)
    refused(db, table, **extra)


ACCEPTED: list[tuple[str, dict[str, object]]] = [
    *[("jobs", {"triage_state": state}) for state in TRIAGE_STATES],
    ("jobs", {"triage_state": "removed", "previous_state": "new"}),
    ("jobs", {"triage_state": "to_apply", "previous_state": "seen"}),
    *[("jobs", {"fit_band": band}) for band in FIT_BANDS],
    *[("jobs", {"removal_reason": reason}) for reason in REMOVAL_REASONS],
    ("jobs", {"deal_breaker": 1}),
    ("jobs", {"snooze_until": "2026-10-26"}),
    # Work mode is free text, normalized later; unknown values are kept and queued.
    ("jobs", {"work_mode": "moon base"}),
    ("jobs", {"company_id": "comp-0001", "merged_into_job_id": "job-0001"}),
    *[("job_sources", {"kind": kind}) for kind in SOURCE_KINDS],
    *[("job_requirements", {"kind": kind}) for kind in REQUIREMENT_KINDS],
    *[("job_assessments", {"fit_band": band}) for band in FIT_BANDS],
    ("job_assessments", {"deal_breaker": 1}),
    ("job_sources", {"kind": "pasted", "url": None, "canonical_url": None}),
    ("job_sources", {"kind": "link"}),
    ("jobs", {"locations": '["Larkspur Vale", "Remote"]'}),
    ("jobs", {"experience_min": 2, "experience_max": 2}),
    ("jobs", {"experience_min": 2, "experience_max": 5}),
    ("jobs", {"experience_min": 3}),
    ("jobs", {"salary_min": 22.5, "salary_max": 27.25}),
    ("jobs", {"salary_max": 50000}),
    ("jobs", {"posted_date": "2026-10-12"}),
    ("companies", {"website": "https://larkspur.example.test"}),
    *[("field_value_queue", {"status": status}) for status in ("pending", "mapped", "ignored")],
]


@pytest.mark.parametrize(
    ("table", "extra"),
    ACCEPTED,
    ids=[f"{table}.{'+'.join(f'{k}={v}' for k, v in extra.items())}" for table, extra in ACCEPTED],
)
def test_job_checks_accept_known_values(db: Database, table: str, extra: dict[str, object]) -> None:
    seed(db)
    before = count(db.connection, table)
    put(db, table, **extra)
    assert count(db.connection, table) == before + 1


WHOLE_NUMBER_COLUMNS = [
    ("companies", "version"),
    ("jobs", "version"),
    ("controlled_values", "version"),
    ("saved_views", "version"),
    ("field_value_queue", "version"),
]


@pytest.mark.parametrize(
    ("table", "column"),
    WHOLE_NUMBER_COLUMNS,
    ids=[f"{table}.{column}" for table, column in WHOLE_NUMBER_COLUMNS],
)
@pytest.mark.parametrize("bad", ["abc", 1.5, -1], ids=["text", "fraction", "negative"])
def test_0004_version_columns_reject_text_fractions_and_negatives(
    db: Database, table: str, column: str, bad: object
) -> None:
    seed(db)
    refused(db, table, **{column: bad})


@pytest.mark.parametrize("bad", ["title", "{}", '{"title": true}', "not json", None])
def test_job_user_corrected_is_a_json_array(db: Database, bad: object) -> None:
    seed(db)
    refused(db, "jobs", user_corrected=bad)
    put(db, "jobs", user_corrected='["title", "work_mode"]')


def test_company_name_key_is_unique(db: Database) -> None:
    seed(db)
    refused(db, "companies", name="LARKSPUR ORCHARDS", name_key="larkspur orchards")


def test_job_cannot_be_merged_into_itself(db: Database) -> None:
    seed(db)
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute("UPDATE jobs SET merged_into_job_id = 'job-0001' WHERE id = 'job-0001'")


def test_dismissed_job_pair_is_stored_once_in_one_order(db: Database) -> None:
    seed(db)
    put(db, "jobs")

    def dismiss(first: str, second: str) -> None:
        with db.transaction() as conn:
            insert(
                conn,
                "job_pair_dismissals",
                {"job_a_id": first, "job_b_id": second, "created_at": STAMP},
            )

    dismiss("job-0001", "job-0002")
    for first, second in (
        ("job-0001", "job-0002"),  # again
        ("job-0002", "job-0001"),  # the other order: a < b keeps one row per pair
        ("job-0001", "job-0001"),
        ("job-0001", "job-9999"),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            dismiss(first, second)
    assert count(db.connection, "job_pair_dismissals") == 1


# --- append-only results ----------------------------------------------------


def test_job_summaries_and_assessments_refuse_update_and_delete(db: Database) -> None:
    seed(db)
    put(db, "job_assessments")
    conn = db.connection
    before = {
        table: sorted(conn.execute(f"SELECT * FROM {table}").fetchall())  # noqa: S608
        for table in ("job_summaries", "job_assessments")
    }

    statements = [
        ("job_summaries", "UPDATE job_summaries SET summary = 'Rewritten' WHERE id = 'sum-0001'"),
        ("job_summaries", "UPDATE job_summaries SET fields_json = '{}'"),
        ("job_summaries", "UPDATE job_summaries SET provider = 'other', model = 'x'"),
        ("job_summaries", "UPDATE job_summaries SET created_at = '2020-01-01T00:00:00+00:00'"),
        ("job_summaries", "DELETE FROM job_summaries WHERE id = 'sum-0001'"),
        ("job_summaries", "DELETE FROM job_summaries"),
        ("job_assessments", "UPDATE job_assessments SET fit_band = 'strong'"),
        ("job_assessments", "UPDATE job_assessments SET deal_breaker = 1"),
        ("job_assessments", "UPDATE job_assessments SET fact_refs_json = '[]'"),
        ("job_assessments", "UPDATE job_assessments SET result_json = '{}'"),
        ("job_assessments", "DELETE FROM job_assessments WHERE id = 'fit-0001'"),
        ("job_assessments", "DELETE FROM job_assessments"),
    ]
    for table, statement in statements:
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as tx:
            tx.execute(statement)
        after = sorted(conn.execute(f"SELECT * FROM {table}").fetchall())  # noqa: S608
        assert after == before[table], statement

    # Results are new rows: appending is still allowed.
    put(db, "job_summaries", created_at=LATER)
    put(db, "job_assessments", id="fit-0002", summary_id="sum-0002", fit_band="strong")
    assert count(conn, "job_summaries") == 2
    assert count(conn, "job_assessments") == 2


@pytest.mark.parametrize("table", ["job_summaries", "job_assessments"])
def test_job_results_refuse_insert_or_replace(db: Database, table: str) -> None:
    # Nothing points at the row being replaced, so only the append-only rule can refuse it.
    seed(db)
    if table == "job_summaries":
        row = summary_row(summary="Overwritten")
    else:
        put(db, "job_assessments")
        row = assessment_row(fit_band="strong")
    before = sorted(db.connection.execute(f"SELECT * FROM {table}").fetchall())  # noqa: S608
    names = ", ".join(f'"{name}"' for name in row)
    marks = ", ".join("?" for _ in row)
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO {table} ({names}) VALUES ({marks})",  # noqa: S608
            tuple(row.values()),
        )
    after = sorted(db.connection.execute(f"SELECT * FROM {table}").fetchall())  # noqa: S608
    assert after == before


def test_job_requirements_refuse_update_delete_and_replace(db: Database) -> None:
    seed(db)
    put(db, "job_requirements")
    conn = db.connection
    before = sorted(conn.execute("SELECT * FROM job_requirements").fetchall())
    row = {
        "id": "req-0001",
        "summary_id": "sum-0001",
        "position": 1,
        "kind": "nice",
        "text": "Rewritten",
    }
    names = ", ".join(f'"{name}"' for name in row)
    replace = f"INSERT OR REPLACE INTO job_requirements ({names}) VALUES (?, ?, ?, ?, ?)"  # noqa: S608
    statements: list[tuple[str, tuple[object, ...]]] = [
        ("UPDATE job_requirements SET text = 'Rewritten'", ()),
        ("UPDATE job_requirements SET kind = 'nice'", ()),
        ("DELETE FROM job_requirements", ()),
        (replace, tuple(row.values())),
    ]
    for statement, values in statements:
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as tx:
            tx.execute(statement, values)
        assert sorted(conn.execute("SELECT * FROM job_requirements").fetchall()) == before
    # A second requirement at the same position (nothing replaced) is refused too.
    refused(db, "job_requirements", id="req-0002")


def test_jobs_refuse_insert_or_replace(db: Database) -> None:
    # job-0002 has nothing pointing at it, so only the guard can refuse the replace.
    seed(db)
    put(db, "jobs")
    before = sorted(db.connection.execute("SELECT * FROM jobs").fetchall())
    row = job_row("job-0002", title="Replaced Title")
    names = ", ".join(f'"{name}"' for name in row)
    marks = ", ".join("?" for _ in row)
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO jobs ({names}) VALUES ({marks})",  # noqa: S608
            tuple(row.values()),
        )
    assert sorted(db.connection.execute("SELECT * FROM jobs").fetchall()) == before
    assert search(db, "replaced") == set()


def test_assessment_must_use_a_summary_of_its_own_job(db: Database) -> None:
    seed(db)
    put(db, "jobs")
    # sum-0001 belongs to job-0001.
    refused(db, "job_assessments", job_id="job-0002", summary_id="sum-0001")
    put(db, "job_assessments", job_id="job-0001", summary_id="sum-0001")


def test_saved_view_names_are_unique_ignoring_letter_case(db: Database) -> None:
    seed(db)
    put(db, "saved_views")
    refused(db, "saved_views", id="view-0002")
    refused(db, "saved_views", id="view-0002", name="REMOTE STRONG FITS")
    put(db, "saved_views", id="view-0002", name="Remote possible fits")


# --- controlled values ------------------------------------------------------


def _assert_seed(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    pairs = controlled(conn)
    assert len(pairs) == len(set(pairs)), "a controlled value is seeded twice"
    by_field: dict[str, set[str]] = {}
    for field, value in pairs:
        by_field.setdefault(field, set()).add(value)
    assert by_field.get("work_mode") == WORK_MODES
    assert by_field.get("employment_type") == EMPLOYMENT_TYPES
    assert len(by_field.get("industry", set())) >= 5
    assert len(by_field.get("seniority", set())) >= 5
    for (aliases,) in conn.execute("SELECT aliases FROM controlled_values").fetchall():
        assert isinstance(json.loads(aliases), list)
    return pairs


def test_controlled_values_seeded_once(tmp_path: Path) -> None:
    _migration_0004()
    with Database.open(tmp_path / "fresh" / "app.db") as fresh:
        apply_migrations(fresh, MIGRATIONS_DIR, tmp_path / "fresh-backups", FrozenClock(START))
        fresh_pairs = _assert_seed(fresh.connection)
        # Running the migrations again changes nothing.
        apply_migrations(fresh, MIGRATIONS_DIR, tmp_path / "fresh-backups", FrozenClock(START))
        assert controlled(fresh.connection) == fresh_pairs

        # One row per field and value.
        with pytest.raises(sqlite3.IntegrityError), fresh.transaction() as conn:
            insert(
                conn,
                "controlled_values",
                {
                    "id": "cv-dup-0001",
                    "field": "work_mode",
                    "value": "remote",
                    "aliases": "[]",
                    "created_at": STAMP,
                    "updated_at": STAMP,
                },
            )
        assert controlled(fresh.connection) == fresh_pairs

    with Database.open(tmp_path / "stepped" / "app.db") as stepped:
        apply_migrations(
            stepped, _folder_up_to(tmp_path, 3), tmp_path / "stepped-backups", FrozenClock(START)
        )
        apply_migrations(stepped, MIGRATIONS_DIR, tmp_path / "stepped-backups", FrozenClock(START))
        assert _assert_seed(stepped.connection) == fresh_pairs


# --- full-text search -------------------------------------------------------


def test_jobs_fts_follows_title_company_summary_and_snapshot(db: Database) -> None:
    from app.modules.storage import jobs_repo

    with db.transaction() as conn:
        jobs_repo.insert_company(
            conn, id="comp-0001", name="Larkspur Orchards", name_key="larkspur orchards", now=STAMP
        )
        jobs_repo.insert_job(
            conn,
            id="job-0001",
            title="Greenhouse Technician",
            snapshot_text="Tend the tomato vines and calibrate the irrigation timers.",
            captured_at=STAMP,
            found_at=STAMP,
            now=STAMP,
            company_id="comp-0001",
        )
        jobs_repo.insert_job(
            conn,
            id="job-0002",
            title="Ferry Deckhand",
            snapshot_text="Coil mooring ropes on the morning crossing.",
            captured_at=STAMP,
            found_at=STAMP,
            now=STAMP,
        )
        insert(conn, "tasks", task_row())
    assert search(db, "greenhouse") == {"job-0001"}
    assert search(db, "GREENHOUSE") == {"job-0001"}
    assert search(db, "larkspur") == {"job-0001"}
    assert search(db, "irrigation") == {"job-0001"}
    assert search(db, "ferry") == {"job-0002"}
    assert search(db, "mooring") == {"job-0002"}
    assert count(db.connection, "jobs_fts") == 2
    # Ids are not searchable text.
    for word in ("job", "0001", "comp", "new"):
        assert search(db, word) == set(), word

    # Title.
    with db.transaction() as conn:
        conn.execute("UPDATE jobs SET title = 'Nursery Technician' WHERE id = 'job-0001'")
    assert search(db, "nursery") == {"job-0001"}
    assert search(db, "greenhouse") == set()

    # Snapshot.
    with db.transaction() as conn:
        conn.execute(
            "UPDATE jobs SET snapshot_text = 'Graft apple saplings in the walled garden.'"
            " WHERE id = 'job-0001'"
        )
    assert search(db, "saplings") == {"job-0001"}
    assert search(db, "irrigation") == set()

    # Company renamed.
    with db.transaction() as conn:
        conn.execute(
            "UPDATE companies SET name = 'Bramblewood Growers', name_key = 'bramblewood growers'"
            " WHERE id = 'comp-0001'"
        )
    assert search(db, "bramblewood") == {"job-0001"}
    assert search(db, "larkspur") == set()

    # Job moved to another company, then to none.
    with db.transaction() as conn:
        jobs_repo.insert_company(
            conn, id="comp-0002", name="Quayside Ferries", name_key="quayside ferries", now=LATER
        )
        conn.execute("UPDATE jobs SET company_id = 'comp-0002' WHERE id = 'job-0002'")
    assert search(db, "quayside") == {"job-0002"}
    with db.transaction() as conn:
        conn.execute("UPDATE jobs SET company_id = NULL WHERE id = 'job-0002'")
    assert search(db, "quayside") == set()
    assert search(db, "deckhand") == {"job-0002"}

    # A summary, then a newer one: only the latest is searchable.
    with db.transaction() as conn:
        insert(conn, "job_summaries", summary_row(summary="Pruning espaliered pear trees."))
    assert search(db, "espaliered") == {"job-0001"}
    with db.transaction() as conn:
        insert(
            conn,
            "job_summaries",
            summary_row("sum-0002", summary="Looks after the polytunnels.", created_at=LATEST),
        )
    assert search(db, "polytunnels") == {"job-0001"}
    assert search(db, "espaliered") == set()

    # Everything else is untouched, and there is still one entry per job.
    assert search(db, "nursery") == {"job-0001"}
    assert search(db, "saplings") == {"job-0001"}
    assert search(db, "bramblewood") == {"job-0001"}
    assert search(db, "ferry") == {"job-0002"}
    assert count(db.connection, "jobs_fts") == 2


def test_deleting_an_unsummarized_job_removes_its_search_entry(db: Database) -> None:
    seed(db)
    put(db, "jobs", title="Ferry Deckhand")
    assert search(db, "deckhand") == {"job-0002"}
    with db.transaction() as conn:
        conn.execute("DELETE FROM jobs WHERE id = 'job-0002'")
    assert search(db, "deckhand") == set()
    assert count(db.connection, "jobs_fts") == 1


def test_latest_summary_at_the_same_time_is_the_last_written(db: Database) -> None:
    seed(db)  # sum-0001 at STAMP
    with db.transaction() as conn:
        insert(conn, "job_summaries", summary_row("sum-0002", summary="Tends the polytunnels."))
    assert search(db, "polytunnels") == {"job-0001"}
    assert search(db, "seasonal") == set()
    # Ids don't decide: a later row with a smaller id and the same time is still the latest.
    with db.transaction() as conn:
        insert(conn, "job_summaries", summary_row("sum-0000", summary="Waters the seedlings."))
    assert search(db, "seedlings") == {"job-0001"}
    assert search(db, "polytunnels") == set()
    assert count(db.connection, "jobs_fts") == 1


# --- repository -------------------------------------------------------------


def test_job_update_with_stale_version_raises_conflict(db: Database) -> None:
    from app.domain.errors import VersionConflict
    from app.modules.storage import jobs_repo

    with db.transaction() as conn:
        jobs_repo.insert_company(
            conn, id="comp-0001", name="Larkspur Orchards", name_key="larkspur orchards", now=STAMP
        )
        jobs_repo.insert_job(
            conn,
            id="job-0001",
            title="Greenhouse Technician",
            snapshot_text="Tend the tomato vines.",
            captured_at=STAMP,
            found_at=STAMP,
            now=STAMP,
            company_id="comp-0001",
        )
    job = jobs_repo.get_job(db.connection, "job-0001")
    assert job is not None
    assert (job.id, job.title, job.triage_state, job.previous_state) == (
        "job-0001",
        "Greenhouse Technician",
        "new",
        None,
    )
    assert job.snooze_until is None
    assert job.user_corrected == ()
    assert job.version == 1
    assert jobs_repo.get_job(db.connection, "job-9999") is None

    with db.transaction() as conn:
        jobs_repo.update_job_triage(
            conn,
            "job-0001",
            expected_version=1,
            triage_state="removed",
            previous_state="new",
            snooze_until=None,
            removal_reason="salary",
            removal_text="Below my range",
            now=LATER,
        )
    moved = jobs_repo.get_job(db.connection, "job-0001")
    assert moved is not None
    assert (moved.triage_state, moved.previous_state, moved.version) == ("removed", "new", 2)
    stored = db.connection.execute(
        "SELECT removal_reason, removal_text, updated_at FROM jobs WHERE id = 'job-0001'"
    ).fetchone()
    assert tuple(stored) == ("salary", "Below my range", LATER)

    # A second writer still holding version 1 is refused, and nothing changes.
    with pytest.raises(VersionConflict), db.transaction() as conn:
        jobs_repo.update_job_triage(
            conn,
            "job-0001",
            expected_version=1,
            triage_state="seen",
            previous_state=None,
            snooze_until=None,
            removal_reason=None,
            removal_text=None,
            now=LATEST,
        )
    assert jobs_repo.get_job(db.connection, "job-0001") == moved


# --- migration hashes -------------------------------------------------------


def test_migration_hashes_include_0004() -> None:
    path = _migration_0004()
    assert path.name == "0004_jobs.sql"
    recorded: dict[str, str] = json.loads(MIGRATION_HASHES.read_text(encoding="utf-8"))
    assert path.name in recorded, f"{MIGRATION_HASHES.name} lacks {path.name}"
    assert recorded[path.name] == hashlib.sha256(path.read_bytes()).hexdigest()
