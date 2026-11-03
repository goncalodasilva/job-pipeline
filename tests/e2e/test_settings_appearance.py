"""M9.2 part B: Settings > Appearance in a real browser (AC-M9.2; ADR-131 items 6-7; ADR-042
follow-up; TD-085 density only; spec 3.9).

By keyboard only the user reaches Theme and Density on ``/settings/appearance``, picks Dark and
Compact and saves: the live region says "Appearance saved.", focus lands on the section heading,
``<html data-theme data-density>`` change at once (no reload: a marker set on ``window`` survives),
the palette follows the choice whatever the device prefers, a reload and another page keep it, and
the choice is stored. With dark and compact chosen, the Settings areas and a few core pages have no
serious or critical axe finding, do not scroll sideways at desktop size and at 390x844, and
every interactive target is at least 24 by 24 px (compact keeps WCAG 2.5.8; AA contrast is the axe
scan's). The markup is described in ``tests/appearance_pages.py``. The app starts with a finished
setup (``running_app`` default); nothing sleeps: the tests wait on conditions. Every value is
fictional.
"""

import json
import re

import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from tests.appearance_pages import GOOD, HEADING_ID, PATH, SAVED
from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.settings_areas import wait_until_loaded
from tests.e2e.test_board import rows
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, wait_said
from tests.e2e.test_settings_board import tab_to
from tests.e2e.test_shell import (
    assert_target_size,
    body_background,
    has_no_horizontal_scroll,
    new_page,
    open_app,
)
from tests.running_app import RunningApp
from tests.settings_areas import AREAS

pytestmark = pytest.mark.e2e

SECTION = "#appearance-settings"
THEME = f"{SECTION} #appearance-theme"
DENSITY = f"{SECTION} #appearance-density"
VIEWPORTS = {"desktop": DESKTOP, "phone": PHONE}
CORE_PAGES = ("/", "/board", "/inbox", "/facts", "/companies")
MIN_TARGET = 24

# Interactive elements that are visible and smaller than 24 by 24 px. Not counted: the skip link
# and visually hidden text (off screen until focused), and links inside running text (the inline
# exception of WCAG 2.5.8).
SMALL_TARGETS = """() => {
  const selector = 'a[href], button, input:not([type="hidden"]), select, textarea, summary,'
    + ' [role="button"], [role="tab"], [role="menuitem"], [role="switch"]';
  const small = [];
  for (const el of document.querySelectorAll(selector)) {
    if (el.closest('.skip-link, .visually-hidden, [hidden], [inert]')) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    const box = el.getBoundingClientRect();
    if (box.width === 0 || box.height === 0) continue;
    const inline = el.tagName === 'A' && style.display === 'inline'
      && el.closest('p, li, dd, td, th, h1, h2, h3, h4, .field-hint');
    if (inline) continue;
    if (box.width < 24 || box.height < 24) {
      const name = (el.textContent || el.getAttribute('aria-label') || el.name || '').trim();
      small.push(`${el.tagName.toLowerCase()}#${el.id}.${el.className} "${name.slice(0, 30)}" `
        + `${box.width.toFixed(1)}x${box.height.toFixed(1)}`);
    }
  }
  return small;
}"""
ATTRS = """() => {
  const root = document.documentElement;
  return [root.getAttribute('data-theme'), root.getAttribute('data-density')];
}"""


def stored(app: RunningApp) -> list[tuple[str, int]]:
    return rows(app, "SELECT value, version FROM settings WHERE key='appearance'")


def attrs_of(page: Page) -> list[str | None]:
    found: list[str | None] = page.evaluate(ATTRS)
    return found


def expect_attrs(page: Page, theme: str, density: str) -> None:
    html = page.locator("html")
    expect(html).to_have_attribute("data-theme", theme)
    expect(html).to_have_attribute("data-density", density)


def save_button(page: Page) -> Locator:
    return page.locator(SECTION).get_by_role("button", name="Save", exact=True)


def choose_by_keyboard(page: Page, theme: str, density: str) -> None:
    """Tab to Theme, type the option's name, Tab to Density, type, Tab to Save and press Enter.
    Type-ahead works on every platform (an arrow key opens the popup on macOS)."""
    tab_to(page, page.locator(THEME))
    page.keyboard.type(theme)
    expect(page.locator(THEME)).to_have_value(theme.lower())
    tab_to(page, page.locator(DENSITY))
    page.keyboard.type(density)
    expect(page.locator(DENSITY)).to_have_value(density.lower())
    tab_to(page, save_button(page))
    page.keyboard.press("Enter")


def open_appearance(page: Page, app: RunningApp) -> None:
    page.add_init_script(RECORD_ANNOUNCEMENTS)
    open_app(page, app, PATH)
    section = page.locator(SECTION)
    expect(section.locator("#appearance-heading")).to_be_visible()
    expect(section.get_by_label("Theme", exact=True)).to_be_visible()
    expect(section.get_by_label("Density", exact=True)).to_be_visible()


def assert_fits(page: Page, label: str) -> None:
    fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
    assert fits, f"{label}: scrollWidth {scroll_width} > {inner_width}"


def assert_targets_at_least_24(page: Page, label: str) -> None:
    small: list[str] = page.evaluate(SMALL_TARGETS)
    assert small == [], f"{label}: targets under {MIN_TARGET}px in compact:\n" + "\n".join(small)


def wait_until_ready(page: Page, path: str) -> None:
    """An area page waits for its own parts; any page waits for htmx and its requests."""
    for area in AREAS:
        if area.path == path:
            wait_until_loaded(page, area.slug)
            return
    page.wait_for_function("() => typeof window.htmx !== 'undefined'")
    page.wait_for_function("() => !document.querySelector('.htmx-request')")


# --- the keyboard path, announced, applied at once, kept -----------------------------------------


def test_appearance_saved_by_keyboard_applied_at_once_and_kept(
    running_app: RunningApp, browser: Browser
) -> None:
    app = running_app
    with new_page(browser, DESKTOP, color_scheme="light") as page:
        open_appearance(page, app)
        expect_attrs(page, "system", "comfortable")
        expect(page.locator(THEME)).to_have_value("system")
        expect(page.locator(DENSITY)).to_have_value("comfortable")
        before = body_background(page)
        page.evaluate("() => { window.__same_document = true; }")

        choose_by_keyboard(page, "Dark", "Compact")

        # Applied at once, with no reload: the marker on window is still there.
        expect_attrs(page, "dark", "compact")
        assert page.evaluate("() => window.__same_document === true"), "the page was reloaded"
        assert body_background(page) != before, "the dark palette applies whatever the device says"
        wait_said(page, SAVED)
        expect(page.locator("#announcer")).to_have_text(SAVED)
        expect(page.locator(f"#{HEADING_ID}")).to_be_focused()
        expect(page.locator(SECTION).locator("#appearance-done")).to_have_text(SAVED)
        expect(page.locator(THEME)).to_have_value("dark")
        expect(page.locator(DENSITY)).to_have_value("compact")
        assert_no_serious_violations(page)
        [(value, version)] = stored(app)
        assert json.loads(value) == GOOD and version == 1

        # Kept: a reload and another page render the choice from the server.
        page.reload()
        page.wait_for_function("() => typeof window.htmx !== 'undefined'")
        expect_attrs(page, "dark", "compact")
        expect(page.locator(THEME)).to_have_value("dark")
        expect(page.locator(DENSITY)).to_have_value("compact")
        open_app(page, app, "/board")
        expect_attrs(page, "dark", "compact")

        # Back to the defaults by keyboard: the version moves on, the page follows at once.
        open_app(page, app, PATH)
        choose_by_keyboard(page, "System", "Comfortable")
        expect_attrs(page, "system", "comfortable")
        expect(page.locator("#appearance-done")).to_have_text(SAVED)
    [(value, version)] = stored(app)
    assert json.loads(value) == {"theme": "system", "density": "comfortable"} and version == 2


def test_appearance_theme_choice_beats_the_device_and_system_follows_it(
    running_app: RunningApp, browser: Browser
) -> None:
    """Dark on a light device and Light on a dark device both win; System follows the device."""
    app = running_app
    with new_page(browser, DESKTOP, color_scheme="light") as light:
        open_appearance(light, app)
        palette_light = body_background(light)
        with new_page(browser, DESKTOP, color_scheme="dark") as dark:
            open_appearance(dark, app)
            palette_dark = body_background(dark)
            assert palette_light != palette_dark, "control: the two palettes differ"
            expect_attrs(dark, "system", "comfortable")

            choose_by_keyboard(dark, "Light", "Comfortable")
            expect_attrs(dark, "light", "comfortable")
            wait_for_settled_styles(dark)
            assert body_background(dark) == palette_light, "Light on a dark device"

            open_app(light, app, PATH)
            choose_by_keyboard(light, "Dark", "Comfortable")
            expect_attrs(light, "dark", "comfortable")
            wait_for_settled_styles(light)
            assert body_background(light) == palette_dark, "Dark on a light device"

            choose_by_keyboard(light, "System", "Comfortable")
            expect_attrs(light, "system", "comfortable")
            wait_for_settled_styles(light)
            assert body_background(light) == palette_light, "System follows the device"
            # The other page (a dark device) loads the saved System choice as dark again.
            dark.reload()
            dark.wait_for_function("() => typeof window.htmx !== 'undefined'")
            expect_attrs(dark, "system", "comfortable")
            assert body_background(dark) == palette_dark


# --- dark and compact on the areas and the core pages ---------------------------------------------


@pytest.mark.parametrize("viewport_name", list(VIEWPORTS))
def test_appearance_dark_and_compact_axe(
    running_app: RunningApp, browser: Browser, viewport_name: str
) -> None:
    """Dark and compact chosen by keyboard: attributes change without a reload and persist after
    one; then Appearance, every other Settings area and the core pages have no serious or
    critical axe finding, no sideways scroll, and no interactive target under 24 by 24 px."""
    app = running_app
    viewport = VIEWPORTS[viewport_name]
    with new_page(browser, viewport, color_scheme="light") as page:
        open_appearance(page, app)
        page.evaluate("() => { window.__same_document = true; }")
        choose_by_keyboard(page, "Dark", "Compact")
        expect_attrs(page, "dark", "compact")
        assert page.evaluate("() => window.__same_document === true"), "the page was reloaded"
        wait_said(page, SAVED)
        wait_for_settled_styles(page)
        assert_fits(page, "appearance after saving")
        assert_no_serious_violations(page)
        assert_targets_at_least_24(page, f"appearance ({viewport_name})")
        for control in (page.locator(THEME), page.locator(DENSITY), save_button(page)):
            assert_target_size(control, "appearance control")

        page.reload()
        page.wait_for_function("() => typeof window.htmx !== 'undefined'")
        expect_attrs(page, "dark", "compact")
        assert attrs_of(page) == ["dark", "compact"]

        for path in [area.path for area in AREAS] + list(CORE_PAGES):
            open_app(page, app, path)
            wait_until_ready(page, path)
            expect_attrs(page, "dark", "compact")
            wait_for_settled_styles(page)
            assert_fits(page, f"{path} ({viewport_name})")
            assert_no_serious_violations(page)
            assert_targets_at_least_24(page, f"{path} ({viewport_name})")


@pytest.mark.parametrize("viewport_name", list(VIEWPORTS))
def test_appearance_refused_value_keeps_the_page_and_passes_axe(
    running_app: RunningApp, browser: Browser, viewport_name: str
) -> None:
    """A tampered value (only reachable by editing the form) is refused in place: the selects
    keep what is saved, the error is tied to the field and announced, <html> does not change."""
    app = running_app
    with new_page(browser, VIEWPORTS[viewport_name], color_scheme="dark") as page:
        open_appearance(page, app)
        page.evaluate(
            """(selector) => {
              const select = document.querySelector(selector);
              select.add(new Option('neon', 'neon'));
              select.value = 'neon';
            }""",
            THEME,
        )
        save_button(page).click()
        invalid = page.locator(f'{THEME}[aria-invalid="true"]')
        expect(invalid).to_have_count(1)
        expect(invalid).to_be_focused()
        expect(page.locator("#announcer")).to_contain_text(re.compile("not saved", re.IGNORECASE))
        expect_attrs(page, "system", "comfortable")
        assert "neon" not in page.content()
        assert_fits(page, "refused value")
        assert_no_serious_violations(page)
    assert stored(app) == []
