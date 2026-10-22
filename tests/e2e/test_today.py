"""Today keyboard actions and light/dark phone layouts on temporary CLI workspaces."""

import re

import pytest
from playwright.sync_api import expect

from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import PHONE
from tests.e2e.test_board import desk
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, wait_said
from tests.e2e.test_settings_board import tab_to
from tests.e2e.test_shell import has_no_horizontal_scroll, new_page, open_app

pytestmark = pytest.mark.e2e


def seed(app):
    with desk(app) as data:
        data.application("today-applied", "Seed Buyer", "Fictional Orchards", "applied")
        data.job("today-new", "Tree Specialist", "Fictional Orchards")


def test_today_with_seeded_data_by_keyboard(running_app, desktop_page):
    seed(running_app)
    page = desktop_page
    page.add_init_script(RECORD_ANNOUNCEMENTS)
    open_app(page, running_app)
    expect(page.get_by_role("heading", name="Today", exact=True)).to_be_visible()
    expect(page.locator("#today-reminders .reminder-item")).to_have_count(1)
    expect(page.locator("#today-jobs")).to_contain_text("1 unseen job")
    button = page.locator("#today-reminders").get_by_role("button", name=re.compile("^Done,"))
    tab_to(page, button)
    page.keyboard.press("Enter")
    expect(page.locator("#today-reminders")).to_have_count(0)
    expect(page.locator("#page-heading")).to_be_focused()
    wait_said(page, "Reminder marked done.")
    expect(page.locator("#notifications-toggle")).to_have_text("Notifications: 0 new")
    assert_no_serious_violations(page)


def test_today_empty_says_nothing_due(running_app, desktop_page):
    open_app(desktop_page, running_app)
    expect(desktop_page.locator("#today-content")).to_contain_text(
        "Nothing needs your attention today."
    )
    expect(desktop_page.locator(".today-block")).to_have_count(0)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_today_phone_390x844_and_axe(running_app, browser, scheme):
    seed(running_app)
    with new_page(browser, PHONE, color_scheme=scheme) as page:
        open_app(page, running_app)
        expect(page.locator("#today-reminders")).to_be_visible()
        expect(page.locator("#today-jobs")).to_be_visible()
        assert has_no_horizontal_scroll(page)[0]
        wait_for_settled_styles(page)
        assert_no_serious_violations(page)


def test_today_and_open_notifications_have_unique_control_ids(running_app, desktop_page):
    seed(running_app)
    page = desktop_page
    open_app(page, running_app)
    page.locator("#notifications-toggle").click()
    expect(page.locator("#notifications-panel .reminder-item")).to_have_count(1)
    expect(page.locator("#notifications-toggle")).to_have_text("Notifications: 0 new")
    duplicates = page.evaluate("""() => {
      const ids = [...document.querySelectorAll('[id]')].map(e => e.id);
      return ids.filter((id, i) => ids.indexOf(id) !== i);
    }""")
    assert duplicates == []
    assert_no_serious_violations(page)


def test_today_stale_reminder_keeps_page_and_reload_path(running_app, desktop_page):
    seed(running_app)
    page = desktop_page
    open_app(page, running_app)
    form = page.locator('#today-reminders form[action$="/done"]')
    form.locator('input[name="version"]').evaluate("e => e.value='999'")
    form.get_by_role("button", name=re.compile("^Done,")).click()
    expect(page.locator("#today-notice [role=alert]")).to_be_visible()
    expect(page.locator("#today-reminders .reminder-item")).to_have_count(1)
    page.locator("#today-notice").get_by_role("link", name="Reload", exact=True).click()
    page.locator('#today-reminders form[action$="/done"]').get_by_role("button").click()
    expect(page.locator("#today-reminders")).to_have_count(0)
