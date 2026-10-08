"""Step 1 of the setup guide in a real browser (M1.16; AC-M1.1, AC-M1.6; spec 3.9).

A true first run (``running_app`` with ``"first_run"``): the data folder is the platform default
under the test's temporary home and nothing exists until step 1 is submitted. Submitting by
keyboard moves the guide to the AI provider step with htmx: the URL, the step list, the title,
the focus and the live announcement all follow. At 390x844 in light and dark the step, and its
field-error state, have no serious or critical axe findings and no horizontal scroll. The app
runs on loopback with temporary folders; every value is fictional.
"""

import re

import pytest
from playwright.sync_api import Browser, Page, expect

from app.config import load_bootstrap
from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_shell import has_no_horizontal_scroll, new_page, open_app
from tests.running_app import RunningApp

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.parametrize("running_app", ["first_run"], indirect=True),
]

SUBMIT = 'button[name="action"][value="open"]'
MAX_TABS = 40


def tab_to(page: Page, selector: str) -> None:
    """Press Tab until ``selector`` has focus (keyboard only, no clicks)."""
    for _ in range(MAX_TABS):
        page.keyboard.press("Tab")
        if page.evaluate("(s) => document.activeElement?.matches(s) ?? false", selector):
            return
    raise AssertionError(f"{selector} never received focus within {MAX_TABS} Tab presses")


def test_welcome_desktop_keyboard_submit(running_app: RunningApp, browser: Browser) -> None:
    assert not running_app.data_dir.exists(), "a first run creates nothing before step 1"
    with new_page(browser, DESKTOP) as page:
        open_app(page, running_app, "/")
        assert page.url == f"{running_app.base_url}/setup/welcome", page.url
        expect(page.locator('[aria-current="step"]')).to_have_text("Welcome and data folder")
        expect(page.locator("#folder")).to_have_value(str(running_app.data_dir))

        tab_to(page, SUBMIT)
        page.keyboard.press("Enter")

        expect(page).to_have_url(f"{running_app.base_url}/setup/provider")
        expect(page.locator('[aria-current="step"]')).to_have_count(1)
        expect(page.locator('[aria-current="step"]')).to_have_text("AI provider")
        expect(page).to_have_title(re.compile(r"^AI provider"))
        expect(page.locator("#step-heading")).to_be_focused()
        expect(page.locator("#announcer")).to_have_text("Step 2 of 4: AI provider")
        expect(page.locator("h1")).to_have_count(1)

    assert (running_app.data_dir / "app.db").is_file()
    bootstrap = load_bootstrap(running_app.config_dir)
    assert bootstrap is not None and bootstrap.data_dir == running_app.data_dir


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_welcome_phone_axe(running_app: RunningApp, browser: Browser, scheme: str) -> None:
    with new_page(browser, PHONE, color_scheme=scheme) as page:
        open_app(page, running_app, "/setup/welcome")
        expect(page.locator("#folder")).to_be_visible()
        assert_no_serious_violations(page)
        fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert fits, f"scrollWidth {scroll_width} > {inner_width}"

        # The field-error state: a relative folder is refused and tied to the field.
        page.locator("#folder").fill("relative/job-data")
        page.locator(SUBMIT).click()
        field = page.locator('#folder[aria-invalid="true"]')
        expect(field).to_have_count(1)
        expect(field).to_have_value("relative/job-data")
        described = field.get_attribute("aria-describedby") or ""
        assert described.split(), "the error is tied to the field"
        expect(page.locator(f"#{described.split()[0]}")).to_be_visible()
        expect(page).to_have_url(f"{running_app.base_url}/setup/welcome")
        assert_no_serious_violations(page)
        fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert fits, f"error state: scrollWidth {scroll_width} > {inner_width}"

    assert not running_app.data_dir.exists(), "a refused folder creates nothing"
    assert load_bootstrap(running_app.config_dir) is None
