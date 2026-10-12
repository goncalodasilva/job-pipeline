"""Helpers for the fact bank service tests (M3.3): a migrated database in a temp folder, the
services built with test doubles, fictional seed data and raw-SQL readers for assertions.

The application modules are imported inside the functions, so a test that uses a helper fails
on its own while a module is missing. Everyone and everything here is fictional.
"""

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory

START = datetime(2026, 3, 5, 9, 15, 0, tzinfo=UTC)
STAMP = "2026-03-05T09:15:00+00:00"

# Every table a fact bank write may touch, with the search index.
TABLES = (
    "roles",
    "cv_imports",
    "facts",
    "fact_versions",
    "fact_events",
    "skills",
    "fact_skills",
)

PROVIDER = "fake"
MODEL = "fake-thorough-1"
PROMPT_ID = "extract_cv"
PROMPT_VERSION = "1"


def open_migrated(path: Path, backups: Path) -> Database:
    database = Database.open(path)
    apply_migrations(database, MIGRATIONS_DIR, backups, FrozenClock(START))
    return database


@dataclass(frozen=True)
class Bank:
    """The four services under test, sharing one database, clock and id factory."""

    facts: Any
    roles: Any
    skills: Any
    library: Any


def make_bank(db: Database, clock: Any, ids: Any) -> Bank:
    from app.services.fact_library import FactLibrary
    from app.services.facts import FactService
    from app.services.roles import RoleService
    from app.services.skills import SkillService

    return Bank(
        facts=FactService(db, clock, ids),
        roles=RoleService(db, clock, ids),
        skills=SkillService(db, clock, ids),
        library=FactLibrary(db),
    )


# --- reading the database for assertions -----------------------------------------


def query(db: Database, sql: str, params: Sequence[object] = ()) -> list[dict[str, Any]]:
    cursor = db.connection.execute(sql, tuple(params))
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def snapshot(db: Database) -> dict[str, list[tuple[object, ...]]]:
    """Every row of every fact bank table and of the search index."""
    found: dict[str, list[tuple[object, ...]]] = {}
    for table in TABLES:
        found[table] = db.connection.execute(
            f"SELECT * FROM {table} ORDER BY rowid"  # noqa: S608 - fixed table names
        ).fetchall()
    found["facts_fts"] = db.connection.execute(
        "SELECT rowid, * FROM facts_fts ORDER BY rowid"
    ).fetchall()
    return found


def fact_row(db: Database, fact_id: str) -> dict[str, Any]:
    rows = query(db, "SELECT * FROM facts WHERE id = ?", (fact_id,))
    assert len(rows) == 1
    return rows[0]


def version_rows(db: Database, fact_id: str) -> list[dict[str, Any]]:
    return query(db, "SELECT * FROM fact_versions WHERE fact_id = ? ORDER BY version", (fact_id,))


def current_version_row(db: Database, fact_id: str) -> dict[str, Any]:
    rows = query(
        db,
        "SELECT v.* FROM fact_versions AS v JOIN facts AS f ON f.current_version_id = v.id "
        "WHERE f.id = ?",
        (fact_id,),
    )
    assert len(rows) == 1
    return rows[0]


def event_rows(db: Database, fact_id: str) -> list[dict[str, Any]]:
    """A fact's events, oldest first (ADR-090 item 3: ``created_at, rowid``)."""
    return query(
        db, "SELECT * FROM fact_events WHERE fact_id = ? ORDER BY created_at, rowid", (fact_id,)
    )


def event_types(db: Database, fact_id: str) -> list[str]:
    return [str(row["type"]) for row in event_rows(db, fact_id)]


def role_row(db: Database, role_id: str) -> dict[str, Any]:
    rows = query(db, "SELECT * FROM roles WHERE id = ?", (role_id,))
    assert len(rows) == 1
    return rows[0]


def skill_names_of(db: Database, fact_id: str) -> set[str]:
    rows = query(
        db,
        "SELECT s.name FROM fact_skills AS fs JOIN skills AS s ON s.id = fs.skill_id "
        "WHERE fs.fact_id = ?",
        (fact_id,),
    )
    return {str(row["name"]) for row in rows}


def indexed_fact_ids(db: Database) -> set[str]:
    return {str(row["fact_id"]) for row in query(db, "SELECT fact_id FROM facts_fts")}


def count(db: Database, table: str) -> int:
    assert table in TABLES
    value: int = db.connection.execute(
        f"SELECT count(*) FROM {table}"  # noqa: S608 - fixed table names
    ).fetchone()[0]
    return value


# --- seeding ---------------------------------------------------------------------


def add_import(db: Database, import_id: str) -> str:
    """A finished CV import row, written directly: making imports is M3.4."""
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO cv_imports (id, kind, stored_name, state, task_id, created_at, "
            "updated_at, version) VALUES (?, 'text', ?, 'done', NULL, ?, ?, 1)",
            (import_id, f"{import_id}.txt", STAMP, STAMP),
        )
    return import_id


def typed(bank: Bank, text: str, **fields: Any) -> Any:
    """A fact the user typed: confirmed at once."""
    from app.services.facts import FactInput

    return bank.facts.create(FactInput(text=text, **fields))


def import_drafts(
    bank: Bank,
    import_id: str | None,
    texts: Sequence[str],
    *,
    role: tuple[str, str] | None = None,
    skills: tuple[str, ...] = (),
    source: str = "cv_import",
) -> list[Any]:
    """Draft facts as the CV import (M3.4) will make them, all in one call."""
    from app.services.facts import DraftInput, DraftOrigin, DraftRole

    origin = DraftOrigin(
        source=source,
        provider=PROVIDER,
        model=MODEL,
        prompt_id=PROMPT_ID,
        prompt_version=PROMPT_VERSION,
        cv_import_id=import_id,
    )
    roles = [] if role is None else [DraftRole(key="r1", employer=role[0], title=role[1])]
    drafts = [
        DraftInput(
            text=text,
            role_key=None if role is None else "r1",
            skills=skills,
            original_answer=f"CV line: {text}",
        )
        for text in texts
    ]
    return list(bank.facts.create_drafts(origin, drafts, roles=roles))


# --- failure injection -----------------------------------------------------------


class IdsExhausted(Exception):
    """Raised by ``FailingIds`` in place of an id."""


class FailingIds:
    """Sequential ids until call number ``fail_at``, which raises instead."""

    def __init__(self, prefix: str, fail_at: int) -> None:
        self._ids = SequentialIdFactory(prefix)
        self._fail_at = fail_at
        self.calls = 0

    def new(self) -> str:
        self.calls += 1
        if self.calls == self._fail_at:
            raise IdsExhausted
        return self._ids.new()


def assert_atomic(
    db: Database, clock: Any, act: Callable[[Bank], object], *, max_ids: int = 60
) -> None:
    """Run ``act`` again and again, failing at the first id, then the second and so on, until
    it succeeds. Every failed run must leave the database exactly as it was and no transaction
    open: whatever the write needs is written in one transaction or not at all."""
    before = snapshot(db)
    failures = 0
    for fail_at in range(1, max_ids + 1):
        bank = make_bank(db, clock, FailingIds(f"try{fail_at}", fail_at))
        try:
            act(bank)
        except IdsExhausted:
            failures += 1
            assert snapshot(db) == before, f"rows were left behind when id {fail_at} failed"
            assert not db.connection.in_transaction
            continue
        assert failures >= 1, "the write asked for no id at all"
        assert snapshot(db) != before
        return
    raise AssertionError(f"the write still needed ids after {max_ids}")


def second_connection(db: Database) -> Database:
    """Another connection to the same database file, as another process would hold."""
    connection = sqlite3.connect(db.path, isolation_level=None, check_same_thread=False)
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA foreign_keys = ON")
    return Database(db.path, connection)
