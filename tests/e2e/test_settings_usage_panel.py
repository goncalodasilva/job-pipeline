"""Settings > the usage panel in a real browser (M2.23; AC-M2.6, AC-M2.8; spec 3.7, 3.9).

The app starts with a completed setup (the fake provider saved; fakes allowed). Before the
page is opened the test writes fictional usage into the app's temporary data folder through
the app's own ``UsageService`` (the real clock, because the app runs on it): a budget of 20
requests for the Fake provider, five requests counted here (three Fast, two Thorough, one of
them never finished), a provider-reported count of 7 for the month, and a Claude plan
utilization of 25%. No practice task can produce a provider count or leave a call unfinished
for good, so the rows are written directly; the browser only reads.

The panel (``#usage-panel``) then shows, in text, "7 / 20", "First warning reached", that
one call didn't finish and is counted, both counts and that the provider's is used. At 360 px
and 390 px the page has no horizontal scroll, while the breakdown's container
(``#usage-fake-breakdown``, a focusable ``role="region"``) scrolls sideways inside itself: it
has the scroll styling, it scrolls whenever its table is wider than it, and it still does
(with the page still not scrolling) when the table is forced much wider. The container is
reached with Tab, shows a focus outline and scrolls with the arrow keys. No serious or
critical axe findings at desktop and phone size, in light and dark.

The panel also follows the tasks: a practice task started from Settings > Testing tools (the
fake provider, no network) raises the Fake provider's count without a reload, and a refresh
keeps focus on a breakdown region that has it.

Selectors follow ``tests/integration/test_settings_usage_panel.py``. No fixed waits.
"""

from datetime import UTC

import pytest
from playwright.sync_api import Browser, Page, expect

from app.domain.budgets import Budget
from app.domain.clock import SystemClock
from app.domain.usage import is_task_type
from app.modules.ai import ProviderUsageSnapshot, TokenUsage
from app.modules.storage import Database
from app.services.practice import PRACTICE_STEPS
from app.services.usage import UsageService
from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_settings_usage_budgets import FOCUS_IN_BLOCK, REMOVE, tab_to
from tests.e2e.test_setup_provider import SMALL_PHONE
from tests.e2e.test_shell import css_token, has_no_horizontal_scroll, new_page, open_app
from tests.fakes.ids import SequentialIdFactory
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e

PANEL = "#usage-panel"
BLOCK = "#usage-fake"
COUNT = "#usage-fake-count"
STATUS = "#usage-fake-status"
UNFINISHED = "#usage-fake-unfinished"
LOCAL = "#usage-fake-local"
PROVIDER = "#usage-fake-provider"
USED = "#usage-fake-used"
REGION = "#usage-fake-breakdown"
TABLE = f"{REGION} table"
PLAN_INFO = "#usage-claude_subscription-info"
PRACTICE_FORM = 'form[action="/tasks/practice"]'
PRACTICE_STATUS = "#practice-status"
PANEL_URL = "/settings/usage"
# Mark the panel that is on the page, so a later check can tell it was swapped for a new one.
MARK_PANEL = "(s) => { document.querySelector(s).dataset.before = 'true'; }"
PANEL_REPLACED = """(s) => {
  const panel = document.querySelector(s);
  return panel !== null && panel.dataset.before === undefined;
}"""
# What the event stream's reader does for each task change (htmx-ui skill, Live updates).
TASK_CHANGED = """() => {
  document.body.dispatchEvent(new CustomEvent('tasks:updated', { bubbles: true }));
}"""

FAKE = "fake"
BUDGET = Budget(allowance=20, first_warning=5, second_warning=10, block_at=20, reset_day=1)
ECHO = "fictional_echo"
# As long as a task type can be (64 characters): the widest row header the table can get.
LONG_TASK = "fictional_long_task_type_name_for_the_narrow_phone_layout_check_"
TOKENS = TokenUsage(input_tokens=120, output_tokens=45)

# The container's scroll state, and whether it really moves sideways (restored afterwards).
SCROLL_STATE = """(selector) => {
  const box = document.querySelector(selector);
  const table = box.querySelector('table');
  const before = box.scrollLeft;
  box.scrollLeft = 100000;
  const moved = box.scrollLeft;
  box.scrollLeft = before;
  return {
    overflowX: getComputedStyle(box).overflowX,
    clientWidth: box.clientWidth,
    scrollWidth: box.scrollWidth,
    tableWidth: table.offsetWidth,
    right: box.getBoundingClientRect().right,
    left: box.getBoundingClientRect().left,
    innerWidth: window.innerWidth,
    moved,
  };
}"""
# Make the table wider than any phone, whatever its content.
WIDEN = """(selector) => {
  const box = document.querySelector(selector);
  box.querySelector('table').style.minWidth = (box.clientWidth + 400) + 'px';
}"""


# The text of ``aria-labelledby`` (or ``aria-label``): the region's accessible name.
ACCESSIBLE_NAME = """(selector) => {
  const box = document.querySelector(selector);
  const ids = (box.getAttribute('aria-labelledby') || '').split(/\\s+/).filter(Boolean);
  const named = ids.map((id) => document.getElementById(id)?.textContent || '').join(' ');
  return (named.trim() || (box.getAttribute('aria-label') || '').trim());
}"""


# --- helpers -------------------------------------------------------------------------------


def seed_usage(app: RunningApp) -> None:
    """Fictional usage in the running app's temporary data folder (see the module text)."""
    assert is_task_type(LONG_TASK) and len(LONG_TASK) == 64
    clock = SystemClock()
    db = Database.open(app.data_dir / "app.db")
    try:
        usage = UsageService(db, clock, SequentialIdFactory("e2e-usage"), UTC)
        usage.save_budget(FAKE, BUDGET, expected_version=usage.load_budgets().version)
        done = [usage.reserve(FAKE, tier="fast", task_type=ECHO) for _ in range(3)]
        done.append(usage.reserve(FAKE, tier="thorough", task_type=LONG_TASK))
        for record_id in done:
            usage.complete(record_id, success=True, tokens=TOKENS)
        usage.reserve(FAKE, tier="thorough", task_type=LONG_TASK)  # never completed
        reported = ProviderUsageSnapshot(
            service=FAKE,
            window="month",
            unit="requests",
            used=7,
            limit=None,
            utilization=None,
            resets_at=None,
            observed_at=clock.now(),
        )
        assert usage.save_snapshot(reported)
        plan = ProviderUsageSnapshot(
            service="claude_subscription",
            window="five_hour",
            unit="fraction",
            used=None,
            limit=None,
            utilization=0.25,
            resets_at=None,
            observed_at=clock.now(),
        )
        assert usage.save_snapshot(plan)
        status = usage.status(FAKE)
        assert (status.local_count, status.provider_count, status.unfinished_count) == (5, 7, 1)
    finally:
        db.close()


def open_panel(page: Page, app: RunningApp) -> None:
    open_app(page, app, "/settings")
    expect(page.locator(PANEL)).to_be_visible()
    expect(page.locator(COUNT)).to_contain_text("7 / 20")
    expect(page.locator(TABLE)).to_be_visible()


def scroll_state(page: Page) -> dict[str, float | str]:
    state: dict[str, float | str] = page.evaluate(SCROLL_STATE, REGION)
    return state


def assert_page_does_not_scroll(page: Page, what: str) -> None:
    fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
    assert fits, f"{what}: the page scrolls sideways ({scroll_width} > {inner_width})"


def assert_table_scrolls_in_its_container(page: Page, what: str) -> None:
    """The container stays inside the page, has the scroll styling, and whenever its table
    is wider than it the container itself scrolls sideways."""
    assert_page_does_not_scroll(page, what)
    state = scroll_state(page)
    assert state["overflowX"] in ("auto", "scroll"), f"{what}: {state}"
    assert float(state["left"]) >= -0.5, f"{what}: {state}"
    assert float(state["right"]) <= float(state["innerWidth"]) + 0.5, f"{what}: {state}"
    if float(state["tableWidth"]) > float(state["clientWidth"]):
        assert float(state["scrollWidth"]) > float(state["clientWidth"]), f"{what}: {state}"
        assert float(state["moved"]) > 0, f"{what}: the container doesn't scroll: {state}"


def widen_table(page: Page) -> None:
    page.evaluate(WIDEN, REGION)
    state = scroll_state(page)
    assert float(state["tableWidth"]) > float(state["clientWidth"]), state


# --- what the panel says -------------------------------------------------------------------------


def test_ac_m2_6_and_m2_8_panel_says_the_counts_in_text(
    running_app: RunningApp, browser: Browser
) -> None:
    """With a budget, usage, an unfinished call and a higher provider count: everything is
    readable as text, also after a reload."""
    seed_usage(running_app)
    with new_page(browser, DESKTOP) as page:
        open_panel(page, running_app)
        for _ in range(2):
            expect(page.locator(BLOCK)).to_contain_text("Fake provider")
            expect(page.locator(COUNT)).to_contain_text("7 / 20")
            expect(page.locator(STATUS)).to_contain_text("First warning reached")
            expect(page.locator(UNFINISHED)).to_contain_text("1 call ")
            expect(page.locator(UNFINISHED)).to_contain_text("is counted")
            expect(page.locator(LOCAL)).to_contain_text("5")
            expect(page.locator(PROVIDER)).to_contain_text("7")
            expect(page.locator(USED)).to_contain_text("The provider's count is used")
            expect(page.locator(PLAN_INFO)).to_contain_text("25%")
            for part in (COUNT, STATUS, UNFINISHED, LOCAL, PROVIDER, USED, PLAN_INFO):
                expect(page.locator(part)).to_be_visible()
            total = (
                page.locator(TABLE)
                .get_by_role("row")
                .filter(has=page.get_by_role("rowheader", name="Total", exact=True))
            )
            expect(total.get_by_role("cell").nth(2)).to_have_text("5")
            expect(page.locator(TABLE).get_by_role("columnheader", name="Fast")).to_be_visible()
            page.reload()
            page.wait_for_function("() => typeof window.htmx !== 'undefined'")


def test_panel_follows_a_removed_budget_without_a_reload(
    running_app: RunningApp, browser: Browser
) -> None:
    """Removing the Fake provider's budget by keyboard: the panel fetches itself again and
    shows the count without an allowance, status or reset date, while focus stays in the
    budget's own block, where the form's answer put it."""
    seed_usage(running_app)
    with new_page(browser, DESKTOP) as page:
        open_panel(page, running_app)
        expect(page.locator(STATUS)).to_be_visible()

        tab_to(page, REMOVE)
        with page.expect_response(lambda r: r.url.endswith("/settings/usage")) as refetched:
            page.keyboard.press("Enter")
        assert refetched.value.status == 200, refetched.value.status

        expect(page.locator(COUNT)).to_have_text("7 requests")
        expect(page.locator(STATUS)).to_have_count(0)
        expect(page.locator("#usage-fake-reset")).to_have_count(0)
        expect(page.locator(BLOCK)).to_contain_text("No limit")
        expect(page.locator(USED)).to_contain_text("The provider's count is used")
        expect(page.locator(PANEL)).to_have_count(1)
        page.wait_for_function(FOCUS_IN_BLOCK, arg="#budget-fake")
        expect(page).to_have_url(f"{running_app.base_url}/settings")


def test_panel_follows_a_practice_task_without_a_reload(
    running_app: RunningApp, browser: Browser
) -> None:
    """A practice task started by keyboard from Settings > Testing tools makes one call to
    the fake provider per step: the panel fetches itself again as the task changes and shows
    the requests counted here, in the count lines and in the table, with no reload. The
    refresh is silent: focus stays on the practice form's answer."""
    after = 5 + PRACTICE_STEPS  # more than the provider's 7: the local count is used again
    assert after > 7
    seed_usage(running_app)
    with new_page(browser, DESKTOP) as page:
        open_panel(page, running_app)
        expect(page.locator(LOCAL)).to_have_text("Counted on this computer: 5")
        page.evaluate(MARK_PANEL, PANEL)
        navigations: list[str] = []
        page.on("framenavigated", lambda frame: navigations.append(frame.url))

        tab_to(page, f'{PRACTICE_FORM} button[type="submit"]')
        page.keyboard.press("Enter")
        # The seeded count (7 reported) is past the first warning (5): the start asks first
        # (M2.24), and Continue by keyboard starts the task.
        proceed = page.get_by_role("dialog").get_by_role("button", name="Continue", exact=True)
        proceed.focus()
        page.keyboard.press("Enter")
        expect(page.locator(PRACTICE_STATUS)).to_have_text("Practice task started.")

        expect(page.locator(LOCAL)).to_have_text(f"Counted on this computer: {after}")
        page.wait_for_function(PANEL_REPLACED, arg=PANEL)
        total = (
            page.locator(TABLE)
            .get_by_role("row")
            .filter(has=page.get_by_role("rowheader", name="Total", exact=True))
        )
        expect(total.get_by_role("cell").nth(2)).to_have_text(str(after))
        expect(page.locator(COUNT)).to_have_text(f"{after} / 20 requests")
        expect(page.locator(USED)).to_contain_text("The count on this computer is used")
        expect(page.locator(PANEL)).to_have_count(1)
        expect(page.locator(PRACTICE_STATUS)).to_be_focused()
        assert navigations == [], f"the page was reloaded or left: {navigations}"
        expect(page).to_have_url(f"{running_app.base_url}/settings")


def test_panel_refresh_keeps_focus_in_the_breakdown_region(
    running_app: RunningApp, browser: Browser
) -> None:
    """Focus is on the Fake provider's breakdown region when a task changes: the panel is
    swapped for a new one and focus is on the new region, not lost to the page."""
    seed_usage(running_app)
    with new_page(browser, DESKTOP) as page:
        open_panel(page, running_app)
        tab_to(page, REGION)
        page.evaluate(MARK_PANEL, PANEL)

        with page.expect_response(lambda r: r.url.endswith(PANEL_URL)) as refetched:
            page.evaluate(TASK_CHANGED)
        assert refetched.value.status == 200, refetched.value.status
        page.wait_for_function(PANEL_REPLACED, arg=PANEL)

        expect(page.locator(REGION)).to_be_focused()
        expect(page.locator(REGION)).to_have_count(1)
        outline = page.evaluate("() => getComputedStyle(document.activeElement).outlineStyle")
        assert outline != "none", "the focused region shows no focus outline"


# --- phone widths --------------------------------------------------------------------------------


@pytest.mark.parametrize("viewport", [SMALL_PHONE, PHONE], ids=["360x740", "390x844"])
def test_usage_table_scrolls_inside_its_container_at_360(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int]
) -> None:
    """The page never scrolls sideways; the table's container does, inside itself. Checked
    with the real content (a 64-character task type) and with the table forced wider."""
    assert SMALL_PHONE["width"] == 360 and PHONE == {"width": 390, "height": 844}
    seed_usage(running_app)
    with new_page(browser, viewport) as page:
        open_panel(page, running_app)
        page.locator(REGION).scroll_into_view_if_needed()
        assert_table_scrolls_in_its_container(page, "seeded usage")
        for part in (COUNT, STATUS, UNFINISHED, USED):  # nothing is clipped out of sight
            expect(page.locator(part)).to_be_visible()

        widen_table(page)
        assert_table_scrolls_in_its_container(page, "a table wider than the phone")
        state = scroll_state(page)
        assert float(state["moved"]) > 0, state


# --- keyboard ------------------------------------------------------------------------------------


def test_usage_table_region_is_reached_and_scrolled_by_keyboard(
    running_app: RunningApp, browser: Browser
) -> None:
    """Tab reaches the container (a named region), focus is visible there, and the arrow
    keys scroll the table inside it without moving the page sideways."""
    seed_usage(running_app)
    with new_page(browser, DESKTOP) as page:
        open_panel(page, running_app)
        region = page.locator(REGION)
        expect(region).to_have_attribute("role", "region")
        expect(region).to_have_attribute("tabindex", "0")
        assert page.evaluate(ACCESSIBLE_NAME, REGION), "the region has no accessible name"
        widen_table(page)

        tab_to(page, REGION)
        outline = page.evaluate("() => getComputedStyle(document.activeElement).outlineStyle")
        assert outline != "none", "the focused region shows no focus outline"
        page.keyboard.press("ArrowRight")
        page.wait_for_function("(s) => document.querySelector(s).scrollLeft > 0", arg=REGION)
        assert_page_does_not_scroll(page, "after scrolling the table by keyboard")
        page.keyboard.press("Tab")
        left = page.evaluate(
            "(s) => !document.querySelector(s).contains(document.activeElement)", REGION
        )
        assert left, "Tab moves on from the region (the table holds no controls)"


# --- axe -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])
def test_axe_usage_panel(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int], scheme: str
) -> None:
    """No serious or critical axe findings with a budget, usage, an unfinished call and a
    provider count shown, and with the table overflowing its container."""
    seed_usage(running_app)
    with new_page(browser, viewport, color_scheme=scheme) as page:
        open_panel(page, running_app)
        expect(page.locator(UNFINISHED)).to_be_visible()
        expect(page.locator(USED)).to_be_visible()
        expect(page.locator(PLAN_INFO)).to_be_visible()
        assert css_token(page, "--color-bg"), "the colour tokens are not loaded"
        assert_no_serious_violations(page)

        widen_table(page)
        assert_no_serious_violations(page)
