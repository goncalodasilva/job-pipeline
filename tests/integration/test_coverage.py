"""M7.1 coverage reads from fictional current requirements and citation snapshots."""

import json
from pathlib import Path

import pytest

from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations, coverage_repo
from tests.fakes.clock import FrozenClock
from tests.integration.test_schema_0003 import add_fact, add_version, skill_row
from tests.integration.test_schema_0004 import STAMP, START, insert, job_row

pytestmark = pytest.mark.integration


@pytest.fixture
def db(tmp_path: Path):
    with Database.open(tmp_path / "fictional.db") as database:
        apply_migrations(database, MIGRATIONS_DIR, tmp_path / "backups", FrozenClock(START))
        yield database


def summary(conn, job_id, id, text="Use Python", stamp=STAMP):
    insert(
        conn,
        "job_summaries",
        dict(
            id=id,
            job_id=job_id,
            summary="Fictional summary " + id,
            fields_json="{}",
            provider="fake",
            model="fake",
            prompt_id="summarize_job",
            prompt_version="1",
            created_at=stamp,
        ),
    )
    insert(
        conn,
        "job_requirements",
        dict(id="r-" + id, summary_id=id, position=1, kind="must_have", text=text),
    )


def application(conn, job_id, status):
    insert(
        conn,
        "applications",
        dict(
            id="a-" + job_id, job_id=job_id, status_key=status, created_at=STAMP, updated_at=STAMP
        ),
    )


def assessment(conn, job_id, summary_id, id, refs, sent=None, met=True, stamp=STAMP):
    insert(
        conn,
        "job_assessments",
        dict(
            id=id,
            job_id=job_id,
            summary_id=summary_id,
            result_json=json.dumps(
                {
                    "requirements": [
                        {"requirement_id": "r-" + summary_id, "met": met, "fact_ids": refs}
                    ]
                }
            ),
            fit_band="strong",
            fact_refs_json=json.dumps(refs if sent is None else sent),
            provider="fake",
            model="fake",
            prompt_id="assess_fit",
            prompt_version="1",
            created_at=stamp,
        ),
    )


def gap(conn, job_id, summary_id, id, refs, sent, verdict="partial", stamp=STAMP):
    insert(
        conn,
        "gap_checks",
        dict(
            id=id,
            application_id="a-" + job_id,
            summary="Fictional summary " + summary_id,
            result=json.dumps(
                {
                    "requirements": [
                        {"requirement_id": "r-" + summary_id, "verdict": verdict, "fact_ids": refs}
                    ]
                }
            ),
            created_at=stamp,
        ),
    )
    for number, version in sent:
        conn.execute(
            "INSERT INTO gap_check_facts (gap_check_id, fact_id, fact_version_id) "
            "SELECT ?, f.id, v.id FROM facts f JOIN fact_versions v ON v.fact_id=f.id "
            "WHERE f.number=? AND v.version=?",
            (id, number, version),
        )


def pairs(data):
    return {(link.requirement_id, link.fact.id) for link in data.evidence}


def test_coverage_counts_saved_and_active_jobs_only(db):
    with db.transaction() as conn:
        for name, triage, status in [
            ("new", "new", None),
            ("seen", "seen", None),
            ("snoozed", "snoozed", None),
            ("to-apply", "to_apply", None),
            ("preparing", "to_apply", "preparing"),
            ("active", "to_apply", "applied"),
            ("closed", "seen", "rejected"),
            ("removed", "removed", None),
            ("removed-active", "removed", "applied"),
            ("merged", "new", None),
        ]:
            insert(conn, "jobs", job_row(name, triage_state=triage))
            summary(conn, name, "s-" + name)
            if status:
                application(conn, name, status)
        conn.execute("UPDATE jobs SET merged_into_job_id='new' WHERE id='merged'")
        summary(conn, "new", "s-newer", stamp="2026-10-20T09:00:00+00:00")
    before = db.connection.total_changes
    with db.read() as conn:
        data = coverage_repo.read(conn)
    assert {r.job_id for r in data.requirements} == {
        "new",
        "seen",
        "snoozed",
        "to-apply",
        "preparing",
        "active",
    }
    assert "r-s-new" not in {r.id for r in data.requirements}
    assert {r.id for r in data.requirements if r.job_id == "new"} == {"r-s-newer"}
    assert {r.job_id: r.job_state for r in data.requirements}["active"] == "active"
    assert {r.job_id: r.job_state for r in data.requirements}["preparing"] == "preparing"
    assert db.connection.total_changes == before


def test_coverage_evidence_from_current_citations_and_skills(db):
    for n in range(1, 9):
        add_fact(
            db,
            f"f{n}",
            n,
            f"v{n}",
            fact={
                "status": "draft" if n == 4 else "archived" if n == 5 else "confirmed",
                "privacy": "never_use" if n == 6 else "normal",
                "type": "story" if n == 1 else "fact",
            },
        )
    add_version(db, "v7-new", "f7", 2, text="Changed fictional evidence")
    with db.transaction() as conn:
        insert(conn, "skills", skill_row("python", "Python", aliases='["py"]'))
        for n in (3, 4, 5, 6):
            insert(conn, "fact_skills", dict(fact_id=f"f{n}", skill_id="python"))
        for name in ("fit", "gap", "old-fit", "old-gap", "missing", "mixed", "stale"):
            insert(conn, "jobs", job_row(name))
            summary(conn, name, name)
            application(conn, name, "preparing")
        assessment(conn, "fit", "fit", "fit1", ["F1@1", "F2@1"])
        gap(conn, "gap", "gap", "gap1", ["F2@1"], [(2, 1)])
        assessment(conn, "old-fit", "old-fit", "of1", ["F1@1"])
        assessment(conn, "old-fit", "old-fit", "of2", [], met=False)
        gap(conn, "old-gap", "old-gap", "og1", ["F1@1"], [(1, 1)])
        gap(conn, "old-gap", "old-gap", "og2", [], [], verdict="missing")
        gap(conn, "missing", "missing", "miss", ["F1@1"], [], verdict="met")
        assessment(conn, "mixed", "mixed", "mix", ["F1@1", "F8@1"], sent=["F1@1"])
        assessment(conn, "stale", "stale", "st", ["F7@1"])
    with db.read() as conn:
        data = coverage_repo.read(conn)
    assert pairs(data) == {
        ("r-fit", "f1"),
        ("r-fit", "f2"),
        ("r-gap", "f2"),
        *(
            ("r-" + name, "f3")
            for name in ("fit", "gap", "old-fit", "old-gap", "missing", "mixed", "stale")
        ),
    }
    assert data.skills[0].aliases == ("py",)


def test_coverage_ignores_superseded_summary_citations_and_gap_versions(db):
    add_fact(db, "f1", 1, "v1", fact={"status": "confirmed"})
    add_fact(db, "f2", 2, "v2", fact={"status": "confirmed"})
    with db.transaction() as conn:
        insert(conn, "jobs", job_row("j"))
        application(conn, "j", "preparing")
        summary(conn, "j", "old")
        assessment(conn, "j", "old", "a1", ["F1@1"])
        gap(conn, "j", "old", "g1", ["F1@1"], [(1, 1)])
        summary(conn, "j", "new")  # same timestamp; existing summary ordering uses rowid
        gap(conn, "j", "new", "g2", ["F2@1"], [(2, 1)])
    add_version(db, "v2-new", "f2", 2, text="Revised fictional content")
    with db.read() as conn:
        assert pairs(coverage_repo.read(conn)) == set()


@pytest.mark.parametrize("job_count", [1, 20])
def test_coverage_reads_use_indexes(db, job_count):
    with db.transaction() as conn:
        for n in range(job_count):
            insert(conn, "jobs", job_row(f"j{n}"))
            summary(conn, f"j{n}", f"s{n}")
    statements = []
    conn = db.connection
    conn.set_trace_callback(statements.append)
    try:
        coverage_repo.read(conn)
    finally:
        conn.set_trace_callback(None)
    reads = [sql for sql in statements if sql.lstrip().upper().startswith(("SELECT", "WITH"))]
    assert len(reads) == 5  # fixed number of sources, never a query per job or citation
    plans = [
        "\n".join(str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql)) for sql in reads
    ]
    for plan in plans[:3]:
        assert "job_summaries_job_id_created_at" in plan
        assert "SEARCH r USING INDEX sqlite_autoindex_job_requirements_" in plan
    assert "job_assessments_job_id_created_at" in plans[1]
    assert "gap_checks_application_id_created_at" in plans[2]
