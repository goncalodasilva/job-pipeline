"""Settings > Providers: model per tier in a real browser (M1.22; spec 3.7, 3.9).

The app starts with a completed setup (``running_app`` default: the fake provider saved; fakes
allowed), so the tier selects list the fake's models. By keyboard only the user reaches the
Fast select inside ``#tiers``, changes it, reaches "Save tier models" and presses Enter: the
live region announces it was saved, focus lands inside ``#tiers`` on its status or heading,
the page stays on ``/settings`` and a reload shows the new selection. ``#tiers`` (also with a
refused value's field error) has no serious or critical axe findings at desktop size and at
390x844, in light and dark, and no horizontal scroll on the phone. Every value is fictional.
"""

import re

import pytest
from playwright.sync_api import Browser, Page, expect

from app.modules.ai.providers.fake import FAKE_FAST_MODEL, FAKE_THOROUGH_MODEL
from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_setup_provider import SMALL_PHONE, tab_to
from tests.e2e.test_setup_provider_test import tab_to_locator
from tests.e2e.test_shell import has_no_horizontal_scroll, new_page, open_app
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e

SECTION = "#providers"
TIERS = f"{SECTION} #tiers"
FAST_SELECT = f'{TIERS} select[name="fast"]'
THOROUGH_SELECT = f'{TIERS} select[name="thorough"]'
SAVE_TIERS = "Save tier models"
SAVED = re.compile("saved", re.IGNORECASE)
UNKNOWN_MODEL = "fictional-model-9"
# Focus after a save: inside #tiers, on its heading or a status line, not on a control.
FOCUS_IN_TIERS = """() => {
  const el = document.activeElement;
  if (!el || !el.closest('#tiers')) return false;
  return el.matches('h3, [role="status"], [role="alert"], output, [aria-live]');
}"""


def assert_layout(page: Page, viewport: dict[str, int]) -> None:
    assert_no_serious_violations(page)
    if viewport == PHONE:
        fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert fits, f"scrollWidth {scroll_width} > {inner_width}"


def test_settings_tier_mapping_keyboard(running_app: RunningApp, browser: Browser) -> None:
    settings_url = f"{running_app.base_url}/settings"
    with new_page(browser, DESKTOP) as page:
        open_app(page, running_app, "/settings")
        tiers = page.locator(TIERS)
        expect(tiers.get_by_role("heading", name="Model per tier", level=3)).to_be_visible()
        fast = page.locator(FAST_SELECT)
        expect(fast).to_have_value(FAKE_FAST_MODEL)
        expect(page.locator(THOROUGH_SELECT)).to_have_value(FAKE_THOROUGH_MODEL)
        expect(page.get_by_label("Fast tier model")).to_have_count(1)
        expect(page.get_by_label("Thorough tier model")).to_have_count(1)

        # Keyboard only: Tab to Fast, change it by typing the option's name, Tab to Save,
        # Enter. Type-ahead works on every platform; on macOS ArrowDown opens the popup
        # instead of changing the value.
        tab_to(page, FAST_SELECT)
        page.keyboard.type("Fake thorough")
        expect(fast).to_have_value(FAKE_THOROUGH_MODEL)
        tab_to_locator(page, tiers.get_by_role("button", name=SAVE_TIERS))
        page.keyboard.press("Enter")

        expect(page.locator("#announcer")).to_contain_text(SAVED)
        page.wait_for_function(FOCUS_IN_TIERS)
        expect(page).to_have_url(settings_url)
        expect(page.locator('[aria-current="step"]')).to_have_count(0)
        expect(page.locator(FAST_SELECT)).to_have_value(FAKE_THOROUGH_MODEL)

        page.reload()
        page.wait_for_function("() => typeof window.htmx !== 'undefined'")
        expect(page.locator(FAST_SELECT)).to_have_value(FAKE_THOROUGH_MODEL)
        expect(page.locator(THOROUGH_SELECT)).to_have_value(FAKE_THOROUGH_MODEL)


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "390x844"])
def test_settings_tier_mapping_axe(
    running_app: RunningApp, browser: Browser, scheme: str, viewport: dict[str, int]
) -> None:
    """The tier block, and its field-error state, have no serious axe findings."""
    with new_page(browser, viewport, color_scheme=scheme) as page:
        open_app(page, running_app, "/settings")
        tiers = page.locator(TIERS)
        expect(tiers.get_by_role("heading", name="Model per tier", level=3)).to_be_visible()
        expect(tiers).to_contain_text(re.compile("comes later", re.IGNORECASE))
        assert_layout(page, viewport)

        # The field-error state: a value the provider doesn't offer (only reachable by a
        # tampered form) is refused and not shown back.
        page.evaluate(
            """([selector, value]) => {
              const select = document.querySelector(selector);
              select.add(new Option(value, value));
              select.value = value;
            }""",
            [FAST_SELECT, UNKNOWN_MODEL],
        )
        tiers.get_by_role("button", name=SAVE_TIERS).click()
        invalid = page.locator(f'{FAST_SELECT}[aria-invalid="true"]')
        expect(invalid).to_have_count(1)
        expect(page).to_have_url(f"{running_app.base_url}/settings")
        assert UNKNOWN_MODEL not in page.content()
        assert_layout(page, viewport)


TIERS_FITS = """() => {
  const block = document.querySelector('#tiers');
  return block.scrollWidth <= block.clientWidth;
}"""


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_settings_tier_mapping_fits_360(
    running_app: RunningApp, browser: Browser, scheme: str
) -> None:
    """At 360x740 the tier block, also with a field error, causes no horizontal scroll and
    has no serious axe findings (M1.22 review)."""
    with new_page(browser, SMALL_PHONE, color_scheme=scheme) as page:
        open_app(page, running_app, "/settings")
        tiers = page.locator(TIERS)
        expect(tiers.get_by_role("heading", name="Model per tier", level=3)).to_be_visible()
        fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert fits, f"scrollWidth {scroll_width} > {inner_width}"
        assert page.evaluate(TIERS_FITS), "the tier block fits its own width"
        assert_no_serious_violations(page)

        page.evaluate(
            """([selector, value]) => {
              const select = document.querySelector(selector);
              select.add(new Option(value, value));
              select.value = value;
            }""",
            [THOROUGH_SELECT, UNKNOWN_MODEL],
        )
        tiers.get_by_role("button", name=SAVE_TIERS).click()
        invalid = page.locator(f'{THOROUGH_SELECT}[aria-invalid="true"]')
        expect(invalid).to_have_count(1)
        expect(invalid).to_be_focused()
        assert UNKNOWN_MODEL not in page.content()
        fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert fits, f"scrollWidth {scroll_width} > {inner_width}"
        assert page.evaluate(TIERS_FITS), "the tier block fits its own width"
        assert_no_serious_violations(page)
