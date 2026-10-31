"""M7.9 entry points into a session in a real browser: the gap check, the application drawer, the
capture inbox, quality hints and the rejection follow-up, by keyboard, at 390x844 and with an axe
scan in light and dark.

The assumed markup is listed in ``tests/entry_point_pages.py``. The app starts with a finished
setup and the fake provider; the jobs, facts, event and note are fictional and written into its
database. The gap check runs through the fake provider by keyboard. No fixed waits.
"""

import re
from datetime import UTC, datetime

import pytest
from playwright.sync_api import Page, expect

from app.domain.facts import FactType
from app.modules.storage import capture_repo
from app.services.facts import FactInput, FactService
from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import PHONE
from tests.e2e.test_board import card, choose_by_keyboard, desk, open_board, rows
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, press_by_keyboard
from tests.e2e.test_shell import assert_target_size, has_no_horizontal_scroll, new_page, open_app
from tests.entry_point_pages import (
    CONTINUE,
    LABELS,
    NOTE_TEXT,
    STORY_NO_RESULT,
    UPCOMING,
)
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory
from tests.reminder_desk import add_event
from tests.running_app import RunningApp
from tests.tailoring_pages import seed_facts, seed_summary

pytestmark = pytest.mark.e2e

JOB_ID = "entry-e2e-job"
OTHER_JOB_ID = "entry-e2e-other"
NOTE_ID = "entry-e2e-note"
EVENT_ID = "entry-e2e-event"
QUESTION_TIMEOUT_MS = 20_000
RESULT_TIMEOUT_MS = 20_000
SESSION_URL = re.compile(r"/sessions/[A-Za-z0-9_-]+$")
HINT_FACT = STORY_NO_RESULT[0]  # a story whose situation names it in the Library


def seeded(app: RunningApp) -> dict[str, str]:
    """An application with a summary, facts, an upcoming interview and a quality hint; a second
    application to reject; one capture note."""
    with desk(app) as seed:
        application = seed.application(
            JOB_ID, "Route Planner", "Harbor Kite Fictional", "preparing"
        )
        seed_summary(seed.db, JOB_ID)
        add_event(seed.db, EVENT_ID, application.id, "interview", UPCOMING)
        other = seed.application(OTHER_JOB_ID, "Boiler Fitter", "Fern Fictional Labs", "applied")
        clock = FrozenClock(datetime(2026, 1, 5, 9, 0, tzinfo=UTC))
        facts = FactService(seed.db, clock, SequentialIdFactory("entryfact"))
        seed_facts(facts)
        situation, action, _ = STORY_NO_RESULT
        facts.create(FactInput(type=FactType.STORY, situation=situation, action=action))
        with seed.db.transaction() as conn:
            capture_repo.insert_note(
                conn, note_id=NOTE_ID, text=NOTE_TEXT, now="2026-10-30T10:00:00+00:00"
            )
    return {"application": str(application.id), "other": str(other.id)}


def region(page: Page):
    return page.locator("#tailoring")


def offer_button(scope, place: str):
    return scope.get_by_role("button", name=LABELS[place], exact=True)


def session_rows(app: RunningApp) -> list[tuple[str, str | None, int, str]]:
    return rows(
        app, "SELECT focus_kind, focus_ref, length, state FROM sessions ORDER BY created_at, id"
    )


def end_session(page: Page) -> None:
    press_by_keyboard(
        page,
        page.locator("#session-panel").get_by_role("button", name="End session", exact=True),
    )
    expect(page.get_by_role("heading", name="Session summary")).to_be_visible()


def run_gap_check(page: Page) -> None:
    press_by_keyboard(page, region(page).get_by_role("button", name="Check gaps", exact=True))
    expect(region(page).locator("[data-requirement-id]")).to_have_count(
        4, timeout=RESULT_TIMEOUT_MS
    )


def test_entry_points_open_focused_session_by_keyboard(running_app: RunningApp, desktop_page):
    page = desktop_page
    made = seeded(running_app)
    page.add_init_script(RECORD_ANNOUNCEMENTS)

    # The gap check: no offer before a check; after one with gaps, one offer, by keyboard.
    open_app(page, running_app, f"/applications/{made['application']}?tab=tailoring")
    expect(offer_button(region(page), "gap")).to_have_count(0)
    run_gap_check(page)
    press_by_keyboard(page, offer_button(region(page), "gap"))
    expect(page).to_have_url(SESSION_URL, timeout=QUESTION_TIMEOUT_MS)
    expect(page.locator("#session-answer")).to_be_visible(timeout=QUESTION_TIMEOUT_MS)
    assert session_rows(running_app) == [("job", JOB_ID, 3, "open")]
    end_session(page)

    # The drawer: the interview offers a session about its job.
    open_board(page, running_app)
    press_by_keyboard(
        page, card(page, made["application"]).get_by_role("link", name="Open application")
    )
    dialog = page.get_by_role("dialog", name="Application details")
    expect(dialog).to_be_visible()
    expect(offer_button(dialog, "interview")).to_have_count(1)
    press_by_keyboard(page, offer_button(dialog, "interview"))
    expect(page).to_have_url(SESSION_URL, timeout=QUESTION_TIMEOUT_MS)
    expect(page.locator("#session-answer")).to_be_visible(timeout=QUESTION_TIMEOUT_MS)
    assert session_rows(running_app) == [
        ("job", JOB_ID, 3, "ended"),
        ("job", JOB_ID, 3, "open"),
    ]
    end_session(page)

    # The capture inbox: a note is expanded in a session focused on it.
    open_app(page, running_app, "/facts?tab=capture")
    item = page.locator(f"#note-{NOTE_ID}")
    press_by_keyboard(page, offer_button(item, "note"))
    expect(page).to_have_url(SESSION_URL, timeout=QUESTION_TIMEOUT_MS)
    expect(page.locator("#session-answer")).to_be_visible(timeout=QUESTION_TIMEOUT_MS)
    assert session_rows(running_app)[-1] == ("capture_note", NOTE_ID, 3, "open")
    session_url = page.url

    # With that session open, the same offer says why nothing started in its own slot (no new
    # screen), moves focus to the reason, and its Continue link returns to the session.
    open_app(page, running_app, "/facts?tab=capture")
    press_by_keyboard(page, offer_button(page.locator(f"#note-{NOTE_ID}"), "note"))
    slot = page.locator(f"#session-refusal-note-{NOTE_ID}")
    expect(slot.get_by_role("link", name=CONTINUE)).to_be_visible()
    expect(slot.locator("[role=alert]")).to_be_focused()
    expect(page.locator("#session-new")).to_have_count(0)
    press_by_keyboard(page, slot.get_by_role("link", name=CONTINUE))
    expect(page).to_have_url(session_url)
    assert len(session_rows(running_app)) == 3


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_entry_points_phone_390x844_and_axe(running_app: RunningApp, browser, theme):
    made = seeded(running_app)
    with new_page(browser, PHONE, color_scheme=theme) as page:

        def checked(button, label: str) -> None:
            expect(button).to_be_visible()
            wait_for_settled_styles(page)
            fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
            assert fits, f"{label}: the page scrolls sideways: {scroll_width} > {inner_width}"
            assert_no_serious_violations(page)
            assert_target_size(button, label)

        # The gap check step after a check.
        open_app(page, running_app, f"/applications/{made['application']}?tab=tailoring")
        run_gap_check(page)
        checked(offer_button(region(page), "gap"), LABELS["gap"])

        # The application page's interview.
        open_app(page, running_app, f"/applications/{made['application']}")
        checked(offer_button(page, "interview"), LABELS["interview"])

        # The capture inbox.
        open_app(page, running_app, "/facts?tab=capture")
        checked(offer_button(page.locator(f"#note-{NOTE_ID}"), "note"), LABELS["note"])

        # The Library: the hint is words, not colour, and non-blocking; the offer fits.
        open_app(page, running_app, "/facts")
        item = page.locator("#library-results .fact-card").filter(has_text=HINT_FACT)
        expect(item).to_contain_text("Add a result?")
        checked(offer_button(item, "hint"), LABELS["hint"])

        # The rejection follow-up on the board.
        open_board(page, running_app)
        choose_by_keyboard(page, card(page, made["other"]), "Rejected")
        dialog = page.get_by_role("dialog", name="Any feedback worth noting?")
        expect(dialog).to_be_visible()
        checked(offer_button(dialog, "rejection"), LABELS["rejection"])
        page.keyboard.press("Escape")
        expect(dialog).not_to_be_visible()
        assert session_rows(running_app) == [], "looking at the offers started nothing"
