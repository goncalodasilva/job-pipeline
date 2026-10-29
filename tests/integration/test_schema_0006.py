"""M6.4 tailoring migration 0006: constraints, append-only guards, Undo guard, conflicts.

Everything here is fictional. The tests write rows through the real migration-applied database.

Names the tests rely on (the implementer writes ``0006_tailoring.sql`` to match). Any column not
listed here is never written by these tests, so it must be nullable or have a default.

Mutable tables (``created_at``, ``updated_at``, ``version``, explicit text ``id`` key):

- ``angles``: name, description, origin (``ai|user``).
- ``workspaces``: application_id (FK applications), cv_country, angle_id (nullable FK angles).
  Not written by the tests: letter_tone, letter_length.
- ``cv_templates``: country (unique), status (``draft|approved``), spec (valid JSON),
  approved_at (nullable). Not written: provenance columns.
- ``documents``: application_id, kind (``cv|cover_letter``, unique per application),
  current_version_id (NOT NULL, deferred composite FK (id, current_version_id) to
  ``document_versions (document_id, id)``, as ``facts.current_version_id``).
- ``document_versions``: document_id, number (unique per document), status
  (``draft|approved|sent``, forward only), origin (free text or a CHECK accepting
  ``generated``), parent_version_id (nullable), template_snapshot (JSON, nullable), angle_text,
  note. Content columns (number, document_id, parent_version_id, template_snapshot, angle_text,
  note) are immutable; only status and the row counters change. The ``version`` column is the
  row counter, ``number`` is the document version number. Not written: tone, length,
  provenance columns, origin changes.

Append-only (no UPDATE, DELETE, INSERT OR REPLACE or UPDATE OR REPLACE), explicit key, WITHOUT
ROWID; no ``updated_at`` or ``version``:

- ``gap_checks``: id, application_id, summary, result (JSON), created_at. Not written: task_id,
  provenance columns.
- ``gap_check_facts``: gap_check_id, fact_id, fact_version_id (composite FK to
  ``fact_versions (fact_id, id)``). No created_at.
- ``angle_proposals``: id, application_id, created_at. Not written: any proposal payload.
- ``document_units``: id, document_version_id, position, section, kind (``content`` accepted),
  text, origin (``ai|user``), created_at. Not written: role_id, added_details, flags.
- ``document_unit_facts``: document_unit_id, fact_id, fact_version_id (composite FK as above).
- ``document_sent_facts``: document_version_id, fact_id, fact_version_id (composite FK).
- ``document_exports``: id, document_version_id, format (``pdf|docx``), stored_name (unique,
  letters digits dot dash underscore, no leading dot), sha256, size_bytes, pages, created_at.
- ``application_submissions``: application_id (the key; no id column), frozen_at,
  cv_version_id, letter_version_id (both nullable FKs to document_versions), created_at.

Repository ``app.modules.storage.tailoring_repo`` (minimal, for the stale-version test), each
checking ``WHERE version = ?`` and raising ``VersionConflict`` when no row matches::

    update_workspace(conn, workspace_id, *, cv_country, angle_id, now, expected_version)
    set_current_version(conn, document_id, version_id, *, now, expected_version)
    set_version_status(conn, version_id, status, *, now, expected_version)

Each bumps ``version`` by one and sets ``updated_at`` to ``now``.

``applications_delete_only_untouched`` is recreated to also refuse an application with a
workspace, gap check, angle proposal, document or submission, with the same message
(``this application already has changes``).
"""

import hashlib
import importlib
import json
import re
import shutil
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.domain.cv_templates import (
    DateFormat,
    DetailId,
    PageSize,
    SectionId,
    SectionSpec,
    TemplateSpec,
)
from app.domain.errors import StatusNotAllowed, VersionConflict
from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations, load_migrations
from tests.application_desk import UNDO_REFUSED, Desk
from tests.fakes.clock import FrozenClock
from tests.integration.test_schema import schema_snapshot
from tests.integration.test_schema_0003 import add_fact
from tests.integration.test_schema_0004 import insert, job_row
from tests.integration.test_schema_0005 import seed

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 25, 10, tzinfo=UTC)
STAMP = NOW.isoformat()
LATER = "2026-10-25T11:00:00+00:00"
APP = "app-example"
MIGRATION_HASHES = Path(__file__).parents[1] / "fixtures" / "migration_hashes.json"
# Built from the domain model, so the fixture is a spec the application itself accepts.
TEMPLATE_SPEC = TemplateSpec(
    sections=(SectionSpec(id=SectionId.SUMMARY), SectionSpec(id=SectionId.EXPERIENCE)),
    max_pages=1,
    photo_slot=False,
    details_shown=(DetailId.EMAIL, DetailId.PHONE),
    date_format=DateFormat.MONTH_YEAR,
    page_size=PageSize.A4,
).model_dump_json()

NEW_TABLES = {
    "workspaces",
    "gap_checks",
    "gap_check_facts",
    "angles",
    "angle_proposals",
    "cv_templates",
    "documents",
    "document_versions",
    "document_units",
    "document_unit_facts",
    "document_sent_facts",
    "document_exports",
    "application_submissions",
}
MUTABLE = ("angles", "workspaces", "cv_templates", "documents", "document_versions")
IMMUTABLE = (
    "gap_checks",
    "gap_check_facts",
    "angle_proposals",
    "document_units",
    "document_unit_facts",
    "document_sent_facts",
    "document_exports",
    "application_submissions",
)
APPLICATION_TABLES = (
    "workspaces",
    "gap_checks",
    "angle_proposals",
    "documents",
    "application_submissions",
)
# Columns the tests write, per table (see the module docstring).
WRITTEN_COLUMNS = {
    "angles": {"id", "name", "description", "origin", "created_at", "updated_at"},
    "workspaces": {"id", "application_id", "cv_country", "angle_id", "created_at", "updated_at"},
    "cv_templates": {
        "id",
        "country",
        "status",
        "spec",
        "approved_at",
        "created_at",
        "updated_at",
    },
    "documents": {
        "id",
        "application_id",
        "kind",
        "current_version_id",
        "created_at",
        "updated_at",
    },
    "document_versions": {
        "id",
        "document_id",
        "number",
        "status",
        "origin",
        "parent_version_id",
        "template_snapshot",
        "angle_text",
        "note",
        "created_at",
        "updated_at",
    },
    "gap_checks": {"id", "application_id", "summary", "result", "created_at"},
    "gap_check_facts": {"gap_check_id", "fact_id", "fact_version_id"},
    "angle_proposals": {"id", "application_id", "created_at"},
    "document_units": {
        "id",
        "document_version_id",
        "position",
        "section",
        "kind",
        "text",
        "origin",
        "created_at",
    },
    "document_unit_facts": {"document_unit_id", "fact_id", "fact_version_id"},
    "document_sent_facts": {"document_version_id", "fact_id", "fact_version_id"},
    "document_exports": {
        "id",
        "document_version_id",
        "format",
        "stored_name",
        "sha256",
        "size_bytes",
        "pages",
        "created_at",
    },
    "application_submissions": {
        "application_id",
        "frozen_at",
        "cv_version_id",
        "letter_version_id",
        "created_at",
    },
}


# --- row builders ------------------------------------------------------------


def angle_row(angle_id: str = "ang-0001", **extra: object) -> dict[str, object]:
    return {
        "id": angle_id,
        "name": "Hands-on grower",
        "description": "Lead with greenhouse automation work.",
        "origin": "ai",
        "created_at": STAMP,
        "updated_at": STAMP,
        **extra,
    }


def workspace_row(
    workspace_id: str = "ws-0001", application_id: str = APP, **extra: object
) -> dict[str, object]:
    return {
        "id": workspace_id,
        "application_id": application_id,
        "cv_country": "PT",
        "created_at": STAMP,
        "updated_at": STAMP,
        **extra,
    }


def gap_check_row(
    gap_id: str = "gap-0001", application_id: str = APP, **extra: object
) -> dict[str, object]:
    return {
        "id": gap_id,
        "application_id": application_id,
        "summary": "Two of three must-haves are covered.",
        "result": '{"requirements": []}',
        "created_at": STAMP,
        **extra,
    }


def proposal_row(
    proposal_id: str = "prop-0001", application_id: str = APP, **extra: object
) -> dict[str, object]:
    return {"id": proposal_id, "application_id": application_id, "created_at": STAMP, **extra}


def template_row(
    template_id: str = "tpl-0001", country: str = "PT", **extra: object
) -> dict[str, object]:
    return {
        "id": template_id,
        "country": country,
        "status": "draft",
        "spec": TEMPLATE_SPEC,
        "created_at": STAMP,
        "updated_at": STAMP,
        **extra,
    }


def document_row(
    document_id: str,
    application_id: str,
    kind: str,
    current_version_id: str,
    **extra: object,
) -> dict[str, object]:
    return {
        "id": document_id,
        "application_id": application_id,
        "kind": kind,
        "current_version_id": current_version_id,
        "created_at": STAMP,
        "updated_at": STAMP,
        **extra,
    }


def doc_version_row(
    version_id: str, document_id: str, number: int = 1, **extra: object
) -> dict[str, object]:
    return {
        "id": version_id,
        "document_id": document_id,
        "number": number,
        "status": "draft",
        "origin": "generated",
        "created_at": STAMP,
        "updated_at": STAMP,
        **extra,
    }


def unit_row(
    unit_id: str, version_id: str, position: int = 1, **extra: object
) -> dict[str, object]:
    return {
        "id": unit_id,
        "document_version_id": version_id,
        "position": position,
        "section": "experience",
        "kind": "content",
        "text": "Cut the invoice backlog by a third",
        "origin": "ai",
        "created_at": STAMP,
        **extra,
    }


def export_row(
    export_id: str = "exp-0001",
    version_id: str = "dv-cv-1",
    stored_name: str = "export-0001.pdf",
    **extra: object,
) -> dict[str, object]:
    return {
        "id": export_id,
        "document_version_id": version_id,
        "format": "pdf",
        "stored_name": stored_name,
        "sha256": "a" * 64,
        "size_bytes": 1234,
        "pages": 1,
        "created_at": STAMP,
        **extra,
    }


def submission_row(
    application_id: str = APP,
    cv_version_id: str | None = None,
    letter_version_id: str | None = None,
    **extra: object,
) -> dict[str, object]:
    return {
        "application_id": application_id,
        "frozen_at": STAMP,
        "cv_version_id": cv_version_id,
        "letter_version_id": letter_version_id,
        "created_at": STAMP,
        **extra,
    }


def cite_row(owner_column: str, owner_id: str, fact: int = 1) -> dict[str, object]:
    return {
        owner_column: owner_id,
        "fact_id": f"fact-{fact:04d}",
        "fact_version_id": f"ver-{fact:04d}",
    }


# --- helpers -----------------------------------------------------------------


def add_document(
    conn: sqlite3.Connection,
    document_id: str,
    application_id: str,
    kind: str,
    version_id: str,
    **version_extra: object,
) -> None:
    """A document and its first version in one transaction, the document first (deferred FK)."""
    insert(conn, "documents", document_row(document_id, application_id, kind, version_id))
    insert(conn, "document_versions", doc_version_row(version_id, document_id, **version_extra))


def advance(conn: sqlite3.Connection, version_id: str, status: str) -> None:
    conn.execute(
        "UPDATE document_versions SET status = ?, updated_at = ?, version = version + 1"
        " WHERE id = ?",
        (status, LATER, version_id),
    )


def dump(conn: sqlite3.Connection, table: str) -> list[tuple[Any, ...]]:
    rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()  # noqa: S608
    return sorted((tuple(row) for row in rows), key=repr)


def seed_base(db: Database) -> None:
    """job-0001 with its application ``app-example`` and the confirmed facts fact-0001 and
    fact-0002 (versions ver-0001 and ver-0002)."""
    with db.transaction() as conn:
        seed(conn)
    add_fact(db, "fact-0001", 1, "ver-0001")
    add_fact(db, "fact-0002", 2, "ver-0002")


def add_second_application(conn: sqlite3.Connection) -> None:
    insert(conn, "jobs", job_row("job-0002"))
    insert(
        conn,
        "applications",
        dict(
            id="app-two",
            job_id="job-0002",
            status_key="preparing",
            created_at=STAMP,
            updated_at=STAMP,
        ),
    )


def seed_tailoring(db: Database) -> None:
    """One of everything; both documents end up approved, exported and sent."""
    seed_base(db)
    with db.transaction() as conn:
        insert(conn, "angles", angle_row())
        insert(conn, "workspaces", workspace_row(angle_id="ang-0001"))
        insert(conn, "gap_checks", gap_check_row("gap-0001"))
        insert(conn, "gap_check_facts", cite_row("gap_check_id", "gap-0001"))
        insert(conn, "gap_checks", gap_check_row("gap-0002"))
        insert(conn, "angle_proposals", proposal_row())
        insert(conn, "cv_templates", template_row())
        add_document(conn, "doc-cv", APP, "cv", "dv-cv-1")
        add_document(conn, "doc-letter", APP, "cover_letter", "dv-letter-1")
        insert(conn, "document_units", unit_row("unit-0001", "dv-cv-1"))
        insert(conn, "document_units", unit_row("unit-0002", "dv-cv-1", 2, origin="user"))
        insert(conn, "document_unit_facts", cite_row("document_unit_id", "unit-0001"))
        insert(conn, "document_sent_facts", cite_row("document_version_id", "dv-cv-1"))
        advance(conn, "dv-cv-1", "approved")
        advance(conn, "dv-letter-1", "approved")
        insert(conn, "document_exports", export_row())
        advance(conn, "dv-cv-1", "sent")
        advance(conn, "dv-letter-1", "sent")
        insert(conn, "application_submissions", submission_row(APP, "dv-cv-1", "dv-letter-1"))


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    assert (MIGRATIONS_DIR / "0006_tailoring.sql").is_file(), "0006_tailoring.sql is missing"
    with Database.open(tmp_path / "data/app.db") as database:
        apply_migrations(database, MIGRATIONS_DIR, tmp_path / "backups", FrozenClock(NOW))
        seed_base(database)
        yield database


@pytest.fixture
def tailored(tmp_path: Path) -> Iterator[Database]:
    assert (MIGRATIONS_DIR / "0006_tailoring.sql").is_file(), "0006_tailoring.sql is missing"
    with Database.open(tmp_path / "data/app.db") as database:
        apply_migrations(database, MIGRATIONS_DIR, tmp_path / "backups", FrozenClock(NOW))
        seed_tailoring(database)
        yield database


@pytest.fixture
def docs(db: Database) -> Database:
    """doc-cv with dv-cv-1 and its child dv-cv-2 (drafts), and doc-letter with dv-letter-1."""
    content = dict(
        template_snapshot='{"sections": ["summary"]}',
        angle_text="Calm hands-on grower",
        note="Shorter please",
    )
    with db.transaction() as conn:
        add_document(conn, "doc-cv", APP, "cv", "dv-cv-1", **content)
        insert(
            conn,
            "document_versions",
            doc_version_row("dv-cv-2", "doc-cv", 2, parent_version_id="dv-cv-1", **content),
        )
        add_document(conn, "doc-letter", APP, "cover_letter", "dv-letter-1")
    return db


# --- migration file, hashes, upgrade -----------------------------------------


def test_0006_migration_file_and_hash_are_recorded() -> None:
    path = MIGRATIONS_DIR / "0006_tailoring.sql"
    assert path.is_file(), "0006_tailoring.sql is missing"
    assert 6 in [migration.version for migration in load_migrations(MIGRATIONS_DIR)]
    recorded: dict[str, str] = json.loads(MIGRATION_HASHES.read_text(encoding="utf-8"))
    assert path.name in recorded, f"{MIGRATION_HASHES.name} lacks {path.name}"
    assert recorded[path.name] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_0006_upgrade_keeps_0005_rows_and_backs_up(tmp_path: Path) -> None:
    assert (MIGRATIONS_DIR / "0006_tailoring.sql").is_file(), "0006_tailoring.sql is missing"
    previous = tmp_path / "previous"
    previous.mkdir()
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if migration.name < "0006":
            shutil.copy(migration, previous / migration.name)
    with Database.open(tmp_path / "data/app.db") as upgraded:
        apply_migrations(upgraded, previous, tmp_path / "backups", FrozenClock(NOW))
        seed_base(upgraded)
        with upgraded.transaction() as conn:
            conn.execute(
                "INSERT INTO settings (key, value, created_at, updated_at)"
                " VALUES ('ui.theme', '\"dark\"', ?, ?)",
                (STAMP, STAMP),
            )
        names = [
            row[0]
            for row in upgraded.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                " AND name NOT LIKE '%\\_fts%' ESCAPE '\\' AND name <> 'schema_migrations'"
            )
        ]
        assert {"applications", "facts", "status_history", "settings"} <= set(names)
        before = {name: dump(upgraded.connection, name) for name in names}
        assert before["applications"] and before["fact_versions"] and before["settings"]

        result = apply_migrations(upgraded, MIGRATIONS_DIR, tmp_path / "backups", FrozenClock(NOW))
        assert 6 in result.applied
        assert result.backup is not None
        assert result.backup.name == "app-20261025T100000Z-before-0006.db"
        conn = upgraded.connection
        for name, rows in before.items():
            assert dump(conn, name) == rows, f"{name} rows changed during the upgrade"
        for table in NEW_TABLES:
            assert conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] == 0  # noqa: S608
        upgraded_schema = schema_snapshot(conn)

    with sqlite3.connect(result.backup) as copy:
        assert copy.execute("SELECT count(*) FROM applications").fetchone()[0] == 1
        assert copy.execute("SELECT count(*) FROM fact_versions").fetchone()[0] == 2
        master = {row[0] for row in copy.execute("SELECT name FROM sqlite_master")}
        assert not master & NEW_TABLES
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1

    with Database.open(tmp_path / "fresh/app.db") as fresh:
        apply_migrations(fresh, MIGRATIONS_DIR, tmp_path / "fresh-backups", FrozenClock(NOW))
        assert schema_snapshot(fresh.connection) == upgraded_schema


def test_0006_adds_exactly_the_tailoring_tables_and_no_personal_details_table(
    tmp_path: Path,
) -> None:
    assert (MIGRATIONS_DIR / "0006_tailoring.sql").is_file(), "0006_tailoring.sql is missing"
    earlier = tmp_path / "earlier"
    earlier.mkdir()
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if migration.name < "0006":
            shutil.copy(migration, earlier / migration.name)
    through_0006 = tmp_path / "through-0006"
    through_0006.mkdir()
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if migration.name < "0007":
            shutil.copy(migration, through_0006 / migration.name)
    query = "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    with Database.open(tmp_path / "before/app.db") as before:
        apply_migrations(before, earlier, tmp_path / "b1", FrozenClock(NOW))
        old = {row[0] for row in before.connection.execute(query)}
    with Database.open(tmp_path / "after/app.db") as after:
        apply_migrations(after, through_0006, tmp_path / "b2", FrozenClock(NOW))
        new = {row[0] for row in after.connection.execute(query)}
        # Personal details are a versioned setting (ADR-106 item 3), so nothing seeds one.
        assert after.connection.execute("SELECT count(*) FROM settings").fetchone()[0] == 0
    assert new - old == NEW_TABLES
    assert not {name for name in new if "personal" in name or "photo" in name}
    assert old <= new


# --- shape -------------------------------------------------------------------


@pytest.mark.parametrize("table", sorted(WRITTEN_COLUMNS))
def test_tables_have_the_columns_these_tests_write(db: Database, table: str) -> None:
    columns = {row[1] for row in db.connection.execute(f'PRAGMA table_info("{table}")')}
    assert WRITTEN_COLUMNS[table] <= columns


@pytest.mark.parametrize("table", MUTABLE)
def test_mutable_tables_carry_created_updated_and_a_checked_version(
    db: Database, table: str
) -> None:
    info = {row[1]: row for row in db.connection.execute(f'PRAGMA table_info("{table}")')}
    assert {"created_at", "updated_at", "version"} <= set(info)
    assert info["version"][3] == 1 and info["version"][4] == "1"  # NOT NULL DEFAULT 1
    assert info["id"][5] == 1  # explicit text key


@pytest.mark.parametrize("table", IMMUTABLE)
def test_immutable_tables_use_explicit_keys_and_without_rowid(db: Database, table: str) -> None:
    sql = db.connection.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()
    assert re.search(r"WITHOUT\s+ROWID", sql[0], re.IGNORECASE), f"{table} must be WITHOUT ROWID"
    keys = [row[1] for row in db.connection.execute(f'PRAGMA table_info("{table}")') if row[5]]
    assert keys, f"{table} needs an explicit primary key"
    for alias in ("rowid", "oid", "_rowid_"):
        with pytest.raises(sqlite3.OperationalError):
            db.connection.execute(f'SELECT {alias} FROM "{table}"')  # noqa: S608


def test_query_columns_are_indexed(db: Database) -> None:
    expected = {
        "document_unit_facts": [("fact_id",), ("document_unit_id",)],
        "documents": [("application_id",)],
        "workspaces": [("application_id",)],
        "document_versions": [("document_id", "number")],
        "document_units": [("document_version_id", "position")],
        "document_exports": [("document_version_id",)],
        "gap_checks": [("application_id",)],
        "angle_proposals": [("application_id",)],
    }
    for table, prefixes in expected.items():
        indexes = [
            tuple(item[2] for item in db.connection.execute(f'PRAGMA index_info("{row[1]}")'))
            for row in db.connection.execute(f'PRAGMA index_list("{table}")')
        ]
        for prefix in prefixes:
            assert any(columns[: len(prefix)] == prefix for columns in indexes), (table, prefix)


@pytest.mark.parametrize("table", APPLICATION_TABLES)
def test_tailoring_rows_need_an_existing_application(db: Database, table: str) -> None:
    rows: dict[str, Any] = {
        "workspaces": lambda conn: insert(conn, table, workspace_row(application_id="missing")),
        "gap_checks": lambda conn: insert(conn, table, gap_check_row(application_id="missing")),
        "angle_proposals": lambda conn: insert(conn, table, proposal_row(application_id="missing")),
        "documents": lambda conn: add_document(conn, "doc-x", "missing", "cv", "dv-x"),
        "application_submissions": lambda conn: insert(conn, table, submission_row("missing")),
    }
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        rows[table](conn)
    assert db.connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] == 0  # noqa: S608


@pytest.mark.parametrize("table", ["gap_check_facts", "document_unit_facts", "document_sent_facts"])
def test_fact_citations_name_a_real_version_of_that_fact(db: Database, table: str) -> None:
    owner = {
        "gap_check_facts": "gap_check_id",
        "document_unit_facts": "document_unit_id",
        "document_sent_facts": "document_version_id",
    }[table]
    owner_id = {
        "gap_check_facts": "gap-0001",
        "document_unit_facts": "unit-0001",
        "document_sent_facts": "dv-cv-1",
    }[table]
    with db.transaction() as conn:
        insert(conn, "gap_checks", gap_check_row("gap-0001"))
        add_document(conn, "doc-cv", APP, "cv", "dv-cv-1")
        insert(conn, "document_units", unit_row("unit-0001", "dv-cv-1"))
    bad_rows = [
        {owner: owner_id, "fact_id": "fact-0001", "fact_version_id": "ver-0002"},  # other fact's
        {owner: owner_id, "fact_id": "fact-0001", "fact_version_id": "missing"},
        {owner: owner_id, "fact_id": "missing", "fact_version_id": "ver-0001"},
        {owner: "missing", "fact_id": "fact-0001", "fact_version_id": "ver-0001"},
    ]
    for row in bad_rows:
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
            insert(conn, table, row)
    with db.transaction() as conn:
        insert(conn, table, cite_row(owner, owner_id))
    assert db.connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] == 1  # noqa: S608


# --- documents and versions --------------------------------------------------


def test_one_document_per_kind_per_application(docs: Database) -> None:
    with docs.transaction() as conn:
        add_second_application(conn)
        add_document(conn, "doc-cv-two", "app-two", "cv", "dv-cv-two")  # another application: fine
    before = dump(docs.connection, "documents")
    with pytest.raises(sqlite3.IntegrityError), docs.transaction() as conn:
        add_document(conn, "doc-cv-again", APP, "cv", "dv-cv-again")
    with pytest.raises(sqlite3.IntegrityError), docs.transaction() as conn:
        add_document(conn, "doc-letter-again", APP, "cover_letter", "dv-letter-again")
    # A replacement must not delete the existing document (INSERT OR REPLACE and its alias).
    for verb in ("INSERT OR REPLACE", "REPLACE"):
        with pytest.raises(sqlite3.IntegrityError), docs.transaction() as conn:
            conn.execute(
                f"{verb} INTO documents (id, application_id, kind, current_version_id,"
                " created_at, updated_at) VALUES ('doc-cv-new', ?, 'cv', 'dv-cv-1', ?, ?)",
                (APP, STAMP, STAMP),
            )
        with pytest.raises(sqlite3.IntegrityError), docs.transaction() as conn:
            conn.execute(
                f"{verb} INTO documents SELECT * FROM documents WHERE id = 'doc-cv'"  # noqa: S608
            )
    with pytest.raises(sqlite3.IntegrityError), docs.transaction() as conn:
        insert(conn, "documents", document_row("doc-memo", APP, "memo", "dv-memo"))
        insert(conn, "document_versions", doc_version_row("dv-memo", "doc-memo"))
    assert dump(docs.connection, "documents") == before
    with pytest.raises(sqlite3.IntegrityError), docs.transaction() as conn:
        conn.execute("UPDATE OR REPLACE documents SET kind = 'cv' WHERE id = 'doc-letter'")
    assert dump(docs.connection, "documents") == before


def test_current_version_must_belong_to_document(docs: Database) -> None:
    before = dump(docs.connection, "documents")
    # The key is deferred: the statement runs and the commit refuses it.
    reached = []
    with pytest.raises(sqlite3.IntegrityError), docs.transaction() as conn:
        conn.execute("UPDATE documents SET current_version_id = 'dv-letter-1' WHERE id = 'doc-cv'")
        reached.append("update")
    with pytest.raises(sqlite3.IntegrityError), docs.transaction() as conn:
        conn.execute("UPDATE documents SET current_version_id = 'missing' WHERE id = 'doc-cv'")
        reached.append("missing")
    with pytest.raises(sqlite3.IntegrityError), docs.transaction() as conn:
        add_second_application(conn)
        insert(conn, "documents", document_row("doc-two", "app-two", "cv", "dv-cv-1"))
        insert(conn, "document_versions", doc_version_row("dv-two", "doc-two"))
        reached.append("insert")
    assert reached == ["update", "missing", "insert"]
    assert dump(docs.connection, "documents") == before
    # A later version of the same document can become current.
    with docs.transaction() as conn:
        conn.execute(
            "UPDATE documents SET current_version_id = 'dv-cv-2', version = version + 1"
            " WHERE id = 'doc-cv'"
        )
    assert docs.connection.execute(
        "SELECT current_version_id FROM documents WHERE id = 'doc-cv'"
    ).fetchone() == ("dv-cv-2",)


def test_document_version_numbers_are_unique_per_document(docs: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError), docs.transaction() as conn:
        insert(conn, "document_versions", doc_version_row("dv-cv-3", "doc-cv", 2))
    with docs.transaction() as conn:  # the same number in another document is fine
        insert(conn, "document_versions", doc_version_row("dv-letter-2", "doc-letter", 2))


@pytest.mark.parametrize(
    "change",
    [
        "number = 9",
        "document_id = 'doc-letter'",
        "parent_version_id = NULL",
        'template_snapshot = \'{"sections": ["experience"]}\'',
        "angle_text = 'Changed angle'",
        "note = 'Changed note'",
    ],
)
def test_document_version_content_immutable(docs: Database, change: str) -> None:
    before = dump(docs.connection, "document_versions")
    for verb in ("UPDATE", "UPDATE OR REPLACE"):
        with pytest.raises(sqlite3.IntegrityError), docs.transaction() as conn:
            conn.execute(f"{verb} document_versions SET {change} WHERE id = 'dv-cv-2'")
    assert dump(docs.connection, "document_versions") == before


def test_document_version_content_immutable_status_forward_only(docs: Database) -> None:
    def status_of(version_id: str) -> str:
        return docs.connection.execute(
            "SELECT status FROM document_versions WHERE id = ?", (version_id,)
        ).fetchone()[0]

    def move(version_id: str, status: str) -> None:
        with docs.transaction() as conn:
            conn.execute(
                "UPDATE document_versions SET status = ?, updated_at = ?, version = version + 1"
                " WHERE id = ?",
                (status, LATER, version_id),
            )

    def refused(version_id: str, statement: str, params: tuple[object, ...] = ()) -> None:
        before = dump(docs.connection, "document_versions")
        with pytest.raises(sqlite3.IntegrityError), docs.transaction() as conn:
            conn.execute(statement, params)
        assert dump(docs.connection, "document_versions") == before

    # Row counters and timestamps may change without a status change.
    with docs.transaction() as conn:
        conn.execute(
            "UPDATE document_versions SET updated_at = ?, version = version + 1"
            " WHERE id = 'dv-cv-1'",
            (LATER,),
        )
    move("dv-cv-1", "approved")
    assert status_of("dv-cv-1") == "approved"

    set_status = "UPDATE document_versions SET status = ? WHERE id = ?"
    refused("dv-cv-1", set_status, ("draft", "dv-cv-1"))
    refused("dv-cv-1", set_status, ("final", "dv-cv-1"))
    refused(
        "dv-cv-1",
        "UPDATE document_versions SET note = 'Edited after approval' WHERE id = ?",
        ("dv-cv-1",),
    )
    assert status_of("dv-cv-1") == "approved"

    move("dv-cv-1", "sent")
    for backwards in ("approved", "draft"):
        refused("dv-cv-1", set_status, (backwards, "dv-cv-1"))
    refused("dv-cv-1", "DELETE FROM document_versions WHERE id = ?", ("dv-cv-1",))
    refused(
        "dv-cv-1",
        "INSERT OR REPLACE INTO document_versions SELECT * FROM document_versions WHERE id = ?",
        ("dv-cv-1",),
    )
    assert status_of("dv-cv-1") == "sent"
    assert status_of("dv-cv-2") == "draft"  # the child version is its own row


# --- append-only tables ------------------------------------------------------

# table: (WHERE for a row that has no child rows, a valid change to it). The change keeps every
# foreign key valid, so only a guard can refuse it.
_OTHER_FACT = "fact_id = 'fact-0002', fact_version_id = 'ver-0002'"
APPEND_ONLY: dict[str, tuple[str, str]] = {
    "gap_checks": ("id = 'gap-0002'", "summary = 'Changed summary'"),
    "gap_check_facts": ("gap_check_id = 'gap-0001'", _OTHER_FACT),
    "angle_proposals": ("id = 'prop-0001'", f"created_at = '{LATER}'"),
    "document_units": ("id = 'unit-0002'", "text = 'Changed text'"),
    "document_unit_facts": ("document_unit_id = 'unit-0001'", _OTHER_FACT),
    "document_sent_facts": ("document_version_id = 'dv-cv-1'", _OTHER_FACT),
    "document_exports": ("id = 'exp-0001'", "pages = 2"),
    "application_submissions": ("application_id = 'app-example'", f"frozen_at = '{LATER}'"),
}


def test_append_only_registry_covers_every_immutable_table() -> None:
    assert set(APPEND_ONLY) == set(IMMUTABLE)


@pytest.mark.parametrize("table", sorted(APPEND_ONLY))
def test_tailoring_append_only_tables_refuse_update_delete_and_replace_paths(
    tailored: Database, table: str
) -> None:
    where, change = APPEND_ONLY[table]
    conn = tailored.connection
    assert conn.execute(f'SELECT count(*) FROM "{table}" WHERE {where}').fetchone()[0] >= 1  # noqa: S608
    before = dump(conn, table)
    statements = [
        f'UPDATE "{table}" SET {change} WHERE {where}',  # noqa: S608
        f'UPDATE OR REPLACE "{table}" SET {change} WHERE {where}',  # noqa: S608
        f'DELETE FROM "{table}" WHERE {where}',  # noqa: S608
        f'INSERT OR REPLACE INTO "{table}" SELECT * FROM "{table}" WHERE {where}',  # noqa: S608
        f'REPLACE INTO "{table}" SELECT * FROM "{table}" WHERE {where}',  # noqa: S608
    ]
    for statement in statements:
        with pytest.raises(sqlite3.IntegrityError), tailored.transaction() as write:
            write.execute(statement)
        assert dump(conn, table) == before, statement
    # Implicit rowid aliases cannot be used to slip past the guards either.
    for alias in ("rowid", "oid", "_rowid_"):
        with pytest.raises(sqlite3.Error), tailored.transaction() as write:
            write.execute(f'UPDATE OR REPLACE "{table}" SET {alias} = 1 WHERE {where}')  # noqa: S608
        with pytest.raises(sqlite3.Error), tailored.transaction() as write:
            write.execute(
                f'INSERT OR REPLACE INTO "{table}" ({alias}) SELECT {alias} FROM "{table}"'  # noqa: S608
            )
        assert dump(conn, table) == before, alias


# --- templates, exports, submissions ------------------------------------------


def test_one_template_per_country_and_status_checked(db: Database) -> None:
    with db.transaction() as conn:
        insert(conn, "cv_templates", template_row("tpl-pt", "PT"))
        insert(
            conn, "cv_templates", template_row("tpl-de", "DE", status="approved", approved_at=STAMP)
        )
    before = dump(db.connection, "cv_templates")
    for duplicate in (
        template_row("tpl-pt-again", "PT"),
        template_row("tpl-de-again", "DE", status="approved", approved_at=STAMP),
    ):
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
            insert(conn, "cv_templates", duplicate)
    for verb in ("INSERT OR REPLACE", "REPLACE"):
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
            conn.execute(
                f"{verb} INTO cv_templates (id, country, status, spec, created_at, updated_at)"
                " VALUES ('tpl-de-new', 'DE', 'draft', ?, ?, ?)",
                (TEMPLATE_SPEC, STAMP, STAMP),
            )
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute("UPDATE OR REPLACE cv_templates SET country = 'DE' WHERE id = 'tpl-pt'")
    for bad in (
        template_row("tpl-bad-status", "ES", status="final"),
        template_row("tpl-bad-status", "ES", status=""),
        template_row("tpl-bad-status", "ES", status=None),
        template_row("tpl-bad-spec", "ES", spec="not json"),
        template_row("tpl-bad-spec", "ES", spec=None),
    ):
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
            insert(conn, "cv_templates", bad)
    assert dump(db.connection, "cv_templates") == before
    with db.transaction() as conn:  # approving is an update with a version bump
        conn.execute(
            "UPDATE cv_templates SET status = 'approved', approved_at = ?, updated_at = ?,"
            " version = version + 1 WHERE id = 'tpl-pt'",
            (LATER, LATER),
        )
    assert db.connection.execute(
        "SELECT status, approved_at, version FROM cv_templates WHERE id = 'tpl-pt'"
    ).fetchone() == ("approved", LATER, 2)


def test_export_stored_name_unique_and_shape_checked(docs: Database) -> None:
    with docs.transaction() as conn:
        insert(conn, "document_exports", export_row("exp-0001", "dv-cv-1", "export-0001.pdf"))
    before = dump(docs.connection, "document_exports")
    bad_names = [
        "export-0001.pdf",  # taken
        "",
        "  ",
        ".hidden.pdf",
        ".",
        "..",
        "../export.pdf",
        "nested/export.pdf",
        "nested\\export.pdf",
        "export 0002.pdf",
        "export\n0002.pdf",
        "expért.pdf",
        None,
    ]
    for name in bad_names:
        with pytest.raises(sqlite3.IntegrityError), docs.transaction() as conn:
            insert(conn, "document_exports", export_row("exp-0002", "dv-cv-1", name))
    for bad_format in ("txt", "PDF", "", None):
        with pytest.raises(sqlite3.IntegrityError), docs.transaction() as conn:
            insert(
                conn,
                "document_exports",
                export_row("exp-0003", "dv-cv-1", "other.pdf", format=bad_format),
            )
    with pytest.raises(sqlite3.IntegrityError), docs.transaction() as conn:
        insert(conn, "document_exports", export_row("exp-0004", "missing", "other.pdf"))
    assert dump(docs.connection, "document_exports") == before
    with docs.transaction() as conn:
        insert(
            conn,
            "document_exports",
            export_row("exp-0005", "dv-cv-1", "Export_0005-v2.docx", format="docx", pages=None),
        )
    assert docs.connection.execute("SELECT count(*) FROM document_exports").fetchone()[0] == 2


def test_submission_once_per_application_and_immutable(docs: Database) -> None:
    with docs.transaction() as conn:
        advance(conn, "dv-cv-1", "approved")
        advance(conn, "dv-cv-1", "sent")
        advance(conn, "dv-letter-1", "approved")
        advance(conn, "dv-letter-1", "sent")
        add_second_application(conn)
        insert(conn, "application_submissions", submission_row(APP, "dv-cv-1", "dv-letter-1"))
    before = dump(docs.connection, "application_submissions")
    # A second submission for the same application, however it is written, is refused.
    with pytest.raises(sqlite3.IntegrityError), docs.transaction() as conn:
        insert(conn, "application_submissions", submission_row(APP, None, None, frozen_at=LATER))
    for verb in ("INSERT OR REPLACE", "REPLACE"):
        with pytest.raises(sqlite3.IntegrityError), docs.transaction() as conn:
            conn.execute(
                f"{verb} INTO application_submissions (application_id, frozen_at, cv_version_id,"
                " letter_version_id, created_at) VALUES (?, ?, NULL, NULL, ?)",
                (APP, LATER, LATER),
            )
    for statement in (
        "UPDATE application_submissions SET frozen_at = '2027-01-01T00:00:00+00:00'",
        "UPDATE application_submissions SET cv_version_id = NULL",
        "UPDATE OR REPLACE application_submissions SET letter_version_id = NULL",
        "DELETE FROM application_submissions",
    ):
        with pytest.raises(sqlite3.IntegrityError), docs.transaction() as conn:
            conn.execute(statement)
    with pytest.raises(sqlite3.IntegrityError), docs.transaction() as conn:
        insert(conn, "application_submissions", submission_row("app-two", "missing", None))
    assert dump(docs.connection, "application_submissions") == before
    # No CV or no letter is allowed: the freeze stores what was exported, else nothing.
    with docs.transaction() as conn:
        insert(conn, "application_submissions", submission_row("app-two"))
    assert (
        docs.connection.execute("SELECT count(*) FROM application_submissions").fetchone()[0] == 2
    )


# --- Undo guard ---------------------------------------------------------------


def add_touching_row(conn: sqlite3.Connection, table: str, application_id: str) -> None:
    if table == "workspaces":
        insert(conn, table, workspace_row(application_id=application_id))
    elif table == "gap_checks":
        insert(conn, table, gap_check_row(application_id=application_id))
    elif table == "angle_proposals":
        insert(conn, table, proposal_row(application_id=application_id))
    elif table == "documents":
        add_document(conn, "doc-cv", application_id, "cv", "dv-cv-1")
    else:
        insert(conn, table, submission_row(application_id))


def test_undo_trigger_is_recreated_and_untouched_application_still_deletes(db: Database) -> None:
    sql = db.connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger'"
        " AND name = 'applications_delete_only_untouched'"
    ).fetchone()
    assert sql is not None
    for table in APPLICATION_TABLES:
        assert table in sql[0], f"the Undo guard must look at {table}"
    with db.transaction() as conn:
        conn.execute("DELETE FROM applications WHERE id = ?", (APP,))
    assert db.connection.execute("SELECT count(*) FROM applications").fetchone()[0] == 0
    assert db.connection.execute("SELECT count(*) FROM status_history").fetchone()[0] == 0


@pytest.mark.parametrize("table", APPLICATION_TABLES)
def test_undo_apply_refused_once_tailoring_rows_exist_trigger(db: Database, table: str) -> None:
    with db.transaction() as conn:
        add_touching_row(conn, table, APP)
    with (
        pytest.raises(sqlite3.IntegrityError, match="this application already has changes"),
        db.transaction() as conn,
    ):
        conn.execute("DELETE FROM applications WHERE id = ?", (APP,))
    assert db.connection.execute("SELECT id FROM applications").fetchall() == [(APP,)]
    assert db.connection.execute("SELECT count(*) FROM status_history").fetchone()[0] == 1
    assert db.connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] == 1  # noqa: S608


@pytest.mark.parametrize("table", APPLICATION_TABLES)
def test_undo_apply_refused_once_tailoring_rows_exist(tmp_path: Path, table: str) -> None:
    assert (MIGRATIONS_DIR / "0006_tailoring.sql").is_file(), "0006_tailoring.sql is missing"
    desk = Desk(tmp_path)
    try:
        desk.add_job("job-0001", "Greenhouse Technician")
        applied = desk.apply("job-0001")
        with desk.db.transaction() as conn:
            add_touching_row(conn, table, applied.application.id)
        job = desk.job("job-0001")
        before = desk.snapshot()
        before_rows = dump(desk.db.connection, table)
        with pytest.raises(StatusNotAllowed) as refused:
            desk.applications.undo_apply("job-0001", expected_job_version=job.version)
        assert refused.value.user_message == UNDO_REFUSED
        assert desk.snapshot() == before
        assert dump(desk.db.connection, table) == before_rows
        assert desk.job("job-0001") == job and job.triage_state == "to_apply"
    finally:
        desk.close()


# --- optimistic concurrency ---------------------------------------------------


def test_stale_workspace_or_document_update_conflicts(docs: Database) -> None:
    repo = importlib.import_module("app.modules.storage.tailoring_repo")
    with docs.transaction() as conn:
        insert(conn, "workspaces", workspace_row())
        repo.update_workspace(
            conn, "ws-0001", cv_country="DE", angle_id=None, now=LATER, expected_version=1
        )
    workspace = "SELECT cv_country, angle_id, updated_at, version FROM workspaces"
    assert docs.connection.execute(workspace).fetchall() == [("DE", None, LATER, 2)]
    with pytest.raises(VersionConflict), docs.transaction() as conn:
        repo.update_workspace(
            conn, "ws-0001", cv_country="US", angle_id=None, now=LATER, expected_version=1
        )
    assert docs.connection.execute(workspace).fetchall() == [("DE", None, LATER, 2)]

    document = "SELECT current_version_id, version FROM documents WHERE id = 'doc-cv'"
    with docs.transaction() as conn:
        repo.set_current_version(conn, "doc-cv", "dv-cv-2", now=LATER, expected_version=1)
    assert docs.connection.execute(document).fetchone() == ("dv-cv-2", 2)
    with pytest.raises(VersionConflict), docs.transaction() as conn:
        repo.set_current_version(conn, "doc-cv", "dv-cv-1", now=LATER, expected_version=1)
    assert docs.connection.execute(document).fetchone() == ("dv-cv-2", 2)

    status = "SELECT status, updated_at, version FROM document_versions WHERE id = 'dv-cv-1'"
    with docs.transaction() as conn:
        repo.set_version_status(conn, "dv-cv-1", "approved", now=LATER, expected_version=1)
    assert docs.connection.execute(status).fetchone() == ("approved", LATER, 2)
    with pytest.raises(VersionConflict), docs.transaction() as conn:
        repo.set_version_status(conn, "dv-cv-1", "sent", now=LATER, expected_version=1)
    assert docs.connection.execute(status).fetchone() == ("approved", LATER, 2)
    # The schema guard still speaks through the repository: no step back to draft.
    with pytest.raises(sqlite3.IntegrityError), docs.transaction() as conn:
        repo.set_version_status(conn, "dv-cv-1", "draft", now=LATER, expected_version=2)
    assert docs.connection.execute(status).fetchone() == ("approved", LATER, 2)


# --- review fixes: rowid replacement, frozen versions, export names, letter choices ---------------


def test_the_test_template_spec_is_a_spec_the_domain_accepts() -> None:
    assert TemplateSpec.model_validate_json(TEMPLATE_SPEC).max_pages == 1


def _two_rows(conn: sqlite3.Connection, table: str) -> None:
    """Make sure ``table`` holds at least two rows."""
    if table == "angles":
        insert(conn, table, angle_row("ang-0001"))
        insert(conn, table, angle_row("ang-0002"))
    elif table == "workspaces":
        add_second_application(conn)
        insert(conn, table, workspace_row("ws-0001"))
        insert(conn, table, workspace_row("ws-0002", "app-two"))
    elif table == "cv_templates":
        insert(conn, table, template_row("tpl-pt", "PT"))
        insert(conn, table, template_row("tpl-de", "DE"))
    elif table == "documents":
        add_document(conn, "doc-cv", APP, "cv", "dv-cv-1")
        add_document(conn, "doc-letter", APP, "cover_letter", "dv-letter-1")
    else:
        add_document(conn, "doc-cv", APP, "cv", "dv-cv-1")
        insert(conn, table, doc_version_row("dv-cv-2", "doc-cv", 2, parent_version_id="dv-cv-1"))
        insert(conn, table, doc_version_row("dv-cv-3", "doc-cv", 3, parent_version_id="dv-cv-1"))


@pytest.mark.parametrize("table", MUTABLE)
def test_mutable_tables_cannot_lose_a_row_through_a_rowid_replacement(
    db: Database, table: str
) -> None:
    """UPDATE OR REPLACE ... SET rowid = (another row's rowid) deletes that other row without
    firing any trigger. Every mutable table is WITHOUT ROWID, so there is no rowid to set."""
    with db.transaction() as conn:
        _two_rows(conn, table)
    before = dump(db.connection, table)
    assert len(before) >= 2
    for alias in ("rowid", "oid", "_rowid_"):
        for verb in ("UPDATE", "UPDATE OR REPLACE"):
            # The first row takes the last row's place, and the last row the first row's.
            for pick, target in (("max", "min"), ("min", "max")):
                with pytest.raises(sqlite3.OperationalError), db.transaction() as conn:
                    conn.execute(
                        f'{verb} "{table}" SET {alias} = (SELECT {pick}({alias}) FROM "{table}")'  # noqa: S608
                        f' WHERE id = (SELECT {target}(id) FROM "{table}")'
                    )
                assert dump(db.connection, table) == before, (alias, verb, pick)


def test_a_leaf_document_version_cannot_be_replaced_through_its_rowid(docs: Database) -> None:
    with docs.transaction() as conn:
        insert(conn, "document_versions", doc_version_row("dv-leaf", "doc-letter", 2))
    before = dump(docs.connection, "document_versions")
    for alias in ("rowid", "oid", "_rowid_"):
        with pytest.raises(sqlite3.OperationalError), docs.transaction() as conn:
            conn.execute(
                f"UPDATE OR REPLACE document_versions SET {alias} = ("  # noqa: S608
                f" SELECT {alias} FROM document_versions WHERE id = 'dv-leaf')"
                " WHERE id = 'dv-letter-1'"
            )
        assert dump(docs.connection, "document_versions") == before, alias


def _draft_with_unit(conn: sqlite3.Connection) -> None:
    add_document(conn, "doc-cv", APP, "cv", "dv-cv-1")
    insert(conn, "document_units", unit_row("unit-0001", "dv-cv-1"))
    insert(conn, "document_unit_facts", cite_row("document_unit_id", "unit-0001"))
    insert(conn, "document_sent_facts", cite_row("document_version_id", "dv-cv-1"))


def _insert_with(conn: sqlite3.Connection, verb: str, table: str, row: dict[str, object]) -> None:
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f'{verb} INTO "{table}" ({columns}) VALUES ({marks})', tuple(row.values()))


@pytest.mark.parametrize("status", ["approved", "sent"])
def test_a_frozen_version_cannot_gain_units_citations_or_sent_facts(
    db: Database, status: str
) -> None:
    with db.transaction() as conn:
        _draft_with_unit(conn)
        advance(conn, "dv-cv-1", "approved")
        if status == "sent":
            advance(conn, "dv-cv-1", "sent")
    tables = ("document_units", "document_unit_facts", "document_sent_facts")
    before = {table: dump(db.connection, table) for table in tables}
    attempts = [
        ("document_units", unit_row("unit-0002", "dv-cv-1", 2)),
        ("document_unit_facts", cite_row("document_unit_id", "unit-0001", 2)),
        ("document_sent_facts", cite_row("document_version_id", "dv-cv-1", 2)),
    ]
    for table, row in attempts:
        for verb in ("INSERT", "INSERT OR REPLACE", "INSERT OR IGNORE"):
            with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
                _insert_with(conn, verb, table, row)
    assert {table: dump(db.connection, table) for table in tables} == before
    # Exports are the one thing a frozen version still gains.
    with db.transaction() as conn:
        insert(conn, "document_exports", export_row("exp-0009", "dv-cv-1", "later-export.pdf"))


def test_a_draft_version_still_gains_units_citations_and_sent_facts(db: Database) -> None:
    with db.transaction() as conn:
        _draft_with_unit(conn)
        insert(conn, "document_units", unit_row("unit-0002", "dv-cv-1", 2))
        insert(conn, "document_unit_facts", cite_row("document_unit_id", "unit-0002", 2))
        insert(conn, "document_sent_facts", cite_row("document_version_id", "dv-cv-1", 2))
    for table, count in (("document_units", 2), ("document_unit_facts", 2)):
        assert db.connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] == count  # noqa: S608
    assert db.connection.execute("SELECT count(*) FROM document_sent_facts").fetchone()[0] == 2


@pytest.mark.parametrize("status", ["approved", "sent"])
def test_a_version_can_only_be_inserted_as_a_draft(db: Database, status: str) -> None:
    with db.transaction() as conn:
        add_document(conn, "doc-cv", APP, "cv", "dv-cv-1")
    before = dump(db.connection, "document_versions")
    for verb in ("INSERT", "INSERT OR REPLACE"):
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
            _insert_with(
                conn,
                verb,
                "document_versions",
                doc_version_row("dv-cv-2", "doc-cv", 2, status=status),
            )
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        add_document(conn, "doc-direct", APP, "cover_letter", "dv-direct", status=status)
    assert dump(db.connection, "document_versions") == before


def test_export_names_and_hashes_refuse_embedded_nul_blobs_and_case_variants(
    docs: Database,
) -> None:
    with docs.transaction() as conn:
        insert(conn, "document_exports", export_row("exp-0001", "dv-cv-1", "export-0001.pdf"))
    before = dump(docs.connection, "document_exports")
    new = "export-0002.pdf"
    bad_rows = [
        export_row("exp-0002", "dv-cv-1", "export-0002\x00.pdf"),
        export_row("exp-0002", "dv-cv-1", "export-0002.pdf\x00"),
        export_row("exp-0002", "dv-cv-1", "export-0002.pdf\x00../../x"),
        export_row("exp-0002", "dv-cv-1", b"export-0002.pdf"),
        export_row("exp-0002", "dv-cv-1", new, sha256="a" * 64 + "\x00zz"),
        export_row("exp-0002", "dv-cv-1", new, sha256="a" * 63 + "\x00"),
        export_row("exp-0002", "dv-cv-1", new, sha256=("a" * 64).encode()),
        export_row("exp-0002", "dv-cv-1", "EXPORT-0001.PDF"),  # a case variant of a taken name
        export_row("exp-0002", "dv-cv-1", "Export-0001.pdf"),
        export_row("exp-0002", "dv-cv-1", "export-0002.docx"),  # extension is not the format
        export_row("exp-0002", "dv-cv-1", "export-0002.docx", format="pdf"),
        export_row("exp-0002", "dv-cv-1", new, format="docx"),
        export_row("exp-0002", "dv-cv-1", "export-0002.PDF"),  # extension is lowercase
        export_row("exp-0002", "dv-cv-1", "export-0002"),
        export_row("exp-0002", "dv-cv-1", "pdf"),
        export_row("exp-0002", "dv-cv-1", "export-0002.pdf.docx"),
    ]
    for row in bad_rows:
        for verb in ("INSERT", "INSERT OR REPLACE"):
            with pytest.raises(sqlite3.IntegrityError), docs.transaction() as conn:
                _insert_with(conn, verb, "document_exports", row)
    assert dump(docs.connection, "document_exports") == before
    with docs.transaction() as conn:
        insert(
            conn,
            "document_exports",
            export_row("exp-0003", "dv-cv-1", "Export-0003.docx", format="docx"),
        )


LETTER_CHOICES = [
    ("letter_tone", ("warm", "formal", "direct")),
    ("letter_length", ("short", "medium", "long")),
]
VERSION_CHOICES = [("tone", LETTER_CHOICES[0][1]), ("length", LETTER_CHOICES[1][1])]


@pytest.mark.parametrize(("column", "allowed"), LETTER_CHOICES)
def test_workspace_letter_tone_and_length_are_one_of_the_closed_set_or_null(
    docs: Database, column: str, allowed: tuple[str, ...]
) -> None:
    with docs.transaction() as conn:
        insert(conn, "workspaces", workspace_row())
    for good in (*allowed, None):
        with docs.transaction() as conn:
            conn.execute(f"UPDATE workspaces SET {column} = ? WHERE id = 'ws-0001'", (good,))  # noqa: S608
    for bad in ("", " ", allowed[0].upper(), "breezy", f"{allowed[0]} ", allowed[0].encode()):
        with pytest.raises(sqlite3.IntegrityError), docs.transaction() as conn:
            conn.execute(f"UPDATE workspaces SET {column} = ? WHERE id = 'ws-0001'", (bad,))  # noqa: S608


@pytest.mark.parametrize(("column", "allowed"), VERSION_CHOICES)
def test_version_tone_and_length_are_one_of_the_closed_set_or_null(
    docs: Database, column: str, allowed: tuple[str, ...]
) -> None:
    """A version's content is fixed at insert, so each value is a new version."""
    for number, good in enumerate((*allowed, None), start=3):
        with docs.transaction() as conn:
            row = doc_version_row(f"dv-cv-{number}", "doc-cv", number, **{column: good})
            insert(conn, "document_versions", row)
    for number, bad in enumerate(
        ("", " ", allowed[0].upper(), "breezy", f"{allowed[0]} ", allowed[0].encode()), start=10
    ):
        with pytest.raises(sqlite3.IntegrityError), docs.transaction() as conn:
            row = doc_version_row(f"dv-cv-{number}", "doc-cv", number, **{column: bad})
            insert(conn, "document_versions", row)


def test_new_rows_refuse_a_tone_or_length_outside_the_set(docs: Database) -> None:
    for extra in ({"letter_tone": "breezy"}, {"letter_length": "epic"}):
        with pytest.raises(sqlite3.IntegrityError), docs.transaction() as conn:
            insert(conn, "workspaces", workspace_row(**extra))
    for extra in ({"tone": "breezy"}, {"length": "epic"}):
        with pytest.raises(sqlite3.IntegrityError), docs.transaction() as conn:
            insert(conn, "document_versions", doc_version_row("dv-cv-3", "doc-cv", 3, **extra))
    with docs.transaction() as conn:
        insert(conn, "workspaces", workspace_row(letter_tone="warm", letter_length="short"))
        insert(
            conn,
            "document_versions",
            doc_version_row("dv-cv-3", "doc-cv", 3, tone="formal", length="long"),
        )


def test_update_workspace_writes_the_letter_tone_and_length_and_checks_the_version(
    docs: Database,
) -> None:
    repo = importlib.import_module("app.modules.storage.tailoring_repo")
    with docs.transaction() as conn:
        insert(conn, "workspaces", workspace_row())
    row = "SELECT cv_country, letter_tone, letter_length, updated_at, version FROM workspaces"

    def update(
        conn: sqlite3.Connection, tone: str | None, length: str | None, version: int
    ) -> None:
        repo.update_workspace(
            conn,
            "ws-0001",
            cv_country="DE",
            angle_id=None,
            letter_tone=tone,
            letter_length=length,
            now=LATER,
            expected_version=version,
        )

    with docs.transaction() as conn:
        update(conn, "formal", "short", 1)
    assert docs.connection.execute(row).fetchall() == [("DE", "formal", "short", LATER, 2)]
    with pytest.raises(VersionConflict), docs.transaction() as conn:  # stale: changes nothing
        update(conn, "warm", "long", 1)
    assert docs.connection.execute(row).fetchall() == [("DE", "formal", "short", LATER, 2)]
    with docs.transaction() as conn:  # both can be cleared
        update(conn, None, None, 2)
    assert docs.connection.execute(row).fetchall() == [("DE", None, None, LATER, 3)]
    with pytest.raises(sqlite3.IntegrityError), docs.transaction() as conn:
        update(conn, "breezy", "short", 3)
    assert docs.connection.execute(row).fetchall() == [("DE", None, None, LATER, 3)]
    # Leaving the two out leaves them as they are (the call shape without letter choices).
    with docs.transaction() as conn:
        update(conn, "direct", "medium", 3)
        repo.update_workspace(
            conn, "ws-0001", cv_country="PT", angle_id=None, now=LATER, expected_version=4
        )
    assert docs.connection.execute(row).fetchall() == [("PT", "direct", "medium", LATER, 5)]
