"""M8 schema integrity and upgrade checks, with fictional temporary data."""

import shutil
import sqlite3

import pytest

from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from tests.fakes.clock import FrozenClock
from tests.integration.test_schema import schema_snapshot
from tests.integration.test_schema_0004 import STAMP, START, insert
from tests.integration.test_schema_0005 import child_row
from tests.integration.test_schema_0006 import seed_base

pytestmark = pytest.mark.integration
TABLES = (
    "bank_questions",
    "bank_question_requirements",
    "bank_question_sightings",
    "prep_items",
    "practice_attempts",
    "prep_documents",
    "prep_document_versions",
    "prep_units",
    "prep_unit_facts",
    "debriefs",
    "debrief_drafts",
    "company_briefs",
)


@pytest.fixture
def db(tmp_path):
    assert (MIGRATIONS_DIR / "0008_interview_prep.sql").is_file()
    with Database.open(tmp_path / "data/app.db") as database:
        apply_migrations(database, MIGRATIONS_DIR, tmp_path / "backups", FrozenClock(START))
        seed_base(database)
        yield database


def rows():
    common = dict(created_at=STAMP, updated_at=STAMP)
    return {
        "bank_questions": dict(
            id="q1",
            text="Tell me about a fictional project.",
            type="behavioral",
            origin="generated",
            **common,
        ),
        "bank_question_requirements": dict(question_id="q1", text="Python development", **common),
        "bank_question_sightings": dict(
            id="s1",
            question_id="q1",
            application_id="app-example",
            origin="generated",
            found_at=STAMP,
            **common,
        ),
        "prep_items": dict(id="p1", application_id="app-example", question_id="q1", **common),
        "practice_attempts": dict(
            id="a1", prep_item_id="p1", answer_text="A fictional answer.", **common
        ),
        "prep_documents": dict(
            id="d1",
            application_id="app-example",
            kind="tell_me_about_yourself",
            current_version_id="v1",
            **common,
        ),
        "prep_document_versions": dict(
            id="v1", document_id="d1", number=1, origin="generated", **common
        ),
        "prep_units": dict(
            id="u1", document_version_id="v1", position=1, text="Fictional work.", **common
        ),
        "prep_unit_facts": dict(
            unit_id="u1", fact_id="fact-0001", fact_version_id="ver-0001", **common
        ),
        "debriefs": dict(
            id="b1", application_id="app-example", event_id="events-example", **common
        ),
        "debrief_drafts": dict(id="bd1", debrief_id="b1", fact_id="fact-0001", **common),
        "company_briefs": dict(
            id="c1",
            company_id="comp-0001",
            content='{"sections":[]}',
            sources="[]",
            found_at=STAMP,
            **common,
        ),
    }


def seed_prep(db):
    with db.transaction() as conn:
        insert(conn, "events", child_row("events"))
        for table, row in rows().items():
            insert(conn, table, row)


def test_0008_upgrade_keeps_0007_rows_and_backs_up(tmp_path):
    assert (MIGRATIONS_DIR / "0008_interview_prep.sql").is_file()
    previous = tmp_path / "previous"
    previous.mkdir()
    for path in MIGRATIONS_DIR.glob("*.sql"):
        if path.name < "0008":
            shutil.copy(path, previous / path.name)
    with Database.open(tmp_path / "data/app.db") as database:
        apply_migrations(database, previous, tmp_path / "backups", FrozenClock(START))
        seed_base(database)
        before = database.connection.execute("SELECT * FROM facts ORDER BY id").fetchall()
        result = apply_migrations(
            database, MIGRATIONS_DIR, tmp_path / "backups", FrozenClock(START)
        )
        assert result.applied == (8, 9)
        assert result.backup.is_file()
        assert database.connection.execute("SELECT * FROM facts ORDER BY id").fetchall() == before
        upgraded = schema_snapshot(database.connection)
        with sqlite3.connect(result.backup) as backup:
            assert backup.execute("SELECT * FROM facts ORDER BY id").fetchall() == before
            assert backup.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 7
    with Database.open(tmp_path / "fresh/app.db") as fresh:
        apply_migrations(fresh, MIGRATIONS_DIR, tmp_path / "fresh-backups", FrozenClock(START))
        assert schema_snapshot(fresh.connection) == upgraded


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("verb", ["INSERT OR REPLACE", "REPLACE"])
def test_0008_tables_refuse_replace_paths(db, table, verb):
    seed_prep(db)
    row = rows()[table]
    before = db.connection.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608 - fixed TABLES
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        columns = ",".join(row)
        conn.execute(
            f"{verb} INTO {table} ({columns}) VALUES ({','.join('?' for _ in row)})",
            tuple(row.values()),
        )
    assert db.connection.execute(f"SELECT * FROM {table}").fetchall() == before  # noqa: S608


@pytest.mark.parametrize("table", TABLES)
def test_0008_tables_have_explicit_keys_without_rowid_and_versions(db, table):
    ddl = db.connection.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[
        0
    ]
    assert "WITHOUT ROWID" in ddl.upper()
    columns = {r[1]: r for r in db.connection.execute(f"PRAGMA table_info({table})")}
    assert {"created_at", "updated_at", "version"} <= columns.keys()
    assert any(row[5] for row in columns.values())


@pytest.mark.parametrize(
    "field,value",
    [
        ("type", "invented"),
        ("origin", "invented"),
        ("text", "x" * 1001),
        ("text", "x\x00y"),
        ("text", " "),
    ],
)
def test_bank_question_values_checked(db, field, value):
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        insert(conn, "bank_questions", rows()["bank_questions"] | {field: value})


def test_prep_item_unique_per_application_and_question(db):
    seed_prep(db)
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        insert(conn, "prep_items", rows()["prep_items"] | {"id": "p2"})
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute("UPDATE OR REPLACE prep_items SET id='p2' WHERE id='p1'")


def test_prep_unit_citations_name_real_fact_versions(db):
    seed_prep(db)
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        insert(
            conn,
            "prep_unit_facts",
            rows()["prep_unit_facts"] | {"fact_id": "fact-0002", "fact_version_id": "ver-0001"},
        )


def test_undo_apply_refused_once_prep_exists(db):
    with db.transaction() as conn:
        insert(conn, "bank_questions", rows()["bank_questions"])
        insert(conn, "prep_items", rows()["prep_items"])
    with (
        pytest.raises(sqlite3.IntegrityError, match="already has changes"),
        db.transaction() as conn,
    ):
        conn.execute("DELETE FROM applications WHERE id='app-example'")


def test_question_bank_fts_tracks_question_text(db):
    with db.transaction() as conn:
        insert(conn, "bank_questions", rows()["bank_questions"])
    assert db.connection.execute(
        "SELECT id FROM bank_questions_fts WHERE bank_questions_fts MATCH 'fictional'"
    ).fetchall() == [("q1",)]
    with db.transaction() as conn:
        conn.execute("UPDATE bank_questions SET text='Describe an orchard.' WHERE id='q1'")
    assert not db.connection.execute(
        "SELECT id FROM bank_questions_fts WHERE bank_questions_fts MATCH 'fictional'"
    ).fetchall()
    assert db.connection.execute(
        "SELECT id FROM bank_questions_fts WHERE bank_questions_fts MATCH 'orchard'"
    ).fetchall() == [("q1",)]


def test_prep_approved_content_and_citations_are_immutable(db):
    seed_prep(db)
    with db.transaction() as conn:
        conn.execute("UPDATE prep_document_versions SET status='approved' WHERE id='v1'")
    for sql in [
        "UPDATE prep_document_versions SET status='draft' WHERE id='v1'",
        "UPDATE prep_document_versions SET angle_text='different' WHERE id='v1'",
        "DELETE FROM prep_document_versions WHERE id='v1'",
        "UPDATE prep_units SET text='invented' WHERE id='u1'",
        "DELETE FROM prep_unit_facts WHERE unit_id='u1'",
    ]:
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
            conn.execute(sql)
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        insert(conn, "prep_units", rows()["prep_units"] | {"id": "u2", "position": 2})
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        insert(
            conn,
            "prep_unit_facts",
            rows()["prep_unit_facts"] | {"fact_id": "fact-0002", "fact_version_id": "ver-0002"},
        )


@pytest.mark.parametrize("table", TABLES)
def test_0008_identity_update_replace_is_refused(db, table):
    seed_prep(db)
    columns = db.connection.execute(f"PRAGMA table_info({table})").fetchall()
    first_key = next(row[1] for row in columns if row[5] == 1)
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute(f"UPDATE OR REPLACE {table} SET {first_key}='replacement'")  # noqa: S608


def test_debrief_must_refer_to_event_of_same_application(db):
    from tests.integration.test_schema_0006 import add_second_application

    with db.transaction() as conn:
        insert(conn, "events", child_row("events"))
        add_second_application(conn)
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        insert(conn, "debriefs", rows()["debriefs"] | {"application_id": "app-two"})


def test_mark_ready_checks_item_version(db):
    from app.domain.errors import VersionConflict
    from app.modules.storage.prep_repo import mark_ready

    seed_prep(db)
    with db.transaction() as conn:
        mark_ready(conn, "p1", "app-example", ready=True, now=STAMP, expected_version=1)
    with pytest.raises(VersionConflict), db.transaction() as conn:
        mark_ready(conn, "p1", "app-example", ready=False, now=STAMP, expected_version=1)
    assert db.connection.execute(
        "SELECT ready,version FROM prep_items WHERE id='p1'"
    ).fetchone() == (1, 2)
