"""M9.1 portable SQLite snapshots, entirely fictional temporary workspaces."""

import copy
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from app.modules.storage.portability import PortabilityError, export_tables, import_tables
from tests.fakes.clock import FrozenClock
from tests.integration.test_schema import schema_snapshot
from tests.integration.test_schema_0002 import SNAPSHOT_REQUESTS, insert_notice, insert_usage
from tests.integration.test_schema_0003 import (
    event_row,
    import_row,
    note_row,
    refine_row,
    role_row,
    skill_row,
    task_row,
)
from tests.integration.test_schema_0004 import START, insert, summary_row
from tests.integration.test_schema_0004 import put as put_job
from tests.integration.test_schema_0005 import child_row
from tests.integration.test_schema_0006 import (
    APP,
    STAMP,
    add_document,
    advance,
    cite_row,
    seed_base,
    seed_tailoring,
    unit_row,
)
from tests.integration.test_schema_0007 import seed as seed_sessions
from tests.integration.test_schema_0008 import seed_prep
from tests.integration.test_schema_0009 import sent_row

pytestmark = pytest.mark.integration


def fresh(path: Path) -> Database:
    db = Database.open(path / "app.db")
    apply_migrations(db, MIGRATIONS_DIR, path / "backups", FrozenClock(START))
    return db


def seed(db: Database) -> None:
    seed_base(db)
    seed_prep(db)
    with db.transaction() as conn:
        insert(conn, "prep_version_facts", sent_row())
        conn.execute("UPDATE prep_document_versions SET status='approved' WHERE id='v1'")
        add_document(conn, "doc-cv", APP, "cv", "dv-cv-1")
        insert(conn, "document_units", unit_row("unit-1", "dv-cv-1"))
        insert(conn, "document_unit_facts", cite_row("document_unit_id", "unit-1"))
        insert(conn, "document_sent_facts", cite_row("document_version_id", "dv-cv-1"))
        advance(conn, "dv-cv-1", "approved")
        advance(conn, "dv-cv-1", "sent")
        insert(
            conn,
            "settings",
            dict(
                key="ai_provider",
                value=json.dumps({"kind": "claude_api_key", "saved_sign_ins": ["claude_api_key"]}),
                created_at=STAMP,
                updated_at=STAMP,
                version=7,
            ),
        )


def seed_every_table(db: Database) -> None:
    """Exercise each persistent table with real schema-valid fictional history."""
    seed_tailoring(db)
    seed_prep(db)
    seed_sessions(db)
    common = dict(created_at=STAMP, updated_at=STAMP)
    with db.transaction() as conn:
        insert(conn, "prep_version_facts", sent_row())
        conn.execute("UPDATE prep_document_versions SET status='approved' WHERE id='v1'")
        for name, row in [
            ("roles", role_row()),
            ("tasks", task_row("task-0001")),
            ("cv_imports", import_row("imp-0001")),
            ("skills", skill_row("skill-0001", "Forecasting")),
            ("fact_events", event_row("event-0001")),
            ("capture_notes", note_row("note-0001")),
            ("refine_sessions", refine_row("refine-0001")),
            ("fact_skills", dict(fact_id="fact-0001", skill_id="skill-0001")),
            (
                "fact_pair_dismissals",
                dict(fact_a_id="fact-0001", fact_b_id="fact-0002", created_at=STAMP),
            ),
            ("settings", dict(key="fictional", value='{"number":1.5,"blank":null}', **common)),
            ("setup_state", dict(step_id="welcome", status="done", **common)),
            ("provider_usage_snapshots", SNAPSHOT_REQUESTS),
            ("job_summaries", summary_row()),
            (
                "job_pair_dismissals",
                dict(job_a_id="job-0001", job_b_id="job-0002", created_at=STAMP),
            ),
            ("reminder_states", dict(id="reminder-1", occurrence_key="fictional:1", **common)),
        ]:
            if name == "job_pair_dismissals":
                from tests.integration.test_schema_0004 import job_row

                insert(conn, "jobs", job_row("job-0002"))
            insert(conn, name, row)
        conn.execute(
            "UPDATE jobs SET triage_state='removed', removal_reason='other', "
            "removal_text='Fictional reason', updated_at=?, version=version+1 WHERE id='job-0002'",
            (STAMP,),
        )
        insert(
            conn,
            "removal_rules",
            dict(
                id="rule-0001",
                field="work_mode",
                condition='{"value":"on_site"}',
                evidence_job_ids='["job-0002"]',
                origin="pattern",
                **common,
            ),
        )
        insert(
            conn,
            "removal_suggestion_dismissals",
            dict(signature="fictional-signature", evidence_count=3, **common),
        )
        task_id = "task-removal-grouping"
        insert(conn, "tasks", task_row(task_id) | {"type": "group_removal_reasons"})
        insert(
            conn,
            "removal_theme_runs",
            dict(
                task_id=task_id,
                themes='[{"field":"work_mode","value":"on_site","evidence":[["job-0002",2]]}]',
                provider="fake",
                model="fake-fast",
                prompt_id="group_removal_reasons",
                prompt_version="1",
                created_at=STAMP,
            ),
        )
        insert_notice(conn, "notice-1")
        insert_usage(conn, "usage-1", service="fake", task_id="task-0001")
        for name in ("contacts", "application_notes", "interactions"):
            insert(conn, name, child_row(name))
        insert(
            conn,
            "application_contacts",
            dict(application_id=APP, contact_id="contacts-example", **common),
        )
    for name in (
        "job_sources",
        "job_requirements",
        "job_assessments",
        "field_value_queue",
        "saved_views",
    ):
        put_job(db, name)


def test_every_persistent_table_round_trips_nonempty_data(tmp_path):
    with fresh(tmp_path / "source") as source, fresh(tmp_path / "target") as target:
        seed_every_table(source)
        snapshot = export_tables(source, include_usage_snapshots=True)
        assert all(table["rows"] for table in snapshot["tables"].values()), [
            name for name, table in snapshot["tables"].items() if not table["rows"]
        ]
        import_tables(target, snapshot)
        assert export_tables(target, include_usage_snapshots=True) == snapshot
        assert schema_snapshot(target.connection) == schema_snapshot(source.connection)


def test_ac_m9_3_export_import_round_trip_exact(tmp_path):
    with fresh(tmp_path / "source") as source, fresh(tmp_path / "target") as target:
        seed(source)
        snapshot = export_tables(source, include_usage_snapshots=True)
        import_tables(target, snapshot)
        restored = export_tables(target, include_usage_snapshots=True)
        expected = copy.deepcopy(snapshot)
        settings = expected["tables"]["settings"]
        value_index = settings["columns"].index("value")
        row = next(r for r in settings["rows"] if r[0] == "ai_provider")
        value = json.loads(row[value_index])
        value["saved_sign_ins"] = []
        row[value_index] = json.dumps(value, separators=(",", ":"))
        assert restored == expected
        assert schema_snapshot(target.connection) == schema_snapshot(source.connection)
        assert target.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        for db in (source, target):
            with pytest.raises(sqlite3.IntegrityError):
                db.connection.execute("DELETE FROM document_units")


def test_export_covers_every_table_or_lists_exclusion(tmp_path):
    with fresh(tmp_path) as db:
        snapshot = export_tables(db)
        tables = {
            r[0] for r in db.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert set(snapshot["tables"]) | set(snapshot["excluded"]) == tables
        assert not set(snapshot["tables"]) & set(snapshot["excluded"])
        assert {
            "schema_migrations",
            "facts_fts",
            "jobs_fts",
            "bank_questions_fts",
            "provider_usage_snapshots",
        } <= set(snapshot["excluded"])


def test_import_fts_search_matches_source(tmp_path):
    with fresh(tmp_path / "source") as source, fresh(tmp_path / "target") as target:
        seed(source)
        import_tables(target, export_tables(source))
        for table, column, word in [
            ("facts_fts", "fact_id", "invoice"),
            ("jobs_fts", "job_id", "Technician"),
            ("bank_questions_fts", "id", "fictional"),
        ]:
            query = f"SELECT {column} FROM {table} WHERE {table} MATCH ? ORDER BY 1"  # noqa: S608 - fixed cases above
            expected = source.connection.execute(query, (word,)).fetchall()
            assert expected, (table, word)
            assert target.connection.execute(query, (word,)).fetchall() == expected


@pytest.mark.parametrize("change", ["unknown_table", "unknown_column", "missing_table", "bad_fk"])
def test_import_failure_rolls_back_rows_and_guards(tmp_path, change):
    with fresh(tmp_path / "source") as source, fresh(tmp_path / "target") as target:
        seed(source)
        snapshot = export_tables(source)
        before = export_tables(target)
        schema = schema_snapshot(target.connection)
        if change == "unknown_table":
            snapshot["tables"]["evil"] = {"columns": ["x"], "rows": []}
        elif change == "unknown_column":
            snapshot["tables"]["facts"]["columns"][0] = "evil"
        elif change == "missing_table":
            del snapshot["tables"]["facts"]
        else:
            facts = snapshot["tables"]["facts"]
            facts["rows"][0][facts["columns"].index("role_id")] = "missing"
        with pytest.raises(PortabilityError):
            import_tables(target, snapshot)
        assert export_tables(target) == before
        assert schema_snapshot(target.connection) == schema


def test_import_into_non_empty_workspace_refused(tmp_path):
    with fresh(tmp_path / "source") as source, fresh(tmp_path / "target") as target:
        seed(target)
        before = export_tables(target)
        with pytest.raises(PortabilityError):
            import_tables(target, export_tables(source))
        assert export_tables(target) == before


def test_import_preserves_content_committed_as_restore_starts(tmp_path, monkeypatch):
    with fresh(tmp_path / "source") as source, fresh(tmp_path / "target") as target:
        transaction = target.transaction

        @contextmanager
        def competing_write():
            # A different connection wins the write lock just before restore obtains it.
            with Database.open(target.path) as writer, writer.transaction() as conn:
                insert(
                    conn,
                    "settings",
                    dict(key="competing", value='"keep me"', created_at=STAMP, updated_at=STAMP),
                )
            with transaction() as conn:
                yield conn

        monkeypatch.setattr(target, "transaction", competing_write)
        with pytest.raises(PortabilityError):
            import_tables(target, export_tables(source))
        assert target.connection.execute(
            "SELECT value FROM settings WHERE key='competing'"
        ).fetchone() == ('"keep me"',)


@pytest.mark.parametrize("value", ["null", "false", "42", '"fictional"', "[]"])
def test_import_refuses_non_object_provider_settings_without_changes(tmp_path, value):
    with fresh(tmp_path / "source") as source, fresh(tmp_path / "target") as target:
        seed(source)
        snapshot = export_tables(source)
        settings = snapshot["tables"]["settings"]
        settings["rows"][0][settings["columns"].index("value")] = value
        before = export_tables(target)
        schema = schema_snapshot(target.connection)
        with pytest.raises(PortabilityError):
            import_tables(target, snapshot)
        assert export_tables(target) == before
        assert schema_snapshot(target.connection) == schema
