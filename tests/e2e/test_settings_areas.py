"""M9.2 part A: the Settings area pages in a real browser (AC-M9.2; ADR-131 item 7; TD-308; spec
3.9): a tab budget per area, switching areas by keyboard through the side list, and every area at
390x844 and desktop size with an axe scan in light and dark.

The areas, the paths and the markup these tests rely on are in ``tests/settings_areas.py``; the
caps are in ``tests/e2e/settings_areas.py`` (``AREA_TAB_BUDGET``). The app starts with a finished
setup (fake provider saved, fakes allowed). Tests wait on conditions; nothing sleeps. Every value
is fictional.
"""

import re

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.settings_areas import (
    ALL_LABELS,
    AREA_TAB_BUDGET,
    NAV,
    assert_area_within_budget,
    current_has_non_colour_cue,
    nav_link,
    tab_to_link,
    wait_until_loaded,
)
from tests.e2e.test_shell import assert_target_size, has_no_horizontal_scroll, new_page, open_app
from tests.running_app import RunningApp
from tests.settings_areas import AREAS, BY_SLUG, SLUGS, SettingsArea

pytestmark = pytest.mark.e2e

VIEWPORTS = {"desktop": DESKTOP, "phone": PHONE}
SIDEBAR_SETTINGS = 'nav[aria-label="Main"] a[aria-current="page"]'


def expect_area_shown(page: Page, app: RunningApp, area: SettingsArea) -> None:
    expect(page).to_have_url(f"{app.base_url}{area.path}")
    wanted = re.compile(
        rf"^({re.escape(area.label)}|Settings)$"
        if area.slug == "providers"
        else rf"^{re.escape(area.label)}$"
    )
    expect(page.get_by_role("heading", level=1, name=wanted)).to_be_visible()
    expect(page.locator(f'{NAV} [aria-current="page"]')).to_have_count(1)
    expect(nav_link(page, area.label)).to_have_attribute("aria-current", "page")


# --- a tab budget per area (closes TD-308) --------------------------------------------------------


@pytest.mark.parametrize("slug", SLUGS)
def test_settings_area_tab_counts_within_budget(
    running_app: RunningApp, browser: Browser, slug: str
) -> None:
    """Walking an area from the top of the page takes no more Tab presses than its budget, on a
    desktop and on a 390x844 phone (where the sidebar is collapsed), once the area has loaded."""
    area = BY_SLUG[slug]
    assert slug in AREA_TAB_BUDGET, "every area has a budget"
    for name, viewport in VIEWPORTS.items():
        with new_page(browser, viewport) as page:
            open_app(page, running_app, area.path)
            wait_until_loaded(page, slug)
            walk = assert_area_within_budget(page, slug, name)
            assert walk.in_nav >= len(AREAS), (
                f"{slug} ({name}): the side list's {len(AREAS)} links are Tab stops "
                f"(reached {walk.in_nav})"
            )


# --- switching areas by keyboard ----------------------------------------------------------------


@pytest.mark.parametrize("viewport_name", list(VIEWPORTS))
def test_settings_areas_are_reached_one_after_another_by_keyboard(
    running_app: RunningApp, browser: Browser, viewport_name: str
) -> None:
    """From Providers and AI, Tab to each area's link in the side list and press Enter: the
    page, its heading and the current link change; the last Enter comes back to Providers and
    AI. The main sidebar keeps Settings current on a desktop. No pointer is used."""
    with new_page(browser, VIEWPORTS[viewport_name]) as page:
        open_app(page, running_app, AREAS[0].path)
        wait_until_loaded(page, AREAS[0].slug)
        expect_area_shown(page, running_app, AREAS[0])
        here = AREAS[0]
        for target in (*AREAS[1:], AREAS[0]):
            link = nav_link(page, target.label)
            tab_to_link(page, link, AREA_TAB_BUDGET[here.slug])
            page.keyboard.press("Enter")
            expect_area_shown(page, running_app, target)
            wait_until_loaded(page, target.slug)
            if viewport_name == "desktop":
                expect(page.locator(SIDEBAR_SETTINGS)).to_have_text("Settings")
            here = target
        assert here.slug == "providers"


# --- phone and accessibility ---------------------------------------------------------------------


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("viewport_name", list(VIEWPORTS))
def test_settings_areas_fit_and_pass_axe_light_and_dark(
    running_app: RunningApp, browser: Browser, viewport_name: str, theme: str
) -> None:
    """Every area page has no serious or critical axe finding in light and dark at desktop size
    and at 390x844, does not scroll sideways, and its side list is visible with targets of at
    least 24 by 24 px and a cue other than colour for the current area."""
    viewport = VIEWPORTS[viewport_name]
    assert PHONE == {"width": 390, "height": 844}
    with new_page(browser, viewport, color_scheme=theme) as page:
        for area in AREAS:
            open_app(page, running_app, area.path)
            wait_until_loaded(page, area.slug)
            wait_for_settled_styles(page)
            fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
            assert fits, f"{area.slug}: scrollWidth {scroll_width} > {inner_width}"
            for label in ALL_LABELS:
                link = nav_link(page, label)
                expect(link).to_be_visible()
                assert_target_size(link, f"{area.slug}: side list link {label}")
            assert current_has_non_colour_cue(page), (
                f"{area.slug}: current area shown by colour only"
            )
            assert_no_serious_violations(page)
