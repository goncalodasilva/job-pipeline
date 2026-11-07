"""M4.6: versioned triage and bounded, literal-search inbox queries."""

import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.domain.errors import JobNotFound, TriageNotAllowed, VersionConflict
from app.domain.inbox import InboxFilters
from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations, jobs_repo
from app.services.inbox import MAX_PAGE, InboxQuery
from app.services.job_triage import JobTriageService
from tests.fakes.clock import FrozenClock
from tests.job_fixtures import generate_jobs, seed_jobs

pytestmark = pytest.mark.integration


@pytest.fixture
def desk(tmp_path):
    clock = FrozenClock(datetime(2026, 10, 20, 21, 59, tzinfo=UTC))
    with Database.open(tmp_path / "app.db") as db:
        apply_migrations(db, MIGRATIONS_DIR, tmp_path / "backups", clock)
        seed_jobs(db, generate_jobs(1000))
        yield (
            db,
            clock,
            JobTriageService(lambda: db, clock, ZoneInfo("Europe/Paris")),
            InboxQuery(lambda: db, clock, ZoneInfo("Europe/Paris")),
        )


def read(db, id="job-0001"):
    with db.read() as conn:
        return jobs_repo.get_job(conn, id)


def test_ac_m4_6_mark_seen_once(desk):
    db, clock, triage, _ = desk
    first = triage.mark_seen("job-0001", expected_version=1)
    assert first.triage_state == "seen" and first.version == 2
    clock.advance(timedelta(minutes=1))
    assert triage.mark_seen(first.id, expected_version=2) == first
    # Idempotence doesn't waive the version check.
    with pytest.raises(VersionConflict):
        triage.mark_seen(first.id, expected_version=1)
    assert read(db) == first


def test_ac_m4_6_snooze_returns_card_as_new(desk):
    db, clock, triage, query = desk
    seen = triage.mark_seen("job-0001", expected_version=1)
    triage.snooze(seen.id, expected_version=seen.version, choice="tomorrow")
    assert read(db).snooze_until == "2026-10-21"
    assert seen.id in {j["id"] for j in query.page(InboxFilters(view="snoozed")).items}
    clock.advance(timedelta(minutes=2))  # Local midnight; UTC date still October 20.
    before = read(db)
    page = query.page(InboxFilters(search="fixture0001"))
    assert page.total == 1 and page.items[0]["state"] == "new"
    assert read(db) == before
    assert not query.page(InboxFilters(view="snoozed", search="fixture0001")).items
    assert triage.mark_seen(seen.id, expected_version=before.version).triage_state == "seen"


@pytest.mark.parametrize(
    "choice,day",
    [
        ("tomorrow", "2026-10-21"),
        ("next_week", "2026-10-27"),
        ("month", "2026-11-20"),
        ("date", "2027-01-03"),
    ],
)
def test_snooze_local_choices(desk, choice, day):
    _, _, triage, _ = desk
    seen = triage.mark_seen("job-0001", expected_version=1)
    item = triage.snooze(
        seen.id,
        expected_version=seen.version,
        choice=choice,
        picked=date(2027, 1, 3) if choice == "date" else None,
    )
    assert item.snooze_until == day


def test_month_snooze_clamps_to_last_day(desk):
    _, clock, triage, _ = desk
    clock.set(datetime(2027, 1, 31, 12, tzinfo=UTC))
    seen = triage.mark_seen("job-0001", expected_version=1)
    assert (
        triage.snooze(seen.id, expected_version=seen.version, choice="month").snooze_until
        == "2027-02-28"
    )


@pytest.mark.parametrize(
    "reason,text", [(None, None), ("salary", None), ("other", "Fictional reason")]
)
def test_ac_m4_6_remove_with_optional_reason(desk, reason, text):
    db, _, triage, query = desk
    item = triage.remove("job-0001", expected_version=1, reason=reason, text=text)
    assert (item.triage_state, item.removal_reason, item.removal_text) == ("removed", reason, text)
    assert query.page(InboxFilters(search="fixture0001")).total == 0
    assert query.page(InboxFilters(view="removed", search="fixture0001")).total == 1
    assert read(db) == item


@pytest.mark.parametrize(
    "reason,text",
    [
        ("other", "x" * 501),
        ("not_interested", "x" * 501),
        ("salary", "text"),
        (None, "text"),
        ("invalid", None),
    ],
)
def test_invalid_removal_writes_nothing(desk, reason, text):
    db, _, triage, _ = desk
    before = read(db)
    with pytest.raises(TriageNotAllowed):
        triage.remove(before.id, expected_version=before.version, reason=reason, text=text)
    assert read(db) == before


@pytest.mark.parametrize("seen", [False, True])
def test_ac_m4_6_undo_restores_previous_state(desk, seen):
    _, _, triage, _ = desk
    job = triage.mark_seen("job-0001", expected_version=1) if seen else read(desk[0])
    removed = triage.remove(
        job.id, expected_version=job.version, reason="other", text="Fictional note"
    )
    restored = triage.undo(job.id, expected_version=removed.version)
    assert restored.triage_state == ("seen" if seen else "new")
    assert restored.removal_reason is None and restored.removal_text is None
    removed = triage.remove(job.id, expected_version=restored.version)
    assert triage.restore(job.id, expected_version=removed.version).triage_state == "seen"


def test_to_apply_and_undo(desk):
    _, _, triage, query = desk
    applied = triage.to_apply("job-0001", expected_version=1)
    assert query.page(InboxFilters(view="to_apply", search="fixture0001")).total == 1
    assert triage.undo(applied.id, expected_version=applied.version).triage_state == "new"


@pytest.mark.parametrize("action", ["mark_seen", "remove", "to_apply", "undo", "restore", "snooze"])
def test_stale_triage_raises_conflict_and_writes_nothing(desk, action):
    db, _, triage, _ = desk
    triage.mark_seen("job-0001", expected_version=1)
    before = read(db)
    extra = {"choice": "tomorrow"} if action == "snooze" else {}
    with pytest.raises(VersionConflict):
        getattr(triage, action)(before.id, expected_version=1, **extra)
    assert read(db) == before


def test_missing_and_merged_jobs_refused(desk):
    db, _, triage, _ = desk
    with pytest.raises(JobNotFound):
        triage.remove("missing", expected_version=1)
    with db.transaction() as conn:
        conn.execute("UPDATE jobs SET merged_into_job_id='job-0002' WHERE id='job-0001'")
    with pytest.raises(JobNotFound):
        triage.mark_seen("job-0001", expected_version=1)


@pytest.mark.parametrize(
    "field", ["band", "work_mode", "industry", "seniority", "company", "flagged", "search"]
)
def test_queue_filters_combine(desk, field):
    _, _, _, query = desk
    row = generate_jobs(1)[0]
    values = {
        "band": row["fit_band"],
        "work_mode": row["work_mode"],
        "industry": row["industry"],
        "seniority": row["seniority"],
        "company": row["company_id"],
        "flagged": bool(row["deal_breaker"]),
        "search": "fixture0001",
    }
    filters = InboxFilters(**{field: values[field]})
    page = query.page(filters)
    assert page.total > 0
    all_rows = []
    for number in range(1, (page.total + 49) // 50 + 1):
        all_rows.extend(query.page(filters, page=number).items)
    for item in all_rows:
        key = {"band": "fit_band", "company": "company_id", "flagged": "deal_breaker"}.get(
            field, field
        )
        assert "fixture0001" in item["title"] if field == "search" else item[key] == values[field]
    combined = query.page(InboxFilters(**values))
    assert combined.total == 1 and combined.items[0]["id"] == "job-0001"


@pytest.mark.parametrize("by", ["company", "industry", "band", "work_mode", "seniority"])
def test_group_by_counts(desk, by):
    _, _, _, query = desk
    groups = query.groups(InboxFilters(), by=by)
    assert sum(g.count for g in groups) == 1000
    expected = {}
    key = {"band": "fit_band", "company": "company_id"}.get(by, by)
    for row in generate_jobs(1000):
        expected[row[key]] = expected.get(row[key], 0) + 1
    assert {g.value: g.count for g in groups} == expected
    filtered = query.groups(InboxFilters(search="fixture0001"), by=by)
    assert sum(g.count for g in filtered) == 1


def test_queue_pages_by_50(desk):
    _, _, triage, query = desk
    triage.mark_seen("job-1000", expected_version=1)
    first = query.page(InboxFilters())
    second = query.page(InboxFilters(), page=2)
    assert first.total == 1000 and len(first.items) == len(second.items) == 50
    assert first.next_card["id"] == second.items[0]["id"]
    assert first.items[0]["id"] == "job-0999"  # New before Seen.
    assert not ({j["id"] for j in first.items} & {j["id"] for j in second.items})
    last = query.page(InboxFilters(), page=20)
    assert last.items[-1]["id"] == "job-1000" and last.next_card is None
    assert query.page(InboxFilters(), page=21).items == ()


@pytest.mark.parametrize(
    "term",
    [
        "fixture0001 OR fixture0002",
        "NOT",
        "title:fixture0001",
        '"',
        "*",
        "NEAR(fixture0001 fixture0002)",
        "fixture0001; DROP TABLE jobs;",
    ],
)
def test_search_operators_are_literal(desk, term):
    db, _, _, query = desk
    assert query.page(InboxFilters(search=term)).total == 0
    assert db.connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1000


def test_merged_postings_never_duplicate_queue_or_groups(desk):
    db, _, _, query = desk
    with db.transaction() as conn:
        conn.execute("UPDATE jobs SET merged_into_job_id='job-0002' WHERE id='job-0001'")
    assert query.page(InboxFilters()).total == 999
    assert sum(g.count for g in query.groups(InboxFilters(), by="company")) == 999
    assert query.page(InboxFilters(search="fixture0001")).total == 0


def test_query_count_is_constant_and_reads_never_write(desk):
    db, _, _, query = desk
    statements = []
    db.connection.set_trace_callback(statements.append)
    try:
        query.page(InboxFilters())
    finally:
        db.connection.set_trace_callback(None)
    selects = [s for s in statements if s.startswith("SELECT")]
    assert len(selects) == 3  # Count, page and the fixed active-rule lookup (AC-M9.1).
    assert not any(
        s.startswith(("UPDATE", "INSERT", "DELETE", "BEGIN IMMEDIATE")) for s in statements
    )


@pytest.mark.parametrize("sort", ["newest", "posting_age", "band"])
def test_inbox_sorts_are_deterministic_and_nulls_last(desk, sort):
    db, _, _, query = desk
    with db.transaction() as conn:
        conn.execute("UPDATE jobs SET posted_date=NULL,fit_band=NULL WHERE id='job-1000'")
    page = query.page(InboxFilters(sort=sort))
    assert page.items == query.page(InboxFilters(sort=sort)).items
    if sort != "newest":
        assert "job-1000" not in {j["id"] for j in page.items}
    rows = [j for p in range(1, 21) for j in query.page(InboxFilters(sort=sort), page=p).items]
    if sort == "posting_age":
        dates = [j["posted_date"] for j in rows if j["posted_date"]]
        assert dates == sorted(dates, reverse=True)
    if sort == "band":
        bands = [j["fit_band"] for j in rows]
        assert bands == sorted(
            bands, key=lambda v: {"strong": 0, "possible": 1, "stretch": 2, None: 3}[v]
        )


@pytest.mark.parametrize(
    "filters",
    [
        InboxFilters(),
        InboxFilters(band="strong"),
        InboxFilters(work_mode="remote"),
        InboxFilters(industry="Fictional agriculture"),
        InboxFilters(seniority="senior"),
        InboxFilters(company="company-0001"),
        InboxFilters(flagged=True),
        InboxFilters(search="fixture0001"),
        InboxFilters(sort="posting_age"),
        InboxFilters(sort="band"),
        InboxFilters(view="snoozed"),
        InboxFilters(view="to_apply"),
    ],
)
def test_inbox_queries_use_indexes(desk, filters):
    _, _, _, query = desk
    plan = " ".join(query.explain(filters))
    assert re.search(r"(?:SEARCH|SCAN) j USING (?:COVERING )?INDEX ", plan), plan
    if filters.search:
        assert "jobs_fts VIRTUAL TABLE INDEX" in plan, plan


@pytest.mark.parametrize(
    "choice,picked",
    [("date", date(2026, 10, 20)), ("date", date(2026, 10, 19)), ("date", None), ("unknown", None)],
)
def test_invalid_snooze_writes_nothing(desk, choice, picked):
    db, _, triage, _ = desk
    before = triage.mark_seen("job-0001", expected_version=1)
    with pytest.raises(TriageNotAllowed):
        triage.snooze(before.id, expected_version=before.version, choice=choice, picked=picked)
    assert read(db) == before


def test_unshown_new_card_cannot_be_snoozed(desk):
    db, _, triage, _ = desk
    before = read(db)
    with pytest.raises(TriageNotAllowed):
        triage.snooze(before.id, expected_version=before.version, choice="tomorrow")
    assert read(db) == before


def test_snoozed_view_actions_and_due_undo(desk):
    _, clock, triage, _ = desk
    seen = triage.mark_seen("job-0001", expected_version=1)
    snoozed = triage.snooze(seen.id, expected_version=seen.version, choice="tomorrow")
    removed = triage.remove(seen.id, expected_version=snoozed.version)
    restored = triage.undo(seen.id, expected_version=removed.version)
    assert restored.triage_state == "seen"
    snoozed = triage.snooze(seen.id, expected_version=restored.version, choice="tomorrow")
    clock.advance(timedelta(minutes=2))
    applied = triage.to_apply(seen.id, expected_version=snoozed.version)
    assert triage.undo(seen.id, expected_version=applied.version).triage_state == "new"


@pytest.mark.parametrize(
    "day,advance",
    [
        (datetime(2026, 10, 24, 21, 59, tzinfo=UTC), timedelta(minutes=2)),
        (datetime(2026, 10, 25, 22, 59, tzinfo=UTC), timedelta(minutes=2)),
    ],
)
def test_snooze_due_uses_local_midnight_across_dst(desk, day, advance):
    _, clock, triage, query = desk
    clock.set(day)
    seen = triage.mark_seen("job-0001", expected_version=1)
    triage.snooze(seen.id, expected_version=seen.version, choice="tomorrow")
    assert query.page(InboxFilters(search="fixture0001")).total == 0
    clock.advance(advance)
    assert query.page(InboxFilters(search="fixture0001")).total == 1


def test_query_rejects_dynamic_sql_options(desk):
    _, _, _, query = desk
    for options in (
        {"view": "removed' OR 1=1 --"},
        {"sort": "title;DROP TABLE jobs"},
        {"band": "strong' OR 1=1 --"},
        {"search": "x" * 501},
    ):
        with pytest.raises(ValueError):
            InboxFilters(**options)
    with pytest.raises(ValueError):
        query.groups(InboxFilters(), by="company; DROP TABLE jobs")
    for number in (0, -1, True, "1", 10**30):
        with pytest.raises(ValueError):
            query.page(InboxFilters(), page=number)


def test_last_representable_page_does_not_overflow(desk):
    _, _, _, query = desk
    assert query.page(InboxFilters(), page=MAX_PAGE).items == ()
    with pytest.raises(ValueError):
        query.page(InboxFilters(), page=MAX_PAGE + 1)
