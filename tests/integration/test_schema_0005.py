"""M5 applications migration, constraints, audited Undo and optimistic updates."""

import importlib
import shutil
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.domain.errors import VersionConflict
from app.domain.ids import is_safe_id
from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from tests.fakes.clock import FrozenClock
from tests.integration.test_schema_0004 import company_row, insert, job_row

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 20, 10, tzinfo=UTC)
STAMP = NOW.isoformat()
MUTABLE = (
    "application_statuses",
    "applications",
    "application_notes",
    "contacts",
    "application_contacts",
    "interactions",
    "events",
    "reminder_rules",
    "reminder_states",
)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    assert (MIGRATIONS_DIR / "0005_applications.sql").is_file()
    with Database.open(tmp_path / "data/app.db") as database:
        apply_migrations(database, MIGRATIONS_DIR, tmp_path / "backups", FrozenClock(NOW))
        yield database


def seed(conn: sqlite3.Connection, source: str = "created") -> None:
    insert(conn, "companies", company_row())
    insert(conn, "companies", company_row("comp-0002", "Example Ferries"))
    insert(conn, "jobs", job_row(company_id="comp-0001", triage_state="to_apply"))
    insert(
        conn,
        "applications",
        dict(
            id="app-example",
            job_id="job-0001",
            status_key="preparing",
            created_at=STAMP,
            updated_at=STAMP,
        ),
    )
    insert(
        conn,
        "status_history",
        dict(
            id="history-example",
            application_id="app-example",
            from_status=None,
            to_status="preparing",
            changed_at=STAMP,
            source=source,
            created_at=STAMP,
        ),
    )


def child_row(kind: str) -> dict[str, object]:
    common = dict(id=f"{kind}-example", created_at=STAMP, updated_at=STAMP)
    if kind == "contacts":
        return dict(**common, company_id="comp-0001", name="Alex Example")
    if kind == "application_notes":
        return dict(**common, application_id="app-example", kind="note", text="Fictional note")
    if kind == "events":
        return dict(
            **common,
            application_id="app-example",
            type="interview",
            starts_at=STAMP,
            ends_at="2026-10-20T11:00:00+00:00",
            time_zone="Europe/Lisbon",
        )
    if kind == "interactions":
        return dict(
            **common,
            application_id="app-example",
            happened_at=STAMP,
            channel="email",
            note="Fictional interaction",
        )
    raise AssertionError(kind)


def test_0005_upgrade_keeps_0004_rows_and_backs_up(tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    previous.mkdir()
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if migration.name < "0005":
            shutil.copy(migration, previous / migration.name)
    with Database.open(tmp_path / "data/app.db") as db:
        apply_migrations(db, previous, tmp_path / "backups", FrozenClock(NOW))
        with db.transaction() as conn:
            insert(conn, "companies", company_row())
            insert(conn, "jobs", job_row(company_id="comp-0001"))
        before = db.connection.execute("SELECT * FROM jobs").fetchall()
        result = apply_migrations(db, MIGRATIONS_DIR, tmp_path / "backups", FrozenClock(NOW))
        assert 5 in result.applied
        assert db.connection.execute("SELECT * FROM jobs").fetchall() == before
        backups = list((tmp_path / "backups").glob("*.db"))
        assert len(backups) == 1
        with sqlite3.connect(backups[0]) as backup:
            assert backup.execute("SELECT * FROM jobs").fetchall() == before
            assert (
                backup.execute(
                    "SELECT count(*) FROM sqlite_master WHERE name='applications'"
                ).fetchone()[0]
                == 0
            )


def test_0005_backfills_one_preparing_application_per_to_apply_job(tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    previous.mkdir()
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if migration.name < "0005":
            shutil.copy(migration, previous / migration.name)
    with Database.open(tmp_path / "data/app.db") as db:
        apply_migrations(db, previous, tmp_path / "backups", FrozenClock(NOW))
        with db.transaction() as conn:
            for key, state, merged in [
                ("ready", "to_apply", None),
                ("ready-two", "to_apply", None),
                ("merged", "to_apply", "ready"),
                ("removed", "removed", None),
                ("new", "new", None),
                ("seen", "seen", None),
            ]:
                insert(conn, "jobs", job_row(key, triage_state=state, merged_into_job_id=merged))
        apply_migrations(db, MIGRATIONS_DIR, tmp_path / "backups", FrozenClock(NOW))
        rows = db.connection.execute(
            "SELECT id,job_id,status_key,applied_at,version FROM applications ORDER BY job_id"
        ).fetchall()
        assert [row[1] for row in rows] == ["ready", "ready-two"]
        for app_id, _job_id, status, applied_at, version in rows:
            assert is_safe_id(app_id)
            assert (status, applied_at, version) == ("preparing", None, 1)
            history = db.connection.execute(
                "SELECT id,from_status,to_status,source FROM status_history WHERE application_id=?",
                (app_id,),
            ).fetchall()
            assert len(history) == 1 and is_safe_id(history[0][0])
            assert history[0][1:] == (None, "preparing", "backfill")
        apply_migrations(db, MIGRATIONS_DIR, tmp_path / "backups", FrozenClock(NOW))
        assert db.connection.execute("SELECT count(*) FROM applications").fetchone()[0] == 2


def test_default_statuses_and_rule_seeded_once(db: Database, tmp_path: Path) -> None:
    expected = [
        ("preparing", "preparing"),
        ("applied", "active"),
        ("screening", "active"),
        ("interviewing", "active"),
        ("offer", "active"),
        ("rejected", "closed"),
        ("withdrawn", "closed"),
        ("accepted", "closed"),
        ("declined", "closed"),
    ]
    assert (
        db.connection.execute(
            "SELECT key,phase FROM application_statuses ORDER BY position"
        ).fetchall()
        == expected
    )
    assert db.connection.execute(
        "SELECT status_key,days,enabled FROM reminder_rules"
    ).fetchall() == [("applied", 14, 1)]
    apply_migrations(db, MIGRATIONS_DIR, tmp_path / "again", FrozenClock(NOW))
    assert db.connection.execute("SELECT count(*) FROM reminder_rules").fetchone()[0] == 1
    for table in MUTABLE:
        columns = {row[1] for row in db.connection.execute(f'PRAGMA table_info("{table}")')}
        assert {"created_at", "updated_at", "version"} <= columns
    assert "phase" not in {
        row[1] for row in db.connection.execute("PRAGMA table_info(applications)")
    }


@pytest.mark.parametrize(
    "phase,label", [("other", "Waiting"), (None, "Waiting"), ("active", ""), ("active", " \t\n ")]
)
def test_status_check_rejects_unknown_phase_and_missing_label(
    db: Database, phase: str | None, label: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        insert(
            conn,
            "application_statuses",
            dict(
                key="custom",
                label=label,
                phase=phase,
                position=10,
                built_in=0,
                created_at=STAMP,
                updated_at=STAMP,
            ),
        )


def test_application_status_must_exist_and_in_use_status_cannot_be_deleted(db: Database) -> None:
    with db.transaction() as conn:
        seed(conn)
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute("UPDATE applications SET status_key='missing'")
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute("DELETE FROM application_statuses WHERE key='preparing'")
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute("DELETE FROM application_statuses WHERE key='declined'")


def test_one_application_per_job(db: Database) -> None:
    with db.transaction() as conn:
        seed(conn)
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        insert(
            conn,
            "applications",
            dict(
                id="duplicate",
                job_id="job-0001",
                status_key="preparing",
                created_at=STAMP,
                updated_at=STAMP,
            ),
        )
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO applications (id,job_id,status_key,created_at,updated_at) "
            "VALUES ('replacement','job-0001','preparing',?,?)",
            (STAMP, STAMP),
        )
    assert db.connection.execute("SELECT id FROM applications").fetchall() == [("app-example",)]


def test_status_history_refuses_update_and_delete(db: Database) -> None:
    with db.transaction() as conn:
        seed(conn)
    for statement in [
        "UPDATE status_history SET source='menu'",
        "DELETE FROM status_history",
        "INSERT OR REPLACE INTO status_history SELECT * FROM status_history",
    ]:
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
            conn.execute(statement)
    assert db.connection.execute("SELECT source FROM status_history").fetchall() == [("created",)]


@pytest.mark.parametrize("source", ["created", "backfill"])
def test_untouched_application_delete_cascades_only_initial_history(
    db: Database, source: str
) -> None:
    with db.transaction() as conn:
        seed(conn, source)
        conn.execute("DELETE FROM applications WHERE id='app-example'")
    assert db.connection.execute("SELECT count(*) FROM applications").fetchone()[0] == 0
    assert db.connection.execute("SELECT count(*) FROM status_history").fetchone()[0] == 0
    assert db.connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


@pytest.mark.parametrize(
    "change",
    [
        "version",
        "status",
        "applied_at",
        "history",
        "application_notes",
        "events",
        "application_contacts",
        "interactions",
    ],
)
def test_changed_application_delete_refused_atomically(db: Database, change: str) -> None:
    with db.transaction() as conn:
        seed(conn)
        if change == "version":
            conn.execute("UPDATE applications SET version=2")
        elif change == "status":
            conn.execute("UPDATE applications SET status_key='applied'")
        elif change == "applied_at":
            conn.execute("UPDATE applications SET applied_at=?", (STAMP,))
        elif change == "history":
            conn.execute(
                "INSERT INTO status_history "
                "(id,application_id,from_status,to_status,changed_at,source,created_at) "
                "VALUES ('second','app-example','preparing','preparing',?,'menu',?)",
                (STAMP, STAMP),
            )
        elif change == "application_contacts":
            insert(conn, "contacts", child_row("contacts"))
            insert(
                conn,
                "application_contacts",
                dict(
                    application_id="app-example",
                    contact_id="contacts-example",
                    created_at=STAMP,
                    updated_at=STAMP,
                ),
            )
        else:
            insert(conn, change, child_row(change))
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute("DELETE FROM applications WHERE id='app-example'")
    assert db.connection.execute("SELECT id FROM applications").fetchall() == [("app-example",)]
    assert db.connection.execute("SELECT count(*) FROM status_history").fetchone()[0] == (
        2 if change == "history" else 1
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"type": "other"},
        {"status": "other"},
        {"time_zone": ""},
        {"time_zone": "  "},
        {"starts_at": "2026-10-20T10:00:00+01:00"},
        {"ends_at": "2026-10-20T09:00:00+00:00"},
    ],
)
def test_event_checks_reject_unknown_type_status_and_empty_zone(
    db: Database, changes: dict[str, str]
) -> None:
    with db.transaction() as conn:
        seed(conn)
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        insert(conn, "events", {**child_row("events"), **changes})


def test_contact_belongs_to_one_company_and_link_unique(db: Database) -> None:
    with db.transaction() as conn:
        seed(conn)
        insert(conn, "contacts", child_row("contacts"))
        row = dict(
            application_id="app-example",
            contact_id="contacts-example",
            created_at=STAMP,
            updated_at=STAMP,
        )
        insert(conn, "application_contacts", row)
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        insert(conn, "application_contacts", row)
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute("UPDATE contacts SET company_id='missing'")
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute("UPDATE contacts SET company_id='comp-0002'")


def test_application_update_with_stale_version_raises_conflict(db: Database) -> None:
    repo = importlib.import_module("app.modules.storage.applications_repo")
    with db.transaction() as conn:
        seed(conn)
        repo.update_application(
            conn,
            "app-example",
            status_key="applied",
            applied_at=STAMP,
            now=STAMP,
            expected_version=1,
        )
    with pytest.raises(VersionConflict), db.transaction() as conn:
        repo.update_application(
            conn,
            "app-example",
            status_key="rejected",
            applied_at=STAMP,
            now=STAMP,
            expected_version=1,
        )
    assert db.connection.execute(
        "SELECT status_key,version,applied_at FROM applications"
    ).fetchall() == [("applied", 2, STAMP)]


@pytest.mark.parametrize("days", [0, 366, 1.5])
def test_reminder_rule_days_bounds_and_state_snooze_check(db: Database, days: float) -> None:
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute("UPDATE reminder_rules SET days=?", (days,))
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        insert(
            conn,
            "reminder_states",
            dict(
                id="state",
                occurrence_key="example",
                state="snoozed",
                created_at=STAMP,
                updated_at=STAMP,
            ),
        )


@pytest.mark.parametrize("change", ["job_company", "application_job", "replace_contact"])
def test_contact_company_links_survive_parent_edits(db: Database, change: str) -> None:
    with db.transaction() as conn:
        seed(conn)
        insert(conn, "jobs", job_row("other-job", company_id="comp-0002"))
        insert(conn, "contacts", child_row("contacts"))
        insert(
            conn,
            "application_contacts",
            dict(
                application_id="app-example",
                contact_id="contacts-example",
                created_at=STAMP,
                updated_at=STAMP,
            ),
        )
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        if change == "job_company":
            conn.execute("UPDATE jobs SET company_id='comp-0002' WHERE id='job-0001'")
        elif change == "application_job":
            conn.execute("UPDATE applications SET job_id='other-job' WHERE id='app-example'")
        else:
            conn.execute(
                "INSERT OR REPLACE INTO contacts (id,company_id,name,created_at,updated_at) "
                "VALUES ('contacts-example','comp-0002','Alex Example',?,?)",
                (STAMP, STAMP),
            )
    assert db.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_interaction_contact_must_match_application_company(db: Database) -> None:
    with db.transaction() as conn:
        seed(conn)
        insert(conn, "contacts", {**child_row("contacts"), "company_id": "comp-0002"})
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        insert(
            conn, "interactions", {**child_row("interactions"), "contact_id": "contacts-example"}
        )


def test_repository_reads_and_preserves_first_applied_time(db: Database) -> None:
    repo = importlib.import_module("app.modules.storage.applications_repo")
    with db.transaction() as conn:
        seed(conn)
        assert repo.get_application(conn, "missing") is None
        original = repo.get_application(conn, "app-example")
        assert original is not None and original.job_id == "job-0001" and original.version == 1
        repo.update_application(
            conn,
            "app-example",
            status_key="applied",
            applied_at=STAMP,
            now=STAMP,
            expected_version=1,
        )
        repo.update_application(
            conn,
            "app-example",
            status_key="rejected",
            applied_at=None,
            now=STAMP,
            expected_version=2,
        )
        assert repo.get_application(conn, "app-example").applied_at == STAMP
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute("UPDATE applications SET applied_at=NULL")


def test_interaction_without_explicit_link_still_protects_contact_company(db: Database) -> None:
    with db.transaction() as conn:
        seed(conn)
        insert(conn, "contacts", child_row("contacts"))
        insert(
            conn, "interactions", {**child_row("interactions"), "contact_id": "contacts-example"}
        )
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute("UPDATE contacts SET company_id='comp-0002'")
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute("UPDATE jobs SET company_id='comp-0002' WHERE id='job-0001'")


def test_board_and_reminder_query_columns_are_indexed(db: Database) -> None:
    expected = {
        "application_statuses": [("phase", "position")],
        "applications": [("status_key", "updated_at"), ("applied_at",), ("updated_at",)],
        "status_history": [("application_id", "changed_at"), ("to_status", "changed_at")],
        "events": [("application_id", "status", "starts_at"), ("status", "starts_at")],
        "contacts": [("company_id", "name")],
        "interactions": [("contact_id", "happened_at"), ("application_id", "happened_at")],
        "reminder_rules": [("enabled", "status_key")],
        "reminder_states": [("state", "snooze_until"), ("seen_at",)],
    }
    for table, prefixes in expected.items():
        indexes = [
            tuple(item[2] for item in db.connection.execute(f'PRAGMA index_info("{row[1]}")'))
            for row in db.connection.execute(f'PRAGMA index_list("{table}")')
        ]
        for prefix in prefixes:
            assert any(columns[: len(prefix)] == prefix for columns in indexes), (table, prefix)


def test_update_or_replace_cannot_delete_changed_application_and_history(db: Database) -> None:
    with db.transaction() as conn:
        seed(conn)
        conn.execute("UPDATE applications SET version=2")
        insert(
            conn,
            "status_history",
            dict(
                id="second",
                application_id="app-example",
                from_status="preparing",
                to_status="preparing",
                changed_at=STAMP,
                source="menu",
                created_at=STAMP,
            ),
        )
        insert(conn, "jobs", job_row("job-other"))
        insert(
            conn,
            "applications",
            dict(
                id="app-other",
                job_id="job-other",
                status_key="preparing",
                created_at=STAMP,
                updated_at=STAMP,
            ),
        )
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute("UPDATE OR REPLACE applications SET job_id='job-0001' WHERE id='app-other'")
    assert db.connection.execute("SELECT id,version FROM applications ORDER BY id").fetchall() == [
        ("app-example", 2),
        ("app-other", 1),
    ]
    assert db.connection.execute("SELECT count(*) FROM status_history").fetchone()[0] == 2


@pytest.mark.parametrize("change", ["label_collision", "key_collision", "built_in_flag"])
def test_status_update_cannot_replace_or_downgrade_builtin(db: Database, change: str) -> None:
    with db.transaction() as conn:
        insert(
            conn,
            "application_statuses",
            dict(
                key="custom",
                label="Waiting",
                phase="active",
                position=10,
                built_in=0,
                created_at=STAMP,
                updated_at=STAMP,
            ),
        )
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        if change == "label_collision":
            conn.execute(
                "UPDATE OR REPLACE application_statuses SET label='Declined' WHERE key='custom'"
            )
        elif change == "key_collision":
            conn.execute(
                "UPDATE OR REPLACE application_statuses SET key='declined' WHERE key='custom'"
            )
        else:
            conn.execute("UPDATE application_statuses SET built_in=0 WHERE key='declined'")
    assert (
        db.connection.execute(
            "SELECT built_in FROM application_statuses WHERE key='declined'"
        ).fetchone()[0]
        == 1
    )


def test_contact_identity_cannot_replace_linked_contact(db: Database) -> None:
    with db.transaction() as conn:
        seed(conn)
        insert(conn, "contacts", child_row("contacts"))
        insert(
            conn,
            "application_contacts",
            dict(
                application_id="app-example",
                contact_id="contacts-example",
                created_at=STAMP,
                updated_at=STAMP,
            ),
        )
        insert(
            conn, "contacts", {**child_row("contacts"), "id": "other", "company_id": "comp-0002"}
        )
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute("UPDATE OR REPLACE contacts SET id='contacts-example' WHERE id='other'")


def test_job_identity_cannot_replace_an_application_company_link(db: Database) -> None:
    with db.transaction() as conn:
        seed(conn)
        insert(conn, "contacts", child_row("contacts"))
        insert(
            conn,
            "application_contacts",
            dict(
                application_id="app-example",
                contact_id="contacts-example",
                created_at=STAMP,
                updated_at=STAMP,
            ),
        )
        insert(conn, "jobs", job_row("other-job", company_id="comp-0002"))
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute("UPDATE OR REPLACE jobs SET id='job-0001' WHERE id='other-job'")
    assert (
        db.connection.execute("SELECT company_id FROM jobs WHERE id='job-0001'").fetchone()[0]
        == "comp-0001"
    )


@pytest.mark.parametrize("alias", ["rowid", "oid", "_rowid_"])
@pytest.mark.parametrize(
    "target",
    [
        "application_update",
        "application_insert",
        "status_update",
        "status_insert",
        "history_insert",
    ],
)
def test_implicit_rowid_cannot_replace_audited_rows(db: Database, alias: str, target: str) -> None:
    with db.transaction() as conn:
        seed(conn)
        conn.execute("UPDATE applications SET version=2")
        insert(conn, "jobs", job_row("job-other"))
        insert(
            conn,
            "applications",
            dict(
                id="app-other",
                job_id="job-other",
                status_key="preparing",
                created_at=STAMP,
                updated_at=STAMP,
            ),
        )
        insert(
            conn,
            "application_statuses",
            dict(
                key="custom",
                label="Waiting",
                phase="active",
                position=10,
                built_in=0,
                created_at=STAMP,
                updated_at=STAMP,
            ),
        )
    with pytest.raises(sqlite3.Error), db.transaction() as conn:
        if target == "application_update":
            conn.execute(
                f"UPDATE OR REPLACE applications SET {alias}="  # noqa: S608 - fixed aliases
                f"(SELECT {alias} FROM applications WHERE id='app-example') "
                "WHERE id='app-other'"
            )
        elif target == "application_insert":
            insert(conn, "jobs", job_row("job-third"))
            conn.execute(
                f"INSERT OR REPLACE INTO applications "  # noqa: S608 - fixed aliases
                f"({alias},id,job_id,status_key,created_at,updated_at) "
                f"SELECT {alias},'app-third','job-third','preparing',created_at,updated_at "
                "FROM applications WHERE id='app-example'"
            )
        elif target == "status_update":
            conn.execute(
                f"UPDATE OR REPLACE application_statuses SET {alias}="  # noqa: S608 - fixed aliases
                f"(SELECT {alias} FROM application_statuses WHERE key='declined') "
                "WHERE key='custom'"
            )
        elif target == "status_insert":
            conn.execute(
                f"INSERT OR REPLACE INTO application_statuses "  # noqa: S608 - fixed aliases
                f"({alias},key,label,phase,position,created_at,updated_at) "
                f"SELECT {alias},'another','Another','active',11,created_at,updated_at "
                "FROM application_statuses WHERE key='declined'"
            )
        else:
            conn.execute(
                f"INSERT OR REPLACE INTO status_history "  # noqa: S608 - fixed aliases
                f"({alias},id,application_id,from_status,to_status,changed_at,source,created_at) "
                f"SELECT {alias},'replaced',application_id,from_status,to_status,changed_at,"
                "source,created_at FROM status_history WHERE id='history-example'"
            )
    assert (
        db.connection.execute("SELECT version FROM applications WHERE id='app-example'").fetchone()[
            0
        ]
        == 2
    )
    assert db.connection.execute("SELECT id FROM status_history").fetchall() == [
        ("history-example",)
    ]
    assert (
        db.connection.execute(
            "SELECT built_in FROM application_statuses WHERE key='declined'"
        ).fetchone()[0]
        == 1
    )
