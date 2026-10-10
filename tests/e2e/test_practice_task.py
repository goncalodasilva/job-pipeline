"""Settings > Testing tools in a real browser (M2.17; ADR-076 item 2).

The app starts with a completed setup (``running_app`` default: the fake provider saved; fakes
allowed), so the practice task is offered. By keyboard only the user reaches the three
selects in order and then the Start button, and Enter starts a task without leaving Settings.
The section has no serious or critical axe findings at desktop size and at 390x844, and no
horizontal scroll on the phone. Every value is fictional.
"""

import pytest
from playwright.sync_api import Browser, expect

from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_setup_provider import SMALL_PHONE, tab_to
from tests.e2e.test_setup_provider_test import tab_to_locator
from tests.e2e.test_shell import assert_target_size, has_no_horizontal_scroll, new_page, open_app
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e

HEADING = "Testing tools"
FORM = 'form[action="/tasks/practice"]'
SELECTS = {
    "duration": ["quick", "slow"],
    "outcome": ["ok", "usage_limit", "network", "invalid_output"],
    "tier": ["fast", "thorough"],
}


def test_practice_form_keyboard_only(running_app: RunningApp, browser: Browser) -> None:
    with new_page(browser, DESKTOP) as page:
        open_app(page, running_app, "/settings")
        expect(page.get_by_role("heading", name=HEADING, level=2)).to_be_visible()
        form = page.locator(FORM)
        expect(form).to_have_count(1)
        for name, values in SELECTS.items():
            select = form.locator(f'select[name="{name}"]')
            options = select.locator("option").evaluate_all("els => els.map(el => el.value)")
            assert options == values, name
            expect(select).to_have_value(values[0])
            tab_to(page, f'{FORM} select[name="{name}"]')  # in order, Tab only
        page.keyboard.type("slow")  # focus is on tier; type-ahead must not reach duration
        expect(form.locator('select[name="duration"]')).to_have_value("quick")
        start = form.get_by_role("button", name="Start")
        tab_to_locator(page, start)
        page.keyboard.press("Enter")
        # The open event stream means the page never goes network-idle; wait for the answer.
        expect(page.locator("#practice-status")).to_have_text("Practice task started.")
        expect(page.locator("#practice-status")).to_be_focused()
        expect(page).to_have_url(f"{running_app.base_url}/settings")
        expect(page.get_by_role("heading", name=HEADING, level=2)).to_be_visible()
        expect(page.get_by_text("Something went wrong")).to_have_count(0)


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])
def test_practice_section_axe_and_phone_width(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int]
) -> None:
    with new_page(browser, viewport) as page:
        open_app(page, running_app, "/settings")
        form = page.locator(FORM)
        expect(form).to_be_visible()
        for name in SELECTS:
            select = form.locator(f'select[name="{name}"]')
            label = select.evaluate("el => el.labels.length ? el.labels[0].textContent.trim() : ''")
            assert label, f"{name} has a visible label"
        start = form.get_by_role("button", name="Start")
        expect(start).to_be_visible()
        assert_no_serious_violations(page)
        if viewport == PHONE:
            assert viewport == {"width": 390, "height": 844}
            fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
            assert fits, f"scrollWidth {scroll_width} > {inner_width}"
            assert_target_size(start, "Start")


def test_practice_section_fits_360(running_app: RunningApp, browser: Browser) -> None:
    """At 360x740 the section, also with its status line after a start, causes no
    horizontal scroll."""
    assert SMALL_PHONE["width"] == 360
    with new_page(browser, SMALL_PHONE) as page:
        open_app(page, running_app, "/settings")
        form = page.locator(FORM)
        expect(form).to_be_visible()
        fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert fits, f"scrollWidth {scroll_width} > {inner_width}"
        form.get_by_role("button", name="Start").click()
        expect(page.locator("#practice-status")).to_have_text("Practice task started.")
        fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert fits, f"scrollWidth {scroll_width} > {inner_width}"
        assert_target_size(page.locator(FORM).get_by_role("button", name="Start"), "Start")
