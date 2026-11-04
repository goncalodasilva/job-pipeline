"""M9.2 fix pass, UX items (spec 3.9; htmx-ui skill): links to a Settings section land keyboard
focus on its heading, the saved line of Appearance and the tier list has room above it, and the
preferences summary arrives by its protected fragment. A real browser; every value is fictional;
nothing sleeps (the tests wait on conditions).
"""

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.settings_areas import wait_until_loaded
from tests.e2e.test_shell import new_page, open_app
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e

VIEWPORTS = {"desktop": DESKTOP, "phone": PHONE}
MIN_ABOVE_PX = 12  # 0.75rem at the default density: more than the 8 px every line had


@pytest.mark.parametrize("viewport_name", list(VIEWPORTS))
@pytest.mark.parametrize(
    ("address", "heading"),
    [
        ("/settings#providers", "#providers-heading"),
        ("/settings/keyboard#shortcuts-settings", "#shortcuts-heading"),
        ("/settings/your-details#details-settings", "#details-heading"),
        ("/settings/budgets#usage-budgets", "#usage-budgets-heading"),
    ],
)
def test_a_link_to_a_settings_section_focuses_its_heading(
    running_app: RunningApp, browser: Browser, address: str, heading: str, viewport_name: str
) -> None:
    """Arriving at ``<area>#<section>`` (the links in the inbox help, the practice task and the
    company brief) puts focus on the section's heading, not on the page heading. Your details
    arrives by a lazy fragment: the loaded section is marked too, and focus is on its heading
    once it is there."""
    with new_page(browser, VIEWPORTS[viewport_name]) as page:
        open_app(page, running_app, address)
        if "your-details" in address:
            expect(page.locator("#details-summary")).to_be_attached()  # the lazy load swapped
        expect(page.locator(heading)).to_be_focused()
        expect(page.locator(heading)).to_be_in_viewport()
        assert page.evaluate("() => location.hash") == "#" + address.split("#")[1]


def margin_above(page: Page, selector: str) -> float:
    value: float = page.evaluate(
        "(s) => parseFloat(getComputedStyle(document.querySelector(s)).marginTop)", selector
    )
    return value


def test_saved_lines_of_appearance_and_task_tiers_have_room_above_them(
    running_app: RunningApp, browser: Browser
) -> None:
    with new_page(browser, DESKTOP) as page:
        open_app(page, running_app, "/settings/appearance")
        section = page.locator("#appearance-settings")
        expect(section.get_by_label("Theme", exact=True)).to_be_visible()
        section.get_by_role("button", name="Save", exact=True).click()
        expect(page.locator("#appearance-done")).to_be_visible()
        assert margin_above(page, "#appearance-done") >= MIN_ABOVE_PX

        open_app(page, running_app, "/settings")
        wait_until_loaded(page, "providers")
        page.locator("#task-tiers-summary").click()
        page.locator("#task-tiers").get_by_role(
            "button", name="Save task tiers", exact=True
        ).click()
        expect(page.locator("#task-tiers-done")).to_be_visible()
        assert margin_above(page, "#task-tiers-done") >= MIN_ABOVE_PX


def test_preferences_summary_is_loaded_by_the_protected_fragment(
    running_app: RunningApp, browser: Browser
) -> None:
    """The page shell is replaced by the summary section (or the empty line), keeps the guide
    link and takes no focus."""
    with new_page(browser, DESKTOP) as page:
        open_app(page, running_app, "/settings/preferences")
        wait_until_loaded(page, "preferences")
        expect(page.locator("#preferences-settings")).to_have_count(1)
        expect(page.locator("#preferences-settings[hx-get]")).to_have_count(0)
        expect(page.locator("#preferences-guide-link")).to_have_attribute(
            "href", "/setup/preferences"
        )
        # Loading takes no focus: nothing inside the section holds it.
        expect(page.locator("#preferences-settings :focus")).to_have_count(0)
