"""M9.4 removal history survives upgrade and triage changes."""

import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.domain.errors import VersionConflict
from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from app.services.job_triage import JobTriageService
from tests.fakes.clock import FrozenClock
from tests.job_fixtures import generate_jobs, seed_jobs

pytestmark = pytest.mark.integration
NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)
JOB = "job-0001"


def history(db: Database) -> list[tuple]:
    return db.connection.execute(
        "SELECT job_id, event, reason, text, removed_at, backfilled "
        "FROM job_removals ORDER BY job_id, job_version"
    ).fetchall()


@pytest.fixture
def db(tmp_path: Path):
    with Database.open(tmp_path / "app.db") as database:
        apply_migrations(database, MIGRATIONS_DIR, tmp_path / "backups", FrozenClock(NOW))
        seed_jobs(database, generate_jobs(2))
        yield database


def test_0010_backfills_removal_history_and_backs_up(tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    previous.mkdir()
    for migration in MIGRATIONS_DIR.glob("*.sql"):
        if migration.name < "0010":
            shutil.copy(migration, previous)
    clock = FrozenClock(NOW)
    backups = tmp_path / "backups"
    with Database.open(tmp_path / "app.db") as database:
        apply_migrations(database, previous, backups, clock)
        seed_jobs(database, generate_jobs(3))
        with database.transaction() as conn:
            conn.execute(
                "UPDATE jobs SET triage_state='removed', removal_reason='other', "
                "removal_text='Fictional reason', updated_at=?, version=2 WHERE id=?",
                (NOW.isoformat(), JOB),
            )
            conn.execute("UPDATE jobs SET triage_state='removed' WHERE id='job-0002'")
            conn.execute("UPDATE jobs SET removal_reason='other' WHERE id='job-0003'")
        result = apply_migrations(database, MIGRATIONS_DIR, backups, clock)

        assert result.applied == (10,)
        assert result.backup is not None and result.backup.is_file()
        assert history(database) == [
            (JOB, "remove", "other", "Fictional reason", NOW.isoformat(), 1)
        ]
        with sqlite3.connect(result.backup) as backup:
            assert backup.execute("SELECT max(version) FROM schema_migrations").fetchone() == (9,)
            assert backup.execute(
                "SELECT removal_text FROM jobs WHERE id=?", (JOB,)
            ).fetchone() == ("Fictional reason",)


def test_0010_removal_history_append_only(db: Database) -> None:
    triage = JobTriageService(lambda: db, FrozenClock(NOW), ZoneInfo("UTC"))
    removed = triage.remove(JOB, expected_version=1, reason="other", text="Fictional reason")
    with pytest.raises(VersionConflict):
        triage.undo(JOB, expected_version=1)
    restored = triage.undo(JOB, expected_version=removed.version)
    removed = triage.remove(JOB, expected_version=restored.version, reason="location")
    triage.restore(JOB, expected_version=removed.version)
    before = history(db)
    assert [(row[1], row[2], row[3], row[5]) for row in before] == [
        ("remove", "other", "Fictional reason", 0),
        ("restore", None, None, 0),
        ("remove", "location", None, 0),
        ("restore", None, None, 0),
    ]
    for sql in (
        "UPDATE job_removals SET text=NULL",
        "DELETE FROM job_removals",
        "INSERT OR REPLACE INTO job_removals SELECT * FROM job_removals",
    ):
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
            conn.execute(sql)
    assert history(db) == before


def test_0010_rules_versioned_and_values_checked(db: Database) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO removal_rules (id, field, condition, mode, evidence_job_ids, origin, "
            "created_at, updated_at) VALUES ('rule-0001', 'work_mode', "
            "'{\"value\":\"on_site\"}', 'flag', '[\"job-0001\"]', 'pattern', ?, ?)",
            (NOW.isoformat(), NOW.isoformat()),
        )
        assert conn.execute("SELECT version, active FROM removal_rules").fetchone() == (1, 1)
    for sql in (
        "UPDATE removal_rules SET field='notes'",
        "UPDATE removal_rules SET mode='delete'",
        "UPDATE removal_rules SET condition='invalid json'",
        "UPDATE removal_rules SET evidence_job_ids='{}'",
        "UPDATE removal_rules SET origin='other'",
        "UPDATE removal_rules SET active=2",
        "UPDATE removal_rules SET version=0",
    ):
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
            conn.execute(sql)


def test_salary_condition_matches_only_the_same_annual_ceiling_and_currency(db: Database) -> None:
    from app.modules.storage.removal_learning_repo import signals

    with db.transaction() as conn:
        conn.execute(
            "UPDATE jobs SET salary_min=40000,salary_max=50000,salary_currency='EUR',"
            "salary_period='annual' WHERE id=?",
            (JOB,),
        )
        conn.execute(
            "UPDATE jobs SET salary_min=35000,salary_max=45000,salary_currency='EUR',"
            "salary_period='year' WHERE id='job-0002'"
        )
        first, second = signals(conn, NOW.date())
        (ceiling,) = (value for value in first.conditions if value.field == "minimum_salary")
        assert ceiling.value == "50000 EUR per year"
        assert ceiling not in second.conditions
        conn.execute("UPDATE jobs SET salary_max=50000,salary_currency='USD' WHERE id='job-0002'")
        assert ceiling not in signals(conn, NOW.date(), job_id="job-0002")[0].conditions
        conn.execute(
            "UPDATE jobs SET salary_currency='EUR',salary_period='month' WHERE id='job-0002'"
        )
        assert ceiling not in signals(conn, NOW.date(), job_id="job-0002")[0].conditions


def test_grouping_reads_only_current_notes_with_their_removal_versions(db: Database) -> None:
    from app.modules.storage.removal_learning_repo import removal_notes

    triage = JobTriageService(lambda: db, FrozenClock(NOW), ZoneInfo("UTC"))
    one = triage.remove(JOB, expected_version=1, reason="other", text="Old fictional note")
    restored = triage.restore(JOB, expected_version=one.version)
    two = triage.remove(
        JOB, expected_version=restored.version, reason="other", text="Current fictional note"
    )
    separate = triage.remove(
        "job-0002", expected_version=1, reason="not_interested", text="Restored fictional note"
    )
    triage.undo(separate.id, expected_version=separate.version)
    with db.read() as conn:
        (note,) = removal_notes(conn)
    assert (note.job_id, note.removal_version, note.text) == (
        JOB,
        two.version,
        "Current fictional note",
    )
