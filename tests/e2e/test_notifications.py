"""Notification actions, focus, displayed-only seen writes and phone accessibility."""

import re

import pytest
from playwright.sync_api import expect

from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import PHONE
from tests.e2e.test_board import desk, rows
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, wait_said
from tests.e2e.test_settings_board import tab_to
from tests.e2e.test_shell import assert_target_size, has_no_horizontal_scroll, new_page, open_app

pytestmark = pytest.mark.e2e


def seed(app):
    with desk(app) as data:
        for i in range(3):
            data.application(f"notify-{i}", f"Orchard role {i}", "Fictional Orchards", "applied")


def test_notifications_open_by_keyboard_done_snooze_dismiss_announced(running_app, desktop_page):
    seed(running_app)
    page = desktop_page
    page.add_init_script(RECORD_ANNOUNCEMENTS)
    open_app(page, running_app)
    toggle = page.get_by_role("button", name="Notifications: 3 new", exact=True)
    assert rows(running_app, "SELECT * FROM reminder_states") == []
    tab_to(page, toggle)
    page.keyboard.press("Enter")
    panel = page.locator("#notifications-panel")
    expect(panel.locator(".reminder-item")).to_have_count(3)
    expect(page.locator("#notifications-toggle")).to_have_text("Notifications: 0 new")
    for action, announcement, remaining in [
        ("Done", "Reminder marked done.", 2),
        ("Snooze", "Reminder snoozed until", 1),
        ("Dismiss", "Reminder dismissed.", 0),
    ]:
        button = panel.get_by_role("button", name=re.compile(f"^{action},")).first
        tab_to(page, button)
        page.keyboard.press("Enter")
        expect(panel.locator(".reminder-item")).to_have_count(remaining)
        if action == "Snooze":
            page.wait_for_function(
                "prefix => (window.__said || []).some(s => s.startsWith(prefix))", arg=announcement
            )
        else:
            wait_said(page, announcement)
        expect(page.locator("#notifications-heading")).to_be_focused()
    expect(panel).to_contain_text("Nothing due. Reminders appear here while the app is open.")
    page.keyboard.press("Escape")
    expect(page.locator("#notifications-toggle")).to_be_focused()
    expect(page.locator("#notifications-popover")).to_be_hidden()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_notifications_phone_390x844_and_axe(running_app, browser, scheme):
    seed(running_app)
    with new_page(browser, PHONE, color_scheme=scheme) as page:
        open_app(page, running_app)
        toggle = page.locator("#notifications-toggle")
        tab_to(page, toggle)
        page.keyboard.press("Space")
        expect(page.locator("#notifications-panel .reminder-item")).to_have_count(3)
        expect(toggle).to_have_text("Notifications: 0 new")
        assert has_no_horizontal_scroll(page)[0]
        assert_target_size(toggle, "Notifications")
        for button in page.locator("#notifications-panel button").all():
            assert_target_size(button, button.inner_text())
        wait_for_settled_styles(page)
        assert_no_serious_violations(page)


def test_background_refresh_keeps_snooze_choice_and_button_focus(running_app, desktop_page):
    seed(running_app)
    page = desktop_page
    open_app(page, running_app)
    page.locator("#notifications-toggle").click()
    expect(page.locator("#notifications-toggle")).to_have_text("Notifications: 0 new")
    choice = page.locator("#notifications-panel select").first
    choice.select_option("next_week")
    button = (
        page.locator("#notifications-panel")
        .get_by_role("button", name=re.compile("^Snooze,"))
        .first
    )
    tab_to(page, button)
    with page.expect_response(lambda r: "/notifications/panel" in r.url):
        page.evaluate("window.htmx.trigger(document.body, 'tasks:updated')")
    page.wait_for_function("!document.querySelector('#notifications-popover.htmx-request')")
    expect(choice).to_have_value("next_week")
    expect(button).to_be_focused()


def test_done_refreshes_board_due_marker_without_reloading(running_app, desktop_page):
    seed(running_app)
    page = desktop_page
    open_app(page, running_app, "/board")
    expect(page.locator(".follow-up-due:visible")).to_have_count(3)
    page.locator("#notifications-toggle").click()
    expect(page.locator("#notifications-toggle")).to_have_text("Notifications: 0 new")
    page.locator("#notifications-panel").get_by_role(
        "button", name=re.compile("^Done,")
    ).first.click()
    expect(page.locator("#notifications-panel .reminder-item")).to_have_count(2)
    expect(page.locator(".follow-up-due:visible")).to_have_count(2)


def test_stale_action_panel_can_reopen(running_app, desktop_page):
    seed(running_app)
    page = desktop_page
    open_app(page, running_app)
    page.locator("#notifications-toggle").click()
    expect(page.locator("#notifications-toggle")).to_have_text("Notifications: 0 new")
    page.evaluate("""() => {
      const row = document.querySelector('.reminder-item');
      row.querySelectorAll('input[name=version]').forEach(e => e.value='999');
    }""")
    page.locator("#notifications-panel").get_by_role(
        "button", name=re.compile("^Done,")
    ).first.click()
    expect(page.locator("#reminder-conflict")).to_be_visible()
    page.keyboard.press("Escape")
    page.locator("#notifications-toggle").click()
    expect(page.locator("#notifications-panel .reminder-item")).to_have_count(3)
