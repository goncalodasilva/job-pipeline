"""M8.2 part B schema: the facts SENT for a narrative version (migration 0009; spec 3.1 item 3,
the parity with the CV's and the letter's sent-facts table; ADR-124 item 5 as amended by the
user-approved 0009). Fictional temporary data.

Assumed interface (the implementer matches it): one forward-only migration file
``0009_*.sql`` creating ``prep_version_facts(version_id, fact_id, fact_version_id, created_at,
updated_at, version)``, ``WITHOUT ROWID``, primary key ``(version_id, fact_id)``, a foreign key
to ``prep_document_versions(id)`` and one to ``fact_versions(fact_id, id)``, and the guards of
0008: a row is never replaced, a row is inserted only while its version is a draft, and a row is
never updated or deleted (a delete is refused on a draft too). 0008 itself is not edited.
"""

import shutil
import sqlite3

import pytest

from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from tests.fakes.clock import FrozenClock
from tests.integration.test_schema import schema_snapshot
from tests.integration.test_schema_0004 import STAMP, START, insert
from tests.integration.test_schema_0006 import seed_base
from tests.integration.test_schema_0008 import TABLES as TABLES_0008
from tests.integration.test_schema_0008 import seed_prep

pytestmark = pytest.mark.integration
TABLE = "prep_version_facts"


def sent_row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = dict(
        version_id="v1",
        fact_id="fact-0001",
        fact_version_id="ver-0001",
        created_at=STAMP,
        updated_at=STAMP,
    )
    return row | changes


@pytest.fixture
def db(tmp_path):
    assert list(MIGRATIONS_DIR.glob("0009_*.sql")), "migration 0009 is missing"
    with Database.open(tmp_path / "data/app.db") as database:
        apply_migrations(database, MIGRATIONS_DIR, tmp_path / "backups", FrozenClock(START))
        seed_base(database)
        seed_prep(database)
        yield database


def approve(db: Database) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE prep_document_versions SET status='approved' WHERE id='v1'")


def sent_rows(db: Database) -> list[tuple]:
    return db.connection.execute(f"SELECT * FROM {TABLE} ORDER BY 1, 2").fetchall()  # noqa: S608


def test_0009_is_one_new_migration_and_0008_is_untouched() -> None:
    names = sorted(path.name for path in MIGRATIONS_DIR.glob("*.sql"))

    assert names[:8] == [
        "0001_initial.sql",
        "0002_tasks_usage_budgets.sql",
        "0003_fact_bank.sql",
        "0004_jobs.sql",
        "0005_applications.sql",
        "0006_tailoring.sql",
        "0007_sessions.sql",
        "0008_interview_prep.sql",
    ]
    assert len([n for n in names if n.startswith("0009_")]) == 1
    assert names[8] == "0009_prep_version_facts.sql"


def test_0009_table_has_an_explicit_key_without_rowid_and_the_versioned_columns(db) -> None:
    ddl = db.connection.execute("SELECT sql FROM sqlite_master WHERE name=?", (TABLE,)).fetchone()[
        0
    ]
    columns = {r[1]: r for r in db.connection.execute(f"PRAGMA table_info({TABLE})")}

    assert "WITHOUT ROWID" in ddl.upper()
    assert {
        "version_id",
        "fact_id",
        "fact_version_id",
        "created_at",
        "updated_at",
        "version",
    } == set(columns)
    keyed = sorted((r[5], r[1]) for r in columns.values() if r[5])
    assert keyed == [(1, "version_id"), (2, "fact_id")]
    assert columns["version"][4] == "1" and columns["version"][3] == 1


def test_a_draft_version_takes_its_sent_facts(db) -> None:
    with db.transaction() as conn:
        insert(conn, TABLE, sent_row())
        insert(conn, TABLE, sent_row(fact_id="fact-0002", fact_version_id="ver-0002"))

    assert [(r[0], r[1], r[2]) for r in sent_rows(db)] == [
        ("v1", "fact-0001", "ver-0001"),
        ("v1", "fact-0002", "ver-0002"),
    ]
    assert db.connection.execute(f"SELECT version FROM {TABLE} LIMIT 1").fetchone() == (1,)  # noqa: S608


def test_the_same_fact_is_sent_once_per_version(db) -> None:
    with db.transaction() as conn:
        insert(conn, TABLE, sent_row())
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        insert(conn, TABLE, sent_row(fact_version_id="ver-0001"))

    assert len(sent_rows(db)) == 1


def test_sent_facts_name_a_real_version_and_a_real_fact_version(db) -> None:
    for bad in (
        sent_row(version_id="no-such-version"),
        sent_row(fact_id="fact-0002", fact_version_id="ver-0001"),
        sent_row(fact_id="fact-0001", fact_version_id="ver-9999"),
        sent_row(fact_id="no-such-fact", fact_version_id="no-such-version"),
    ):
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
            insert(conn, TABLE, bad)

    assert sent_rows(db) == []


def test_an_approved_version_takes_no_more_sent_facts_and_loses_none(db) -> None:
    with db.transaction() as conn:
        insert(conn, TABLE, sent_row())
    approve(db)
    before = sent_rows(db)

    for statement in (
        f"DELETE FROM {TABLE} WHERE version_id='v1'",  # noqa: S608
        f"UPDATE {TABLE} SET fact_version_id='ver-0002' WHERE version_id='v1'",  # noqa: S608
        f"UPDATE {TABLE} SET updated_at='2030-01-01T00:00:00+00:00' WHERE version_id='v1'",  # noqa: S608
    ):
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
            conn.execute(statement)
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        insert(conn, TABLE, sent_row(fact_id="fact-0002", fact_version_id="ver-0002"))

    assert sent_rows(db) == before


def test_a_sent_fact_is_never_updated_even_on_a_draft(db) -> None:
    with db.transaction() as conn:
        insert(conn, TABLE, sent_row())
    before = sent_rows(db)

    for statement in (
        f"UPDATE {TABLE} SET fact_version_id='ver-0002'",  # noqa: S608
        f"UPDATE {TABLE} SET version_id='v2'",  # noqa: S608
        f"UPDATE {TABLE} SET created_at='2030-01-01T00:00:00+00:00'",  # noqa: S608
    ):
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
            conn.execute(statement)

    assert sent_rows(db) == before


@pytest.mark.parametrize("verb", ["INSERT OR REPLACE", "REPLACE"])
def test_0009_table_refuses_replace_paths(db, verb: str) -> None:
    with db.transaction() as conn:
        insert(conn, TABLE, sent_row())
    before = sent_rows(db)
    row = sent_row(fact_version_id="ver-0001", updated_at="2031-01-01T00:00:00+00:00")

    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute(
            f"{verb} INTO {TABLE} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
            tuple(row.values()),
        )

    assert sent_rows(db) == before


def test_0009_identity_update_replace_is_refused(db) -> None:
    with db.transaction() as conn:
        insert(conn, TABLE, sent_row())
        insert(conn, TABLE, sent_row(fact_id="fact-0002", fact_version_id="ver-0002"))
    before = sent_rows(db)

    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute(f"UPDATE OR REPLACE {TABLE} SET fact_id='fact-0001'")  # noqa: S608

    assert sent_rows(db) == before


def test_a_version_with_sent_facts_still_cannot_be_deleted(db) -> None:
    with db.transaction() as conn:
        insert(conn, TABLE, sent_row())

    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute("DELETE FROM prep_document_versions WHERE id='v1'")

    assert len(sent_rows(db)) == 1


def test_0009_upgrade_from_0008_keeps_every_row_and_backs_up(tmp_path) -> None:
    previous = tmp_path / "previous"
    previous.mkdir()
    through_0009 = tmp_path / "through-0009"
    through_0009.mkdir()
    for path in MIGRATIONS_DIR.glob("*.sql"):
        if path.name < "0009":
            shutil.copy(path, previous / path.name)
        if path.name < "0010":
            shutil.copy(path, through_0009 / path.name)
    with Database.open(tmp_path / "data/app.db") as database:
        apply_migrations(database, previous, tmp_path / "backups", FrozenClock(START))
        seed_base(database)
        seed_prep(database)
        before = {
            table: database.connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()  # noqa: S608
            for table in ("facts", "fact_versions", *TABLES_0008)
        }
        assert not database.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)
        ).fetchone()

        result = apply_migrations(database, through_0009, tmp_path / "backups", FrozenClock(START))

        assert result.applied == (9,)
        assert result.backup is not None and result.backup.is_file()
        for table, rows in before.items():
            assert (
                database.connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() == rows  # noqa: S608
            ), table
        assert sent_rows(database) == []
        upgraded = schema_snapshot(database.connection)
        with sqlite3.connect(result.backup) as backup:
            assert backup.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 8
            assert not backup.execute(
                "SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)
            ).fetchone()
            for table, rows in before.items():
                assert backup.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() == rows  # noqa: S608
    with Database.open(tmp_path / "fresh/app.db") as fresh:
        apply_migrations(fresh, through_0009, tmp_path / "fresh-backups", FrozenClock(START))
        assert schema_snapshot(fresh.connection) == upgraded


def test_the_old_prep_documents_flow_still_writes_with_the_new_table_present(db) -> None:
    """0009 changes nothing about how a draft version, its units and their citations are
    written (the 0008 guards still hold)."""
    with db.transaction() as conn:
        insert(conn, TABLE, sent_row())
    approve(db)

    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute("UPDATE prep_units SET text='invented' WHERE id='u1'")
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute("DELETE FROM prep_unit_facts WHERE unit_id='u1'")
