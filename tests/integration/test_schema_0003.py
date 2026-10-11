"""Migration 0003, the fact bank (M3.2; groundwork for AC-M3.1 to AC-M3.3 and AC-M3.6; spec 2.7,
3.1, 3.2, 3.5; ADR-027, ADR-038, ADR-089 items 5, 6, 8, 11).

0003 creates every table M3 needs, because a committed migration is never edited: ``roles``,
``facts``, ``fact_versions`` (append-only: triggers refuse UPDATE and DELETE), ``fact_events``,
``skills``, ``fact_skills``, ``capture_notes``, ``cv_imports``, ``refine_sessions``,
``fact_pair_dismissals`` and the FTS5 table ``facts_fts`` (the current version's text and story
parts, the role's employer and title and the skill names, kept in sync by triggers; a discarded
fact drops out). Mutable tables carry ``created_at``, ``updated_at`` and ``version`` (ADR-027);
tables aren't STRICT, so whole-number columns check ``typeof`` themselves, as in 0001 and 0002.

Two names to keep apart: ``facts.version`` is the row's optimistic-concurrency counter (spec
3.5), ``fact_versions.version`` is the content version number (the 3 in ``F12@3``), and
``facts.number`` is the short number in that reference (the 12).

Tests check behavior through SQL (inserts accepted or refused, searches) and the schema through
PRAGMAs, never through index or trigger names. Rows go in through ``Database.transaction()``,
because ``facts.current_version_id`` is a deferred foreign key: a fact and its first version
are written in one transaction, the fact first. Everyone and everything here is fictional.
"""

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

import pytest

from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations, load_migrations
from tests.fakes.clock import FrozenClock

pytestmark = pytest.mark.integration

START = datetime(2026, 3, 5, 9, 15, 0, tzinfo=UTC)
STAMP = "2026-03-05T09:15:00+00:00"
LATER = "2026-03-05T10:00:00+00:00"

MIGRATION_HASHES = Path(__file__).parents[1] / "fixtures" / "migration_hashes.json"

FACT_TYPES = ("fact", "story")
FACT_STATUSES = ("draft", "confirmed", "discarded", "archived")
PRIVACY_VALUES = ("normal", "never_use", "generalize")
ROLE_STATUSES = ("draft", "confirmed")
IMPORT_STATES = ("uploaded", "done")
IMPORT_KINDS = ("pdf", "docx", "text")  # app.modules.extract.KINDS
REFINE_STATES = ("open", "accepted", "discarded")

MUTABLE_TABLES = ("roles", "facts", "skills", "capture_notes", "cv_imports", "refine_sessions")

EXPECTED_COLUMNS: dict[str, set[str]] = {
    "roles": {
        "id",
        "employer",
        "title",
        "start_date",
        "end_date",
        "status",
        "created_at",
        "updated_at",
        "version",
    },
    "facts": {
        "id",
        "number",
        "type",
        "status",
        "role_id",
        "privacy",
        "tags",
        "start_date",
        "end_date",
        "current_version_id",
        "merged_into_fact_id",
        "cv_import_id",
        "created_at",
        "updated_at",
        "version",
    },
    # Append-only: created_at only. "version" is the content version number, not a row counter.
    "fact_versions": {
        "id",
        "fact_id",
        "version",
        "text",
        "situation",
        "action",
        "result",
        "source",
        "original_answer",
        "provider",
        "model",
        "prompt_id",
        "prompt_version",
        "needs_check",
        "created_at",
    },
    "fact_events": {"id", "fact_id", "type", "fact_version_id", "detail", "created_at"},
    "skills": {
        "id",
        "name",
        "normalized_name",
        "aliases",
        "created_at",
        "updated_at",
        "version",
    },
    "fact_skills": {"fact_id", "skill_id"},
    "capture_notes": {
        "id",
        "text",
        "resolved_fact_id",
        "deleted_at",
        "created_at",
        "updated_at",
        "version",
    },
    "cv_imports": {
        "id",
        "kind",
        "stored_name",
        "state",
        "task_id",
        "created_at",
        "updated_at",
        "version",
    },
    "refine_sessions": {
        "id",
        "fact_id",
        "base_version_id",
        "state",
        "answer_rounds",
        "turns",
        "suggestion",
        "flagged_claims",
        "acknowledged_claims",
        "provider",
        "model",
        "prompt_id",
        "prompt_version",
        "task_id",
        "created_at",
        "updated_at",
        "version",
    },
    "fact_pair_dismissals": {"fact_a_id", "fact_b_id", "created_at"},
}

TABLES_AT_0002 = {
    "schema_migrations",
    "settings",
    "setup_state",
    "tasks",
    "usage_records",
    "provider_usage_snapshots",
    "budget_notices",
}


# --- helpers ----------------------------------------------------------------


def _migration_0003() -> Path:
    found = [m.path for m in load_migrations(MIGRATIONS_DIR) if m.version == 3]
    assert found, "migration 0003 is missing from the migrations folder"
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
    _migration_0003()
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


class IndexInfo(NamedTuple):
    name: str
    unique: int
    partial: int
    columns: list[str]


def indexes(conn: sqlite3.Connection, table: str) -> list[IndexInfo]:
    found = []
    for row in conn.execute(f'PRAGMA index_list("{table}")').fetchall():
        name = row[1]
        keys = [info[2] for info in conn.execute(f'PRAGMA index_info("{name}")').fetchall()]
        found.append(IndexInfo(name=name, unique=row[2], partial=row[4], columns=keys))
    return found


def insert(conn: sqlite3.Connection, table: str, row: dict[str, object]) -> None:
    names = ", ".join(f'"{name}"' for name in row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})", tuple(row.values()))  # noqa: S608


def count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # noqa: S608


def role_row(role_id: str = "role-0001", **extra: object) -> dict[str, object]:
    return {
        "id": role_id,
        "employer": "Northwind Logistics",
        "title": "Operations Analyst",
        "start_date": "2019-03",
        "end_date": "2022-11",
        "status": "confirmed",
        "created_at": STAMP,
        "updated_at": STAMP,
        **extra,
    }


def fact_row(fact_id: str, number: int, version_id: str, **extra: object) -> dict[str, object]:
    return {
        "id": fact_id,
        "number": number,
        "type": "fact",
        "status": "confirmed",
        "privacy": "normal",
        "current_version_id": version_id,
        "created_at": STAMP,
        "updated_at": STAMP,
        **extra,
    }


def version_row(
    version_id: str, fact_id: str, number: int = 1, **extra: object
) -> dict[str, object]:
    return {
        "id": version_id,
        "fact_id": fact_id,
        "version": number,
        "text": "Cut the invoice backlog by a third",
        "source": "manual",
        "created_at": STAMP,
        **extra,
    }


def skill_row(skill_id: str, name: str, **extra: object) -> dict[str, object]:
    return {
        "id": skill_id,
        "name": name,
        "normalized_name": name.casefold(),
        "created_at": STAMP,
        "updated_at": STAMP,
        **extra,
    }


def task_row(task_id: str) -> dict[str, object]:
    return {
        "id": task_id,
        "type": "extract_cv",
        "status": "succeeded",
        "dedupe_key": f"cv_import:{task_id}",
        "created_at": STAMP,
        "updated_at": STAMP,
    }


def import_row(import_id: str, **extra: object) -> dict[str, object]:
    return {
        "id": import_id,
        "kind": "pdf",
        "stored_name": f"{import_id}.pdf",
        "state": "uploaded",
        "created_at": STAMP,
        "updated_at": STAMP,
        **extra,
    }


def note_row(note_id: str, **extra: object) -> dict[str, object]:
    return {
        "id": note_id,
        "text": "Remember the warehouse relabelling weekend",
        "created_at": STAMP,
        "updated_at": STAMP,
        **extra,
    }


def refine_row(session_id: str, **extra: object) -> dict[str, object]:
    return {
        "id": session_id,
        "fact_id": "fact-0001",
        "base_version_id": "ver-0001",
        "state": "open",
        "created_at": STAMP,
        "updated_at": STAMP,
        **extra,
    }


def event_row(event_id: str, **extra: object) -> dict[str, object]:
    return {
        "id": event_id,
        "fact_id": "fact-0001",
        "type": "confirmed",
        "fact_version_id": "ver-0001",
        "created_at": STAMP,
        **extra,
    }


def add_fact(
    db: Database,
    fact_id: str,
    number: int,
    version_id: str,
    *,
    fact: dict[str, object] | None = None,
    version: dict[str, object] | None = None,
) -> None:
    """A fact and its first version in one transaction, the fact first."""
    with db.transaction() as conn:
        insert(conn, "facts", {**fact_row(fact_id, number, version_id), **(fact or {})})
        insert(conn, "fact_versions", {**version_row(version_id, fact_id), **(version or {})})


def add_version(db: Database, version_id: str, fact_id: str, number: int, **extra: object) -> None:
    """A later version, made current the way an edit does it."""
    with db.transaction() as conn:
        insert(conn, "fact_versions", version_row(version_id, fact_id, number, **extra))
        conn.execute(
            "UPDATE facts SET current_version_id = ?, updated_at = ?, version = version + 1"
            " WHERE id = ?",
            (version_id, LATER, fact_id),
        )


def seed(db: Database) -> None:
    """One of everything the other rows point at: role-0001, task-0001, imp-0001, skill-0001
    and the confirmed fact-0001 (number 1) with its version ver-0001."""
    with db.transaction() as conn:
        insert(conn, "roles", role_row())
        insert(conn, "tasks", task_row("task-0001"))
        insert(conn, "cv_imports", import_row("imp-0001"))
        insert(conn, "skills", skill_row("skill-0001", "Forecasting"))
    add_fact(db, "fact-0001", 1, "ver-0001", fact={"role_id": "role-0001"})


def put(db: Database, table: str, **extra: object) -> None:
    """Insert one more valid row into ``table`` (after ``seed``), with ``extra`` overriding
    columns. Everything happens in one transaction, so a refused row leaves nothing behind."""
    if table == "facts":
        add_fact(db, "fact-0002", 2, "ver-0002", fact=extra)
        return
    rows: dict[str, Callable[[], dict[str, object]]] = {
        "roles": lambda: role_row("role-0002"),
        "fact_versions": lambda: version_row("ver-0002", "fact-0001", 2),
        "fact_events": lambda: event_row("event-0001"),
        "skills": lambda: skill_row("skill-0002", "Scheduling"),
        "fact_skills": lambda: {"fact_id": "fact-0001", "skill_id": "skill-0001"},
        "capture_notes": lambda: note_row("note-0001"),
        "cv_imports": lambda: import_row("imp-0002"),
        "refine_sessions": lambda: refine_row("refine-0001"),
    }
    with db.transaction() as conn:
        insert(conn, table, {**rows[table](), **extra})


def refused(db: Database, table: str, **extra: object) -> None:
    before = count(db.connection, table)
    with pytest.raises(sqlite3.IntegrityError):
        put(db, table, **extra)
    assert count(db.connection, table) == before, f"a refused {table} row was stored"


def replace_refused(db: Database, table: str, row: dict[str, object]) -> None:
    """``INSERT OR REPLACE`` of ``row`` is refused and leaves ``table`` exactly as it was."""
    before = sorted(db.connection.execute(f"SELECT * FROM {table}").fetchall())  # noqa: S608
    names = ", ".join(f'"{name}"' for name in row)
    marks = ", ".join("?" for _ in row)
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO {table} ({names}) VALUES ({marks})",  # noqa: S608
            tuple(row.values()),
        )
    after = sorted(db.connection.execute(f"SELECT * FROM {table}").fetchall())  # noqa: S608
    assert after == before, f"INSERT OR REPLACE changed {table}"


def fts_entries(db: Database) -> list[tuple[object, ...]]:
    rows = db.connection.execute(
        "SELECT rowid, fact_id, text, situation, action, result, role, skills"
        " FROM facts_fts ORDER BY rowid"
    ).fetchall()
    return [tuple(row) for row in rows]


def search(db: Database, word: str) -> set[str]:
    """Ids of the facts whose search entry holds ``word`` (quoted, so it is one literal token)."""
    quoted = '"' + word.replace('"', '""') + '"'
    rows = db.connection.execute(
        "SELECT fact_id FROM facts_fts WHERE facts_fts MATCH ?", (quoted,)
    ).fetchall()
    return {row[0] for row in rows}


# --- upgrade from 0002 ------------------------------------------------------


def test_0003_upgrade_keeps_0002_rows_and_backs_up(tmp_path: Path) -> None:
    _migration_0003()
    backups = tmp_path / "backups"
    with Database.open(tmp_path / "data" / "app.db") as database:
        apply_migrations(database, _folder_up_to(tmp_path, 2), backups, FrozenClock(START))
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO settings (key, value, created_at, updated_at)"
                " VALUES ('ui.theme', '\"dark\"', ?, ?)",
                (STAMP, STAMP),
            )
            conn.execute(
                "INSERT INTO setup_state (step_id, status, completed_at, created_at, updated_at)"
                " VALUES ('welcome', 'done', ?, ?, ?)",
                (STAMP, STAMP, STAMP),
            )
            insert(conn, "tasks", task_row("task-0001"))
            insert(
                conn,
                "usage_records",
                {
                    "id": "usage-0001",
                    "provider": "fake",
                    "task_type": "practice",
                    "service": "fake",
                    "task_id": "task-0001",
                    "units": 1,
                    "success": 1,
                    "requested_at": STAMP,
                    "created_at": STAMP,
                    "updated_at": STAMP,
                },
            )
            insert(
                conn,
                "provider_usage_snapshots",
                {
                    "id": "snap-0001",
                    "service": "fake",
                    "window": "month",
                    "unit": "requests",
                    "used": 12,
                    "observed_at": STAMP,
                    "created_at": STAMP,
                    "updated_at": STAMP,
                },
            )
            insert(
                conn,
                "budget_notices",
                {
                    "id": "notice-0001",
                    "service": "fake",
                    "cycle_start": "2026-03-01",
                    "threshold": "first",
                    "shown_at": STAMP,
                },
            )
        before = {
            table: sorted(database.connection.execute(f"SELECT * FROM {table}").fetchall())  # noqa: S608
            for table in sorted(TABLES_AT_0002 - {"schema_migrations"})
        }
        assert all(before.values()), "seed one row in every 0002 table"

        later = FrozenClock(datetime(2026, 3, 6, 8, 0, 0, tzinfo=UTC))
        result = apply_migrations(database, MIGRATIONS_DIR, backups, later)
        conn = database.connection

        assert 3 in result.applied
        assert result.backup is not None
        assert result.backup.name == "app-20260306T080000Z-before-0003.db"
        assert result.backup.is_file()

        for table, rows in before.items():
            after = sorted(conn.execute(f"SELECT * FROM {table}").fetchall())  # noqa: S608
            assert after == rows, f"{table} rows changed during the upgrade"

        # The skills list starts empty (ADR-089 item 11), like every other new table.
        for table in EXPECTED_COLUMNS:
            assert count(conn, table) == 0, f"0003 must not seed {table}"
        assert count(conn, "facts_fts") == 0

    with sqlite3.connect(result.backup) as copy:
        assert [r[0] for r in copy.execute("SELECT version FROM schema_migrations")] == [1, 2]
        assert copy.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        names = {r[0] for r in copy.execute("SELECT name FROM sqlite_master")}
        assert "facts" not in names


def test_0003_adds_exactly_the_fact_bank_tables(tmp_path: Path) -> None:
    # Up to 0003 only, so later migrations adding tables don't break this check.
    _migration_0003()
    with Database.open(tmp_path / "data" / "app.db") as database:
        apply_migrations(
            database, _folder_up_to(tmp_path, 3), tmp_path / "backups", FrozenClock(START)
        )
        rows = database.connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    # FTS5 keeps its own shadow tables (facts_fts_data, facts_fts_idx, ...).
    names = {row[0] for row in rows if not row[0].startswith("facts_fts_")}
    assert names == TABLES_AT_0002 | set(EXPECTED_COLUMNS) | {"facts_fts"}
    fts_sql = next(row[1] for row in rows if row[0] == "facts_fts")
    assert "fts5" in fts_sql.lower()


# --- shape ------------------------------------------------------------------


@pytest.mark.parametrize("table", sorted(EXPECTED_COLUMNS))
def test_table_has_exactly_the_columns_later_tasks_rely_on(db: Database, table: str) -> None:
    # M3.3 to M3.9 read and write these columns; a committed migration is never edited.
    assert set(columns(db.connection, table)) == EXPECTED_COLUMNS[table]


def test_columns_later_tasks_rely_on_have_the_expected_nullability(db: Database) -> None:
    conn = db.connection
    expected: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
        # table: (NOT NULL columns, columns that allow NULL)
        "facts": (
            ("number", "type", "status", "privacy", "tags", "current_version_id"),
            ("role_id", "start_date", "end_date", "merged_into_fact_id", "cv_import_id"),
        ),
        "fact_versions": (
            ("fact_id", "version", "source", "needs_check", "created_at"),
            (
                "text",
                "situation",
                "action",
                "result",
                "original_answer",
                # A fact the user types has no provider, model or prompt (ADR-089 item 8).
                "provider",
                "model",
                "prompt_id",
                "prompt_version",
            ),
        ),
        "fact_events": (("fact_id", "type", "created_at"), ("fact_version_id", "detail")),
        "skills": (("name", "normalized_name", "aliases"), ()),
        "capture_notes": (("text",), ("resolved_fact_id", "deleted_at")),
        "cv_imports": (("kind", "stored_name", "state"), ("task_id",)),
        "refine_sessions": (
            (
                "fact_id",
                "base_version_id",
                "state",
                "answer_rounds",
                "turns",
                "flagged_claims",
                "acknowledged_claims",
            ),
            ("suggestion", "provider", "model", "prompt_id", "prompt_version", "task_id"),
        ),
        "fact_pair_dismissals": (("fact_a_id", "fact_b_id", "created_at"), ()),
    }
    for table, (required, optional) in expected.items():
        found = columns(conn, table)
        for name in required:
            assert found[name]["notnull"] == 1, f"{table}.{name} must be NOT NULL"
        for name in optional:
            assert found[name]["notnull"] == 0, f"{table}.{name} must allow NULL"
    for table, name in (
        ("facts", "number"),
        ("fact_versions", "version"),
        ("fact_versions", "needs_check"),
        ("refine_sessions", "answer_rounds"),
    ):
        assert columns(conn, table)[name]["type"] == "INTEGER", f"{table}.{name}"


@pytest.mark.parametrize("table", MUTABLE_TABLES)
def test_0003_mutable_tables_have_created_updated_version(db: Database, table: str) -> None:
    found = columns(db.connection, table)
    for name in ("created_at", "updated_at", "version"):
        assert name in found, f"{table} lacks {name}"
        assert found[name]["notnull"] == 1, f"{table}.{name} must be NOT NULL"
    assert found["version"]["type"] == "INTEGER"
    assert str(found["version"]["default"]) == "1"


def test_new_rows_get_the_documented_defaults(db: Database) -> None:
    seed(db)
    put(db, "refine_sessions")
    conn = db.connection
    fact = conn.execute("SELECT tags, version FROM facts WHERE id = 'fact-0001'").fetchone()
    assert tuple(fact) == ("[]", 1)
    assert conn.execute("SELECT needs_check FROM fact_versions").fetchone() == (0,)
    assert conn.execute("SELECT aliases, version FROM skills").fetchone() == ("[]", 1)
    session = conn.execute(
        "SELECT answer_rounds, turns, flagged_claims, acknowledged_claims, suggestion"
        " FROM refine_sessions"
    ).fetchone()
    assert tuple(session) == (0, "[]", "[]", "[]", None)


WHOLE_NUMBER_COLUMNS = [
    ("roles", "version"),
    ("facts", "version"),
    ("facts", "number"),
    ("fact_versions", "version"),
    ("fact_versions", "needs_check"),
    ("skills", "version"),
    ("capture_notes", "version"),
    ("cv_imports", "version"),
    ("refine_sessions", "version"),
    ("refine_sessions", "answer_rounds"),
]


@pytest.mark.parametrize(
    ("table", "column"),
    WHOLE_NUMBER_COLUMNS,
    ids=[f"{table}.{column}" for table, column in WHOLE_NUMBER_COLUMNS],
)
@pytest.mark.parametrize("bad", ["abc", 1.5, -1], ids=["text", "fraction", "negative"])
def test_0003_whole_number_columns_reject_text_fractions_and_negatives(
    db: Database, table: str, column: str, bad: object
) -> None:
    seed(db)
    refused(db, table, **{column: bad})


@pytest.mark.parametrize(
    ("table", "leading"),
    [
        ("facts", ["status"]),
        ("facts", ["role_id"]),
        ("facts", ["cv_import_id"]),
        ("fact_skills", ["skill_id"]),
        ("fact_events", ["fact_id"]),
        ("fact_versions", ["fact_id", "version"]),
    ],
    ids=[
        "facts.status",
        "facts.role",
        "facts.import",
        "fact_skills.skill",
        "fact_events.fact",
        "fact_versions.fact_version",
    ],
)
def test_0003_indexes_for_list_filters_exist(db: Database, table: str, leading: list[str]) -> None:
    found = indexes(db.connection, table)
    assert any(
        index.columns[: len(leading)] == leading and index.partial == 0 for index in found
    ), f"no full index on {table}({', '.join(leading)}); found {found}"


# --- enumerations -----------------------------------------------------------

ENUMERATIONS: list[tuple[str, str, tuple[str, ...]]] = [
    ("facts", "status", FACT_STATUSES),
    ("facts", "privacy", PRIVACY_VALUES),
    ("facts", "type", FACT_TYPES),
    ("roles", "status", ROLE_STATUSES),
    ("cv_imports", "state", IMPORT_STATES),
    ("cv_imports", "kind", IMPORT_KINDS),
    ("refine_sessions", "state", REFINE_STATES),
]


@pytest.mark.parametrize(
    ("table", "column", "allowed"),
    ENUMERATIONS,
    ids=[f"{table}.{column}" for table, column, _ in ENUMERATIONS],
)
def test_status_and_privacy_checks_reject_unknown_values(
    db: Database, table: str, column: str, allowed: tuple[str, ...]
) -> None:
    seed(db)
    for bad in ("approved", "deleted", allowed[0].upper(), f" {allowed[0]}", "", None):
        refused(db, table, **{column: bad})

    # Every allowed value is stored as given, by an update of one row.
    put(db, table, **{column: allowed[0]})
    key = {"facts": "fact-0002", "roles": "role-0002", "cv_imports": "imp-0002"}.get(
        table, "refine-0001"
    )
    with db.transaction() as conn:
        for value in allowed:
            conn.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (value, key))  # noqa: S608
            stored = conn.execute(f"SELECT {column} FROM {table} WHERE id = ?", (key,)).fetchone()  # noqa: S608
            assert stored == (value,)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"UPDATE {table} SET {column} = 'unknown' WHERE id = ?", (key,))  # noqa: S608


# --- foreign keys -----------------------------------------------------------

UNKNOWN_PARENTS: list[tuple[str, dict[str, object]]] = [
    ("facts", {"role_id": "role-9999"}),
    ("facts", {"cv_import_id": "imp-9999"}),
    ("facts", {"merged_into_fact_id": "fact-9999"}),
    ("fact_versions", {"fact_id": "fact-9999"}),
    ("fact_events", {"fact_id": "fact-9999"}),
    ("fact_events", {"fact_version_id": "ver-9999"}),
    ("fact_skills", {"fact_id": "fact-9999"}),
    ("fact_skills", {"skill_id": "skill-9999"}),
    ("capture_notes", {"resolved_fact_id": "fact-9999"}),
    ("cv_imports", {"task_id": "task-9999"}),
    ("refine_sessions", {"fact_id": "fact-9999"}),
    ("refine_sessions", {"base_version_id": "ver-9999"}),
    ("refine_sessions", {"task_id": "task-9999"}),
]


@pytest.mark.parametrize(
    ("table", "link"),
    UNKNOWN_PARENTS,
    ids=[f"{table}.{next(iter(link))}" for table, link in UNKNOWN_PARENTS],
)
def test_foreign_keys_refuse_unknown_parents(
    db: Database, table: str, link: dict[str, object]
) -> None:
    assert db.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    seed(db)
    refused(db, table, **link)
    # The same row with a real parent is accepted.
    known = {
        "role_id": "role-0001",
        "cv_import_id": "imp-0001",
        "merged_into_fact_id": "fact-0001",
        "fact_id": "fact-0001",
        "fact_version_id": "ver-0001",
        "skill_id": "skill-0001",
        "resolved_fact_id": "fact-0001",
        "task_id": "task-0001",
        "base_version_id": "ver-0001",
    }
    name = next(iter(link))
    # A fact that was merged into another one is closed (M3.3), so store it as discarded.
    closed: dict[str, object] = {"status": "discarded"} if name == "merged_into_fact_id" else {}
    put(db, table, **{name: known[name]}, **closed)


def test_fact_and_first_version_are_written_in_one_transaction(db: Database) -> None:
    # facts.current_version_id is a deferred foreign key: the fact row may name a version that
    # is inserted later in the same transaction, and the commit checks it.
    add_fact(db, "fact-0001", 1, "ver-0001")
    conn = db.connection
    assert conn.execute("SELECT current_version_id FROM facts").fetchone() == ("ver-0001",)

    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        insert(conn, "facts", fact_row("fact-0002", 2, "ver-9999"))
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        insert(conn, "facts", fact_row("fact-0003", 3, "ver-0003", current_version_id=None))
    assert count(db.connection, "facts") == 1


def test_deleting_a_parent_never_deletes_facts_or_their_history(db: Database) -> None:
    seed(db)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE facts SET cv_import_id = 'imp-0001' WHERE id = 'fact-0001'",
        )
        conn.execute("UPDATE cv_imports SET task_id = 'task-0001' WHERE id = 'imp-0001'")
        insert(conn, "refine_sessions", refine_row("refine-0001", task_id="task-0001"))
        insert(conn, "fact_events", event_row("event-0001"))

    # A role or a fact that is still referenced can't be deleted.
    for statement in (
        "DELETE FROM roles WHERE id = 'role-0001'",
        "DELETE FROM facts WHERE id = 'fact-0001'",
    ):
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
            conn.execute(statement)

    # Removing an import (M3.4) or a task only clears the link.
    with db.transaction() as conn:
        conn.execute("DELETE FROM tasks WHERE id = 'task-0001'")
        conn.execute("DELETE FROM cv_imports WHERE id = 'imp-0001'")
    conn = db.connection
    assert conn.execute("SELECT cv_import_id, role_id FROM facts").fetchone() == (
        None,
        "role-0001",
    )
    assert conn.execute("SELECT task_id FROM refine_sessions").fetchone() == (None,)
    assert count(conn, "fact_versions") == 1
    assert count(conn, "fact_events") == 1


@pytest.mark.parametrize(
    ("table", "column"),
    [("cv_imports", "task_id"), ("refine_sessions", "task_id"), ("facts", "cv_import_id")],
)
def test_optional_links_are_set_null_on_delete(db: Database, table: str, column: str) -> None:
    links = db.connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
    matching = [row for row in links if row[3] == column]
    assert len(matching) == 1, f"{table}.{column} needs one declared foreign key"
    assert matching[0][6] == "SET NULL"


# --- roles ------------------------------------------------------------------


def test_role_needs_an_employer_or_a_title(db: Database) -> None:
    seed(db)
    refused(db, "roles", employer="", title="  ")
    put(db, "roles", employer="", title="Volunteer Treasurer")
    put(db, "roles", id="role-0003", employer="Harbour Street Bakery", title="")


DATE_COLUMNS = [
    ("roles", "start_date"),
    ("roles", "end_date"),
    ("facts", "start_date"),
    ("facts", "end_date"),
]


@pytest.mark.parametrize(("table", "column"), DATE_COLUMNS)
def test_dates_are_a_year_a_month_or_a_day(db: Database, table: str, column: str) -> None:
    # CVs often give only a year or a month, so dates are 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD'
    # text (sortable), or NULL when unknown or still current.
    seed(db)
    for bad in ("March 2019", "", "2019-3", "19", "2019/03", "2019-03-15T00:00:00", "2019-03 "):
        refused(db, table, **{column: bad})
    key = "fact-0002" if table == "facts" else "role-0002"
    put(db, table, **{column: "2019"})
    with db.transaction() as conn:
        for good in ("2019-03", "2019-03-15", None):
            conn.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (good, key))  # noqa: S608


# --- facts ------------------------------------------------------------------


def test_fact_number_is_unique_and_positive(db: Database) -> None:
    # The number is the 12 in F12@3: short, stable and never shared by two facts.
    seed(db)
    refused(db, "facts", number=1)
    refused(db, "facts", number=0)
    refused(db, "facts", number=None)
    put(db, "facts", number=2)


def test_fact_tags_are_a_json_array(db: Database) -> None:
    seed(db)
    for bad in ("not json", '{"a": 1}', '"metrics"', "", None):
        refused(db, "facts", tags=bad)
    put(db, "facts", tags='["metrics", "finance"]')


def test_fact_cannot_be_merged_into_itself(db: Database) -> None:
    seed(db)
    refused(db, "facts", merged_into_fact_id="fact-0002")
    put(db, "facts", status="discarded", merged_into_fact_id="fact-0001")


def test_discarded_fact_cannot_be_merged_into_itself(db: Database) -> None:
    # The same check on a row that is otherwise a valid merged draft.
    seed(db)
    refused(db, "facts", status="discarded", merged_into_fact_id="fact-0002")


def test_fact_number_never_changes(db: Database) -> None:
    # Answers already given cite F<number>@<version>, and the search entry is keyed by it.
    seed(db)
    add_fact(db, "fact-0002", 2, "ver-0002", version={"text": "Trained four new cashiers"})
    entries = fts_entries(db)
    for statement in (
        "UPDATE facts SET number = 9 WHERE id = 'fact-0001'",
        "UPDATE facts SET number = number + 10",
        "UPDATE OR REPLACE facts SET number = 2 WHERE id = 'fact-0001'",
        "UPDATE facts SET number = 9, status = 'archived' WHERE id = 'fact-0002'",
    ):
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
            conn.execute(statement)
    conn = db.connection
    assert conn.execute("SELECT id, number, status FROM facts ORDER BY id").fetchall() == [
        ("fact-0001", 1, "confirmed"),
        ("fact-0002", 2, "confirmed"),
    ]
    assert fts_entries(db) == entries
    # Writing the same number back, as a whole-row UPDATE does, is not a change.
    with db.transaction() as conn:
        conn.execute("UPDATE facts SET number = 1, updated_at = ? WHERE id = 'fact-0001'", (LATER,))


def test_fact_current_version_must_be_a_version_of_the_same_fact(db: Database) -> None:
    seed(db)
    add_fact(db, "fact-0002", 2, "ver-0002", version={"text": "Trained four new cashiers"})
    entries = fts_entries(db)

    # The key is deferred, so the statement runs and the commit refuses it.
    reached = []
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute("UPDATE facts SET current_version_id = 'ver-0002' WHERE id = 'fact-0001'")
        reached.append("update")
    # A new fact with a version of its own that names another fact's version as current.
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        insert(conn, "facts", fact_row("fact-0003", 3, "ver-0001"))
        insert(conn, "fact_versions", version_row("ver-0003", "fact-0003"))
        reached.append("insert")
    assert reached == ["update", "insert"]

    conn = db.connection
    assert conn.execute("SELECT id, current_version_id FROM facts ORDER BY id").fetchall() == [
        ("fact-0001", "ver-0001"),
        ("fact-0002", "ver-0002"),
    ]
    assert count(conn, "fact_versions") == 2
    assert fts_entries(db) == entries


def test_merged_fact_is_discarded_and_merge_undo_restores_the_draft(db: Database) -> None:
    # A draft merged into another fact is closed as discarded (M3.3); the link never sits on a
    # fact that is still a draft, confirmed or archived.
    seed(db)
    for status in ("draft", "confirmed", "archived"):
        refused(db, "facts", status=status, merged_into_fact_id="fact-0001")
    add_fact(
        db,
        "fact-0002",
        2,
        "ver-0002",
        fact={"status": "draft"},
        version={"text": "Trained four new cashiers"},
    )
    assert search(db, "cashiers") == {"fact-0002"}

    def update(sql: str) -> None:
        with db.transaction() as conn:
            conn.execute(sql)

    with pytest.raises(sqlite3.IntegrityError):
        update("UPDATE facts SET merged_into_fact_id = 'fact-0001' WHERE id = 'fact-0002'")

    # Merge: one UPDATE closes the draft and records where it went; search drops it.
    update(
        "UPDATE facts SET status = 'discarded', merged_into_fact_id = 'fact-0001',"
        " version = version + 1 WHERE id = 'fact-0002'"
    )
    assert search(db, "cashiers") == set()
    assert count(db.connection, "facts_fts") == 1

    # Bringing it back while it still says it was merged is refused.
    for status in ("draft", "confirmed", "archived"):
        with pytest.raises(sqlite3.IntegrityError):
            update(f"UPDATE facts SET status = '{status}' WHERE id = 'fact-0002'")  # noqa: S608
    assert search(db, "cashiers") == set()

    # Merge undo: one UPDATE clears the link and restores the draft; search has it again.
    update(
        "UPDATE facts SET status = 'draft', merged_into_fact_id = NULL,"
        " version = version + 1 WHERE id = 'fact-0002'"
    )
    row = db.connection.execute(
        "SELECT status, merged_into_fact_id, version FROM facts WHERE id = 'fact-0002'"
    ).fetchone()
    assert tuple(row) == ("draft", None, 3)
    assert search(db, "cashiers") == {"fact-0002"}
    assert count(db.connection, "facts_fts") == 2


def test_insert_or_replace_cannot_rewrite_or_renumber_a_fact(db: Database) -> None:
    # REPLACE deletes the old row without firing delete triggers, so it would slip past
    # "facts.number never changes"; an insert guard refuses it.
    seed(db)
    add_fact(db, "fact-0002", 2, "ver-0002", version={"text": "Trained four new cashiers"})
    entries = fts_entries(db)

    # Same id with another number, same id with other data, and another id taking a number.
    replace_refused(db, "facts", fact_row("fact-0001", 99, "ver-0001", role_id="role-0001"))
    replace_refused(db, "facts", fact_row("fact-0001", 1, "ver-0001", status="discarded"))
    replace_refused(db, "facts", fact_row("fact-0002", 1, "ver-0002"))
    replace_refused(db, "facts", fact_row("fact-0009", 2, "ver-0002"))

    conn = db.connection
    assert conn.execute("SELECT id, number FROM facts ORDER BY id").fetchall() == [
        ("fact-0001", 1),
        ("fact-0002", 2),
    ]
    assert fts_entries(db) == entries
    assert search(db, "backlog") == {"fact-0001"}

    # A number is taken as soon as its fact row is written, before the first version exists
    # (the one moment a fact has nothing pointing at it yet).
    with pytest.raises(sqlite3.IntegrityError, match="replaced"), db.transaction() as conn:
        insert(conn, "facts", fact_row("fact-0003", 3, "ver-0003"))
        conn.execute(
            "INSERT OR REPLACE INTO facts"
            " (id, number, type, status, current_version_id, created_at, updated_at)"
            " VALUES ('fact-0009', 3, 'fact', 'draft', 'ver-0009', ?, ?)",
            (STAMP, STAMP),
        )
        insert(conn, "fact_versions", version_row("ver-0009", "fact-0009"))
    assert count(db.connection, "facts") == 2

    # Plain inserts still work, with or without OR REPLACE when nothing is replaced.
    add_fact(db, "fact-0003", 3, "ver-0003")
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO facts"
            " (id, number, type, status, current_version_id, created_at, updated_at)"
            " VALUES ('fact-0004', 4, 'fact', 'draft', 'ver-0004', ?, ?)",
            (STAMP, STAMP),
        )
        insert(conn, "fact_versions", version_row("ver-0004", "fact-0004"))
    assert count(db.connection, "facts") == 4
    assert count(db.connection, "facts_fts") == 4


# --- fact_versions ----------------------------------------------------------


def test_insert_or_replace_cannot_rewrite_a_fact_version(db: Database) -> None:
    # REPLACE deletes the old row without firing "never deleted", so an insert guard refuses
    # a row that would take the place of an existing one.
    seed(db)
    put(db, "fact_versions", text="Cut the invoice backlog by half")  # ver-0002, not current
    entries = fts_entries(db)

    # The same id (the current version, then the other one).
    replace_refused(
        db, "fact_versions", version_row("ver-0001", "fact-0001", 1, text="Rewrote history")
    )
    replace_refused(
        db, "fact_versions", version_row("ver-0002", "fact-0001", 2, text="Rewrote history")
    )
    replace_refused(
        db, "fact_versions", version_row("ver-0002", "fact-0001", 7, text="Rewrote history")
    )
    # A new id taking an existing (fact, version number).
    replace_refused(
        db, "fact_versions", version_row("ver-0009", "fact-0001", 2, text="Rewrote history")
    )
    replace_refused(
        db, "fact_versions", version_row("ver-0009", "fact-0001", 1, text="Rewrote history")
    )

    assert fts_entries(db) == entries
    assert search(db, "third") == {"fact-0001"}
    assert search(db, "history") == set()
    assert db.connection.execute("SELECT number FROM facts").fetchall() == [(1,)]

    # Appending still works, with or without OR REPLACE when nothing is replaced.
    put(db, "fact_versions", id="ver-0003", version=3)
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO fact_versions (id, fact_id, version, text, source, created_at)"
            " VALUES ('ver-0004', 'fact-0001', 4, 'Cleared the invoice backlog', 'manual', ?)",
            (STAMP,),
        )
    assert count(db.connection, "fact_versions") == 4


def test_fact_versions_refuse_update_and_delete(db: Database) -> None:
    seed(db)
    put(
        db,
        "fact_versions",
        provider="fake",
        model="fake-thorough",
        prompt_id="extract_cv",
        prompt_version="1",
        needs_check=1,
        source="cv_import",
        original_answer="Cut invoice backlog 30%",
    )
    conn = db.connection
    before = sorted(conn.execute("SELECT * FROM fact_versions").fetchall())
    assert len(before) == 2

    statements = [
        "UPDATE fact_versions SET text = 'Cut the invoice backlog by half' WHERE id = 'ver-0001'",
        "UPDATE fact_versions SET situation = 'A busy quarter' WHERE id = 'ver-0001'",
        "UPDATE fact_versions SET needs_check = 0 WHERE id = 'ver-0002'",
        "UPDATE fact_versions SET provider = 'claude_api_key', model = 'x' WHERE id = 'ver-0002'",
        "UPDATE fact_versions SET fact_id = 'fact-0001', version = 7 WHERE id = 'ver-0002'",
        "UPDATE fact_versions SET created_at = '2020-01-01T00:00:00+00:00'",
        "DELETE FROM fact_versions WHERE id = 'ver-0002'",
        "DELETE FROM fact_versions",
    ]
    for statement in statements:
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as tx:
            tx.execute(statement)
        assert sorted(conn.execute("SELECT * FROM fact_versions").fetchall()) == before, statement

    # Appending is still allowed.
    put(db, "fact_versions", id="ver-0003", version=3)
    assert count(conn, "fact_versions") == 3


def test_fact_version_stores_what_made_it(db: Database) -> None:
    # ADR-089 item 8: every stored version keeps provider and model, so drafts made by the fake
    # provider can be labelled; M3.4 also needs the prompt id and version and needs_check.
    seed(db)
    put(
        db,
        "fact_versions",
        text=None,
        situation="The depot missed its dispatch window twice a week",
        action="Rebuilt the loading rota",
        result="No missed windows for two quarters",
        source="cv_import",
        original_answer="Rebuilt loading rota; zero missed windows for 2 quarters",
        provider="fake",
        model="fake-thorough",
        prompt_id="extract_cv",
        prompt_version="2+override",
        needs_check=1,
    )
    row = db.connection.execute(
        "SELECT text, situation, action, result, source, original_answer, provider, model,"
        " prompt_id, prompt_version, needs_check, typeof(needs_check), version"
        " FROM fact_versions WHERE id = 'ver-0002'"
    ).fetchone()
    assert tuple(row) == (
        None,
        "The depot missed its dispatch window twice a week",
        "Rebuilt the loading rota",
        "No missed windows for two quarters",
        "cv_import",
        "Rebuilt loading rota; zero missed windows for 2 quarters",
        "fake",
        "fake-thorough",
        "extract_cv",
        "2+override",
        1,
        "integer",
        2,
    )


def test_fact_version_numbers_are_unique_per_fact_and_start_at_one(db: Database) -> None:
    seed(db)
    refused(db, "fact_versions", version=1)
    refused(db, "fact_versions", version=0)
    refused(db, "fact_versions", version=None)
    put(db, "fact_versions", version=2)
    # Another fact has its own numbering.
    add_fact(db, "fact-0003", 3, "ver-0003")


def test_fact_version_needs_some_content(db: Database) -> None:
    seed(db)
    refused(db, "fact_versions", text=None)
    refused(db, "fact_versions", text="", situation="  ", action="\n", result="\t")
    put(db, "fact_versions", text=None, result="Halved the month-end close")


def test_fact_version_needs_check_is_a_flag(db: Database) -> None:
    seed(db)
    refused(db, "fact_versions", needs_check=2)
    refused(db, "fact_versions", needs_check=None)
    put(db, "fact_versions", needs_check=1)


@pytest.mark.parametrize(
    "source", ["manual", "cv_import", "refine", "merge", "capture", "session_fictional"]
)
def test_fact_version_source_accepts_known_and_later_names(db: Database, source: str) -> None:
    # Later milestones add sources (sessions, interview prep), and a CHECK can't change without
    # a table rebuild, so the schema only requires a name; the domain validates the set.
    seed(db)
    put(db, "fact_versions", source=source)


@pytest.mark.parametrize("source", ["", "   ", " \t\n", None])
def test_fact_version_source_rejects_blank(db: Database, source: str | None) -> None:
    seed(db)
    refused(db, "fact_versions", source=source)


# --- fact_events ------------------------------------------------------------


def test_fact_events_record_type_version_and_json_detail(db: Database) -> None:
    seed(db)
    put(db, "fact_events", type="privacy_changed", detail='{"from": "normal", "to": "never_use"}')
    put(db, "fact_events", id="event-0002", type="discarded", fact_version_id=None, detail=None)
    refused(db, "fact_events", id="event-0003", type="")
    refused(db, "fact_events", id="event-0003", type="  ")
    refused(db, "fact_events", id="event-0003", type=None)
    refused(db, "fact_events", id="event-0003", detail="not json")
    assert count(db.connection, "fact_events") == 2


def test_fact_events_refuse_update_and_delete(db: Database) -> None:
    seed(db)
    put(db, "fact_events", detail='{"from": "draft"}')
    put(db, "fact_events", id="event-0002", type="archived", fact_version_id=None)
    conn = db.connection
    before = sorted(conn.execute("SELECT * FROM fact_events").fetchall())
    assert len(before) == 2

    statements = [
        "UPDATE fact_events SET type = 'discarded' WHERE id = 'event-0001'",
        "UPDATE fact_events SET detail = NULL WHERE id = 'event-0001'",
        "UPDATE fact_events SET fact_version_id = 'ver-0001' WHERE id = 'event-0002'",
        "UPDATE fact_events SET created_at = '2020-01-01T00:00:00+00:00'",
        "UPDATE OR REPLACE fact_events SET id = 'event-0002' WHERE id = 'event-0001'",
        "DELETE FROM fact_events WHERE id = 'event-0002'",
        "DELETE FROM fact_events",
    ]
    for statement in statements:
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as tx:
            tx.execute(statement)
        assert sorted(conn.execute("SELECT * FROM fact_events").fetchall()) == before, statement

    # Appending is still allowed.
    put(db, "fact_events", id="event-0003", type="restored")
    assert count(conn, "fact_events") == 3


def test_insert_or_replace_cannot_rewrite_a_fact_event(db: Database) -> None:
    seed(db)
    put(db, "fact_events")
    replace_refused(db, "fact_events", event_row("event-0001", type="discarded"))
    replace_refused(db, "fact_events", event_row("event-0001"))
    assert db.connection.execute("SELECT id, type FROM fact_events").fetchall() == [
        ("event-0001", "confirmed")
    ]

    # Appending still works, with or without OR REPLACE when nothing is replaced.
    put(db, "fact_events", id="event-0002", type="archived")
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO fact_events (id, fact_id, type, created_at)"
            " VALUES ('event-0003', 'fact-0001', 'restored', ?)",
            (STAMP,),
        )
    assert count(db.connection, "fact_events") == 3


def test_fact_event_version_must_be_a_version_of_the_events_fact(db: Database) -> None:
    seed(db)
    add_fact(db, "fact-0002", 2, "ver-0002")
    refused(db, "fact_events", fact_id="fact-0001", fact_version_id="ver-0002")
    refused(db, "fact_events", fact_id="fact-0002", fact_version_id="ver-0001")
    put(db, "fact_events", fact_id="fact-0002", fact_version_id="ver-0002")
    # An event that concerns no version is still fine.
    put(db, "fact_events", id="event-0002", fact_id="fact-0002", fact_version_id=None)
    assert count(db.connection, "fact_events") == 2


# --- skills -----------------------------------------------------------------


def test_skill_normalized_name_is_unique(db: Database) -> None:
    # M3.3 normalizes names in the domain; the schema refuses two rows with the same result,
    # so "Forecasting" and "FORECASTING" can't both exist.
    seed(db)
    refused(db, "skills", name="FORECASTING", normalized_name="forecasting")
    refused(db, "skills", normalized_name="")
    refused(db, "skills", normalized_name="  ")
    refused(db, "skills", name=" ")
    put(db, "skills")


def test_skill_aliases_are_a_json_array(db: Database) -> None:
    seed(db)
    for bad in ("not json", '{"a": 1}', '"sql"', "", None):
        refused(db, "skills", aliases=bad)
    put(db, "skills", aliases='["rota planning", "shift planning"]')


def test_fact_skill_link_is_stored_once(db: Database) -> None:
    seed(db)
    put(db, "fact_skills")
    refused(db, "fact_skills")
    with db.transaction() as conn:
        conn.execute("DELETE FROM fact_skills")
    assert count(db.connection, "fact_skills") == 0


# --- capture_notes ----------------------------------------------------------


def test_capture_note_text_resolution_and_soft_delete(db: Database) -> None:
    seed(db)
    refused(db, "capture_notes", text="")
    refused(db, "capture_notes", text=" \n")
    refused(db, "capture_notes", text=None)
    put(db, "capture_notes")
    with db.transaction() as conn:
        # "Turn into a fact" sets resolved_fact_id; delete with undo sets and clears deleted_at.
        conn.execute(
            "UPDATE capture_notes SET resolved_fact_id = 'fact-0001', deleted_at = ?", (LATER,)
        )
        conn.execute("UPDATE capture_notes SET deleted_at = NULL")
        # After the undo period a note can be removed for good.
        conn.execute("DELETE FROM capture_notes")
    assert count(db.connection, "capture_notes") == 0


# --- cv_imports -------------------------------------------------------------


def test_cv_import_stored_name_is_a_unique_plain_file_name(db: Database) -> None:
    # The stored name is generated by the app and never a path (spec 2.9 rule 5).
    seed(db)
    refused(db, "cv_imports", stored_name="imp-0001.pdf")
    for bad in ("", "  ", "../imp.pdf", "nested/imp.pdf", "nested\\imp.pdf", "..", ".", None):
        refused(db, "cv_imports", stored_name=bad)
    put(db, "cv_imports", kind="text", stored_name="imp-0002.txt", task_id="task-0001")
    with db.transaction() as conn:
        conn.execute(
            "UPDATE cv_imports SET state = 'done', updated_at = ?, version = version + 1"
            " WHERE id = 'imp-0002'",
            (LATER,),
        )
    assert db.connection.execute(
        "SELECT state, version FROM cv_imports WHERE id = 'imp-0002'"
    ).fetchone() == ("done", 2)


# --- refine_sessions --------------------------------------------------------


def test_refine_session_allows_at_most_three_answer_rounds(db: Database) -> None:
    seed(db)
    refused(db, "refine_sessions", answer_rounds=4)
    put(db, "refine_sessions", answer_rounds=0)
    with db.transaction() as conn:
        for rounds in (1, 2, 3):
            conn.execute("UPDATE refine_sessions SET answer_rounds = ?", (rounds,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE refine_sessions SET answer_rounds = 4")


def test_refine_session_keeps_conversation_suggestion_and_flags_as_json(db: Database) -> None:
    seed(db)
    for column in ("turns", "flagged_claims", "acknowledged_claims"):
        for bad in ("not json", '{"a": 1}', "", None):
            refused(db, "refine_sessions", **{column: bad})
    refused(db, "refine_sessions", suggestion="not json")
    put(
        db,
        "refine_sessions",
        answer_rounds=1,
        turns='[{"questions": ["How large was the backlog?"], "answer": "About 900 invoices"}]',
        suggestion='{"text": "Cut a backlog of about 900 invoices by a third"}',
        flagged_claims='[{"id": "c1", "text": "900", "kind": "number"}]',
        acknowledged_claims='["c1"]',
        provider="fake",
        model="fake-fast",
        prompt_id="refine_fact",
        prompt_version="1",
        task_id="task-0001",
    )
    assert count(db.connection, "refine_sessions") == 1


def test_one_open_refine_session_per_fact(db: Database) -> None:
    # A turn is the task refine_fact:<fact id>, so a fact has one conversation at a time.
    seed(db)
    add_fact(db, "fact-0003", 3, "ver-0003")
    put(db, "refine_sessions")
    refused(db, "refine_sessions", id="refine-0002")
    put(db, "refine_sessions", id="refine-0003", fact_id="fact-0003", base_version_id="ver-0003")
    with db.transaction() as conn:
        conn.execute("UPDATE refine_sessions SET state = 'discarded' WHERE id = 'refine-0001'")
    put(db, "refine_sessions", id="refine-0002")
    put(db, "refine_sessions", id="refine-0004", state="accepted")
    assert count(db.connection, "refine_sessions") == 4


def test_refine_session_base_version_must_be_a_version_of_its_fact(db: Database) -> None:
    seed(db)
    add_fact(db, "fact-0002", 2, "ver-0002")
    refused(db, "refine_sessions", fact_id="fact-0001", base_version_id="ver-0002")
    refused(db, "refine_sessions", fact_id="fact-0002", base_version_id="ver-0001")
    put(db, "refine_sessions", fact_id="fact-0002", base_version_id="ver-0002")
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute("UPDATE refine_sessions SET base_version_id = 'ver-0001'")
    assert db.connection.execute(
        "SELECT fact_id, base_version_id FROM refine_sessions"
    ).fetchall() == [("fact-0002", "ver-0002")]


# --- fact_pair_dismissals ---------------------------------------------------


def test_dismissed_pair_is_stored_once_in_one_order(db: Database) -> None:
    seed(db)
    add_fact(db, "fact-0002", 2, "ver-0002")

    def dismiss(first: str, second: str) -> None:
        with db.transaction() as conn:
            insert(
                conn,
                "fact_pair_dismissals",
                {"fact_a_id": first, "fact_b_id": second, "created_at": STAMP},
            )

    dismiss("fact-0001", "fact-0002")
    for first, second in (
        ("fact-0001", "fact-0002"),  # again
        ("fact-0002", "fact-0001"),  # the other order: a < b keeps one row per pair
        ("fact-0001", "fact-0001"),
        ("fact-0001", "fact-9999"),
        ("fact-0000", "fact-0001"),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            dismiss(first, second)
    assert count(db.connection, "fact_pair_dismissals") == 1


# --- full-text search (AC-M3.6 groundwork) ----------------------------------


def story(db: Database) -> None:
    """role-0001 (Northwind Logistics, Operations Analyst), skill-0001 (Forecasting) and the
    story fact-0001 with a summary and all three parts, linked to both."""
    with db.transaction() as conn:
        insert(conn, "roles", role_row())
        insert(conn, "skills", skill_row("skill-0001", "Forecasting"))
    add_fact(
        db,
        "fact-0001",
        1,
        "ver-0001",
        fact={"type": "story", "role_id": "role-0001"},
        version={
            "text": "Dispatch rota rebuild",
            "situation": "The depot missed its lorry window twice weekly",
            "action": "Rewrote the loading timetable with the supervisors",
            "result": "Punctuality held for two quarters",
        },
    )
    with db.transaction() as conn:
        insert(conn, "fact_skills", {"fact_id": "fact-0001", "skill_id": "skill-0001"})


@pytest.mark.parametrize(
    "word",
    ["rebuild", "lorry", "timetable", "punctuality", "northwind", "analyst", "forecasting"],
    ids=["text", "situation", "action", "result", "employer", "role-title", "skill"],
)
def test_fts_finds_text_story_parts_role_and_skill(db: Database, word: str) -> None:
    story(db)
    add_fact(db, "fact-0002", 2, "ver-0002", version={"text": "Trained four new cashiers"})
    assert search(db, word) == {"fact-0001"}
    assert search(db, word.upper()) == {"fact-0001"}
    assert search(db, "cashiers") == {"fact-0002"}
    assert search(db, "submarine") == set()


def test_fts_has_one_entry_per_fact_and_never_matches_ids(db: Database) -> None:
    story(db)
    add_fact(db, "fact-0002", 2, "ver-0002", version={"text": "Trained four new cashiers"})
    rows = db.connection.execute("SELECT fact_id FROM facts_fts ORDER BY fact_id").fetchall()
    assert [row[0] for row in rows] == ["fact-0001", "fact-0002"]
    # Ids, sources and statuses are not searchable text.
    for word in ("fact", "0001", "ver", "role", "skill", "manual", "confirmed", "story"):
        assert search(db, word) == set(), word


def test_fts_sees_only_the_current_version(db: Database) -> None:
    add_fact(db, "fact-0001", 1, "ver-0001", version={"text": "Paddled a kayak to work"})
    # A version that exists but isn't current (not yet pointed at) is not searchable.
    with db.transaction() as conn:
        insert(
            conn, "fact_versions", version_row("ver-0002", "fact-0001", 2, text="Carried a lantern")
        )
    assert search(db, "kayak") == {"fact-0001"}
    assert search(db, "lantern") == set()

    with db.transaction() as conn:
        conn.execute("UPDATE facts SET current_version_id = 'ver-0002' WHERE id = 'fact-0001'")
    assert search(db, "lantern") == {"fact-0001"}
    assert search(db, "kayak") == set()
    assert count(db.connection, "facts_fts") == 1


def test_fts_follows_new_version_role_rename_and_skill_change(db: Database) -> None:
    story(db)
    add_fact(
        db,
        "fact-0002",
        2,
        "ver-0002",
        fact={"role_id": "role-0001"},
        version={"text": "Trained four new cashiers"},
    )

    # A new version replaces every part of the old one.
    add_version(
        db,
        "ver-0003",
        "fact-0001",
        2,
        text="Dispatch rota overhaul",
        situation=None,
        action="Introduced a shared whiteboard",
        result=None,
    )
    assert search(db, "overhaul") == {"fact-0001"}
    assert search(db, "whiteboard") == {"fact-0001"}
    for gone in ("rebuild", "lorry", "timetable", "punctuality"):
        assert search(db, gone) == set(), gone
    assert search(db, "northwind") == {"fact-0001", "fact-0002"}
    assert search(db, "forecasting") == {"fact-0001"}

    # Renaming a role reaches every fact of that role.
    with db.transaction() as conn:
        conn.execute(
            "UPDATE roles SET employer = 'Southbank Freight', title = 'Planning Lead'"
            " WHERE id = 'role-0001'"
        )
    assert search(db, "southbank") == {"fact-0001", "fact-0002"}
    assert search(db, "planning") == {"fact-0001", "fact-0002"}
    assert search(db, "northwind") == set()
    assert search(db, "analyst") == set()

    # Moving a fact to another role, and to no role.
    with db.transaction() as conn:
        insert(conn, "roles", role_row("role-0002", employer="Harbour Bakery", title="Cashier"))
        conn.execute("UPDATE facts SET role_id = 'role-0002' WHERE id = 'fact-0002'")
    assert search(db, "harbour") == {"fact-0002"}
    assert search(db, "southbank") == {"fact-0001"}
    with db.transaction() as conn:
        conn.execute("UPDATE facts SET role_id = NULL WHERE id = 'fact-0002'")
    assert search(db, "harbour") == set()
    assert search(db, "cashiers") == {"fact-0002"}

    # Skills: link added, skill renamed, link removed.
    with db.transaction() as conn:
        insert(conn, "skills", skill_row("skill-0002", "Mentoring"))
        insert(conn, "fact_skills", {"fact_id": "fact-0002", "skill_id": "skill-0002"})
        insert(conn, "fact_skills", {"fact_id": "fact-0001", "skill_id": "skill-0002"})
    assert search(db, "mentoring") == {"fact-0001", "fact-0002"}
    assert search(db, "forecasting") == {"fact-0001"}
    with db.transaction() as conn:
        conn.execute(
            "UPDATE skills SET name = 'Coaching', normalized_name = 'coaching'"
            " WHERE id = 'skill-0002'"
        )
    assert search(db, "coaching") == {"fact-0001", "fact-0002"}
    assert search(db, "mentoring") == set()
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM fact_skills WHERE fact_id = 'fact-0001' AND skill_id = 'skill-0002'"
        )
        conn.execute(
            "DELETE FROM fact_skills WHERE fact_id = 'fact-0001' AND skill_id = 'skill-0001'"
        )
    assert search(db, "coaching") == {"fact-0002"}
    assert search(db, "forecasting") == set()
    # The text is untouched by all of it, and there is still one entry per fact.
    assert search(db, "overhaul") == {"fact-0001"}
    assert count(db.connection, "facts_fts") == 2


def test_fts_follows_a_skill_link_moved_to_another_fact_or_skill(db: Database) -> None:
    story(db)  # fact-0001 is linked to skill-0001, Forecasting
    add_fact(db, "fact-0002", 2, "ver-0002", version={"text": "Trained four new cashiers"})
    with db.transaction() as conn:
        insert(conn, "skills", skill_row("skill-0002", "Mentoring"))
    assert search(db, "forecasting") == {"fact-0001"}

    # The link moves to another fact: the old fact loses the skill and the new one gains it.
    with db.transaction() as conn:
        conn.execute("UPDATE fact_skills SET fact_id = 'fact-0002' WHERE fact_id = 'fact-0001'")
    assert search(db, "forecasting") == {"fact-0002"}

    # The link moves to another skill.
    with db.transaction() as conn:
        conn.execute("UPDATE fact_skills SET skill_id = 'skill-0002' WHERE fact_id = 'fact-0002'")
    assert search(db, "mentoring") == {"fact-0002"}
    assert search(db, "forecasting") == set()

    # Both at once, back to where it started.
    with db.transaction() as conn:
        conn.execute("UPDATE fact_skills SET fact_id = 'fact-0001', skill_id = 'skill-0001'")
    assert search(db, "forecasting") == {"fact-0001"}
    assert search(db, "mentoring") == set()

    # Everything else in both entries is untouched, and there is still one entry per fact.
    assert search(db, "rebuild") == {"fact-0001"}
    assert search(db, "northwind") == {"fact-0001"}
    assert search(db, "cashiers") == {"fact-0002"}
    assert count(db.connection, "facts_fts") == 2


def test_fts_drops_discarded_fact(db: Database) -> None:
    story(db)
    add_fact(
        db,
        "fact-0002",
        2,
        "ver-0002",
        fact={"status": "draft", "role_id": "role-0001"},
        version={"text": "Trained four new cashiers"},
    )
    # Drafts are searchable (the status filter separates them).
    assert search(db, "cashiers") == {"fact-0002"}

    def set_status(fact_id: str, status: str) -> None:
        with db.transaction() as conn:
            conn.execute("UPDATE facts SET status = ? WHERE id = ?", (status, fact_id))

    set_status("fact-0002", "discarded")
    assert search(db, "cashiers") == set()
    assert search(db, "northwind") == {"fact-0001"}
    assert count(db.connection, "facts_fts") == 1

    # Changes made while it is discarded don't bring it back.
    add_version(db, "ver-0003", "fact-0002", 2, text="Trained five new cashiers")
    with db.transaction() as conn:
        conn.execute("UPDATE roles SET title = 'Operations Planner' WHERE id = 'role-0001'")
        insert(conn, "fact_skills", {"fact_id": "fact-0002", "skill_id": "skill-0001"})
    assert search(db, "cashiers") == set()
    assert search(db, "planner") == {"fact-0001"}
    assert search(db, "forecasting") == {"fact-0001"}

    # Undo restores the draft with its current text, role and skills.
    set_status("fact-0002", "draft")
    assert search(db, "five") == {"fact-0002"}
    assert search(db, "four") == set()
    assert search(db, "planner") == {"fact-0001", "fact-0002"}
    assert search(db, "forecasting") == {"fact-0001", "fact-0002"}

    # Confirmed and archived facts stay searchable.
    set_status("fact-0002", "confirmed")
    assert search(db, "cashiers") == {"fact-0002"}
    set_status("fact-0002", "archived")
    assert search(db, "cashiers") == {"fact-0002"}

    # A fact stored as discarded from the start never appears.
    add_fact(
        db,
        "fact-0003",
        3,
        "ver-0004",
        fact={"status": "discarded"},
        version={"text": "Juggled at the office party"},
    )
    assert search(db, "juggled") == set()


# --- migration hashes -------------------------------------------------------


def test_migration_hashes_include_0003() -> None:
    path = _migration_0003()
    assert path.name == "0003_fact_bank.sql"
    recorded: dict[str, str] = json.loads(MIGRATION_HASHES.read_text(encoding="utf-8"))
    assert path.name in recorded, f"{MIGRATION_HASHES.name} lacks {path.name}"
    assert recorded[path.name] == hashlib.sha256(path.read_bytes()).hexdigest()
