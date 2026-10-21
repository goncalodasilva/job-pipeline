"""M5.5 keyboard drawer, event download, retained drafts and phone accessibility."""

import pytest
from icalendar import Calendar
from playwright.sync_api import expect

from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_board import card, choose_by_keyboard, desk, open_board, status_of
from tests.e2e.test_facts_page import press_by_keyboard
from tests.e2e.test_shell import has_no_horizontal_scroll, new_page, open_app

pytestmark = pytest.mark.e2e


def made(app):
    with desk(app) as seed:
        return seed.application("detail-job", "Seed Buyer", "Fictional Orchard", "applied")


def test_drawer_from_board_by_keyboard_focus_trap_and_esc_returns(running_app, browser):
    a = made(running_app)
    with new_page(browser, DESKTOP) as page:
        open_board(page, running_app)
        opener = card(page, a.id).get_by_role("link", name="Open application")
        press_by_keyboard(page, opener)
        dialog = page.get_by_role("dialog", name="Application details")
        expect(dialog).to_be_visible()
        close = dialog.get_by_role("button", name="Close", exact=True)
        expect(close).to_be_focused()
        page.keyboard.press("Shift+Tab")
        expect(dialog.locator("button[type=submit]").last).to_be_focused()
        page.keyboard.press("Tab")
        expect(close).to_be_focused()
        dialog.get_by_label("Note", exact=True).fill("Unsent orchard note")
        page.keyboard.press("Escape")
        expect(dialog).to_have_count(0)
        expect(opener).to_be_focused()
        press_by_keyboard(page, opener)
        expect(page.get_by_label("Note", exact=True)).to_have_value("Unsent orchard note")
        assert_no_serious_violations(page)


def test_add_interview_event_and_download_ics(running_app, browser, tmp_path):
    a = made(running_app)
    with new_page(browser, DESKTOP) as page:
        open_app(page, running_app, f"/applications/{a.id}")
        form = page.locator("#event-form")
        form.get_by_label("Date", exact=True).fill("2027-01-12")
        form.get_by_label("Time", exact=True).fill("14:00")
        form.get_by_label("Time zone", exact=True).fill("America/New_York")
        form.get_by_label("Duration (minutes)", exact=True).fill("60")
        press_by_keyboard(page, form.get_by_role("button", name="Save event", exact=True))
        download = page.get_by_role("button", name="Download calendar file", exact=True)
        expect(download).to_be_visible()
        with page.expect_download() as info:
            press_by_keyboard(page, download)
        saved = tmp_path / "event.ics"
        info.value.save_as(saved)
        assert info.value.suggested_filename == "jobpipeline-event.ics"
        event = Calendar.from_ical(saved.read_bytes()).walk("VEVENT")[0]
        assert str(event["DTSTART"].params["TZID"]) == "America/New_York"
        assert event.decoded("DTSTART").hour == 14
        assert_no_serious_violations(page)


def test_status_change_from_drawer_matches_board(running_app, browser):
    a = made(running_app)
    with new_page(browser, DESKTOP) as page:
        open_board(page, running_app)
        press_by_keyboard(page, card(page, a.id).get_by_role("link", name="Open application"))
        dialog = page.get_by_role("dialog", name="Application details")
        choose_by_keyboard(page, dialog, "Interviewing")
        expect(page.get_by_role("dialog", name="Add the interview date?")).to_be_visible()
        expect(page.get_by_role("dialog", name="Add the interview date?")).to_have_attribute(
            "open", ""
        )
        page.keyboard.press("Escape")
        expect(dialog.locator("#application-status-heading")).to_contain_text("Interviewing")
        page.keyboard.press("Escape")
        expect(dialog).to_have_count(0)
        expect(card(page, a.id)).to_have_attribute("data-status", "interviewing")
        assert status_of(running_app, a.id) == "interviewing"


def test_application_page_phone_390x844_and_axe(running_app, browser):
    a = made(running_app)
    with new_page(browser, PHONE) as page:
        open_app(page, running_app, f"/applications/{a.id}")
        expect(page.get_by_role("heading", level=1, name="Application details")).to_be_visible()
        assert has_no_horizontal_scroll(page)[0]
        assert_no_serious_violations(page)


def test_status_move_without_followup_keeps_drawer_focus(running_app, browser):
    a = made(running_app)
    with new_page(browser, DESKTOP) as page:
        open_board(page, running_app)
        press_by_keyboard(page, card(page, a.id).get_by_role("link", name="Open application"))
        dialog = page.get_by_role("dialog", name="Application details")
        choose_by_keyboard(page, dialog, "Screening")
        expect(dialog.locator("#application-status-heading")).to_contain_text("Screening")
        expect(dialog.locator("#application-title")).to_be_focused()


def test_typing_after_submission_survives_delayed_success(running_app, browser):
    from tests.waiting import wait_until

    a = made(running_app)
    with new_page(browser, DESKTOP) as page:
        open_app(page, running_app, f"/applications/{a.id}")
        held = []
        page.route(
            f"**/applications/{a.id}/notes", lambda route: held.append((route, route.fetch()))
        )
        note = page.get_by_label("Note", exact=True)
        note.fill("Submitted orchard note")
        press_by_keyboard(page, page.get_by_role("button", name="Add note", exact=True))
        wait_until(lambda: page.evaluate("true") and held, timeout=5, message="held note response")
        note.fill("Newer orchard typing")
        route, response = held.pop()
        route.fulfill(response=response)
        expect(page.locator(".save-message")).to_have_text("Note saved.")
        expect(note).to_have_value("Newer orchard typing")
        expect(page.locator(".application-timeline")).to_contain_text("Submitted orchard note")


def test_drawer_save_preserves_expanded_closed_and_focus(running_app, browser):
    with desk(running_app) as seed:
        a = seed.application("closed-detail-job", "Seed Buyer", "Fictional Orchard", "rejected")
    with new_page(browser, DESKTOP) as page:
        open_board(page, running_app)
        press_by_keyboard(page, page.get_by_role("button", name="Closed: 1", exact=True))
        opener = card(page, a.id).get_by_role("link", name="Open application")
        press_by_keyboard(page, opener)
        page.get_by_label("Note", exact=True).fill("Remember the fictional orchard")
        press_by_keyboard(page, page.get_by_role("button", name="Add note", exact=True))
        expect(page.locator(".save-message")).to_have_text("Note saved.")
        page.evaluate("""() => {
            window.__boardSettled = false;
            document.body.addEventListener('htmx:afterSettle', e => {
                if (e.detail.target.id === 'board') window.__boardSettled = true;
            });
        }""")
        page.keyboard.press("Escape")
        page.wait_for_function("window.__boardSettled")
        expect(page.get_by_role("dialog", name="Application details")).to_have_count(0)
        expect(opener).to_be_focused()
        expect(page.get_by_role("button", name="Closed: 1", exact=True)).to_have_attribute(
            "aria-expanded", "true"
        )
        expect(page).to_have_url(running_app.base_url + "/board?closed=1")


def fill_event(page, *, all_day=False):
    form = page.locator("#event-form")
    if all_day:
        form.get_by_label("Event type", exact=True).select_option("deadline")
        form.get_by_label("All day (deadlines only)", exact=True).check()
    form.get_by_label("Date", exact=True).fill("2026-11-01")
    form.get_by_label("Time", exact=True).fill("14:00") if not all_day else None
    form.get_by_label("Time zone", exact=True).fill("America/New_York")
    press_by_keyboard(page, form.get_by_role("button", name="Save event", exact=True))
    expect(page.locator(".save-message")).to_have_text("Event saved.")


def test_all_day_deadline_edit_on_25_hour_date(running_app, browser):
    a = made(running_app)
    with new_page(browser, PHONE) as page:
        open_app(page, running_app, f"/applications/{a.id}")
        fill_event(page, all_day=True)
        press_by_keyboard(
            page, page.locator(".application-timeline summary").filter(has_text="Edit event")
        )
        form = page.locator(".application-timeline form[data-detail-editor]")
        form.get_by_label("Event details", exact=True).fill("Changed all-day orchard deadline")
        press_by_keyboard(page, form.get_by_role("button", name="Save event changes", exact=True))
        expect(page.locator(".application-timeline")).to_contain_text(
            "Changed all-day orchard deadline"
        )
        assert_no_serious_violations(page)


def test_calendar_download_failure_is_visible_and_retryable(running_app, browser):
    a = made(running_app)
    with new_page(browser, PHONE) as page:
        open_app(page, running_app, f"/applications/{a.id}")
        fill_event(page)
        page.route("**/events/*/ics", lambda route: route.abort())
        button = page.get_by_role("button", name="Download calendar file", exact=True)
        press_by_keyboard(page, button)
        expect(page.locator("[data-calendar-status]")).to_have_text(
            "Calendar file could not be downloaded. Try again."
        )
        expect(button).to_be_enabled()


@pytest.mark.parametrize("stale", [False, True])
def test_typing_after_submission_survives_delayed_refusal(running_app, browser, stale):
    from tests.waiting import wait_until

    a = made(running_app)
    with new_page(browser, PHONE) as page:
        open_app(page, running_app, f"/applications/{a.id}")
        if stale:
            fill_event(page)
            press_by_keyboard(
                page, page.locator(".application-timeline summary").filter(has_text="Edit event")
            )
            form = page.locator(".application-timeline form[data-detail-editor]")
            form.locator("[name=version]").evaluate('(el) => { el.value = "99"; }')
            path = "**/events/*"
        else:
            form = page.locator("#event-form")
            path = f"**/applications/{a.id}/events"
        form.get_by_label("Date", exact=True).fill("2027-03-14")
        form.get_by_label("Time", exact=True).fill("02:30")
        form.get_by_label("Time zone", exact=True).fill("America/New_York")
        held = []
        page.route(path, lambda route: held.append((route, route.fetch())))
        press_by_keyboard(
            page,
            form.get_by_role(
                "button", name="Save event changes" if stale else "Save event", exact=True
            ),
        )
        wait_until(lambda: page.evaluate("true") and held, timeout=5, message="held refused event")
        route, response = held.pop()
        assert response.status == (409 if stale else 422)
        form.get_by_label("Time", exact=True).fill("03:30")
        form.get_by_label("Event details", exact=True).fill("Added while request pending")
        route.fulfill(response=response)
        expect(form.locator(".field-error, [role=alert]").first).to_be_visible()
        expect(form.get_by_label("Time", exact=True)).to_have_value("03:30")
        expect(form.get_by_label("Event details", exact=True)).to_have_value(
            "Added while request pending"
        )
        if stale:
            expect(form.locator("[name=version]")).to_have_value("99")


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE])
def test_add_event_action_visible_before_long_timeline(running_app, browser, viewport):
    with desk(running_app) as seed:
        a = seed.application("event-entry-job", "Seed Buyer", "Fictional Orchard", "applied")
        for number in range(20):
            seed.applications.add_feedback(a.id, f"Fictional orchard feedback {number}.")
    with new_page(browser, viewport) as page:
        open_board(page, running_app)
        press_by_keyboard(page, card(page, a.id).get_by_role("link", name="Open application"))
        drawer = page.get_by_role("dialog", name="Application details")
        action = drawer.get_by_role("link", name="Add event", exact=True)
        expect(action).to_be_visible()
        expect(action).to_be_in_viewport()
        press_by_keyboard(page, action)
        expect(drawer.get_by_role("heading", name="Add an event", exact=True)).to_be_focused()
        form = drawer.locator("#event-form")
        expect(form.get_by_label("Date", exact=True)).to_be_in_viewport()
        form.get_by_label("Date", exact=True).fill("2027-01-12")
        form.get_by_label("Time", exact=True).fill("14:00")
        form.get_by_label("Time zone", exact=True).fill("Europe/Paris")
        press_by_keyboard(page, form.get_by_role("button", name="Save event", exact=True))
        expect(drawer.locator(".save-message")).to_have_text("Event saved.")
        expect(drawer.get_by_role("button", name="Download calendar file")).to_be_visible()
        assert_no_serious_violations(page)


@pytest.mark.parametrize("source", ["board", "job"])
def test_add_event_directly_from_board_and_job_details(running_app, browser, source):
    a = made(running_app)
    with new_page(browser, PHONE) as page:
        if source == "board":
            open_board(page, running_app)
            container = card(page, a.id)
        else:
            open_app(page, running_app, "/jobs/detail-job")
            container = page.locator("#job-detail-header")
        link = container.get_by_role("link", name="Add event")
        if source == "board":
            expect(link).to_have_accessible_name("Add event, Fictional Orchard, Seed Buyer")
        press_by_keyboard(page, link)
        expect(page).to_have_url(
            running_app.base_url + f"/applications/{a.id}#application-event-editor"
        )
        expect(page.get_by_role("heading", name="Add an event", exact=True)).to_be_focused()
        expect(page.locator("#event-form").get_by_label("Date", exact=True)).to_be_in_viewport()
        assert_no_serious_violations(page)
