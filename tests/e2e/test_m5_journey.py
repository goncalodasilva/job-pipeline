"""M5 acceptance journey through the real CLI with fake providers and fictional data."""

import re

import pytest
from icalendar import Calendar
from playwright.sync_api import expect

from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_board import card, choose_by_keyboard, history, status_of
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, press_by_keyboard, tab_until
from tests.e2e.test_m4_journey import LINK, clean_screen, query, summaries
from tests.e2e.test_shell import open_app
from tests.waiting import wait_until

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])
def test_m5_journey_apply_board_drawer_event_ics_today(running_app, browser, tmp_path, viewport):
    app = running_app
    with browser.new_context(viewport=viewport) as context:
        page = context.new_page()
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        open_app(page, app, "/inbox")
        press_by_keyboard(page, page.get_by_role("button", name="+ Add job", exact=True))
        tab_until(page, page.locator("#job-url"))
        page.keyboard.type(LINK)
        press_by_keyboard(page, page.get_by_role("button", name="Add job", exact=True))
        wait_until(lambda: summaries(app) == 1, timeout=20, message="fictional summary saved")
        expect(page.locator("#job-intake")).to_contain_text("Practice data (fake provider)")
        page.keyboard.press("Escape")
        open_app(page, app, "/inbox")
        tab_until(page, page.locator("#inbox-card-heading"))
        page.keyboard.press("a")
        expect(page.locator("#inbox-undo")).to_contain_text("Marked to apply.")
        applications = query(app, "SELECT id, status_key FROM applications")
        assert len(applications) == 1 and applications[0][1] == "preparing"
        aid = str(applications[0][0])
        clean_screen(page)

        if viewport == PHONE:
            press_by_keyboard(page, page.get_by_role("button", name="Menu", exact=True))
        press_by_keyboard(page, page.get_by_role("link", name="Board", exact=True))
        expect(card(page, aid)).to_be_visible()
        choose_by_keyboard(page, card(page, aid), "Applied")
        expect(card(page, aid)).to_have_attribute("data-status", "applied")
        assert status_of(app, aid) == "applied"
        clean_screen(page)
        press_by_keyboard(page, card(page, aid).get_by_role("link", name="Open application"))
        drawer = page.get_by_role("dialog", name="Application details", exact=True)
        expect(drawer).to_be_visible()
        choose_by_keyboard(page, drawer, "Interviewing")
        expect(page.get_by_role("dialog", name="Add the interview date?")).to_be_visible()
        page.keyboard.press("Escape")
        expect(drawer.locator("#application-status-heading")).to_contain_text("Interviewing")
        assert history(app, aid)[-2:] == [
            ("preparing", "applied", "menu"),
            ("applied", "interviewing", "drawer"),
        ]
        form = drawer.locator("#event-form")
        form.get_by_label("Date", exact=True).fill("2027-01-12")
        form.get_by_label("Time", exact=True).fill("14:00")
        form.get_by_label("Time zone", exact=True).fill("America/New_York")
        form.get_by_label("Duration (minutes)", exact=True).fill("60")
        press_by_keyboard(page, form.get_by_role("button", name="Save event", exact=True))
        expect(drawer).to_contain_text("Event saved.")
        with page.expect_download() as info:
            press_by_keyboard(page, drawer.get_by_role("button", name="Download calendar file"))
        saved = tmp_path / "journey.ics"
        info.value.save_as(saved)
        assert info.value.suggested_filename == "jobpipeline-event.ics"
        event = Calendar.from_ical(saved.read_bytes()).walk("VEVENT")[0]
        assert str(event["DTSTART"].params["TZID"]) == "America/New_York"
        assert event.decoded("DTSTART").isoformat() == "2027-01-12T14:00:00-05:00"
        assert event.decoded("DTEND").isoformat() == "2027-01-12T15:00:00-05:00"
        clean_screen(page)

        # A past follow-up makes this independent of the subprocess's real clock.
        form = drawer.locator("#event-form")
        tab_until(page, form.get_by_label("Event type", exact=True))
        page.keyboard.press("f")
        page.keyboard.press("Tab")
        form.get_by_label("Date", exact=True).fill("2000-01-12")
        form.get_by_label("Time", exact=True).fill("09:00")
        form.get_by_label("Time zone", exact=True).fill("UTC")
        press_by_keyboard(page, form.get_by_role("button", name="Save event", exact=True))
        expect(drawer).to_contain_text("Follow-up")
        expect(drawer.get_by_role("button", name="Download calendar file")).to_have_count(2)
        assert query(app, "SELECT type FROM events ORDER BY created_at, id") == [
            ("interview",),
            ("follow_up",),
        ]
        page.keyboard.press("Escape")
        expect(drawer).to_have_count(0)
        if viewport == PHONE:
            press_by_keyboard(page, page.get_by_role("button", name="Menu", exact=True))
        today = page.locator("#today-link")
        expect(today).to_contain_text("Today")
        press_by_keyboard(page, today)
        due = page.locator("#today-reminders")
        expect(due.locator(".reminder-item")).to_have_count(1)
        expect(page.locator("#notifications-toggle")).to_contain_text("1 new")
        clean_screen(page)
        press_by_keyboard(page, due.get_by_role("button", name=re.compile("^Done,")))
        expect(due).to_have_count(0)
        expect(page.locator("#notifications-toggle")).to_contain_text("0 new")
        assert query(app, "SELECT state FROM reminder_states") == [("done",)]
        clean_screen(page)
