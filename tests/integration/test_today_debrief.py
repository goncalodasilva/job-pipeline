"""Today offers a debrief after an interview ends (M8.3 part A; AC-M8.5; ADR-124 item 9; design
"Debrief"), with a frozen clock and a computer time zone. Fictional data.

An offer is an attention item. It appears for a scheduled, uncancelled INTERVIEW event whose end
has passed, up to ``DEBRIEF_WINDOW_DAYS`` (14) days after the end, while the event has no done
or dismissed debrief. Events are stored in UTC with an IANA zone, so the end is an instant: the
Europe/Lisbon events below end an hour apart on the wall clock from their UTC instants, and the
offer follows the instant. An application that is closed is still offered (the person may want
the questions in the bank). The markup contract is in ``tests/debrief_pages.py``.

Assumed interface: ``TodayView.debriefs`` lists the offers (each with ``id`` = the event id and
``application_id``), ``TodayView.empty`` is false while one is offered, and the block
``#today-debriefs`` follows ``#today-interviews`` and precedes ``#today-reminders``.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.board_pages import by_id, elements
from tests.debrief_pages import DISMISSED, base, offers, says
from tests.reminder_desk import reminding
from tests.tailoring_pages import announced

pytestmark = pytest.mark.integration

END = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
ONE_SECOND = timedelta(seconds=1)


def interview(
    run, application, event_id: str, end: datetime = END, *, kind="interview", status="scheduled"
):
    start = end - timedelta(hours=1)
    return run.event(
        event_id,
        application,
        kind,
        start.isoformat(),
        ends_at=end.isoformat(),
        zone="Europe/Lisbon",
        status=status,
    )


def at(run, moment: datetime) -> None:
    run.at(moment.isoformat())


def offered(run) -> set[str]:
    return set(offers(run.get("/", token=False).text))


def test_debrief_offered_on_today_after_interview_passes(tmp_path: Path):
    with reminding(tmp_path, now="2026-10-20T09:00:00+00:00", zone="Europe/Lisbon") as run:
        application = run.applied("debrief-job")
        interview(run, application, "just-ended")

        at(run, END - ONE_SECOND)
        before = run.get("/", token=False).text
        assert offers(before) == {} and by_id(before, "today-debriefs") is None
        assert run.app.state.today.view().debriefs == ()

        at(run, END + ONE_SECOND)
        html = run.get("/", token=False).text
        shown = offers(html)
        assert set(shown) == {"just-ended"}
        block = by_id(html, "today-debriefs")
        assert block is not None and block.tag == "section" and "today-block" in block.classes
        assert shown["just-ended"].within(block)
        heading = by_id(html, "today-debriefs-heading")
        assert heading is not None and heading.text == "Interview debriefs"
        row = shown["just-ended"]
        assert "How did the interview go?" in row.text
        links = [e for e in elements(html) if e.tag == "a" and e.within(row)]
        [debrief] = [e for e in links if e.text == "Debrief"]
        assert debrief.attrs["href"] == base(application.id, "just-ended")
        [form] = [e for e in elements(html) if e.tag == "form" and e.within(row)]
        action = base(application.id, "just-ended") + "/dismiss"
        assert form.attrs["action"] == action and form.attrs["hx-post"] == action
        assert form.attrs["hx-target"] == "#today-content"
        values = {
            str(e.attrs["name"]): e.attrs.get("value")
            for e in elements(html)
            if e.tag == "input" and e.within(form)
        }
        assert values == {"version": "0", "return": "today"}
        buttons = [e for e in elements(html) if e.tag == "button" and e.within(form)]
        assert [b.text for b in buttons] == ["Not needed"]
        assert "Nothing needs your attention today." not in html
        today = run.app.state.today.view()
        assert [o.id for o in today.debriefs] == ["just-ended"] and not today.empty
        # The fragment (token) shows the same offer.
        fragment = run.get("/today/content", htmx=True).text
        assert set(offers(fragment)) == {"just-ended"} and "<html" not in fragment


def test_debrief_block_follows_the_interviews_block(tmp_path: Path):
    with reminding(tmp_path, now="2026-10-20T09:00:00+00:00") as run:
        application = run.applied("order-job")
        interview(run, application, "ended")
        interview(run, application, "coming", END + timedelta(days=2))
        at(run, END + timedelta(hours=2))

        html = run.get("/", token=False).text

        sections = [
            e.attrs["id"]
            for e in elements(html)
            if e.tag == "section" and "today-block" in e.classes
        ]
        assert sections[:2] == ["today-interviews", "today-debriefs"], sections


def test_debrief_offer_window_is_fourteen_days_after_the_end(tmp_path: Path):
    with reminding(tmp_path, now="2026-10-20T09:00:00+00:00") as run:
        application = run.applied("window-job")
        interview(run, application, "old")
        window = timedelta(days=14)

        at(run, END + window - ONE_SECOND)
        assert offered(run) == {"old"}, "still inside the window"

        at(run, END + window + ONE_SECOND)
        html = run.get("/", token=False).text
        assert offers(html) == {} and by_id(html, "today-debriefs") is None
        # Today is not empty by now: the applied application's own 14-day reminder is due.
        assert "How did the interview go?" not in says(html)


def test_debrief_offer_excludes_cancelled_other_kinds_and_interviews_not_over(tmp_path: Path):
    with reminding(tmp_path, now="2026-10-20T09:00:00+00:00") as run:
        application = run.applied("exclude-job")
        interview(run, application, "good")
        interview(run, application, "cancelled", status="cancelled")
        interview(run, application, "follow-up", kind="follow_up")
        interview(run, application, "deadline", kind="deadline")
        interview(run, application, "offer-deadline", kind="offer_deadline")
        interview(run, application, "ends-later", END + timedelta(hours=3))
        interview(run, application, "too-old", END - timedelta(days=15))
        at(run, END + timedelta(hours=2))

        assert offered(run) == {"good"}


def test_debrief_offer_follows_the_instant_not_the_wall_clock(tmp_path: Path):
    """Lisbon is an hour ahead of UTC on 20 October 2026: this interview ends at 11:00 local,
    10:00 UTC. At 10:30 UTC the wall clock in Lisbon shows 11:30; at 09:30 UTC it shows 10:30,
    before the end, so a comparison of the wall time with the instant would offer it too early."""
    with reminding(tmp_path, now="2026-10-20T09:00:00+00:00", zone="Europe/Lisbon") as run:
        application = run.applied("zone-job")
        interview(run, application, "lisbon")

        run.at("2026-10-20T09:30:00+00:00")
        assert offered(run) == set()
        run.at("2026-10-20T10:30:00+00:00")
        assert offered(run) == {"lisbon"}


def test_debrief_offer_stays_for_a_closed_application(tmp_path: Path):
    with reminding(tmp_path, now="2026-10-20T09:00:00+00:00") as run:
        closed = run.seeder().application(
            "closed-job", "Seed Buyer", "Larkspur Orchards", "rejected"
        )
        interview(run, closed, "after-rejection")
        at(run, END + timedelta(hours=2))

        assert offered(run) == {"after-rejection"}


def test_debrief_dismiss_hides_offer(tmp_path: Path):
    with reminding(tmp_path, now="2026-10-20T09:00:00+00:00") as run:
        application = run.applied("dismiss-job")
        interview(run, application, "first")
        interview(run, application, "second", END - timedelta(hours=1))
        at(run, END + timedelta(hours=2))
        assert offered(run) == {"first", "second"}
        path = base(application.id, "first") + "/dismiss"

        dismissed = run.post(path, version="0", **{"return": "today"})

        assert dismissed.status_code == 200 and announced(dismissed) == [DISMISSED]
        assert 'id="today-content"' in dismissed.text
        assert set(offers(dismissed.text)) == {"second"}, "only the dismissed one is gone"
        assert offered(run) == {"second"}
        [row] = run.rows("SELECT * FROM debriefs")
        assert row["state"] == "dismissed" and row["event_id"] == "first"
        assert run.rows("SELECT id FROM bank_questions") == []

        run.post(base(application.id, "second") + "/dismiss", version="0", **{"return": "today"})

        html = run.get("/", token=False).text
        assert offers(html) == {} and by_id(html, "today-debriefs") is None
        assert "Nothing needs your attention today." in says(html)
        assert run.app.state.today.view().empty


def test_debrief_done_hides_offer(tmp_path: Path):
    with reminding(tmp_path, now="2026-10-20T09:00:00+00:00") as run:
        application = run.applied("done-job")
        interview(run, application, "talked")
        at(run, END + timedelta(hours=2))
        assert offered(run) == {"talked"}

        saved = run.post(
            base(application.id, "talked") + "/save",
            version="0",
            questions="Why do you want this role?",
            how_it_went="Fine.",
        )

        assert saved.status_code == 200
        assert offered(run) == set()
        assert [r["state"] for r in run.rows("SELECT state FROM debriefs")] == ["done"]


def test_debrief_offer_is_not_changed_by_reading_today(tmp_path: Path):
    with reminding(tmp_path, now="2026-10-20T09:00:00+00:00") as run:
        application = run.applied("read-job")
        interview(run, application, "read")
        at(run, END + timedelta(hours=2))
        assert offered(run) == {"read"}
        before = run.tables()

        for path in ("/", "/today/content"):
            assert run.get(path).status_code == 200

        assert run.tables() == before


# --- review fixes: unique names, softer copy, the empty view past the window -------------------


def test_debrief_offer_buttons_and_links_are_described_by_their_own_offer(tmp_path: Path):
    with reminding(tmp_path, now="2026-10-20T09:00:00+00:00") as run:
        application = run.applied("names-job")
        interview(run, application, "first")
        interview(run, application, "second", END - timedelta(hours=1))
        at(run, END + timedelta(hours=2))

        html = run.get("/", token=False).text

        shown = offers(html)
        assert set(shown) == {"first", "second"}
        for event_id, row in shown.items():
            text = by_id(html, f"debrief-offer-{event_id}")
            assert text is not None and text.tag == "p" and text.within(row)
            assert "How did the interview go?" in text.text
            assert "optional" in text.text.lower(), "the offer says a debrief is optional"
            named = [
                e
                for e in elements(html)
                if e.within(row)
                and e.tag in {"a", "button"}
                and e.text in {"Debrief", "Not needed"}
            ]
            assert sorted(e.text for e in named) == ["Debrief", "Not needed"]
            assert all(
                e.attrs.get("aria-describedby") == f"debrief-offer-{event_id}" for e in named
            )
            assert all("aria-label" not in e.attrs for e in named), "the visible name stays"


def test_debrief_offer_view_is_empty_after_the_window(tmp_path: Path):
    with reminding(tmp_path, now="2026-10-20T09:00:00+00:00") as run:
        application = run.applied("view-job")
        interview(run, application, "old")
        window = timedelta(days=14)

        at(run, END + window - ONE_SECOND)
        assert [o.id for o in run.app.state.today.view().debriefs] == ["old"]

        at(run, END + window + ONE_SECOND)
        assert run.app.state.today.view().debriefs == ()
