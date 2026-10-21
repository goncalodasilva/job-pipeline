"""M5.3 board read model: columns, cards, the collapsed Closed column and the table view.

Assumed interface (the implementer matches it; ``tests/application_desk.py`` has the rest):

- ``BoardQuery(database, clock).board(*, closed=False)`` answers an object with ``columns`` (one
  per preparing or active status in position order, each with ``status`` (``key``, ``label``,
  ``phase``) and ``cards``), ``closed_count`` (every application in a closed status) and
  ``closed_cards`` (empty unless ``closed=True``).
- A card has ``application_id``, ``job_id``, ``company`` (empty when the job has none), ``role``
  (the job title), ``status_key``, ``next_event`` (None, or an object with ``starts_at``, an aware
  instant) and ``days_since_activity`` (whole days from the latest change time the database
  holds for the application to now; a scheduled date never counts). A column lists its most
  recently active cards first.
- ``BoardQuery.table(sort, *, descending=False, page=1)`` answers ``rows`` (``application_id``,
  ``company``, ``role``, ``status_key``), ``total``, ``page`` and ``page_size`` (50). ``sort`` is
  one of ``company``, ``role``, ``status`` (pipeline position), ``applied`` or ``activity``;
  company and role ignore case. Every application is listed, closed ones too.
- A board or table read runs exactly two SELECT statements; the table page's statement uses the
  indexes migration 0005 made and scans none of the three big tables without one.

Everyone and everything here is fictional.
"""

from datetime import UTC, datetime, timedelta
from functools import partial
from itertools import product

import pytest

from tests.application_desk import Desk, wall

pytestmark = pytest.mark.integration

COMPANIES = ("alder Mills", "Birch Co", "cedar Works", "Dune Ltd", "elm Yard", "Fern Labs")
TITLES = ("analyst", "Buyer", "clerk", "Driver", "editor", "Fitter", "grower")
SORTS = ("company", "role", "status", "applied", "activity")
INDEX_FOR = {
    "company": "companies_name_application_sort",
    "role": "jobs_title_application_sort",
    "status": "applications_status_activity",
    "applied": "applications_applied_at",
    "activity": "applications_updated_at",
}


@pytest.fixture
def desk(tmp_path):
    working = Desk(tmp_path)
    yield working
    working.close()


# --- columns and cards ------------------------------------------------------------------------


def test_board_columns_order_and_closed_collapsed(desk):
    waiting = desk.statuses.add("Waiting", "active")
    desk.statuses.reorder(waiting.key, "up", expected_version=waiting.version)
    lost = desk.statuses.add("Lost to a referral", "closed")
    plan = (
        ("job-1", "Greenhouse Technician", "Larkspur Orchards", "preparing"),
        ("job-2", "Irrigation Planner", "Birch Co", "applied"),
        ("job-3", "Seed Buyer", "cedar Works", "screening"),
        ("job-4", "Crop Analyst", "Dune Ltd", "interviewing"),
        ("job-5", "Pallet Driver", "elm Yard", "rejected"),
        ("job-6", "Boiler Fitter", "Fern Labs", "withdrawn"),
        ("job-7", "Frost Watcher", "alder Mills", waiting.key),
        ("job-8", "Pruning Lead", "Birch Co", "applied"),
        ("job-9", "Orchard Scout", "Dune Ltd", lost.key),
    )
    made = {}
    for job_id, title, company, status in plan:
        desk.add_job(job_id, title, company)
        made[job_id] = desk.apply(job_id).application
        desk.tick(minutes=1)
        if status != "preparing":
            desk.move(made[job_id], status)
            desk.tick(minutes=1)
    # Two days on, job-2 is touched again; one more day passes.
    desk.tick(days=2)
    desk.move(made["job-2"], "applied", source="menu")
    desk.tick(days=1)
    desk.applications.add_interview_date(
        made["job-4"].id, starts_at=wall("2026-11-03T14:00"), time_zone="America/New_York"
    )

    view = desk.board.board()
    assert [column.status.key for column in view.columns] == [
        "preparing",
        "applied",
        "screening",
        "interviewing",
        waiting.key,
        "offer",
    ]
    assert [column.status.label for column in view.columns][4:] == ["Waiting", "Offer"]
    by_status = {column.status.key: column.cards for column in view.columns}
    assert [card.job_id for card in by_status["preparing"]] == ["job-1"]
    assert [card.job_id for card in by_status["screening"]] == ["job-3"]
    assert [card.job_id for card in by_status["interviewing"]] == ["job-4"]
    assert [card.job_id for card in by_status[waiting.key]] == ["job-7"]
    assert by_status["offer"] == ()  # an empty column is still a column
    # Most recently active first: job-2 was touched again after job-8 moved.
    assert [card.job_id for card in by_status["applied"]] == ["job-2", "job-8"]

    card = by_status["interviewing"][0]
    assert (card.application_id, card.company, card.role, card.status_key) == (
        made["job-4"].id,
        "Dune Ltd",
        "Crop Analyst",
        "interviewing",
    )
    assert card.next_event is not None
    assert card.next_event.starts_at == datetime(2026, 11, 3, 19, 0, tzinfo=UTC)
    assert by_status["screening"][0].next_event is None

    # Closed: only a count until the user expands it. Rejected, Withdrawn and the custom one.
    assert view.closed_count == 3
    assert view.closed_cards == ()
    expanded = desk.board.board(closed=True)
    assert expanded.closed_count == 3
    assert {card.job_id for card in expanded.closed_cards} == {"job-5", "job-6", "job-9"}
    assert [c.status.key for c in expanded.columns] == [c.status.key for c in view.columns]
    for column in expanded.columns:
        assert all(card.status_key == column.status.key for card in column.cards)


def test_board_card_days_since_activity_counts_changes_not_scheduled_dates(desk):
    for job_id in ("job-1", "job-2", "job-3"):
        desk.add_job(job_id, f"Fictional Role {job_id}")
    desk.apply("job-1")
    second = desk.apply("job-2").application
    third = desk.apply("job-3").application
    desk.tick(days=2)
    desk.move(second, "interviewing")
    desk.applications.add_interview_date(
        second.id, starts_at=wall("2026-12-15T10:00"), time_zone="Europe/Lisbon"
    )
    desk.move(third, "applied")
    desk.tick(days=1, hours=3)

    cards = {card.job_id: card for column in desk.board.board().columns for card in column.cards}
    assert cards["job-1"].days_since_activity == 3
    assert cards["job-2"].days_since_activity == 1  # an event two months away adds nothing
    assert cards["job-3"].days_since_activity == 1


def test_board_lists_a_job_without_a_company(desk):
    desk.add_job("job-1", "Greenhouse Technician", company=None)
    desk.apply("job-1")
    [column] = [c for c in desk.board.board().columns if c.status.key == "preparing"]
    [card] = column.cards
    assert card.role == "Greenhouse Technician" and not card.company


# --- the table --------------------------------------------------------------------------------


def populate(desk, count=120):
    """Applications with known sort keys; each operation happens a minute after the last."""
    expected = {}
    for index in range(count):
        company, title = COMPANIES[index % 6], TITLES[(index * 5 + index // 6) % 7]
        job_id = desk.add_job(f"job-{index:04d}", title, company)
        application = desk.apply(job_id).application
        desk.tick(minutes=1)
        desk.move(application, "applied")
        applied_at = desk.clock.now()
        desk.tick(minutes=1)
        status = "applied"
        if index % 4 == 0:
            status = "rejected"
        elif index % 5 == 0:
            status = "interviewing"
        if status != "applied":
            desk.move(application, status)
            desk.tick(minutes=1)
        [(position,)] = desk.rows(
            "SELECT position FROM application_statuses WHERE key=?", (status,)
        )
        expected[application.id] = {
            "company": company,
            "role": title,
            "status": status,
            "position": position,
            "applied_at": applied_at,
            "activity": desk.clock.now() - timedelta(minutes=1),
        }
    return expected


def read_all(desk, sort, descending):
    pages = [desk.board.table(sort, descending=descending, page=n) for n in (1, 2, 3)]
    return pages, [row for page in pages for row in page.rows]


def expected_order(expected, sort, descending):
    key = {
        "company": lambda item: item["company"].casefold(),
        "role": lambda item: item["role"].casefold(),
        "status": lambda item: item["position"],
        "applied": lambda item: item["applied_at"],
        "activity": lambda item: item["activity"],
    }[sort]
    return sorted(expected.items(), key=lambda pair: key(pair[1]), reverse=descending)


def test_board_table_sorts_and_pages_by_50(desk):
    expected = populate(desk)
    for sort, descending in product(SORTS, (False, True)):
        label = f"{sort} {'desc' if descending else 'asc'}"
        pages, rows = read_all(desk, sort, descending)
        assert [len(page.rows) for page in pages] == [50, 50, 20], label
        assert [(page.page, page.total, page.page_size) for page in pages] == [
            (1, 120, 50),
            (2, 120, 50),
            (3, 120, 50),
        ], label
        ids = [row.application_id for row in rows]
        assert len(set(ids)) == 120 == len(expected), label  # a stable total order
        ordered = expected_order(expected, sort, descending)
        if sort in ("applied", "activity"):  # the instants are all different: one exact order
            assert ids == [application_id for application_id, _ in ordered], label
        elif sort == "status":
            assert [expected[i]["position"] for i in ids] == [
                item["position"] for _, item in ordered
            ]
        else:  # case never decides the order
            shown = [getattr(row, sort).casefold() for row in rows]
            assert shown == [item[sort].casefold() for _, item in ordered], label
        for row in rows:
            known = expected[row.application_id]
            assert (row.company, row.role, row.status_key) == (
                known["company"],
                known["role"],
                known["status"],
            )
        assert {row.status_key for row in rows} == {"applied", "rejected", "interviewing"}
    # Beyond the last page there is nothing to show, and the total stays right.
    assert desk.board.table("company", page=3).total == 120


# --- how many queries, and which indexes ------------------------------------------------------


def selects(desk, call):
    seen = []
    desk.db.connection.set_trace_callback(seen.append)
    try:
        call()
    finally:
        desk.db.connection.set_trace_callback(None)
    return [text for text in seen if text.lstrip().upper().startswith(("SELECT", "WITH"))]


def test_board_read_is_two_queries(desk):
    populate(desk, 30)
    for call in (
        lambda: desk.board.board(),
        lambda: desk.board.board(closed=True),
        lambda: desk.board.table("company", page=1),
        lambda: desk.board.table("activity", descending=True, page=2),
    ):
        statements = selects(desk, call)
        assert len(statements) == 2, statements


def test_board_queries_use_indexes(desk):
    populate(desk, 60)
    for sort, descending in product(SORTS, (False, True)):
        label = f"{sort} {'desc' if descending else 'asc'}"
        statements = selects(desk, partial(desk.board.table, sort, descending=descending, page=2))
        [page_query] = [text for text in statements if "LIMIT" in text.upper()]
        plan = [
            detail
            for *_, detail in desk.db.connection.execute(
                "EXPLAIN QUERY PLAN " + page_query
            ).fetchall()
        ]
        text = "\n".join(plan)
        assert INDEX_FOR[sort] in text, f"{label}\n{text}"
        if sort != "status":  # the status sort may read the few status rows without an index
            unindexed = [
                line
                for line in plan
                if line.startswith("SCAN") and "USING" not in line and "CONSTANT" not in line
            ]
            assert not unindexed, f"{label}\n{text}"


def test_board_reads_write_nothing(desk):
    populate(desk, 10)
    before = desk.snapshot()
    desk.board.board(closed=True)
    desk.board.table("status", page=1)
    assert desk.snapshot() == before


# --- pipeline order, next events, null keys and page clamping ----------------------------------


def test_board_columns_and_status_sort_follow_phase_then_position(desk):
    researching = desk.statuses.add("Researching", "preparing")
    desk.statuses.reorder(researching.key, "up", expected_version=researching.version)
    waiting = desk.statuses.add("Waiting", "active")
    lost = desk.statuses.add("Lost to a referral", "closed")
    for _ in range(4):  # to the front of the closed statuses
        lost = desk.statuses.reorder(lost.key, "up", expected_version=lost.version)
    keys = [column.status.key for column in desk.board.board().columns]
    # The reordered Preparing status stays in the Preparing group, left of Applied.
    assert keys == [
        researching.key,
        "preparing",
        "applied",
        "screening",
        "interviewing",
        "offer",
        waiting.key,
    ]
    assert desk.order("closed")[0] == lost.key
    made = {}
    for job_id, status in (
        ("job-1", researching.key),
        ("job-2", "rejected"),
        ("job-3", lost.key),
        ("job-4", "applied"),
        ("job-5", "preparing"),
    ):
        desk.add_job(job_id, f"Role {job_id}", "Birch Co")
        made[job_id] = desk.apply(job_id).application
        desk.tick(minutes=1)
        desk.move(made[job_id], status)  # Apply used the first Preparing status: Researching
        desk.tick(minutes=1)
    ascending = desk.board.table("status").rows
    order = [row.status_key for row in ascending]
    assert order == [researching.key, "preparing", "applied", lost.key, "rejected"]
    descending = desk.board.table("status", descending=True).rows
    assert [row.status_key for row in descending] == order[::-1]


def test_board_card_shows_the_earliest_upcoming_scheduled_event(desk):
    desk.add_job("job-1", "Greenhouse Technician")
    application = desk.apply("job-1").application
    desk.tick(days=1)  # 2026-10-21 09:00 UTC
    for text in ("2026-10-10T10:00", "2026-12-01T10:00", "2026-11-03T14:00"):
        desk.applications.add_interview_date(
            application.id, starts_at=wall(text), time_zone="America/New_York"
        )
    with desk.db.transaction() as conn:  # earlier than all, but cancelled
        conn.execute(
            "INSERT INTO events (id, application_id, type, starts_at, ends_at, time_zone, status, "
            "created_at, updated_at) VALUES ('cancelled-1', ?, 'interview', "
            "'2026-10-25T10:00:00+00:00', '2026-10-25T11:00:00+00:00', 'UTC', 'cancelled', ?, ?)",
            (application.id, "2026-10-21T09:00:00+00:00", "2026-10-21T09:00:00+00:00"),
        )
    [column] = [c for c in desk.board.board().columns if c.status.key == "preparing"]
    [card] = column.cards
    assert card.next_event is not None
    assert card.next_event.starts_at == datetime(2026, 11, 3, 19, 0, tzinfo=UTC)


def test_board_card_keeps_an_event_that_is_still_going_on(desk):
    desk.add_job("job-1", "Greenhouse Technician")
    application = desk.apply("job-1").application
    desk.applications.add_interview_date(
        application.id, starts_at=wall("2026-10-20T05:30"), time_zone="America/New_York"
    )  # 09:30 to 10:30 UTC; the clock reads 09:00 UTC, so move it into the event
    desk.tick(minutes=45)
    [column] = [c for c in desk.board.board().columns if c.status.key == "preparing"]
    assert column.cards[0].next_event is not None
    desk.tick(hours=1)
    [column] = [c for c in desk.board.board().columns if c.status.key == "preparing"]
    assert column.cards[0].next_event is None


def populate_with_null_keys(desk):
    """60 applications; every tenth has no company and every seventh was never applied for."""
    for index in range(60):
        company = None if index % 10 == 3 else COMPANIES[index % 6]
        job_id = desk.add_job(f"job-{index:04d}", TITLES[index % 7], company)
        application = desk.apply(job_id).application
        desk.tick(minutes=1)
        if index % 7 != 0:
            desk.move(application, "applied")
            desk.tick(minutes=1)


def test_board_table_orders_companyless_and_never_applied_rows(desk):
    populate_with_null_keys(desk)
    meta = {
        application_id: (name, company_id, applied_at)
        for application_id, name, company_id, applied_at in desk.rows(
            "SELECT a.id, c.name, c.id, a.applied_at FROM applications a "
            "JOIN jobs j ON j.id = a.job_id LEFT JOIN companies c ON c.id = j.company_id"
        )
    }
    keys = {
        "company": lambda i: ((meta[i][0] or "").casefold(), meta[i][1] or "", i),
        "applied": lambda i: (meta[i][2] or "", i),
    }
    for sort in ("company", "applied"):
        for descending in (False, True):
            label = f"{sort} {'desc' if descending else 'asc'}"
            rows = desk.board.table(sort, descending=descending, page=1).rows
            rows += desk.board.table(sort, descending=descending, page=2).rows
            ids = [row.application_id for row in rows]
            assert len(ids) == len(set(ids)) == 60, label  # one total order across both pages
            assert ids == sorted(meta, key=keys[sort], reverse=descending), label
    first = desk.board.table("company").rows
    assert not first[0].company and not first[5].company  # company-less come first ascending
    last = desk.board.table("company", descending=True, page=2).rows
    assert not last[-1].company  # and last descending
    never_applied = {i for i, (_, _, applied_at) in meta.items() if applied_at is None}
    assert len(never_applied) == 9
    ascending = [row.application_id for row in desk.board.table("applied").rows]
    assert set(ascending[:9]) == never_applied  # no date sorts as the smallest value


def test_board_table_page_is_clamped_and_never_raises(desk):
    populate(desk, 60)
    last = desk.board.table("company", page=2)
    assert len(last.rows) == 10
    for wanted in (0, -5, 10**30, -(10**30), None, "2", 1.5):
        page = desk.board.table("company", page=wanted)
        assert page.total == 60 and 1 <= page.page <= 2 and page.rows
    assert desk.board.table("company", page=10**30).rows == last.rows
    assert desk.board.table("company", page=0).page == 1
    assert desk.board.table("company", page=0).rows == desk.board.table("company", page=1).rows
    assert desk.board.table("role", page=10**30).page == 2
