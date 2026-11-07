"""M9.5 part A: the command palette in a real browser (AC-M9.5; ADR-098 item 7; ADR-131 item 10;
detailed design "Cross-cutting UX" and its accessibility specifics): Ctrl/Cmd+K opens a modal
dialog from any page; typing searches; the arrow keys and Enter open a result or run an action;
Tab stays inside; Esc closes and focus goes back; the single-key switch does not touch it; it
works at 390x844 and passes axe in light and dark.

The contract (ids, data attributes, routes, copy) is in ``tests/palette_pages.py``; the fragment
behaviour is in ``tests/integration/test_palette_routes.py``. Part B (G then T/I/B/F, N, ?) has
its own tests later. Every value is fictional; no network; no fixed waits.
"""

from datetime import UTC, datetime

import pytest
from playwright.sync_api import Locator, Page, expect

from app.modules.storage import Database
from app.services.applications import ApplicationService, ApplicationStatuses
from app.services.facts import FactService
from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_facts_page import press_by_keyboard, tab_until
from tests.e2e.test_shell import (
    assert_target_size,
    has_no_horizontal_scroll,
    new_page,
    open_app,
)
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory
from tests.palette_pages import (
    ACTIONS,
    INPUT,
    OWN_COMPANY,
    RESULTS,
    WORD,
    Seeded,
    item_path,
    seed_palette,
)
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e

OPEN_KEY = "ControlOrMeta+k"
OPEN_BUTTON = "[data-palette-open]"
RESULT = "[data-palette-result]"
ACTION = "[data-palette-action]"
RECENT = "[data-palette-recent]"
EMPTY = "[data-palette-empty]"
SELECTED = '[aria-selected="true"]'
DIALOG_NAME = "Command palette"
PAGES = ("/", "/board", "/inbox", "/facts", "/companies", "/settings")
MAX_PRESSES = 30
FOCUS_IN_DIALOG = """() => {
  const dialog = document.querySelector('#palette');
  const active = document.activeElement;
  return dialog !== null && active !== null && active !== document.body
    && dialog.contains(active);
}"""
IS_MODAL = "() => document.querySelector('#palette').matches(':modal')"
DIALOG_FITS = """() => {
  const box = document.querySelector('#palette').getBoundingClientRect();
  return { left: box.left, right: box.right, top: box.top, bottom: box.bottom,
           width: window.innerWidth, height: window.innerHeight };
}"""
MARK_ACTIVE = "() => document.activeElement.setAttribute('data-test-opener', 'yes')"
OPENER_FOCUSED = "() => document.activeElement.getAttribute('data-test-opener') === 'yes'"


def seed(app: RunningApp) -> Seeded:
    """The fictional palette rows, through the app's own services on the app's database."""
    clock = FrozenClock(datetime(2026, 11, 2, 9, 0, tzinfo=UTC))
    with Database.open(app.data_dir / "app.db") as db:
        return seed_palette(
            db,
            ApplicationService(lambda: db, clock, SequentialIdFactory("e2eapp")),
            ApplicationStatuses(lambda: db, clock, SequentialIdFactory("e2estatus")),
            FactService(db, clock, SequentialIdFactory("e2efact")),
        )


def dialog_of(page: Page) -> Locator:
    return page.get_by_role("dialog", name=DIALOG_NAME)


def open_palette(page: Page) -> Locator:
    """Ctrl/Cmd+K, then the dialog is open and modal and the search field has focus."""
    page.keyboard.press(OPEN_KEY)
    dialog = dialog_of(page)
    expect(dialog).to_be_visible()
    assert page.evaluate(IS_MODAL) is True
    expect(page.locator(INPUT)).to_be_focused()
    return dialog


def type_query(page: Page, text: str) -> None:
    page.keyboard.type(text)
    expect(page.locator(INPUT)).to_have_value(text)


def arrow_to(page: Page, target: Locator) -> None:
    """ArrowDown until ``target`` is the active item (``aria-selected="true"``)."""
    expect(target).to_have_count(1)
    for _ in range(MAX_PRESSES):
        if target.get_attribute("aria-selected") == "true":
            return
        page.keyboard.press("ArrowDown")
    raise AssertionError("the arrow keys never reached the item")


def test_palette_ctrl_k_search_and_open_by_keyboard(running_app, browser):
    seeded = seed(running_app)
    with new_page(browser, DESKTOP) as page:
        # From Today: the empty state lists the four actions.
        open_app(page, running_app, "/")
        dialog = open_palette(page)
        expect(dialog.locator(ACTION)).to_have_count(len(ACTIONS))
        for label in ACTIONS.values():
            expect(dialog.locator(ACTION, has_text=label)).to_have_count(1)
        expect(dialog.locator(RESULT)).to_have_count(0)

        # Typing searches jobs, applications and facts, grouped; the count is announced.
        type_query(page, WORD)
        results = dialog.locator(f"{RESULTS} {RESULT}")
        expect(results).to_have_count(4)
        expect(dialog.locator('[data-kind="job"]')).to_have_count(2)
        expect(dialog.locator('[data-kind="application"]')).to_have_count(1)
        expect(dialog.locator('[data-kind="fact"]')).to_have_count(1)
        expect(dialog.locator("[data-palette-status]")).to_contain_text("4")

        # Arrow keys move one active item at a time; ArrowUp goes back.
        last = results.last
        arrow_to(page, last)
        expect(dialog.locator(f"{RESULT}{SELECTED}")).to_have_count(1)
        page.keyboard.press("ArrowUp")
        expect(last).not_to_have_attribute("aria-selected", "true")
        expect(dialog.locator(f"{RESULT}{SELECTED}")).to_have_count(1)

        # Enter opens the active result's page (the job), and the dialog is gone there.
        target = dialog.locator(f'{RESULT}[href="{item_path("job", seeded.rigger_job)}"]')
        arrow_to(page, target)
        page.keyboard.press("Enter")
        page.wait_for_url(f"**{item_path('job', seeded.rigger_job)}")
        expect(dialog_of(page)).not_to_be_visible()

        # From the Board: companies are found by name, and Enter opens the company page.
        open_app(page, running_app, "/board")
        open_palette(page)
        type_query(page, "lantern")
        company = page.locator(f'{RESULT}[data-kind="company"]')
        expect(company).to_have_count(1)
        expect(company).to_contain_text(OWN_COMPANY)
        arrow_to(page, company)
        page.keyboard.press("Enter")
        page.wait_for_url(f"**{item_path('company', seeded.own_company)}")

        # An action runs by keyboard too: "session" finds Start session.
        open_palette(page)
        type_query(page, "session")
        action = page.locator(f'{ACTION}[data-action="start-session"]')
        expect(action).to_have_count(1)
        expect(page.locator(RESULT)).to_have_count(0)
        arrow_to(page, action)
        page.keyboard.press("Enter")
        page.wait_for_url("**/sessions/new")

        # Opens from every page, also from a screen with its own keys and with a field focused.
        for path in PAGES:
            open_app(page, running_app, path)
            open_palette(page)
            page.keyboard.press("Escape")
            expect(dialog_of(page)).not_to_be_visible()


def test_palette_lists_recent_items_after_visits(running_app, browser):
    seeded = seed(running_app)
    with new_page(browser, DESKTOP) as page:
        open_app(page, running_app, item_path("job", seeded.wrangler_job))
        open_app(page, running_app, item_path("company", seeded.own_company))
        open_app(page, running_app, "/board")
        dialog = open_palette(page)
        recent = dialog.locator(RECENT)
        expect(recent).to_have_count(2)
        # Newest first; titles come from the server.
        expect(recent.nth(0)).to_contain_text(OWN_COMPANY)
        expect(recent.nth(0)).to_have_attribute("href", item_path("company", seeded.own_company))
        expect(recent.nth(1)).to_contain_text("Quokka Wrangler")
        arrow_to(page, recent.nth(1))
        page.keyboard.press("Enter")
        page.wait_for_url(f"**{item_path('job', seeded.wrangler_job)}")


def test_palette_focus_trap_and_return_on_esc(running_app, browser):
    seed(running_app)
    with new_page(browser, DESKTOP) as page:
        open_app(page, running_app, "/board")
        # Focus something by keyboard and mark it, so its return can be checked.
        page.keyboard.press("Tab")
        page.keyboard.press("Tab")
        page.evaluate(MARK_ACTIVE)
        dialog = open_palette(page)
        expect(dialog.locator(ACTION)).to_have_count(len(ACTIONS))

        # Tab and Shift+Tab cycle inside the dialog, however often they are pressed.
        seen: set[str] = set()
        for _ in range(MAX_PRESSES):
            page.keyboard.press("Tab")
            assert page.evaluate(FOCUS_IN_DIALOG) is True
            seen.add(page.evaluate("() => document.activeElement.outerHTML.slice(0, 80)"))
        assert len(seen) >= 2  # it really moved (the input, the items and the Close button)
        for _ in range(MAX_PRESSES):
            page.keyboard.press("Shift+Tab")
            assert page.evaluate(FOCUS_IN_DIALOG) is True

        # Esc closes (also with results showing) and focus returns to the marked element.
        type_query(page, WORD)
        expect(page.locator(RESULT)).to_have_count(4)
        page.keyboard.press("Escape")
        expect(dialog).not_to_be_visible()
        assert page.evaluate(OPENER_FOCUSED) is True

        # The Close button closes it too, by keyboard, and returns focus the same way.
        open_palette(page)
        close = dialog.get_by_role("button", name="Close", exact=True)
        tab_until(page, close)
        page.keyboard.press("Enter")
        expect(dialog).not_to_be_visible()
        assert page.evaluate(OPENER_FOCUSED) is True

        # It can be opened again, empty, and the page behind did not move.
        open_palette(page)
        expect(page.locator(INPUT)).to_have_value("")


def test_single_key_shortcuts_off_keeps_ctrl_k(running_app, browser):
    seed(running_app)
    with new_page(browser, DESKTOP) as page:
        open_app(page, running_app, "/settings/keyboard")
        toggle = page.get_by_label("Use single-key shortcuts", exact=True)
        tab_until(page, toggle)
        page.keyboard.press("Space")
        section = page.locator("#shortcuts-settings")
        press_by_keyboard(page, section.get_by_role("button", name="Save", exact=True))
        expect(section).to_contain_text("Saved.")

        # The switch is off on the inbox, and Ctrl/Cmd+K still opens the palette there: from
        # the page, and with the focus inside a text field.
        open_app(page, running_app, "/inbox")
        assert page.locator("#inbox-page").get_attribute("data-shortcuts") == "false"
        dialog = open_palette(page)
        page.keyboard.press("Escape")
        expect(dialog).not_to_be_visible()
        press_by_keyboard(page, page.get_by_role("button", name="+ Add job", exact=True))
        field = page.locator("#job-text")
        tab_until(page, field)
        open_palette(page)
        type_query(page, WORD)
        expect(page.locator(RESULT)).to_have_count(4)
        page.keyboard.press("Escape")
        expect(dialog).not_to_be_visible()
        expect(field).to_be_focused()
        expect(field).to_have_value("")

        # And from the Board.
        open_app(page, running_app, "/board")
        open_palette(page)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_palette_phone_390x844_and_axe(running_app, browser, scheme):
    seed(running_app)
    with new_page(browser, PHONE, color_scheme=scheme) as page:
        open_app(page, running_app, "/board")
        assert_no_serious_violations(page)

        # A phone has no Ctrl+K: the top bar has a visible button, reached by keyboard.
        opener = page.locator(OPEN_BUTTON)
        expect(opener).to_have_count(1)
        expect(opener).to_be_visible()
        assert_target_size(opener, "the top bar's palette button")
        sidebar_toggle = page.locator("button.sidebar-toggle")
        tab_until(page, opener)
        page.keyboard.press("Enter")
        dialog = dialog_of(page)
        expect(dialog).to_be_visible()
        assert page.evaluate(IS_MODAL) is True
        expect(page.locator(INPUT)).to_be_focused()
        expect(sidebar_toggle).to_be_visible()  # the page is still there, behind the dialog

        # Empty state: the actions fit, are tall enough to touch and leave nothing sideways.
        expect(dialog.locator(ACTION)).to_have_count(len(ACTIONS))
        fits: dict[str, float] = page.evaluate(DIALOG_FITS)
        assert fits["left"] >= 0 and fits["right"] <= fits["width"], fits
        assert fits["top"] >= 0 and fits["bottom"] <= fits["height"], fits
        for index in range(len(ACTIONS)):
            assert_target_size(dialog.locator(ACTION).nth(index), f"action {index}")
        wait_for_settled_styles(page)
        assert_no_serious_violations(page)
        assert has_no_horizontal_scroll(page)[0]

        # Results: grouped, readable, still inside the screen.
        type_query(page, WORD)
        expect(dialog.locator(RESULT)).to_have_count(4)
        for index in range(4):
            assert_target_size(dialog.locator(RESULT).nth(index), f"result {index}")
        fits = page.evaluate(DIALOG_FITS)
        assert fits["left"] >= 0 and fits["right"] <= fits["width"], fits
        assert fits["top"] >= 0 and fits["bottom"] <= fits["height"], fits
        wait_for_settled_styles(page)
        assert_no_serious_violations(page)
        assert has_no_horizontal_scroll(page)[0]

        # Nothing found: an empty state with words, also clean.
        page.keyboard.press("ControlOrMeta+a")
        type_query(page, "zzzxqwv")
        expect(dialog.locator(RESULT)).to_have_count(0)
        expect(dialog.locator(EMPTY)).to_be_visible()
        assert dialog.locator(EMPTY).inner_text().strip() != ""
        wait_for_settled_styles(page)
        assert_no_serious_violations(page)

        # Esc closes it and the button gets the focus back.
        page.keyboard.press("Escape")
        expect(dialog).not_to_be_visible()
        expect(opener).to_be_focused()
        assert has_no_horizontal_scroll(page)[0]


BAR_WIDTHS = """() => Array.from(document.querySelectorAll('#palette-results [role=option]'))
  .map(option => [option.getAttribute('aria-selected'),
                  parseFloat(getComputedStyle(option).borderLeftWidth)])"""


def test_palette_active_option_shows_in_forced_colors(running_app, browser):
    """Forced colors (Windows High Contrast) replace the fill: only the active option keeps a
    bar, in the system highlight color (UX review of M9.5)."""
    seed(running_app)
    with new_page(browser, DESKTOP) as page:
        page.emulate_media(forced_colors="active")
        open_app(page, running_app, "/board")
        open_palette(page)
        expect(page.locator(ACTION)).to_have_count(len(ACTIONS))
        widths = page.evaluate(BAR_WIDTHS)
        assert [width for selected, width in widths if selected == "true"] == [4.0], widths
        assert all(width == 0 for selected, width in widths if selected != "true"), widths
