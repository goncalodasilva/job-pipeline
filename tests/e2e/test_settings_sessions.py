"""M7.8 Settings > Sessions in a real browser (AC-M7.6): the section at 390x844 with no sideways
scroll and an axe scan in light and dark, and a keyboard-only save of the pause switch.

The form sits in a collapsed native disclosure (one tab stop, as Your details does), so each test
opens it first: by keyboard in the keyboard test (focus the summary, Enter), by a click on the
phone. A save leaves it open.

The assumed markup is in ``tests/session_settings_pages.py`` and the behaviour in
``tests/integration/test_settings_sessions.py``. The app starts with a finished setup
(``running_app`` default). No fixed waits; every value is fictional.
"""

import json

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_board import rows
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, wait_said
from tests.e2e.test_settings_board import tab_to
from tests.e2e.test_shell import assert_target_size, has_no_horizontal_scroll, new_page, open_app
from tests.running_app import RunningApp
from tests.session_settings_pages import LABELS, SAVED

pytestmark = pytest.mark.e2e

SECTION = "#sessions-settings"
SUMMARY = "#sessions-summary"
FITS = """(selector) => {
  const box = document.querySelector(selector);
  return box.scrollWidth <= box.clientWidth;
}"""


def stored(app: RunningApp) -> list[tuple[str, int]]:
    return rows(app, "SELECT value, version FROM settings WHERE key='sessions'")


def open_settings(page: Page, app: RunningApp) -> None:
    page.add_init_script(RECORD_ANNOUNCEMENTS)
    open_app(page, app, "/settings")
    section = page.locator(SECTION)
    expect(section.get_by_role("heading", name="Sessions", level=2, exact=True)).to_be_visible()


def open_form_by_keyboard(page: Page) -> None:
    """Tab to the disclosure's summary and open it with Enter; the form is then reachable."""
    section = page.locator(SECTION)
    expect(section.locator("details")).not_to_have_attribute("open", "")
    tab_to(page, section.locator(SUMMARY))
    page.keyboard.press("Enter")
    expect(section.locator("details")).to_have_attribute("open", "")
    expect(section.get_by_label(LABELS["frequency"])).to_be_visible()


def assert_fits(page: Page) -> None:
    fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
    assert fits, f"scrollWidth {scroll_width} > {inner_width}"
    assert page.evaluate(FITS, SECTION), "the Sessions section fits its own width"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_settings_sessions_phone_390x844_and_axe(running_app: RunningApp, browser, theme):
    app = running_app
    with new_page(browser, PHONE, color_scheme=theme) as page:
        open_settings(page, app)
        section = page.locator(SECTION)
        # Collapsed on first load: still clean, and the summary is a full-size target.
        expect(section.locator("details")).not_to_have_attribute("open", "")
        assert_target_size(section.locator(SUMMARY), "summary")
        wait_for_settled_styles(page)
        assert_fits(page)
        assert_no_serious_violations(page)
        section.locator(SUMMARY).click()
        expect(section.get_by_label(LABELS["frequency"])).to_be_visible()
        for name in ("frequency", "length", "ask_later_default"):
            expect(section.locator(f'select[name="{name}"]')).to_have_count(1)
        # Every control has a visible name; the switch says what it does.
        expect(section.get_by_label(LABELS["frequency"])).to_have_value("weekly")
        expect(section.get_by_label(LABELS["length"])).to_have_value("3")
        expect(section.get_by_label(LABELS["ask_later_default"])).to_have_value("week")
        expect(section.get_by_label(LABELS["paused"])).not_to_be_checked()
        wait_for_settled_styles(page)
        assert_fits(page)
        assert_no_serious_violations(page)
        for label in ("frequency", "length", "ask_later_default", "paused"):
            assert_target_size(section.get_by_label(LABELS[label]), label)
        assert_target_size(section.get_by_role("button", name="Save"), "Save")

        # A save on the phone layout: announced, shown, still no sideways scroll, still clean.
        section.get_by_label(LABELS["frequency"]).select_option("twice_weekly")
        section.get_by_label(LABELS["length"]).select_option("5")
        section.get_by_role("button", name="Save").click()
        expect(section.locator("#sessions-settings-done")).to_have_text(SAVED)
        wait_said(page, SAVED)
        assert_fits(page)
        wait_for_settled_styles(page)
        assert_no_serious_violations(page)
    [(value, version)] = stored(app)
    assert json.loads(value) == {
        "frequency": "twice_weekly",
        "length": 5,
        "ask_later_default": "week",
        "paused": False,
    }
    assert version == 1


def test_settings_sessions_pause_saved_by_keyboard(running_app: RunningApp, browser):
    app = running_app
    with new_page(browser, DESKTOP) as page:
        open_settings(page, app)
        section = page.locator(SECTION)
        open_form_by_keyboard(page)
        pause = section.get_by_label(LABELS["paused"])
        tab_to(page, pause)
        page.keyboard.press("Space")
        expect(pause).to_be_checked()
        tab_to(page, section.get_by_role("button", name="Save"))
        page.keyboard.press("Enter")
        expect(section.locator("#sessions-settings-done")).to_have_text(SAVED)
        expect(section.locator("#sessions-settings-heading")).to_be_focused()
        wait_said(page, SAVED)
        expect(section.get_by_label(LABELS["paused"])).to_be_checked()
        assert_no_serious_violations(page)
    [(value, version)] = stored(app)
    assert json.loads(value)["paused"] is True and version == 1
