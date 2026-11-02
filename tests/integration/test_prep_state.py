"""Prep state on board cards, in the drawer and in Today's interviews block, with batched reads
(M8.2 part D; ADR-124 item 10; design "Prep state"). Fictional data, frozen clock.

Assumed interface (chosen here where the sources left it open; the implementer matches it; the
markup contract is in ``tests/prep_state_pages.py``, the pure rules in
``tests/unit/domain/test_prep_state.py``):

- ``prep_repo.states(conn, application_ids, now) -> dict[str, PrepSummary]`` where ``now`` is
  an ISO UTC instant (text, as ``applications_repo.board_cards`` takes it) and ``PrepSummary``
  is a frozen dataclass ``(item_count, practiced_count, has_future_interview)``.
  ``practiced_count`` counts the application's items that have at least one practice attempt
  (not attempts). ``has_future_interview`` is true when the application has a ``scheduled``
  event of type ``interview`` whose ``ends_at`` is not before ``now`` (the rule the board's
  next-event line already uses). Every given id gets an entry (zero counts and False for an
  unknown id). It reads in a constant number of SELECT statements (at most three, none for an
  empty list) and writes nothing.
- ``BoardCard.prep_state`` and ``today.Interview.prep_state``: ``None`` when
  ``should_show(item_count, has_future_interview)`` is false, else ``prep_state(...)``.
- The board's prep columns are folded into its one card statement, so
  ``test_board_read_is_two_queries`` in ``tests/integration/test_board_query.py`` passes
  unchanged. Today and the drawer read the state with ``prep_repo.states``.
- A closed application (status phase ``closed``, for example rejected) shows no prep state
  anywhere, whether it has a future interview or prep items; ``PrepSummary.closed`` says so.
"""

from datetime import timedelta
from pathlib import Path

import pytest

from app.modules.storage import prep_repo
from tests.prep_state_pages import (
    FUTURE,
    FUTURE_END,
    LABELS,
    NOW,
    PAST,
    PAST_END,
    card_of,
    drawer_of,
    focusable_count,
    interview_of,
    mark_state,
    prep_marks,
    seed_prep,
)
from tests.reminder_desk import ReminderDesk, reminding

pytestmark = pytest.mark.integration

CASES = {"not_started": (0, 0), "questions_ready": (2, 0), "practiced": (2, 1)}


def build(run, data, job, *, items=0, practiced=0, event=None, kind="interview", status="applied"):
    """An application for a fictional job, with prep rows and an optional event
    ``(id, start, end, event status)``."""
    application = data.application(job, "Seed Buyer", "Fictional Orchards", status)
    if items:
        seed_prep(run.db, application.id, items=items, practiced=practiced)
    if event:
        event_id, start, end, state = event
        run.event(event_id, application, kind, start, ends_at=end, status=state)
    run.at(NOW)
    return application


def future(event_id="ps-interview"):
    return (event_id, FUTURE, FUTURE_END, "scheduled")


def board_cards(run) -> dict:
    columns = run.app.state.board.board().columns
    return {c.application_id: c for column in columns for c in column.cards}


def board_html(run) -> str:
    return run.get("/board", token=False).text


def drawer_html(run, application) -> str:
    response = run.get(f"/applications/{application.id}/detail", htmx=True)
    assert response.status_code == 200
    return response.text


def today_html(run) -> str:
    return run.get("/", token=False).text


# --- the three states, on every surface --------------------------------------------------------


@pytest.mark.parametrize("state", list(CASES))
def test_prep_state(tmp_path: Path, state: str):
    items, practiced = CASES[state]
    with reminding(tmp_path, now=NOW) as run:
        application = build(
            run, run.seeder(), "ps-job", items=items, practiced=practiced, event=future()
        )
        expected = (state, LABELS[state])

        board = board_html(run)
        assert mark_state(board, card_of(board, application.id)) == expected

        drawer = drawer_html(run, application)
        assert mark_state(drawer, drawer_of(drawer)) == expected

        today = today_html(run)
        assert mark_state(today, interview_of(today, "ps-interview")) == expected


def test_each_application_shows_its_own_state(tmp_path: Path):
    with reminding(tmp_path, now=NOW) as run:
        data = run.seeder()
        apps = {
            name: build(
                run,
                data,
                f"ps-{name}",
                items=CASES[name][0],
                practiced=CASES[name][1],
                event=future(f"ps-event-{name}"),
            )
            for name in CASES
        }
        board, today = board_html(run), today_html(run)
        for name, application in apps.items():
            assert mark_state(board, card_of(board, application.id)) == (name, LABELS[name])
            row = interview_of(today, f"ps-event-{name}")
            assert mark_state(today, row) == (name, LABELS[name])
        assert len(prep_marks(board)) == 3 and len(prep_marks(today)) == 3


def test_a_second_try_does_not_make_a_second_practiced_item(tmp_path: Path):
    with reminding(tmp_path, now=NOW) as run:
        application = build(run, run.seeder(), "ps-job", items=3, practiced=1, event=future())
        board = board_html(run)
        assert mark_state(board, card_of(board, application.id)) == (
            "practiced",
            LABELS["practiced"],
        )
        assert board_cards(run)[application.id].prep_state == "practiced"


# --- when nothing is shown ---------------------------------------------------------------------

NOTHING = {
    "no_event": None,
    "past_interview": ("ps-old", PAST, PAST_END, "scheduled"),
    "cancelled_interview": ("ps-cancelled", FUTURE, FUTURE_END, "cancelled"),
}


@pytest.mark.parametrize("case", list(NOTHING))
def test_no_prep_items_and_no_future_interview_shows_nothing(tmp_path: Path, case: str):
    with reminding(tmp_path, now=NOW) as run:
        application = build(run, run.seeder(), "ps-job", event=NOTHING[case])
        board = board_html(run)
        assert mark_state(board, card_of(board, application.id)) is None
        drawer = drawer_html(run, application)
        assert mark_state(drawer, drawer_of(drawer)) is None
        assert prep_marks(today_html(run)) == []
        assert board_cards(run)[application.id].prep_state is None


@pytest.mark.parametrize("kind", ["follow_up", "deadline", "offer_deadline"])
def test_a_future_event_that_is_not_an_interview_shows_nothing(tmp_path: Path, kind: str):
    with reminding(tmp_path, now=NOW) as run:
        application = build(run, run.seeder(), "ps-job", event=future("ps-other"), kind=kind)
        board = board_html(run)
        assert mark_state(board, card_of(board, application.id)) is None
        drawer = drawer_html(run, application)
        assert mark_state(drawer, drawer_of(drawer)) is None


@pytest.mark.parametrize("case", ["no_event", "past_interview", "cancelled_interview"])
def test_prep_items_alone_show_the_state_on_board_and_drawer_not_in_today(
    tmp_path: Path, case: str
):
    with reminding(tmp_path, now=NOW) as run:
        application = build(run, run.seeder(), "ps-job", items=2, event=NOTHING[case])
        expected = ("questions_ready", "Questions ready")
        board = board_html(run)
        assert mark_state(board, card_of(board, application.id)) == expected
        drawer = drawer_html(run, application)
        assert mark_state(drawer, drawer_of(drawer)) == expected
        assert prep_marks(today_html(run)) == []


def test_a_past_interview_with_no_items_does_not_count_but_a_future_one_does(tmp_path: Path):
    with reminding(tmp_path, now=NOW) as run:
        data = run.seeder()
        old = build(run, data, "ps-old-job", event=("ps-old", PAST, PAST_END, "scheduled"))
        soon = build(run, data, "ps-soon-job", event=future("ps-soon"))
        board = board_html(run)
        assert mark_state(board, card_of(board, old.id)) is None
        assert mark_state(board, card_of(board, soon.id)) == ("not_started", "Prep not started")


def test_time_moving_on_ends_the_future_interview(tmp_path: Path):
    with reminding(tmp_path, now=NOW) as run:
        application = build(run, run.seeder(), "ps-job", event=future())
        board = board_html(run)
        assert mark_state(board, card_of(board, application.id)) is not None
        run.clock.advance(timedelta(days=3))
        board = board_html(run)
        assert mark_state(board, card_of(board, application.id)) is None


# --- closed applications -----------------------------------------------------------------------


def open_board_closed(run) -> str:
    return run.get("/board/columns?closed=1", htmx=True).text


@pytest.mark.parametrize("status", ["rejected", "withdrawn"])
@pytest.mark.parametrize("items", [0, 2])
def test_a_closed_application_shows_no_prep_state(tmp_path: Path, status: str, items: int):
    with reminding(tmp_path, now=NOW) as run:
        data = run.seeder()
        closed = build(
            run, data, "ps-closed", items=items, event=future("ps-closed-ev"), status=status
        )
        open_one = build(run, data, "ps-open", event=future("ps-open-ev"))
        board = open_board_closed(run)
        assert mark_state(board, card_of(board, closed.id)) is None
        assert mark_state(board, card_of(board, open_one.id)) == (
            "not_started",
            LABELS["not_started"],
        )
        drawer = drawer_html(run, closed)
        assert mark_state(drawer, drawer_of(drawer)) is None
        today = today_html(run)
        assert prep_marks(today, interview_of(today, "ps-closed-ev")) == []
        cards = {c.application_id: c for c in run.app.state.board.board(closed=True).closed_cards}
        assert cards[closed.id].prep_state is None
        interviews = {i.id: i for i in run.app.state.today.view().interviews}
        assert (
            interviews.get("ps-closed-ev") is None or interviews["ps-closed-ev"].prep_state is None
        )


def test_prep_state_returns_when_a_closed_application_is_reopened(tmp_path: Path):
    with reminding(tmp_path, now=NOW) as run:
        application = build(run, run.seeder(), "ps-job", items=2, event=future(), status="rejected")
        drawer = drawer_html(run, application)
        assert mark_state(drawer, drawer_of(drawer)) is None
        run.execute(
            "UPDATE applications SET status_key = 'applied' WHERE id = ?", (application.id,)
        )
        drawer = drawer_html(run, application)
        assert mark_state(drawer, drawer_of(drawer)) == (
            "questions_ready",
            LABELS["questions_ready"],
        )


def test_states_flag_a_closed_application(desk):
    open_one, closed = desk.applied("ps-job-open"), desk.applied("ps-job-closed")
    with desk.db.transaction() as conn:
        conn.execute("UPDATE applications SET status_key = 'rejected' WHERE id = ?", (closed.id,))
    found = states(desk, [open_one.id, closed.id])
    assert (found[open_one.id].closed, found[closed.id].closed) == (False, True)
    assert states(desk, ["no-such-application"])["no-such-application"].closed is False


# --- service-level fields ----------------------------------------------------------------------


def test_board_cards_and_today_interviews_carry_the_state_key(tmp_path: Path):
    with reminding(tmp_path, now=NOW) as run:
        data = run.seeder()
        apps = {
            name: build(
                run,
                data,
                f"ps-{name}",
                items=CASES[name][0],
                practiced=CASES[name][1],
                event=future(f"ps-event-{name}"),
            )
            for name in CASES
        }
        plain = build(run, data, "ps-plain")
        cards = board_cards(run)
        for name, application in apps.items():
            assert cards[application.id].prep_state == name
        assert cards[plain.id].prep_state is None
        interviews = {i.id: i for i in run.app.state.today.view().interviews}
        for name in CASES:
            assert interviews[f"ps-event-{name}"].prep_state == name


# --- reads: batched, writing nothing, no new tab stops ----------------------------------------


def selects(run, call):
    seen: list[str] = []
    run.db.connection.set_trace_callback(seen.append)
    try:
        call()
    finally:
        run.db.connection.set_trace_callback(None)
    return [text for text in seen if text.lstrip().upper().startswith(("SELECT", "WITH"))]


def populate(run, count: int) -> None:
    data = run.seeder()
    for number in range(count):
        items, practiced = list(CASES.values())[number % 3]
        build(
            run,
            data,
            f"ps-job-{number}",
            items=items,
            practiced=practiced,
            event=future(f"ps-event-{number}"),
        )


def test_prep_state_reads_batched(tmp_path: Path):
    measured: dict[int, tuple[int, int]] = {}
    for size in (3, 30):
        folder = tmp_path / f"size{size}"
        folder.mkdir()
        with reminding(folder, now=NOW) as run:
            populate(run, size)
            board_statements = selects(run, lambda run=run: run.app.state.board.board())
            today_statements = selects(run, lambda run=run: run.app.state.today.view())
            measured[size] = (len(board_statements), len(today_statements))
            # The statements are not saved by leaving the state out: every card carries it.
            columns = run.app.state.board.board().columns
            cards = [card for column in columns for card in column.cards]
            assert len(cards) == size
            assert all(card.prep_state in LABELS for card in cards)
            interviews = run.app.state.today.view().interviews
            assert len(interviews) == size
            assert all(item.prep_state in LABELS for item in interviews)
    assert measured[3] == measured[30], measured


def test_reads_write_nothing(tmp_path: Path):
    with reminding(tmp_path, now=NOW) as run:
        application = build(run, run.seeder(), "ps-job", items=2, practiced=1, event=future())
        before = run.tables()
        board = board_html(run)
        assert mark_state(board, card_of(board, application.id)) == (
            "practiced",
            LABELS["practiced"],
        )
        drawer_html(run, application)
        today_html(run)
        run.app.state.board.board()
        run.app.state.today.view()
        assert run.tables() == before


def test_the_state_adds_no_tab_stop(tmp_path: Path):
    """Seeding prep changes the words, never the number of focusable elements."""
    with reminding(tmp_path, now=NOW) as run:
        application = build(run, run.seeder(), "ps-job", event=future())
        board, drawer, today = board_html(run), drawer_html(run, application), today_html(run)
        regions = (
            (board, card_of(board, application.id)),
            (drawer, drawer_of(drawer)),
            (today, interview_of(today, "ps-interview")),
        )
        before = [focusable_count(html, region) for html, region in regions]
        assert [mark_state(html, region) for html, region in regions] == [
            ("not_started", "Prep not started")
        ] * 3

        seed_prep(run.db, application.id, items=3, practiced=2)
        board, drawer, today = board_html(run), drawer_html(run, application), today_html(run)
        regions = (
            (board, card_of(board, application.id)),
            (drawer, drawer_of(drawer)),
            (today, interview_of(today, "ps-interview")),
        )
        after = [focusable_count(html, region) for html, region in regions]
        assert [mark_state(html, region) for html, region in regions] == [
            ("practiced", LABELS["practiced"])
        ] * 3
        assert after == before


# --- prep_repo.states --------------------------------------------------------------------------


@pytest.fixture
def desk(tmp_path):
    working = ReminderDesk(tmp_path)
    yield working
    working.close()


def states(desk, ids, now=NOW):
    with desk.db.read() as conn:
        return prep_repo.states(conn, list(ids), now)


def test_states_counts_items_and_practiced_items_per_application(desk):
    a, b, c = (desk.applied(f"ps-job-{n}") for n in range(3))
    seed_prep(desk.db, a.id, items=3, practiced=2)
    seed_prep(desk.db, b.id, items=1)
    found = states(desk, [a.id, b.id, c.id])
    assert set(found) == {a.id, b.id, c.id}
    assert (found[a.id].item_count, found[a.id].practiced_count) == (3, 2)
    assert (found[b.id].item_count, found[b.id].practiced_count) == (1, 0)
    assert (found[c.id].item_count, found[c.id].practiced_count) == (0, 0)
    assert not any(found[i].has_future_interview for i in found)


def test_states_do_not_leak_other_applications_rows(desk):
    a, b = desk.applied("ps-job-a"), desk.applied("ps-job-b")
    seed_prep(desk.db, a.id, items=2, practiced=2)
    desk.event("ps-ev", a, "interview", FUTURE, ends_at=FUTURE_END)
    only_b = states(desk, [b.id])
    assert set(only_b) == {b.id}
    assert (only_b[b.id].item_count, only_b[b.id].practiced_count) == (0, 0)
    assert only_b[b.id].has_future_interview is False
    both = states(desk, [a.id, b.id])
    assert both[a.id].has_future_interview is True
    assert both[b.id].has_future_interview is False


def test_states_future_interview_rules(desk):
    cases = {
        "future": ("interview", FUTURE, FUTURE_END, "scheduled", True),
        "past": ("interview", PAST, PAST_END, "scheduled", False),
        "cancelled": ("interview", FUTURE, FUTURE_END, "cancelled", False),
        "follow_up": ("follow_up", FUTURE, FUTURE_END, "scheduled", False),
        "deadline": ("deadline", FUTURE, FUTURE_END, "scheduled", False),
        # Started before now and not yet over: still the next interview (as on the board).
        "ongoing": (
            "interview",
            "2026-10-20T08:30:00+00:00",
            "2026-10-20T09:30:00+00:00",
            "scheduled",
            True,
        ),
        "just_ended": (
            "interview",
            "2026-10-20T07:59:00+00:00",
            "2026-10-20T08:59:00+00:00",
            "scheduled",
            False,
        ),
    }
    made = {}
    for name, (kind, start, end, state, _) in cases.items():
        application = desk.applied(f"ps-job-{name}")
        desk.event(f"ps-ev-{name}", application, kind, start, ends_at=end, status=state)
        made[name] = application
    found = states(desk, [a.id for a in made.values()])
    for name, (*_, expected) in cases.items():
        assert found[made[name].id].has_future_interview is expected, name


def test_states_for_an_unknown_id_and_an_empty_list(desk):
    assert states(desk, []) == {}
    found = states(desk, ["no-such-application"])
    assert (
        found["no-such-application"].item_count,
        found["no-such-application"].practiced_count,
    ) == (0, 0)
    assert found["no-such-application"].has_future_interview is False


def test_states_use_a_constant_number_of_statements_and_write_nothing(desk):
    apps = [desk.applied(f"ps-job-{n}") for n in range(20)]
    for number, application in enumerate(apps):
        seed_prep(desk.db, application.id, items=number % 3, practiced=min(number % 3, 1))
        desk.event(f"ps-ev-{number}", application, "interview", FUTURE, ends_at=FUTURE_END)
    before = desk.tables()
    counts = {}
    for size in (1, 5, 20):
        seen: list[str] = []
        desk.db.connection.set_trace_callback(seen.append)
        try:
            states(desk, [a.id for a in apps[:size]])
        finally:
            desk.db.connection.set_trace_callback(None)
        counts[size] = len([s for s in seen if s.lstrip().upper().startswith(("SELECT", "WITH"))])
    assert counts[1] == counts[5] == counts[20] and 1 <= counts[1] <= 3, counts
    empty: list[str] = []
    desk.db.connection.set_trace_callback(empty.append)
    try:
        states(desk, [])
    finally:
        desk.db.connection.set_trace_callback(None)
    assert not [s for s in empty if s.lstrip().upper().startswith(("SELECT", "WITH"))]
    assert desk.tables() == before


def test_states_summary_is_a_frozen_value(desk):
    application = desk.applied("ps-job")
    summary = states(desk, [application.id])[application.id]
    with pytest.raises(AttributeError):
        summary.item_count = 5  # type: ignore[misc]
    assert summary == prep_repo.PrepSummary(0, 0, False)
