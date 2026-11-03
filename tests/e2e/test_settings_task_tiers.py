"""M9.2 part C: the per-task tier list in a real browser (AC-M9.2; ADR-089 item 9; ADR-069 item 6;
ADR-125 item 1; spec 3.9).

The list is a collapsed native ``<details id="task-tiers">`` in Providers and AI (``/settings``),
so it is one Tab stop and the Providers tab budget stays 70 (``tests/e2e/settings_areas.py``). By
keyboard only the user opens it, moves one task to the other tier by typing the tier's name, saves,
hears "Task tiers saved." and finds the choice after a reload. At 390x844 it does not scroll
sideways and, in light and dark, the section also passes axe, with its error state. The markup is
described in ``tests/integration/test_settings_task_tiers.py``. The app starts with a finished setup
(the fake provider saved); nothing sleeps. Every value is fictional.
"""

import json
import re

import pytest
from playwright.sync_api import Browser, Page, expect

from app.modules.ai.tasks import TASK_SPECS
from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.settings_areas import AREA_TAB_BUDGET, count_tab_stops, wait_until_loaded
from tests.e2e.test_board import rows
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, wait_said
from tests.e2e.test_settings_board import tab_to
from tests.e2e.test_shell import has_no_horizontal_scroll, new_page, open_app
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e

BOX = "#task-tiers"
SUMMARY = "#task-tiers-summary"
SELECT = f'{BOX} select[name="tier_summarize_job"]'
OTHER_SELECT = f'{BOX} select[name="tier_tailor_cv"]'
SAVE = "Save task tiers"
SAVED = "Task tiers saved."
UNKNOWN = "medium"
FITS = """(selector) => {
  const box = document.querySelector(selector);
  return box.scrollWidth <= box.clientWidth;
}"""


def stored_overrides(app: RunningApp) -> dict[str, str]:
    [(value,)] = rows(app, "SELECT value FROM settings WHERE key='ai_provider'")
    found: dict[str, str] = json.loads(value)["task_tiers"]
    return found


def open_providers(page: Page, app: RunningApp) -> None:
    page.add_init_script(RECORD_ANNOUNCEMENTS)
    open_app(page, app, "/settings")
    wait_until_loaded(page, "providers")


def open_by_keyboard(page: Page) -> None:
    """Tab to the disclosure's summary and open it with Enter (the list is one stop until then)."""
    expect(page.locator(BOX)).not_to_have_attribute("open", "")
    tab_to(page, page.locator(SUMMARY))
    page.keyboard.press("Enter")
    expect(page.locator(BOX)).to_have_attribute("open", "")
    expect(page.locator(SELECT)).to_be_visible()


def assert_fits(page: Page) -> None:
    fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
    assert fits, f"scrollWidth {scroll_width} > {inner_width}"
    assert page.evaluate(FITS, BOX), "the task tier list fits its own width"


def test_task_tier_change_by_keyboard_announced(running_app: RunningApp, browser: Browser) -> None:
    app = running_app
    with new_page(browser, DESKTOP) as page:
        open_providers(page, app)
        expect(page.locator(SELECT)).to_be_hidden()  # collapsed: not reachable yet
        open_by_keyboard(page)
        select = page.locator(SELECT)
        expect(select).to_have_value("fast")  # summarize_job's default
        expect(page.locator(OTHER_SELECT)).to_have_value("thorough")  # tailor_cv's default

        # Keyboard only: Tab to the select, type the tier's name, Tab to Save, Enter.
        tab_to(page, select)
        page.keyboard.type("Thorough")
        expect(select).to_have_value("thorough")
        tab_to(page, page.locator(BOX).get_by_role("button", name=SAVE, exact=True))
        page.keyboard.press("Enter")

        wait_said(page, SAVED)
        expect(page.locator("#announcer")).to_have_text(SAVED)
        expect(page.locator(BOX)).to_have_attribute("open", "")
        expect(page.locator("#task-tiers-done")).to_have_text(SAVED)
        expect(page.locator(SUMMARY)).to_be_focused()
        expect(page).to_have_url(f"{app.base_url}/settings")
        expect(page.locator(SELECT)).to_have_value("thorough")
        assert stored_overrides(app) == {"summarize_job": "thorough"}
        assert_no_serious_violations(page)

        # A reload shows the saved tier; a row equal to its default stores nothing.
        page.reload()
        wait_until_loaded(page, "providers")
        open_by_keyboard(page)
        expect(page.locator(SELECT)).to_have_value("thorough")
        expect(page.locator(OTHER_SELECT)).to_have_value("thorough")
        tab_to(page, page.locator(SELECT))
        page.keyboard.type("Fast")
        expect(page.locator(SELECT)).to_have_value("fast")
        tab_to(page, page.locator(BOX).get_by_role("button", name=SAVE, exact=True))
        page.keyboard.press("Enter")
        wait_said(page, SAVED)
        assert stored_overrides(app) == {}


def test_task_tier_list_is_one_tab_stop_while_collapsed(
    running_app: RunningApp, browser: Browser
) -> None:
    """Collapsed, the list adds only its summary to the walk of the page, so Providers and AI
    stays inside its budget (70, not raised for part C)."""
    with new_page(browser, DESKTOP) as page:
        open_providers(page, running_app)
        expect(page.locator(BOX)).not_to_have_attribute("open", "")
        walk = count_tab_stops(page, AREA_TAB_BUDGET["providers"])
        assert not walk.limit_hit and walk.stops <= AREA_TAB_BUDGET["providers"], walk
        names: list[str] = []
        page.evaluate("() => document.activeElement?.blur()")
        for _ in range(walk.stops):
            page.keyboard.press("Tab")
            names.append(
                page.evaluate(
                    "() => (document.activeElement.name || document.activeElement.id || '')"
                )
            )
        assert names.count("task-tiers-summary") == 1, "the summary is a stop"
        assert not [name for name in names if name.startswith("tier_")], names


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "390x844"])
def test_task_tier_list_fits_and_passes_axe(
    running_app: RunningApp, browser: Browser, scheme: str, viewport: dict[str, int]
) -> None:
    """Open, the list has no serious or critical axe finding in light and dark, does not scroll
    sideways at 390x844, and the refused-tier state (a tampered form) is clean as well."""
    with new_page(browser, viewport, color_scheme=scheme) as page:
        open_providers(page, running_app)
        page.locator(SUMMARY).click()
        expect(page.locator(SELECT)).to_be_visible()
        expect(page.locator(f"{BOX} select")).to_have_count(len(TASK_SPECS))
        wait_for_settled_styles(page)
        assert_fits(page)
        assert_no_serious_violations(page)

        page.evaluate(
            """([selector, value]) => {
              const select = document.querySelector(selector);
              select.add(new Option(value, value));
              select.value = value;
            }""",
            [OTHER_SELECT, UNKNOWN],
        )
        page.locator(BOX).get_by_role("button", name=SAVE, exact=True).click()
        invalid = page.locator(f'{OTHER_SELECT}[aria-invalid="true"]')
        expect(invalid).to_have_count(1)
        expect(invalid).to_be_focused()
        expect(page.locator("#announcer")).to_contain_text(re.compile("not saved", re.IGNORECASE))
        expect(page.locator(BOX)).to_have_attribute("open", "")
        # The section, not the whole document: the axe scan above injects axe-core, whose colour
        # table holds words such as "mediumblue" in a <script> on this very page.
        assert UNKNOWN not in page.locator("#providers").inner_html()
        assert_fits(page)
        wait_for_settled_styles(page)
        assert_no_serious_violations(page)
    assert stored_overrides(running_app) == {}, "a refused save stores nothing"
