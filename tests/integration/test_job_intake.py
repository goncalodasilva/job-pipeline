"""M4.3 intake: fictional pages, temporary SQLite, no provider and no network."""

import asyncio
from pathlib import Path

import pytest

from app.domain.errors import JobLinkInvalid, JobNotFound, JobTextNotFound, JobTextTooLong
from app.modules.fetch.providers.fake import FakeJobPageFetcher
from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from app.services.job_intake import JobIntakeService
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory
from tests.integration.test_schema_0004 import STAMP, START
from tests.job_pages import fake_pages, page_url, relations

pytestmark = pytest.mark.integration


@pytest.fixture
def intake(tmp_path: Path):
    db = Database.open(tmp_path / "jobs.sqlite3")
    clock = FrozenClock(START)
    apply_migrations(db, MIGRATIONS_DIR, tmp_path / "backups", clock=clock)
    service = JobIntakeService(
        lambda: db, clock, SequentialIdFactory(), FakeJobPageFetcher(fake_pages())
    )
    yield service, db
    db.close()


def add_url(service, page_id):
    return asyncio.run(service.add_url(page_url(page_id)))


def test_ac_m4_2_snapshot_and_source_stored_at_intake(intake):
    service, db = intake
    result = add_url(service, "plain")
    row = db.connection.execute(
        "SELECT snapshot_text, captured_at, triage_state FROM jobs WHERE id=?", (result.job_id,)
    ).fetchone()
    assert "Senior Widget Engineer" in row[0]
    assert row[1:] == (STAMP, "new")
    assert db.connection.execute("SELECT url, kind FROM job_sources").fetchone() == (
        page_url("plain"),
        "fetched",
    )


def test_same_url_again_adds_a_source_not_a_job(intake):
    service, db = intake
    first = add_url(service, "plain")
    second = add_url(service, "plain")
    assert second.job_id == first.job_id
    assert db.connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    assert db.connection.execute("SELECT count(*) FROM job_sources").fetchone()[0] == 2


@pytest.mark.parametrize("first,second", relations()["duplicates"])
def test_ac_m4_5_fixture_duplicates_merge_above_threshold(intake, first, second):
    service, db = intake
    a, b = add_url(service, first), add_url(service, second)
    assert b.job_id == a.job_id
    assert b.source_job_id != a.job_id
    assert (
        db.connection.execute(
            "SELECT merged_into_job_id FROM jobs WHERE id=?", (b.source_job_id,)
        ).fetchone()[0]
        == a.job_id
    )


@pytest.mark.parametrize("first,second", relations()["distinct"])
def test_distinct_postings_not_merged(intake, first, second):
    service, _ = intake
    assert add_url(service, first).job_id != add_url(service, second).job_id


def test_ac_m4_5_not_a_duplicate_splits_and_stays_split(intake):
    service, db = intake
    add_url(service, "board_a_same_ad")
    b = add_url(service, "board_b_same_ad")
    service.not_duplicate(b.source_job_id)
    assert (
        db.connection.execute(
            "SELECT merged_into_job_id FROM jobs WHERE id=?", (b.source_job_id,)
        ).fetchone()[0]
        is None
    )
    assert db.connection.execute("SELECT count(*) FROM job_pair_dismissals").fetchone()[0] == 1
    assert add_url(service, "board_b_same_ad").job_id == b.source_job_id
    # A later repost must not bridge the explicitly separated pair.
    add_url(service, "reposted_with_edits")
    assert (
        db.connection.execute(
            "SELECT count(*) FROM jobs WHERE merged_into_job_id IS NULL"
        ).fetchone()[0]
        == 2
    )


def test_job_text_controls_and_bidi_stripped(intake):
    service, db = intake
    service.add_text("Role\r\nfictional\tcompany\x00\u202e\u200b\ufeff\x7f")
    assert (
        db.connection.execute("SELECT snapshot_text FROM jobs").fetchone()[0]
        == "Role\nfictional\tcompany"
    )


def test_text_over_limit_refused_not_cut(intake):
    service, db = intake
    with pytest.raises(JobTextTooLong):
        service.add_text("a" * 50_001)
    assert db.connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    service.add_text("é" * 50_000)
    assert len(db.connection.execute("SELECT snapshot_text FROM jobs").fetchone()[0]) == 50_000


@pytest.mark.parametrize(
    "link",
    [
        "javascript:alert(1)",
        "ftp://fictional.example.test",
        "https://",
        "https://x.example.test/\nfoo",
        "https://user:pass@x.example.test",
    ],
)
def test_link_must_be_http_or_https(intake, link):
    service, _ = intake
    with pytest.raises(JobLinkInvalid):
        service.add_text("Fictional role", link=link)


def test_intake_without_provider_saves_triageable_job(intake):
    service, db = intake
    result = service.add_text("Fictional gardener", link="https://original.example.test/ad")
    assert (
        db.connection.execute(
            "SELECT triage_state FROM jobs WHERE id=?", (result.job_id,)
        ).fetchone()[0]
        == "new"
    )
    assert db.connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_no_text_page_refused(intake):
    service, db = intake
    with pytest.raises(JobTextNotFound):
        add_url(service, "js_shell")
    assert db.connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_not_duplicate_missing_safe_id(intake):
    service, _ = intake
    with pytest.raises(JobNotFound):
        service.not_duplicate("missing")


def test_redirect_stores_final_url_and_never_fetches_pasted_link(intake):
    from app.modules.fetch.providers.fake import FakePage

    service, db = intake
    url, final = "https://board.example.test/ad", "https://employer.example.test/job#apply"
    service._fetcher = FakeJobPageFetcher(
        {
            url: FakePage(
                "text/plain",
                b"Fictional gardener role. Grow imaginary flowers "
                b"and tend the pretend plants with the team.",
                final_url=final,
            )
        }
    )
    result = asyncio.run(service.add_url(url))
    assert db.connection.execute(
        "SELECT url, canonical_url FROM job_sources WHERE job_id=?", (result.source_job_id,)
    ).fetchone() == (final, "https://employer.example.test/job")
    # The injected fetcher has no such page: this would refuse if paste fetched its link.
    service.add_text("Different fictional role", link="https://never-fetch.example.test/ad")


def test_markup_cap_refuses_before_parsing(intake, monkeypatch):
    from app.modules.fetch.providers.fake import FakePage
    from app.services.job_intake import MAX_PARSE_CHARACTERS

    service, db = intake
    url = "https://board.example.test/large"
    service._fetcher = FakeJobPageFetcher(
        {url: FakePage("text/html", b"<b>" * (MAX_PARSE_CHARACTERS // 3 + 1))}
    )

    def must_not_parse(page):
        pytest.fail("oversize markup reached parser")

    monkeypatch.setattr("app.services.job_intake.page_text", must_not_parse)
    with pytest.raises(JobTextNotFound):
        asyncio.run(service.add_url(url))
    assert db.connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_intake_source_failure_rolls_back_job(intake, monkeypatch):
    from app.modules.storage import job_intake_repo

    service, db = intake

    def fail(*args, **kwargs):
        raise RuntimeError("fictional write failure")

    monkeypatch.setattr(job_intake_repo, "insert_source", fail)
    with pytest.raises(RuntimeError):
        service.add_text("Fictional mechanic")
    assert db.connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_uppercase_link_scheme_saved(intake):
    service, db = intake
    service.add_text("Fictional gardener", link="HTTPS://Jobs.Example.test/ad?ID=7#Apply")
    assert (
        db.connection.execute("SELECT url FROM job_sources").fetchone()[0]
        == "https://Jobs.Example.test/ad?ID=7#Apply"
    )


def test_split_repost_matches_strongest_snapshot(intake):
    service, db = intake
    a = "Fictional gardener joins Orchard company to grow apples pears flowers and tend plants"
    b = a.replace("Orchard", "Meadow")
    first = service.add_text(a)
    second = service.add_text(b)
    assert second.job_id == first.job_id
    service.not_duplicate(second.source_job_id)
    assert service.add_text(b).job_id == second.source_job_id
    assert (
        service.add_text(b, link="https://new-board.example.test/b").job_id == second.source_job_id
    )
    assert (
        db.connection.execute(
            "SELECT coalesce(j.merged_into_job_id, j.id) FROM job_sources s "
            "JOIN jobs j ON j.id = s.job_id WHERE s.url=?",
            ("https://new-board.example.test/b",),
        ).fetchone()[0]
        == second.source_job_id
    )


def test_ambiguous_repost_of_dismissed_pair_stays_separate(intake):
    service, _ = intake
    text = "Fictional gardener role at an unspecified company growing imaginary plants"
    a = service.add_text(text)
    b = service.add_text(text)
    service.not_duplicate(b.source_job_id)
    c = service.add_text(text)
    assert c.job_id not in {a.job_id, b.source_job_id}


@pytest.mark.parametrize(
    "markup",
    ["<span>" * 300 + "</x>" * 300, "<span>" * 200 + "</x>" * 10_000],
    ids=["depth", "work"],
)
def test_hostile_parser_work_refused_whole(intake, markup):
    from app.modules.fetch.providers.fake import FakePage

    service, db = intake
    url = "https://board.example.test/hostile"
    text = (
        markup
        + "Fictional gardener role growing imaginary plants with a team of imaginary gardeners."
    )
    service._fetcher = FakeJobPageFetcher({url: FakePage("text/html", text.encode())})
    with pytest.raises(JobTextNotFound):
        asyncio.run(service.add_url(url))
    assert db.connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
