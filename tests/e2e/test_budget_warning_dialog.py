"""The budget warning dialog and a blocked action in a real browser (M2.24; AC-M2.7; spec 3.7,
3.9; ADR-076 item 2; ADR-085; detailed design "Settings and usage budgets").

The app starts with a completed setup (the fake provider saved; fakes allowed), so Settings >
Testing tools offers the practice task. Before the page is opened the test writes a budget for
the Fake provider (allowance 40, first warning 10, second 20, blocking point 30, reset day 1)
and fictional usage into the app's temporary database through the app's own ``UsageService``
(ADR-085 item 5), on the real clock and the computer's time zone, as the app does.

- 9 used: starting the practice task opens the warning dialog (``role="dialog"``,
  ``aria-modal="true"``) naming 10, 40 and the reset date. Focus moves into it, onto "Not
  now" (a doubled Enter confirms nothing), and Tab and Shift+Tab stay inside. Esc and "Not
  now" close it, start nothing and put focus back on the Start button; the next start asks
  again. "Continue" by keyboard (reached with Tab) starts the task, and focus lands on the
  status line as after any start. While that start is on its way, Esc and "Not now" do
  nothing. "Change limits" closes the dialog and puts focus on the Usage budgets heading.
- 30 used: the Start button is ``aria-disabled="true"`` and described by "Monthly limit
  reached for Fake provider. Resets on <date>." above it, with a link to the budgets; the
  keyboard and the pointer can't start a task (no request is sent), and each press says the
  reason in the live region.
- The block follows the budgets (M9.2: the budget forms are on Usage budgets, Testing tools on
  Providers and AI, so the two are on different pages): removing the budget on its page
  frees the button when the person comes back to Providers and AI, and lowering the blocking
  point to the count blocks it; focus stays in the budget's block. A page that stays open
  follows a change made in another tab when the body gets ``usage:changed``, keeping the
  chosen Tier. The section is not replaced while its dialog is open.
- Thorough at its cap: the dialog offers "Run once on Fast", which starts one task on Fast
  and leaves the form's Tier on Thorough.
- With the dialog open: no horizontal scroll at 390x844 and 360x740, and no serious or
  critical axe findings at desktop and phone size, in light and dark.

Selectors and copy follow ``tests/integration/test_budget_warning_dialog.py``. No fixed waits;
every value is fictional.

Known limit: the seeding uses the real clock, and the budget resets on day 1. A run that
crosses local midnight into the first day of a month, between the seeding and the start, puts
the seeded requests in the cycle that just ended: the dialog (or the block) would not show
and the test would fail. Run it again.
"""

import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import date

import pytest
from playwright.sync_api import Browser, Locator, Page, Request, Route, expect

from app.domain.budgets import Budget
from app.domain.clock import SystemClock
from app.domain.errors import format_day
from app.modules.storage import Database
from app.services.usage import UsageService
from app.wiring import local_timezone
from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.settings_areas import go_to_area_by_keyboard, wait_until_loaded
from tests.e2e.test_settings_usage_budgets import BLOCK as BUDGET_BLOCK
from tests.e2e.test_settings_usage_budgets import FOCUS_IN_BLOCK, REMOVE, field, tab_to
from tests.e2e.test_setup_provider import MAX_TABS, SMALL_PHONE
from tests.e2e.test_setup_provider_test import is_focused, tab_to_locator
from tests.e2e.test_shell import (
    assert_target_size,
    css_token,
    has_no_horizontal_scroll,
    new_page,
    open_app,
)
from tests.fakes.ids import SequentialIdFactory
from tests.running_app import RunningApp
from tests.waiting import wait_until

pytestmark = pytest.mark.e2e

FAKE = "fake"
SERVICE_NAME = "Fake provider"
ECHO = "fictional_echo"
BUDGET = Budget(allowance=40, first_warning=10, second_warning=20, block_at=30, reset_day=1)
THOROUGH_CAPPED = Budget(
    allowance=40, first_warning=10, second_warning=20, block_at=30, reset_day=1, thorough_cap=3
)
TOOLS = "#testing-tools"
STATUS = "#practice-status"
MSG_STARTED = "Practice task started."
CONTINUE = "Continue"
NOT_NOW = "Not now"
CHANGE_LIMITS = "Change limits"
RUN_ON_FAST = "Run once on Fast"
MAX_DIALOG_TABS = 12
DEADLINE_S = 10.0
ANNOUNCER = "#announcer"
BUDGETS_HEADING = "#usage-budgets-heading"
TOOLS_URL = "/settings/testing-tools"
BUDGETS_PAGE = "/settings/budgets"
BUDGETS_POST = "/settings/usage/budgets"
PRACTICE_URL = re.compile(r"/tasks/practice$")
BUDGETS_CHANGED = (
    "() => document.body.dispatchEvent(new CustomEvent('usage:changed', { bubbles: true }))"
)
# Mark the Testing tools section and count the requests htmx tried to make for it (a request
# for a region that holds an open dialog is cancelled by app.js, so it never reaches the network).
WATCH_TOOLS = """() => {
  document.querySelector('#testing-tools').dataset.mark = 'kept';
  window.__toolsAttempts = 0;
  document.addEventListener('htmx:beforeRequest', (event) => {
    const path = event.detail && event.detail.pathInfo ? event.detail.pathInfo.requestPath : '';
    if (path.includes('/settings/testing-tools')) window.__toolsAttempts += 1;
  });
}"""
TOOLS_MARK_KEPT = "() => document.querySelector('#testing-tools')?.dataset.mark === 'kept'"
DIALOG_OPEN = "() => document.querySelector('[role=\"dialog\"]')?.open === true"

FOCUS_IN_DIALOG = """() => {
  const dialog = document.querySelector('[role="dialog"]');
  const active = document.activeElement;
  return dialog !== null && active !== null && active !== document.body
    && dialog.contains(active);
}"""
FOCUSED_TEXT = "() => (document.activeElement?.textContent || '').trim()"
# The dialog's box against the window: it must sit inside it.
DIALOG_BOX = """() => {
  const box = document.querySelector('[role="dialog"]').getBoundingClientRect();
  return { left: box.left, right: box.right, innerWidth: window.innerWidth };
}"""
# What describes the Start button, as text, and whether each describing element is shown.
DESCRIPTION = """(button) => {
  const ids = (button.getAttribute('aria-describedby') || '').split(/\\s+/).filter(Boolean);
  return ids.map((id) => {
    const element = document.getElementById(id);
    if (element === null) return { text: '', shown: false };
    const box = element.getBoundingClientRect();
    return {
      text: element.textContent.replace(/\\s+/g, ' ').trim(),
      shown: box.width > 0 && box.height > 0
        && getComputedStyle(element).visibility === 'visible',
    };
  });
}"""


# --- helpers -------------------------------------------------------------------------------


def seed(app: RunningApp, budget: Budget, *, fast: int = 0, thorough: int = 0) -> date:
    """A budget for the Fake provider and finished requests this cycle; returns the date the
    cycle resets, as the app works it out (the computer's time zone)."""
    db = Database.open(app.data_dir / "app.db")
    try:
        usage = UsageService(db, SystemClock(), SequentialIdFactory("e2e-budget"), local_timezone())
        usage.save_budget(FAKE, budget, expected_version=usage.load_budgets().version)
        for tier, count in (("fast", fast), ("thorough", thorough)):
            for _ in range(count):
                record_id = usage.reserve(FAKE, tier=tier, task_type=ECHO)
                usage.complete(record_id, success=True, tokens=None)
        status = usage.status(FAKE)
        assert status.local_count == fast + thorough
        return status.cycle.reset_date
    finally:
        db.close()


def practice_inputs(app: RunningApp) -> list[str]:
    """The stored input of every task, read through a read-only connection."""
    uri = f"{(app.data_dir / 'app.db').as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
        rows = conn.execute("SELECT input_ref FROM tasks ORDER BY rowid").fetchall()
    return [str(row[0]) for row in rows]


def says_number(text: str, number: int) -> bool:
    return re.search(rf"(?<![\d.,]){number}(?![\d])", text) is not None


def start_button(page: Page) -> Locator:
    return page.locator(TOOLS).get_by_role("button", name="Start practice task")


def dialog(page: Page) -> Locator:
    return page.get_by_role("dialog")


def dialog_button(page: Page, name: str) -> Locator:
    return dialog(page).get_by_role("button", name=name, exact=True)


def open_settings(page: Page, app: RunningApp) -> None:
    open_app(page, app, "/settings")
    expect(start_button(page)).to_be_visible()


def start_by_keyboard(page: Page) -> None:
    """Tab to the Start button (unless it has focus) and press Enter."""
    if not is_focused(start_button(page)):
        tab_to_locator(page, start_button(page))
    page.keyboard.press("Enter")


def expect_dialog(page: Page, *numbers: int, resets_on: date) -> Locator:
    """The open dialog, with focus inside it."""
    shown = dialog(page)
    expect(shown).to_be_visible()
    expect(shown).to_have_count(1)
    expect(shown).to_have_attribute("aria-modal", "true")
    text = " ".join(shown.inner_text().split())
    assert SERVICE_NAME in text, text
    assert format_day(resets_on) in text, text
    for number in numbers:
        assert says_number(text, number), f"{number} in: {text}"
    page.wait_for_function(FOCUS_IN_DIALOG)
    # It opens with focus on "Not now": an Enter meant for the action confirms nothing.
    expect(dialog_button(page, NOT_NOW)).to_be_focused()
    return shown


def tab_in_dialog_to(page: Page, target: Locator) -> None:
    """Tab, never leaving the dialog, until ``target`` has focus."""
    for _ in range(MAX_DIALOG_TABS):
        if is_focused(target):
            return
        page.keyboard.press("Tab")
        assert page.evaluate(FOCUS_IN_DIALOG), "Tab left the dialog"
    raise AssertionError(f"{target} never received focus inside the dialog")


def assert_focus_trapped(page: Page, key: str) -> set[str]:
    """Press ``key`` more often than the dialog has controls: focus never leaves it. Returns
    the text of every control that was reached."""
    reached: set[str] = set()
    for _ in range(MAX_DIALOG_TABS):
        page.keyboard.press(key)
        assert page.evaluate(FOCUS_IN_DIALOG), f"{key} moved focus out of the dialog"
        reached.add(str(page.evaluate(FOCUSED_TEXT)))
    return reached


def assert_closed_without_a_start(page: Page, app: RunningApp, posts: list[Request]) -> None:
    """The dialog is gone, focus is back on the Start button and nothing was started."""
    expect(dialog(page)).to_be_hidden()
    expect(start_button(page)).to_be_focused()
    for request in posts:  # whatever was sent has been answered
        assert request.response() is not None
    expect(page.locator(STATUS)).to_have_count(0)
    assert practice_inputs(app) == []


def record_posts(page: Page) -> list[Request]:
    posts: list[Request] = []
    page.on("request", lambda request: posts.append(request) if request.method == "POST" else None)
    return posts


def record_refreshes(page: Page) -> list[str]:
    """The URL of every request for the Testing tools section alone."""
    urls: list[str] = []
    page.on(
        "request", lambda request: urls.append(request.url) if TOOLS_URL in request.url else None
    )
    return urls


def record_navigations(page: Page) -> list[str]:
    urls: list[str] = []
    page.on("framenavigated", lambda frame: urls.append(frame.url))
    return urls


def shift_tab_to_locator(page: Page, target: Locator) -> None:
    """Press Shift+Tab until ``target`` has focus (keyboard only, no clicks)."""
    for _ in range(MAX_TABS):
        page.keyboard.press("Shift+Tab")
        if is_focused(target):
            return
    raise AssertionError(f"{target} never received focus within {MAX_TABS} Shift+Tab presses")


def press_blocked(page: Page, press: Callable[[], None], message: str, what: str) -> None:
    """``press`` on a blocked action says the reason in the live region (emptied first, so
    each press is checked on its own)."""
    page.evaluate("(selector) => { document.querySelector(selector).textContent = ''; }", ANNOUNCER)
    press()
    expect(page.locator(ANNOUNCER), what).to_have_text(message)


# --- the dialog by keyboard ----------------------------------------------------------------


def test_warning_dialog_keyboard_continue_and_esc_not_now(
    running_app: RunningApp, browser: Browser
) -> None:
    resets_on = seed(running_app, BUDGET, fast=9)
    with new_page(browser, DESKTOP) as page:
        open_settings(page, running_app)
        posts = record_posts(page)

        start_by_keyboard(page)
        expect_dialog(page, 10, 40, resets_on=resets_on)
        expect(dialog_button(page, CONTINUE)).to_be_visible()
        expect(dialog_button(page, NOT_NOW)).to_be_visible()
        limits = dialog(page).get_by_role("link", name=CHANGE_LIMITS, exact=True)
        expect(limits).to_have_attribute("href", re.compile(r"#usage-budgets$"))
        assert practice_inputs(running_app) == [], "showing the dialog starts nothing"

        # The focus trap, both ways: every control is reached and focus never leaves.
        forward = assert_focus_trapped(page, "Tab")
        assert {CONTINUE, NOT_NOW, CHANGE_LIMITS} <= forward, forward
        backward = assert_focus_trapped(page, "Shift+Tab")
        assert {CONTINUE, NOT_NOW, CHANGE_LIMITS} <= backward, backward

        # Esc is "Not now".
        page.keyboard.press("Escape")
        assert_closed_without_a_start(page, running_app, posts)

        # Not confirmed, so the next start asks again; Continue by keyboard starts the task.
        # Focus starts on "Not now", so Continue is reached with Tab.
        start_by_keyboard(page)
        expect_dialog(page, 10, 40, resets_on=resets_on)
        assert not is_focused(dialog_button(page, CONTINUE))
        tab_in_dialog_to(page, dialog_button(page, CONTINUE))
        page.keyboard.press("Enter")

        expect(page.locator(STATUS)).to_have_text(MSG_STARTED)
        expect(page.locator(STATUS)).to_be_focused()
        expect(dialog(page)).to_be_hidden()
        expect(page).to_have_url(f"{running_app.base_url}/settings")
        expect(page.get_by_text("Something went wrong")).to_have_count(0)
        wait_until(
            lambda: len(practice_inputs(running_app)) == 1,
            timeout=DEADLINE_S,
            message="one practice task was enqueued",
        )
        assert practice_inputs(running_app) == ["quick/ok/fast"]


def test_focus_returns_to_action_after_dialog(running_app: RunningApp, browser: Browser) -> None:
    """Closed with "Not now" or with Esc, the dialog hands focus back to the Start button,
    which works again from there."""
    resets_on = seed(running_app, BUDGET, fast=9)
    with new_page(browser, DESKTOP) as page:
        open_settings(page, running_app)
        posts = record_posts(page)

        start_by_keyboard(page)
        expect_dialog(page, 10, 40, resets_on=resets_on)
        # Enter again, as a doubled key press would: focus is on "Not now", nothing starts.
        page.keyboard.press("Enter")
        assert_closed_without_a_start(page, running_app, posts)

        page.keyboard.press("Enter")  # focus is on the Start button
        expect_dialog(page, 10, 40, resets_on=resets_on)
        page.keyboard.press("Escape")
        assert_closed_without_a_start(page, running_app, posts)

        page.keyboard.press("Enter")
        expect_dialog(page, 10, 40, resets_on=resets_on)


def test_thorough_cap_run_once_on_fast_by_keyboard(
    running_app: RunningApp, browser: Browser
) -> None:
    resets_on = seed(running_app, THOROUGH_CAPPED, thorough=3)
    with new_page(browser, DESKTOP) as page:
        open_settings(page, running_app)
        tier = page.locator(f'{TOOLS} select[name="tier"]')
        tab_to_locator(page, tier)
        page.keyboard.type("Thorough")
        expect(tier).to_have_value("thorough")

        start_by_keyboard(page)
        shown = expect_dialog(page, resets_on=resets_on)
        expect(shown).to_contain_text("Thorough")
        expect(dialog_button(page, NOT_NOW)).to_be_visible()
        expect(dialog_button(page, CONTINUE)).to_have_count(0)
        assert practice_inputs(running_app) == []

        tab_in_dialog_to(page, dialog_button(page, RUN_ON_FAST))
        page.keyboard.press("Enter")

        expect(page.locator(STATUS)).to_have_text(MSG_STARTED)
        expect(page.locator(STATUS)).to_be_focused()
        expect(dialog(page)).to_be_hidden()
        wait_until(
            lambda: len(practice_inputs(running_app)) == 1,
            timeout=DEADLINE_S,
            message="one practice task was enqueued",
        )
        assert practice_inputs(running_app) == ["quick/ok/fast"], "this run is on Fast"
        # This run only: the form still shows the tier the user chose.
        expect(page.locator(f'{TOOLS} select[name="tier"]')).to_have_value("thorough")


# --- the blocked action --------------------------------------------------------------------


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])
def test_blocked_button_explains_reset_date(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int]
) -> None:
    resets_on = seed(running_app, BUDGET, fast=30)
    message = f"Monthly limit reached for {SERVICE_NAME}. Resets on {format_day(resets_on)}."
    with new_page(browser, viewport) as page:
        open_settings(page, running_app)
        posts = record_posts(page)
        start = start_button(page)

        # aria-disabled, not disabled: the button stays reachable, so its reason is read.
        assert start.get_attribute("aria-disabled") == "true", "the button is disabled"
        assert start.get_attribute("disabled") is None
        described = start.evaluate(DESCRIPTION)
        assert any(message in part["text"] and part["shown"] for part in described), described
        expect(page.locator(TOOLS).get_by_text(message)).to_be_visible()
        link = page.locator(f'{TOOLS} a[href$="#usage-budgets"]')
        expect(link).to_be_visible()
        assert_target_size(link, "the link to the budgets")
        fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert fits, f"scrollWidth {scroll_width} > {inner_width}"

        # The keyboard can't start a task: Enter in a field, Enter and Space on the button.
        # Nothing is sent (the round trip that follows would have seen a request).
        duration = page.locator(f'{TOOLS} select[name="duration"]')
        tab_to_locator(page, duration)
        page.keyboard.press("Enter")
        tab_to_locator(page, start)
        page.keyboard.press("Enter")
        page.keyboard.press("Space")
        expect(start).to_be_focused()
        assert [request.url for request in posts] == [], "a blocked action sends nothing"

        # Each press of the button says the reason, and a click does (forced: to Playwright
        # the button is not enabled). Still nothing is sent.
        press_blocked(page, lambda: page.keyboard.press("Enter"), message, "Enter on the button")
        press_blocked(page, lambda: page.keyboard.press("Space"), message, "Space on the button")
        press_blocked(page, lambda: start.click(force=True), message, "a click on the button")
        expect(start).to_be_focused()
        assert [request.url for request in posts] == [], "a blocked action sends nothing"
        expect(page.locator(STATUS)).to_have_count(0)
        expect(dialog(page)).to_have_count(0)
        expect(page.locator(TOOLS).get_by_text(message)).to_be_visible()
        assert practice_inputs(running_app) == []

        # The link to the budgets is on their own page here: focus goes to their heading.
        tab_to_locator(page, link)
        page.keyboard.press("Enter")
        expect(page.locator(BUDGETS_HEADING)).to_be_focused()
        expect(page.locator(BUDGETS_HEADING)).to_be_in_viewport()
        expect(page).to_have_url(f"{running_app.base_url}/settings/budgets#usage-budgets")


def change_budget_by_keyboard(page: Page, change: str) -> None:
    """On Usage budgets, remove the Fake provider's budget (``remove``) or save its blocking
    point as 25 (``lower``), by keyboard; focus stays in the budget's block."""
    if change == "remove":
        tab_to(page, REMOVE)
    else:
        tab_to(page, field("block_at"))
        page.keyboard.press("ControlOrMeta+a")
        page.keyboard.type("25")
    with page.expect_response(
        lambda r: r.url.endswith(BUDGETS_POST) and r.request.method == "POST"
    ) as saved:
        page.keyboard.press("Enter")
    assert saved.value.status == 200, saved.value.status
    page.wait_for_function(FOCUS_IN_BLOCK, arg=BUDGET_BLOCK)


def budget_changed_in_another_tab(page: Page, app: RunningApp, change: str) -> Page:
    """A second tab of the same browser changes the budget on its own page."""
    other = page.context.new_page()
    open_app(other, app, BUDGETS_PAGE)
    wait_until_loaded(other, "budgets")
    change_budget_by_keyboard(other, change)
    return other


def test_ac_m2_10_removing_the_budget_on_its_page_frees_the_blocked_button(
    running_app: RunningApp, browser: Browser
) -> None:
    """Blocked at 30 of 30 on Providers and AI. The budget is removed by keyboard on Usage
    budgets (M2.10; the two are on different pages since M9.2, so there is no "without a
    reload" any more): focus stays in the budget's block, and back on Providers and AI by the
    side list the Start button is free, with no reason or link, and starts a task on the chosen
    Tier."""
    resets_on = seed(running_app, BUDGET, fast=30)
    message = f"Monthly limit reached for {SERVICE_NAME}. Resets on {format_day(resets_on)}."
    with new_page(browser, DESKTOP) as page:
        open_settings(page, running_app)
        expect(start_button(page)).to_have_attribute("aria-disabled", "true")
        expect(page.locator(TOOLS).get_by_text(message)).to_be_visible()

        go_to_area_by_keyboard(page, running_app, "providers", "budgets")
        change_budget_by_keyboard(page, "remove")
        go_to_area_by_keyboard(page, running_app, "budgets", "providers")

        start = start_button(page)
        expect(start).to_be_visible()
        expect(start).not_to_have_attribute("aria-disabled", "true")
        expect(start).not_to_have_attribute("aria-describedby", re.compile(".+"))
        expect(page.locator(TOOLS).get_by_text(message)).to_have_count(0)
        expect(page.locator(f'{TOOLS} a[href$="#usage-budgets"]')).to_have_count(0)
        assert practice_inputs(running_app) == []

        tier = page.locator(f'{TOOLS} select[name="tier"]')
        tab_to_locator(page, tier)
        page.keyboard.type("Thorough")
        expect(tier).to_have_value("thorough")
        tab_to_locator(page, start)
        page.keyboard.press("Enter")

        expect(page.locator(STATUS)).to_have_text(MSG_STARTED)
        expect(dialog(page)).to_have_count(0)
        wait_until(
            lambda: len(practice_inputs(running_app)) == 1,
            timeout=DEADLINE_S,
            message="one practice task was enqueued",
        )
        assert practice_inputs(running_app) == ["quick/ok/thorough"]


def test_lowering_the_blocking_point_on_its_page_blocks_the_button(
    running_app: RunningApp, browser: Browser
) -> None:
    """25 used and the app stops at 30. The blocking point is saved as 25 by keyboard on Usage
    budgets (focus stays in the budget's block); back on Providers and AI by the side list the
    Start button is blocked, with its reason and a link to the budgets, and sends nothing."""
    resets_on = seed(running_app, BUDGET, fast=25)
    message = f"Monthly limit reached for {SERVICE_NAME}. Resets on {format_day(resets_on)}."
    with new_page(browser, DESKTOP) as page:
        open_settings(page, running_app)
        posts = record_posts(page)
        expect(start_button(page)).not_to_have_attribute("aria-disabled", "true")
        expect(page.locator(TOOLS).get_by_text(message)).to_have_count(0)

        go_to_area_by_keyboard(page, running_app, "providers", "budgets")
        change_budget_by_keyboard(page, "lower")
        go_to_area_by_keyboard(page, running_app, "budgets", "providers")

        start = start_button(page)
        expect(start).to_have_attribute("aria-disabled", "true")
        described = start.evaluate(DESCRIPTION)
        assert any(message in part["text"] and part["shown"] for part in described), described
        expect(page.locator(f'{TOOLS} a[href$="#usage-budgets"]')).to_be_visible()

        saves = len(posts)
        tab_to_locator(page, start)
        press_blocked(page, lambda: page.keyboard.press("Enter"), message, "Enter on the button")
        assert len(posts) == saves, "the blocked action sends nothing"
        assert practice_inputs(running_app) == []


@pytest.mark.parametrize("change", ["remove", "lower"])
def test_open_testing_tools_follow_a_budget_changed_in_another_tab(
    running_app: RunningApp, browser: Browser, change: str
) -> None:
    """The Providers and AI page that stays open follows the budget when the body gets
    ``usage:changed`` (what a budget save sends on its own page): the Start button is freed
    (``remove``, blocked at 30 of 30) or blocked (``lower``, 25 used), with the reason, the
    link and the chosen Tier kept, and the page is not reloaded."""
    removed = change == "remove"
    resets_on = seed(running_app, BUDGET, fast=30 if removed else 25)
    message = f"Monthly limit reached for {SERVICE_NAME}. Resets on {format_day(resets_on)}."
    with new_page(browser, DESKTOP) as page:
        open_settings(page, running_app)
        navigations = record_navigations(page)
        start = start_button(page)
        if removed:
            expect(start).to_have_attribute("aria-disabled", "true")
        tier = page.locator(f'{TOOLS} select[name="tier"]')
        tab_to_locator(page, tier)
        page.keyboard.type("Thorough")
        expect(tier).to_have_value("thorough")

        other = budget_changed_in_another_tab(page, running_app, change)
        with page.expect_response(lambda r: TOOLS_URL in r.url) as refetched:
            page.evaluate(BUDGETS_CHANGED)
        assert refetched.value.status == 200, refetched.value.status

        if removed:
            expect(start).not_to_have_attribute("aria-disabled", "true")
            expect(start).not_to_have_attribute("aria-describedby", re.compile(".+"))
            expect(page.locator(TOOLS).get_by_text(message)).to_have_count(0)
            expect(page.locator(f'{TOOLS} a[href$="#usage-budgets"]')).to_have_count(0)
        else:
            expect(start).to_have_attribute("aria-disabled", "true")
            described = start.evaluate(DESCRIPTION)
            assert any(message in part["text"] and part["shown"] for part in described), described
            expect(page.locator(f'{TOOLS} a[href$="#usage-budgets"]')).to_be_visible()
        expect(page.locator(TOOLS)).to_have_count(1)
        expect(tier).to_have_value("thorough")
        assert practice_inputs(running_app) == []
        assert navigations == [], f"the page was reloaded or left: {navigations}"
        other.close()


def test_testing_tools_is_not_refreshed_while_its_dialog_is_open(
    running_app: RunningApp, browser: Browser
) -> None:
    """A budget change while the warning is on screen: the Testing tools section is not
    replaced (the dialog would vanish) and nothing is fetched for it. Once the dialog is
    closed, the same event refreshes the section."""
    resets_on = seed(running_app, BUDGET, fast=9)
    with new_page(browser, DESKTOP) as page:
        open_settings(page, running_app)
        refreshes = record_refreshes(page)
        start_by_keyboard(page)
        expect_dialog(page, 10, 40, resets_on=resets_on)
        page.evaluate(WATCH_TOOLS)  # the start's answer (the dialog) replaced the section

        # htmx tried to refresh the section (so the event was handled) and was stopped.
        page.evaluate(BUDGETS_CHANGED)
        page.wait_for_function("() => window.__toolsAttempts >= 1")
        assert refreshes == [], "the section was fetched while its dialog was open"
        assert page.evaluate(TOOLS_MARK_KEPT), "the section was replaced while its dialog was open"
        expect_dialog(page, 10, 40, resets_on=resets_on)

        page.keyboard.press("Escape")
        expect(dialog(page)).to_be_hidden()
        expect(start_button(page)).to_be_focused()
        with page.expect_response(lambda r: TOOLS_URL in r.url) as refetched:
            page.evaluate(BUDGETS_CHANGED)
        assert refetched.value.status == 200, refetched.value.status
        assert len(refreshes) == 1, refreshes
        assert not page.evaluate(TOOLS_MARK_KEPT), "the section was replaced once it was closed"
        # Focus was on the Start button when the section was replaced: it is on the new one.
        expect(start_button(page)).to_be_focused()
        assert practice_inputs(running_app) == []


def test_esc_and_not_now_do_nothing_while_continue_is_on_its_way(
    running_app: RunningApp, browser: Browser
) -> None:
    """The confirmed start is held on its way to the app: Esc and "Not now" leave the dialog
    open, so the user isn't told nothing started while the task is being queued."""
    resets_on = seed(running_app, BUDGET, fast=9)
    with new_page(browser, DESKTOP) as page:
        open_settings(page, running_app)
        start_by_keyboard(page)
        expect_dialog(page, 10, 40, resets_on=resets_on)
        held: list[Route] = []
        page.route(PRACTICE_URL, lambda route: held.append(route))

        tab_in_dialog_to(page, dialog_button(page, CONTINUE))
        with page.expect_request(PRACTICE_URL):
            page.keyboard.press("Enter")
        expect(dialog(page).locator("form.htmx-request")).to_have_count(1)
        assert len(held) == 1, "Continue was sent and is held"

        page.keyboard.press("Escape")
        assert page.evaluate(DIALOG_OPEN), "Esc closed the dialog while the start was on its way"
        page.keyboard.press("Escape")
        assert page.evaluate(DIALOG_OPEN), "a second Esc closed the dialog"
        dialog_button(page, NOT_NOW).click()
        assert page.evaluate(DIALOG_OPEN), '"Not now" closed the dialog'
        expect(dialog(page)).to_be_visible()
        assert practice_inputs(running_app) == []

        held[0].continue_()

        expect(page.locator(STATUS)).to_have_text(MSG_STARTED)
        expect(page.locator(STATUS)).to_be_focused()
        expect(dialog(page)).to_be_hidden()
        wait_until(
            lambda: len(practice_inputs(running_app)) == 1,
            timeout=DEADLINE_S,
            message="one practice task was enqueued",
        )
        assert practice_inputs(running_app) == ["quick/ok/fast"]


def test_change_limits_on_settings_moves_focus_to_the_budgets_heading(
    running_app: RunningApp, browser: Browser
) -> None:
    """The dialog's link goes to a section of the page it is on: the dialog closes, nothing
    starts, and focus is on that section's heading instead of nowhere."""
    resets_on = seed(running_app, BUDGET, fast=9)
    with new_page(browser, DESKTOP) as page:
        open_settings(page, running_app)
        start_by_keyboard(page)
        expect_dialog(page, 10, 40, resets_on=resets_on)

        limits = dialog(page).get_by_role("link", name=CHANGE_LIMITS, exact=True)
        tab_in_dialog_to(page, limits)
        page.keyboard.press("Enter")

        expect(dialog(page)).to_be_hidden()
        expect(page.locator(BUDGETS_HEADING)).to_be_focused()
        expect(page.locator(BUDGETS_HEADING)).to_be_in_viewport()
        expect(page).to_have_url(f"{running_app.base_url}/settings/budgets#usage-budgets")
        expect(page.locator(STATUS)).to_have_count(0)
        assert practice_inputs(running_app) == []


# --- narrow screens and axe, with the dialog open ------------------------------------------


@pytest.mark.parametrize("viewport", [PHONE, SMALL_PHONE], ids=["390x844", "360x740"])
def test_warning_dialog_has_no_horizontal_scroll_on_phones(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int]
) -> None:
    assert viewport["width"] in (390, 360)
    resets_on = seed(running_app, BUDGET, fast=9)
    with new_page(browser, viewport) as page:
        open_settings(page, running_app)
        start_button(page).click()
        expect_dialog(page, 10, 40, resets_on=resets_on)

        fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert fits, f"scrollWidth {scroll_width} > {inner_width}"
        box = page.evaluate(DIALOG_BOX)
        assert box["left"] >= -0.5 and box["right"] <= box["innerWidth"] + 0.5, box
        controls = {
            CONTINUE: dialog_button(page, CONTINUE),
            NOT_NOW: dialog_button(page, NOT_NOW),
            CHANGE_LIMITS: dialog(page).get_by_role("link", name=CHANGE_LIMITS, exact=True),
        }
        for name, locator in controls.items():
            expect(locator).to_be_in_viewport()
            assert_target_size(locator, name)

        page.keyboard.press("Escape")
        expect(dialog(page)).to_be_hidden()
        fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert fits, f"after closing: scrollWidth {scroll_width} > {inner_width}"
        assert practice_inputs(running_app) == []


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])
def test_axe_warning_dialog_open(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int], scheme: str
) -> None:
    resets_on = seed(running_app, BUDGET, fast=9)
    with new_page(browser, viewport, color_scheme=scheme) as page:
        open_settings(page, running_app)
        start_button(page).click()
        expect_dialog(page, 10, 40, resets_on=resets_on)
        assert css_token(page, "--color-bg"), "the colour tokens are not loaded"
        assert_no_serious_violations(page)


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])
def test_axe_blocked_action(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int], scheme: str
) -> None:
    resets_on = seed(running_app, BUDGET, fast=30)
    message = f"Monthly limit reached for {SERVICE_NAME}. Resets on {format_day(resets_on)}."
    with new_page(browser, viewport, color_scheme=scheme) as page:
        open_settings(page, running_app)
        expect(page.locator(TOOLS).get_by_text(message)).to_be_visible()
        assert css_token(page, "--color-bg"), "the colour tokens are not loaded"
        assert_no_serious_violations(page)
