"""M7.3 session persistence: forward migration, constraints, replacement guards and Undo.

All answers, questions and facts are fictional. Tests use temporary migrated databases.
"""

import hashlib
import importlib
import json
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from app.domain.enrichment import ItemState
from app.domain.errors import VersionConflict
from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from tests.application_desk import Desk
from tests.fakes.clock import FrozenClock
from tests.integration.test_schema import schema_snapshot
from tests.integration.test_schema_0003 import add_fact, note_row
from tests.integration.test_schema_0004 import STAMP, START, insert
from tests.integration.test_schema_0006 import seed_base

pytestmark = pytest.mark.integration
TABLES = ("enrichment_questions", "sessions", "session_answers")


@pytest.fixture
def db(tmp_path):
    with Database.open(tmp_path / "fictional.db") as database:
        apply_migrations(database, MIGRATIONS_DIR, tmp_path / "backups", FrozenClock(START))
        yield database


def question_row(id="q1", **extra):
    return dict(
        id=id,
        kind="cluster",
        cluster_key="skill:python",
        created_at=STAMP,
        updated_at=STAMP,
        **extra,
    )


def session_row(id="s1", **extra):
    return (
        dict(
            id=id,
            focus_kind="most_useful",
            length=3,
            state="open",
            started_at=STAMP,
            created_at=STAMP,
            updated_at=STAMP,
        )
        | extra
    )


def answer_row(id="a1", **extra):
    return (
        dict(
            id=id,
            session_id="s1",
            position=1,
            question_id="q1",
            question="Tell me about a fictional orchard project.",
            why="This skill is requested by saved jobs.",
            created_at=STAMP,
            updated_at=STAMP,
        )
        | extra
    )


def seed(db):
    with db.transaction() as conn:
        insert(conn, "enrichment_questions", question_row())
        insert(conn, "sessions", session_row())
        insert(conn, "session_answers", answer_row())


def dump(conn, table):
    return conn.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall()  # noqa: S608


def test_0007_upgrade_keeps_0006_rows_and_backs_up(tmp_path):
    assert (MIGRATIONS_DIR / "0007_sessions.sql").is_file()
    earlier = tmp_path / "through-0006"
    earlier.mkdir()
    through_0007 = tmp_path / "through-0007"
    through_0007.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name < "0007":
            shutil.copy(path, earlier / path.name)
        if path.name < "0008":
            shutil.copy(path, through_0007 / path.name)
    with Database.open(tmp_path / "old/app.db") as old:
        apply_migrations(old, earlier, tmp_path / "backups", FrozenClock(START))
        seed_base(old)
        with old.transaction() as conn:
            insert(
                conn,
                "angles",
                dict(
                    id="angle1",
                    name="Fictional orchard work",
                    description="A fictional angle",
                    origin="user",
                    created_at=STAMP,
                    updated_at=STAMP,
                ),
            )
        names = [
            row[0]
            for row in old.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name <> 'schema_migrations'"
            )
        ]
        before = {name: dump(old.connection, name) for name in names}
        guard = old.connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='applications_delete_only_untouched'"
        ).fetchone()[0]
        result = apply_migrations(old, through_0007, tmp_path / "backups", FrozenClock(START))
        assert result.applied == (7,)
        assert result.backup is not None and result.backup.is_file()
        for name in names:
            assert dump(old.connection, name) == before[name]
        assert (
            old.connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='applications_delete_only_untouched'"
            ).fetchone()[0]
            == guard
        )
        assert all(dump(old.connection, table) == [] for table in TABLES)
        upgraded = schema_snapshot(old.connection)
    with sqlite3.connect(result.backup) as backup:
        assert backup.execute("SELECT count(*) FROM facts").fetchone()[0] == 2
        assert backup.execute("SELECT count(*) FROM angles").fetchone()[0] == 1
        assert not set(TABLES) & {
            row[0] for row in backup.execute("SELECT name FROM sqlite_master")
        }
    with Database.open(tmp_path / "fresh/app.db") as fresh:
        apply_migrations(fresh, through_0007, tmp_path / "fresh-backups", FrozenClock(START))
        assert schema_snapshot(fresh.connection) == upgraded
    recorded = json.loads(
        (Path(__file__).parents[1] / "fixtures/migration_hashes.json").read_text()
    )
    assert (
        recorded["0007_sessions.sql"]
        == hashlib.sha256((MIGRATIONS_DIR / "0007_sessions.sql").read_bytes()).hexdigest()
    )


def test_one_open_session_at_a_time(db):
    with db.transaction() as conn:
        insert(conn, "sessions", session_row())
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        insert(conn, "sessions", session_row("s2"))
    with db.transaction() as conn:
        conn.execute("UPDATE sessions SET state='ended', ended_at=? WHERE id='s1'", (STAMP,))
        insert(conn, "sessions", session_row("s2"))
    assert db.connection.execute("SELECT id FROM sessions WHERE state='open'").fetchall() == [
        ("s2",)
    ]


@pytest.mark.parametrize(
    "kind,target", [("cluster", "skill:python"), ("quality_hint", "f1"), ("capture_note", "n1")]
)
def test_enrichment_question_one_target_and_unique_per_target(db, kind, target):
    add_fact(db, "f1", 1, "v1")
    with db.transaction() as conn:
        insert(conn, "capture_notes", note_row("n1"))
    column = {
        "cluster": "cluster_key",
        "quality_hint": "fact_id",
        "capture_note": "capture_note_id",
    }[kind]
    row = dict(id="q1", kind=kind, created_at=STAMP, updated_at=STAMP, **{column: target})
    with db.transaction() as conn:
        insert(conn, "enrichment_questions", row)
    for bad in [
        dict(row, id="q2"),
        dict(row, id="q2", **{column: None}),
        dict(
            row,
            id="q2",
            **{
                "fact_id" if kind != "quality_hint" else "capture_note_id": "f1"
                if kind != "quality_hint"
                else "n1"
            },
        ),
    ]:
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
            insert(conn, "enrichment_questions", bad)


@pytest.mark.parametrize(
    "field,value",
    [
        ("outcome", "invented"),
        ("later_choice", "tomorrow"),
        ("answer_text", "x" * 4001),
        ("answer_text", "x\u0000" + "x" * 4000),
        ("position", 0),
        ("fallback", 2),
        ("missing", "{}"),
        ("missing", "broken JSON"),
        ("flagged_claims", "bad JSON"),
        ("question", "x" * 301),
        ("question", ""),
        ("why", "x" * 201),
    ],
)
def test_session_answer_values_checked(db, field, value):
    seed(db)
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        insert(conn, "session_answers", answer_row("bad", position=2) | {field: value})


@pytest.mark.parametrize("length", [0, 4, 5.5, "bad"])
def test_session_answer_values_checked_length(db, length):
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        insert(conn, "sessions", session_row(length=length))


@pytest.mark.parametrize(
    "field,value",
    [
        ("earlier_wordings", json.dumps(["question"] * 6)),
        ("earlier_wordings", "{}"),
        ("earlier_wordings", "broken JSON"),
        ("earlier_wordings", "[1]"),
        ("status", "done"),
        ("later_next_session", 2),
        ("not_sure_count", -1),
        ("asked_count", 1.5),
        ("version", 0),
    ],
)
def test_enrichment_values_checked(db, field, value):
    seed(db)
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute(f"UPDATE enrichment_questions SET \"{field}\"=? WHERE id='q1'", (value,))  # noqa: S608


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize(
    "path",
    ["insert_replace", "insert_replace_other_unique", "update_replace", "rowid", "oid", "_rowid_"],
)
def test_session_tables_refuse_replace_paths(db, table, path):
    seed(db)
    conn = db.connection
    before = {name: dump(conn, name) for name in TABLES}
    row = {
        "enrichment_questions": question_row(),
        "sessions": session_row(),
        "session_answers": answer_row(),
    }[table]
    if path.startswith("insert_replace"):
        if path == "insert_replace_other_unique":
            row["id"] = "other-id"
        columns = ",".join(row)
        values = ",".join("?" for _ in row)
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as transaction:
            transaction.execute(
                f'INSERT OR REPLACE INTO "{table}" ({columns}) VALUES ({values})',  # noqa: S608
                tuple(row.values()),
            )
    elif path == "update_replace":
        other = dict(row, id="other")
        if table == "enrichment_questions":
            other["cluster_key"] = "skill:sql"
        elif table == "sessions":
            other.update(state="ended", ended_at=STAMP)
        else:
            other["position"] = 2
        with db.transaction() as transaction:
            insert(transaction, table, other)
        before = {name: dump(conn, name) for name in TABLES}
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as transaction:
            transaction.execute(
                f"UPDATE OR REPLACE \"{table}\" SET id=? WHERE id='other'",  # noqa: S608
                (row["id"],),
            )
    else:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(f'SELECT {path} FROM "{table}"')  # noqa: S608
    assert {name: dump(conn, name) for name in TABLES} == before


@pytest.mark.parametrize("target", ["open_session", "question_target", "answer_position"])
def test_unique_conflict_replace_cannot_remove_another_row(db, target):
    seed(db)
    with db.transaction() as conn:
        insert(conn, "sessions", session_row("s2", state="ended", ended_at=STAMP))
        insert(conn, "enrichment_questions", question_row("q2") | {"cluster_key": "skill:sql"})
        insert(conn, "session_answers", answer_row("a2", position=2))
    sql = {
        "open_session": "UPDATE OR REPLACE sessions SET state='open', ended_at=NULL WHERE id='s2'",
        "question_target": (
            "UPDATE OR REPLACE enrichment_questions SET cluster_key='skill:python' WHERE id='q2'"
        ),
        "answer_position": "UPDATE OR REPLACE session_answers SET position=1 WHERE id='a2'",
    }[target]
    before = {name: dump(db.connection, name) for name in TABLES}
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute(sql)
    assert {name: dump(db.connection, name) for name in TABLES} == before


def test_draft_link_must_be_a_fact(db):
    seed(db)
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute("UPDATE session_answers SET draft_fact_id='missing' WHERE id='a1'")
    add_fact(db, "draft1", 1, "v1")
    with db.transaction() as conn:
        conn.execute("UPDATE session_answers SET draft_fact_id='draft1' WHERE id='a1'")


def test_follow_up_belongs_to_same_session_question_and_follows_its_parent(db):
    seed(db)
    with db.transaction() as conn:
        insert(conn, "sessions", session_row("s2", state="ended", ended_at=STAMP))
        insert(conn, "enrichment_questions", question_row("q2") | {"cluster_key": "skill:sql"})
        insert(conn, "session_answers", answer_row("follow", position=2, follow_up_of="a1"))
    for extra in [
        dict(session_id="s2", position=2),
        dict(question_id="q2", position=3),
        dict(position=1),
    ]:
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
            insert(conn, "session_answers", answer_row("bad", follow_up_of="a1") | extra)


def test_question_and_structure_task_links_clear_on_task_delete(db):
    seed(db)
    with db.transaction() as conn:
        for id in ("question-task", "structure-task"):
            conn.execute(
                "INSERT INTO tasks (id,type,status,dedupe_key,created_at,updated_at) "
                "VALUES (?,'fictional','succeeded',?,?,?)",
                (id, id, STAMP, STAMP),
            )
        conn.execute(
            "UPDATE session_answers SET question_task_id='question-task', "
            "structure_task_id='structure-task' WHERE id='a1'"
        )
        conn.execute("DELETE FROM tasks WHERE id IN ('question-task','structure-task')")
    assert db.connection.execute(
        "SELECT question_task_id,structure_task_id FROM session_answers"
    ).fetchone() == (None, None)


def test_stale_session_or_answer_update_conflicts(db):
    sessions = importlib.import_module("app.modules.storage.sessions_repo")
    enrichment = importlib.import_module("app.modules.storage.enrichment_repo")
    with db.transaction() as conn:
        enrichment.insert_question(
            conn, id="q1", kind="cluster", cluster_key="skill:python", now=STAMP
        )
        sessions.insert_session(conn, id="s1", focus_kind="most_useful", length=3, now=STAMP)
        sessions.insert_answer(
            conn,
            id="a1",
            session_id="s1",
            position=1,
            question_id="q1",
            question="Tell me about orchard work.",
            why="Saved jobs ask for it.",
            now=STAMP,
        )
        sessions.update_answer(
            conn,
            "a1",
            answer_text="Counted 12 seedlings.",
            outcome="answered",
            now=STAMP,
            expected_version=1,
        )
        sessions.end_session(conn, "s1", now=STAMP, expected_version=1)
        enrichment.update_question(
            conn,
            "q1",
            state=replace(
                ItemState(), not_sure_count=1, asked_count=1, earlier_wordings=("Earlier question",)
            ),
            now=STAMP,
            expected_version=1,
        )
    with db.read() as conn:
        assert sessions.open_session(conn) is None
        assert sessions.get_session(conn, "s1").version == 2
        assert sessions.list_answers(conn, "s1")[0].answer_text == "Counted 12 seedlings."
        assert enrichment.find_question(conn, "cluster", "skill:python").earlier_wordings == (
            "Earlier question",
        )
        assert len(enrichment.list_questions(conn)) == 1
    for change in (
        lambda conn: sessions.end_session(conn, "s1", now=STAMP, expected_version=1),
        lambda conn: sessions.update_answer(
            conn,
            "a1",
            answer_text="Stale answer",
            outcome="answered",
            now=STAMP,
            expected_version=1,
        ),
        lambda conn: enrichment.update_question(
            conn, "q1", state=ItemState(), now=STAMP, expected_version=1
        ),
    ):
        before = {name: dump(db.connection, name) for name in TABLES}
        with pytest.raises(VersionConflict), db.transaction() as conn:
            change(conn)
        assert {name: dump(db.connection, name) for name in TABLES} == before


def test_session_and_draft_queries_have_indexes(db):
    expected = {
        "session_answers": [("session_id", "position"), ("draft_fact_id",)],
        "sessions": [("job_id",)],
    }
    for table, prefixes in expected.items():
        indexes = [
            tuple(row[2] for row in db.connection.execute(f'PRAGMA index_info("{index[1]}")'))
            for index in db.connection.execute(f'PRAGMA index_list("{table}")')
        ]
        for prefix in prefixes:
            assert any(columns[: len(prefix)] == prefix for columns in indexes)


def test_repository_roundtrip_preserves_wording_provenance_and_state(db):
    sessions = importlib.import_module("app.modules.storage.sessions_repo")
    enrichment = importlib.import_module("app.modules.storage.enrichment_repo")
    add_fact(db, "draft1", 1, "v1")
    with db.transaction() as conn:
        enrichment.insert_question(conn, id="q1", kind="quality_hint", fact_id="draft1", now=STAMP)
        sessions.insert_session(
            conn, id="s1", focus_kind="fact", focus_ref="draft1", length=5, now=STAMP
        )
        sessions.insert_answer(
            conn,
            id="a1",
            session_id="s1",
            position=1,
            question_id="q1",
            question="Tell me about a fictional orchard result.",
            why="A result would strengthen this story.",
            question_provider="fake",
            question_model="fixture-model",
            question_prompt_id="enrichment_question",
            question_prompt_version="1",
            fallback=True,
            now=STAMP,
        )
        text = "Fictional answer '; DROP TABLE sessions; --\nCounted 12 seedlings."
        sessions.update_answer(
            conn,
            "a1",
            answer_text=text,
            outcome="answered",
            draft_fact_id="draft1",
            missing=("result",),
            flagged_claims=({"kind": "number", "claim": "20"},),
            structure_provider="fake",
            structure_model="fixture-model",
            structure_prompt_id="structure_answer",
            structure_prompt_version="2",
            now=STAMP,
            expected_version=1,
        )
        state = ItemState(
            cooldown_until=START,
            not_sure_count=1,
            asked_count=2,
            last_outcome="not_sure",
            last_asked_at=START,
            earlier_wordings=("Earlier fictional question",),
        )
        enrichment.update_question(conn, "q1", state=state, now=STAMP, expected_version=1)
    with db.read() as conn:
        assert sessions.open_session(conn).id == "s1"
        answer = sessions.get_answer(conn, "a1")
        assert answer == sessions.list_answers(conn, "s1")[0]
        assert answer.answer_text == text and answer.draft_fact_id == "draft1"
        assert answer.missing == ("result",) and answer.flagged_claims == (
            {"kind": "number", "claim": "20"},
        )
        assert answer.fallback and answer.question_provider == "fake"
        assert (
            answer.question_model,
            answer.question_prompt_id,
            answer.question_prompt_version,
        ) == ("fixture-model", "enrichment_question", "1")
        assert (
            answer.structure_provider,
            answer.structure_model,
            answer.structure_prompt_id,
            answer.structure_prompt_version,
        ) == ("fake", "fixture-model", "structure_answer", "2")
        stored = enrichment.get_question(conn, "q1")
        assert stored == enrichment.find_question(conn, "quality_hint", "draft1")
        assert stored.cooldown_until == START.isoformat()
        assert (stored.not_sure_count, stored.asked_count, stored.last_outcome) == (
            1,
            2,
            "not_sure",
        )
        assert stored.last_asked_at == START.isoformat() and stored.version == 2
        assert stored.earlier_wordings == ("Earlier fictional question",)
        assert sessions.get_session(conn, "missing") is None
        assert sessions.get_answer(conn, "missing") is None
        assert enrichment.get_question(conn, "missing") is None
        assert enrichment.find_question(conn, "capture_note", "missing") is None


@pytest.mark.parametrize(
    "outcome,choice",
    [
        ("later", "next_session"),
        ("later", "week"),
        ("later", "month"),
        ("later", "2026-11-12"),
    ],
)
def test_answer_later_choices_roundtrip(db, outcome, choice):
    seed(db)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE session_answers SET outcome=?,later_choice=? WHERE id='a1'", (outcome, choice)
        )
    assert db.connection.execute("SELECT later_choice FROM session_answers").fetchone()[0] == choice


@pytest.mark.parametrize("extra", [dict(outcome="later"), dict(later_choice="week")])
def test_later_choice_only_on_later_outcomes(db, extra):
    seed(db)
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        insert(conn, "session_answers", answer_row("bad", position=2) | extra)


@pytest.mark.parametrize(
    "target", ["question_fact", "question_note", "session_job", "answer_session", "answer_question"]
)
def test_session_targets_reference_existing_rows(db, target):
    seed(db)
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        if target == "question_fact":
            insert(
                conn,
                "enrichment_questions",
                dict(
                    id="bad",
                    kind="quality_hint",
                    fact_id="missing",
                    created_at=STAMP,
                    updated_at=STAMP,
                ),
            )
        elif target == "question_note":
            insert(
                conn,
                "enrichment_questions",
                dict(
                    id="bad",
                    kind="capture_note",
                    capture_note_id="missing",
                    created_at=STAMP,
                    updated_at=STAMP,
                ),
            )
        elif target == "session_job":
            insert(
                conn,
                "sessions",
                session_row(
                    "bad",
                    state="ended",
                    ended_at=STAMP,
                    focus_kind="job",
                    focus_ref="missing",
                    job_id="missing",
                ),
            )
        else:
            column = "session_id" if target == "answer_session" else "question_id"
            insert(conn, "session_answers", answer_row("bad", position=2) | {column: "missing"})


def test_undo_apply_unaffected_by_job_focused_sessions(tmp_path):
    desk = Desk(tmp_path)
    try:
        desk.add_job("job1", "Fictional greenhouse technician")
        applied = desk.apply("job1")
        with desk.db.transaction() as conn:
            insert(conn, "sessions", session_row(focus_kind="job", focus_ref="job1", job_id="job1"))
        desk.applications.undo_apply("job1", expected_job_version=applied.job.version)
        assert desk.rows("SELECT * FROM applications") == []
        assert desk.rows("SELECT job_id FROM sessions") == [("job1",)]
    finally:
        desk.close()
