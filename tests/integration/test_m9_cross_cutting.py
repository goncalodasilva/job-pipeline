"""M9.6 part B: performance at scale (AC-M9.5's companion; spec 3.9; ADR-131; ADR-138).

One workspace with 1,000 jobs (500 of them with an application), 300 facts, 500 applications,
1,000 bank questions and 20 removal rules, written quickly through the storage layer and the
app's own services. Every fragment and page timed here answers in under 150 ms (median of 5),
including the inbox with rules, the Hidden view, palette search, every Settings area and the
prompt list.
Export and import of that workspace are timed too; the numbers are recorded in ADR-138.

Both tests are ``slow``. Fictional data; no network.
"""

import json
from collections.abc import Callable
from pathlib import Path
from time import perf_counter
from typing import Any

import pytest

from app.services.data_portability import DataPortabilityService
from tests.brief_pages import company_id
from tests.fakes.clock import FrozenClock
from tests.integration.test_m7_cross_cutting import BUDGET_SECONDS, measure, seed_scale
from tests.integration.test_m8_cross_cutting import seed_bank
from tests.integration.test_portability import START, fresh
from tests.refine_harness import Refining, refining
from tests.tailoring_pages import Tailoring

pytestmark = pytest.mark.integration

APPLICATIONS = 500
OTHER_JOBS = 500  # with the applications' own jobs: 1,000 jobs
FACTS = 300
RULES = 20
STATUSES = ("preparing", "applied", "screening", "interviewing", "rejected")
EXPORT_CEILING_S = 30.0  # a regression guard, far above what is measured (ADR-138)
IMPORT_CEILING_S = 60.0


def seed_m9_scale(r: Refining) -> dict[str, str]:
    """The M9 scale workspace; returns ids the timed requests need."""
    tw = Tailoring(r)
    first = tw.ready("scale-measured-job")
    for n in range(APPLICATIONS - 1):
        tw.seed_application(
            f"app-job-{n:04d}",
            f"Fictional Role {n}",
            f"Fictional Company {n % 40}",
            STATUSES[n % len(STATUSES)],
        )
    seed_scale(r, jobs=OTHER_JOBS, facts=FACTS)
    run = r.run
    db = run.app.state.workspace.db
    # Spread the inbox jobs over work modes and industries, remove a tenth with reasons, and add
    # twenty rules: hide and flag, on and off, over every field a rule can have.
    run.execute(
        "UPDATE jobs SET work_mode = CASE CAST(substr(id, 11) AS INTEGER) % 3 "
        "WHEN 0 THEN 'remote' WHEN 1 THEN 'hybrid' ELSE 'on_site' END, industry = 'logistics' "
        "WHERE id LIKE 'scale-job-%'"
    )
    for n in range(0, OTHER_JOBS, 10):
        version = run.rows("SELECT version FROM jobs WHERE id=?", (f"scale-job-{n:04d}",))
        run.app.state.job_triage.remove(
            f"scale-job-{n:04d}", expected_version=version[0]["version"], reason="location"
        )
    conditions = (
        ("work_mode", {"value": "remote"}),
        ("industry", {"value": "hospitality"}),
        ("company", {"value": "Fictional Company 7"}),
        ("seniority", {"value": "director"}),
        ("location", {"value": "Fictional Harbor"}),
    )
    with db.transaction() as conn:
        for n in range(RULES):
            field, condition = conditions[n % len(conditions)]
            conn.execute(
                "INSERT INTO removal_rules (id,field,condition,mode,evidence_job_ids,active,origin,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    f"scale-rule-{n:02d}",
                    field,
                    json.dumps(condition),
                    "hide" if n % 2 == 0 else "flag",
                    "[]",
                    0 if n % 7 == 6 else 1,
                    "pattern",
                    START.isoformat(),
                    START.isoformat(),
                ),
            )
    aid = str(first.id)
    cid = company_id(r)
    requirements = [
        row["text"] for row in run.rows("SELECT text FROM job_requirements ORDER BY position")
    ]
    seed_bank(db, aid, cid, requirements)
    counts = {
        table: run.rows(f"SELECT count(*) AS n FROM {table}")[0]["n"]  # noqa: S608 - fixed names
        for table in ("jobs", "applications", "facts", "bank_questions", "removal_rules")
    }
    assert counts["jobs"] == APPLICATIONS + OTHER_JOBS, counts
    assert counts["applications"] == APPLICATIONS, counts
    assert counts["facts"] >= FACTS, counts
    assert counts["bank_questions"] == 1000 and counts["removal_rules"] == RULES, counts
    fact = run.rows("SELECT id FROM facts WHERE status='confirmed' ORDER BY number LIMIT 1")
    return {
        "application": aid,
        "company": cid,
        "job": "scale-job-0001",
        "fact": str(fact[0]["id"]),
    }


def timed_requests(r: Refining, ids: dict[str, str]) -> dict[str, tuple[str, bool, str]]:
    """label -> (path, needs the launch token, what the answer must hold)."""
    aid, cid, job, fact = ids["application"], ids["company"], ids["job"], ids["fact"]
    fragments = {
        "inbox queue with rules": ("/inbox/queue", "data-job-id="),
        "inbox Hidden view": ("/inbox/queue?view=hidden", "data-job-id="),
        "inbox Removed view": ("/inbox/queue?view=removed", "data-job-id="),
        "removal suggestions": ("/inbox/removal-suggestions", ""),
        "job card": (f"/inbox/jobs/{job}/card", job),
        "job detail": (f"/inbox/jobs/{job}/detail", job),
        "palette search": ("/palette/search?q=fictional", "data-palette-result"),
        "palette search, a prefix": ("/palette/search?q=fict", "data-palette-result"),
        "palette search, nothing": ("/palette/search?q=zzzqqq", "data-palette-empty"),
        "palette recent": (f"/palette/recent?path=/jobs/{job}&path=/facts/{fact}/edit", job),
        "board columns": ("/board/columns", f'data-application-id="{aid}"'),
        "today": ("/today/content", ""),
        "application detail": (f"/applications/{aid}/detail", ""),
        "application prep": (f"/applications/{aid}/prep", "prep-question"),
        "fact library": ("/facts/library", "facts found"),
        "fact drafts": ("/facts/drafts", ""),
        "fact history": (f"/facts/{fact}/history", ""),
        "notifications": ("/notifications/panel", ""),
        "tasks panel": ("/tasks/panel", ""),
        "removal rules": ("/settings/preferences/removal-rules", "scale-rule-"),
        "preferences summary": ("/settings/preferences/summary", ""),
        "usage": ("/settings/usage", ""),
        "prompt editor": ("/settings/prompts/summarize_job/editor", "prompt-"),
    }
    pages = {
        "Settings: providers": ("/settings", ""),
        "Settings: appearance": ("/settings/appearance", ""),
        "Settings: budgets": ("/settings/budgets", ""),
        "Settings: data": ("/settings/data", ""),
        "Settings: keyboard": ("/settings/keyboard", ""),
        "Settings: lists": ("/settings/lists", ""),
        "Settings: preferences": ("/settings/preferences", ""),
        "Settings: prompt list": ("/settings/prompts", "prompt-link-summarize_job"),
        "Settings: sessions": ("/settings/sessions", ""),
        "Settings: your details": ("/settings/your-details", ""),
        "companies": ("/companies", "Fictional Company 7"),
        "company": (f"/companies/{cid}", ""),
        "inbox page": ("/inbox", ""),
        "board page": ("/board", ""),
    }
    found = {label: (path, True, needle) for label, (path, needle) in fragments.items()}
    found.update({label: (path, False, needle) for label, (path, needle) in pages.items()})
    return found


@pytest.mark.slow
def test_m9_fragments_under_150ms_at_scale(tmp_path: Path, record_property: Any) -> None:
    with refining(tmp_path) as r:
        ids = seed_m9_scale(r)
        client, htmx = r.client, r.run.run.htmx()
        hidden = client.get("/inbox/queue?view=hidden", headers=htmx).text
        assert hidden.count("data-job-id=") > 0, "the hide rules hide some jobs"
        results: dict[str, float] = {}
        for label, (path, protected, needle) in timed_requests(r, ids).items():

            def request(path: str = path, protected: bool = protected) -> Any:
                return client.get(path, headers=htmx if protected else None)

            check: Callable[[Any], bool] = lambda response, needle=needle: (  # noqa: E731
                response.status_code == 200 and needle in response.text
            )
            measure(results, label, request, check)
        for label, seconds in results.items():
            record_property(label, round(seconds * 1000, 2))
        report = ", ".join(f"{label}: {s * 1000:.1f} ms" for label, s in results.items())
        print(f"M9 150 ms medians (median of 5): {report}")
        slow = {label: round(s * 1000, 1) for label, s in results.items() if s >= BUDGET_SECONDS}
        assert not slow, f"over the {BUDGET_SECONDS * 1000:.0f} ms budget (median of 5): {slow}"


@pytest.mark.slow
def test_export_import_at_scale_timed(tmp_path: Path, record_property: Any) -> None:
    with refining(tmp_path / "source") as r:
        seed_m9_scale(r)
        db = r.run.app.state.workspace.db
        folder = db.path.parent
        exporter = DataPortabilityService(lambda: db, lambda: folder, FrozenClock(START))
        begin = perf_counter()
        archive = exporter.export(include_usage_snapshots=True)
        export_seconds = perf_counter() - begin
        counts = {
            table: db.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608
            for table in ("jobs", "applications", "facts", "bank_questions", "removal_rules")
        }
    with fresh(tmp_path / "target") as target:
        importer = DataPortabilityService(
            lambda: target, lambda: target.path.parent, FrozenClock(START)
        )
        begin = perf_counter()
        importer.restore(archive)
        import_seconds = perf_counter() - begin
        restored = {
            table: target.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608
            for table in counts
        }
    size_mb = archive.stat().st_size / 1_000_000
    record_property("export seconds", round(export_seconds, 3))
    record_property("import seconds", round(import_seconds, 3))
    record_property("export MB", round(size_mb, 2))
    print(f"M9 export {export_seconds:.2f} s, import {import_seconds:.2f} s, {size_mb:.1f} MB")
    assert restored == counts
    assert export_seconds < EXPORT_CEILING_S and import_seconds < IMPORT_CEILING_S
