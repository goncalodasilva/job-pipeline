"""The closed Tasks button says how many failed or interrupted tasks haven't been looked at
(M2.26; ADR-077 update of the M2.20 checkpoint), in a real browser.

Assumed contract (chosen to sit beside the M2.20 button without changing it):

- ``#tasks-toggle`` keeps "Tasks: " and ``#tasks-count`` ("No tasks running", "1 running",
  "1 running, 1 queued") exactly as they are. Next to the count, inside the button, is
  ``#tasks-attention``: "1 needs attention" for one failed or interrupted task that has not
  been looked at, "2 need attention" for two (the count's own style: a number and words, no
  colour alone). With none, the element is empty, hidden or absent. Being inside the button,
  its words are part of the button's accessible name.
- A task counts from the moment it ends as Failed or Interrupted while the panel is closed.
  Succeeded and cancelled tasks never count. Opening the panel (pointer or keyboard) is
  "looking": the words go away once the panel shows the task's row and stay away after the
  panel closes and after a reload. A task that fails while the panel is open has been looked
  at.
- Until the panel is opened the words survive a reload (and so a page opened later).
- It follows ``tasks:resync`` and ``tasks:updated`` like the count: no reload is needed.

Added with the M2.26 review:

- Looking is seeing the row: a panel that opens but can't load its tasks (the request is
  dropped, or answers an error) marks nothing, so the words stay, also after a reload.
- "Looked at" is remembered in ``localStorage["jobpipeline-tasks-seen"]`` as ``<id>:<attempt>``
  keys, and only keys the server still lists are kept: a key the server no longer lists is
  dropped when the list is next written, and a retry that fails again replaces its key.
  Another tab of the same browser drops the words when the panel is opened in this one,
  with no request of its own.
- A refetched panel whose only difference is a task's attempt (the retry failed the same
  way between two fetches) is swapped in, so that attempt is looked at too.
- The button's accessible name reads "Tasks: 1 running, 1 needs attention" at every width:
  the comma is real text (``#tasks-attention-gap``), shown and hidden with the words;
  ``#tasks-attention``'s own text is exactly "1 needs attention".
- Up to about 600 px the words stand in for the count (which stays in the name), so the bar
  is one row at 390, 430 and 480 px with the longest count, and at 600 px showing it all.
- An open, idle panel asks for its tasks again every 60 s (so "5 minutes ago" moves on);
  closed, it asks nothing. When only the relative words differ, they are written into the
  ``<time>`` elements already there: no node is replaced.

On a phone the button must still fit one row of the top bar.

The practice task (M2.17) is the vehicle. Nothing sleeps: every wait is a condition with a
deadline. Every value is fictional.
"""

import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Browser, Locator, Page, Route, expect

from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_setup_provider import tab_to
from tests.e2e.test_shell import has_no_horizontal_scroll, new_page, open_app
from tests.e2e.test_tasks_panel import (
    DEADLINE_S,
    ERROR_LINE,
    INDICATOR_URL,
    INIT_JS,
    NONE_RUNNING,
    ONE_RUNNING,
    PANEL_URL,
    POPOVER,
    SETTINGS,
    SLOT,
    STEP_MS,
    TOGGLE,
    assert_one_row,
    count,
    item_with_status,
    open_panel,
    popover,
    refetch_panel,
    reopen,
    seed_failed_and_running,
    server_panel,
    settings_page,
    start_task,
    toggle,
    wait_server_shows,
)
from tests.running_app import AppDidNotStart, RunningApp, start_app
from tests.waiting import wait_until

pytestmark = pytest.mark.e2e

ATTENTION = f"{TOGGLE} #tasks-attention"
GAP = f"{TOGGLE} #tasks-attention-gap"
RUNNING_PART = f"{TOGGLE} .tasks-running"  # the count and the comma after it
ONE = "1 needs attention"
TWO = "2 need attention"
SEEN_STORE = "jobpipeline-tasks-seen"
LONGEST_COUNT = "1 running, 1 queued"
CLOCK_START = datetime(2026, 4, 6, 10, 0, 0, tzinfo=UTC)
MINUTE_MS = 60_000
# Cancels the running task through the app's own route, with the panel closed.
CANCEL_RUNNING_JS = """async () => {
  const token = JSON.parse(document.body.getAttribute("hx-headers"))["X-JP-Token"];
  const headers = { "X-JP-Token": token };
  const html = await (await fetch("/tasks/panel", { headers })).text();
  const panel = new DOMParser().parseFromString(html, "text/html");
  const running = [...panel.querySelectorAll(".task-item")].filter(
    (item) => item.querySelector(".task-status").textContent.trim() === "Running");
  if (running.length !== 1) return -running.length;
  const id = running[0].id.replace(/^task-/, "");
  return (await fetch(`/tasks/${id}/cancel`, { method: "POST", headers })).status;
}"""


def attention(page: Page) -> Locator:
    return page.locator(ATTENTION)


def expect_attention(page: Page, words: str | None) -> None:
    """The closed (or open) button says ``words``; ``None``: it says nothing of the kind."""
    if words is None:
        expect(attention(page)).to_be_hidden()
        expect(toggle(page)).not_to_contain_text("attention")
        return
    expect(attention(page)).to_have_text(words, timeout=STEP_MS)
    expect(page.locator(SLOT).get_by_role("button", name=re.compile(re.escape(words)))).to_have_id(
        "tasks-toggle"
    )


def seen_store(page: Page) -> list[str]:
    """The keys this browser remembers as looked at."""
    keys: list[str] = page.evaluate(
        "(name) => JSON.parse(window.localStorage.getItem(name) || '[]')", SEEN_STORE
    )
    return keys


def listed_keys(page: Page) -> list[str]:
    """The ``<id>:<attempt>`` keys the server lists on the page's count."""
    return (count(page).get_attribute("data-attention") or "").split()


def wait_seen_store(page: Page, expected: list[str]) -> None:
    wait_until(
        lambda: seen_store(page) == expected,
        timeout=DEADLINE_S,
        message=f"the looked-at keys are {expected}, found {seen_store(page)}",
    )


def boxes_of(page: Page, *selectors: str) -> list[dict[str, float]]:
    """The boxes of the elements, from one read: the page can still be shifting after a
    task starts, and boxes read one by one could come from different frames."""
    boxes: list[dict[str, float] | None] = page.evaluate(
        """(selectors) => selectors.map((selector) => {
            const element = document.querySelector(selector);
            if (!element) return null;
            const box = element.getBoundingClientRect();
            return { x: box.x, y: box.y, width: box.width, height: box.height };
        })""",
        list(selectors),
    )
    assert all(box is not None for box in boxes), selectors
    return [box for box in boxes if box is not None]


def box_of(page: Page, selector: str) -> dict[str, float]:
    return boxes_of(page, selector)[0]


def close_panel(page: Page) -> None:
    page.keyboard.press("Escape")
    expect(toggle(page)).to_have_attribute("aria-expanded", "false")
    expect(popover(page)).to_be_hidden()


def open_panel_by_keyboard(page: Page) -> None:
    """From the top of the page, Tab to the Tasks button and press Enter (no clicks)."""
    tab_to(page, TOGGLE)
    page.keyboard.press("Enter")
    expect(toggle(page)).to_have_attribute("aria-expanded", "true")
    expect(popover(page)).to_be_visible()


def fail_one(page: Page, outcome: str = "network") -> None:
    start_task(page, outcome=outcome)
    wait_server_shows(page, "Failed")


def test_failed_task_needs_attention_until_the_panel_is_opened(
    running_app: RunningApp, browser: Browser
) -> None:
    with settings_page(browser, running_app, DESKTOP) as page:
        expect_attention(page, None)  # nothing has failed
        fail_one(page)

        # No reload: the closed button says so, next to the unchanged count.
        expect_attention(page, ONE)
        expect(attention(page)).to_be_visible()
        expect(count(page)).to_have_text(NONE_RUNNING)
        expect(toggle(page)).to_have_attribute("aria-expanded", "false")
        expect(page.locator(SLOT).get_by_role("button", name=f"Tasks: {NONE_RUNNING}")).to_have_id(
            "tasks-toggle"
        )
        assert_no_serious_violations(page)

        # It survives a reload until the panel is opened.
        reopen(page, running_app)
        expect_attention(page, ONE)

        # Opening the panel by keyboard is looking at it.
        open_panel_by_keyboard(page)
        expect(item_with_status(page, "Failed")).to_have_count(1)
        expect_attention(page, None)
        close_panel(page)
        expect_attention(page, None)
        expect(count(page)).to_have_text(NONE_RUNNING)

        reopen(page, running_app)
        expect(count(page)).to_have_text(NONE_RUNNING)
        expect_attention(page, None)


def test_two_failed_tasks_use_the_plural(running_app: RunningApp, browser: Browser) -> None:
    with settings_page(browser, running_app, DESKTOP) as page:
        fail_one(page, "network")
        expect_attention(page, ONE)
        start_task(page, outcome="invalid_output")
        expect_attention(page, TWO)

        open_panel(page)  # the pointer counts as looking too
        expect(item_with_status(page, "Failed")).to_have_count(2)
        expect_attention(page, None)


def test_succeeded_and_cancelled_tasks_need_no_attention(
    running_app: RunningApp, browser: Browser
) -> None:
    with settings_page(browser, running_app, DESKTOP) as page:
        start_task(page)
        wait_server_shows(page, "Done")
        start_task(page, duration="slow")
        expect(count(page)).to_have_text(ONE_RUNNING, timeout=STEP_MS)
        expect_attention(page, None)

        # Cancelled with the panel closed the whole time: nothing here was ever looked at,
        # so if a cancelled task counted, the words would be showing.
        assert page.evaluate(CANCEL_RUNNING_JS) == 200
        wait_server_shows(page, "Cancelled")
        expect(count(page)).to_have_text(NONE_RUNNING, timeout=STEP_MS)
        expect(toggle(page)).to_have_attribute("aria-expanded", "false")
        assert seen_store(page) == [], "control: the panel was never opened"
        assert listed_keys(page) == [], "the server lists neither task"
        expect_attention(page, None)
        reopen(page, running_app)
        expect_attention(page, None)

        # Control: the count is live. A failure now is one task, not two or three.
        fail_one(page)
        expect_attention(page, ONE)
        assert len(listed_keys(page)) == 1


def test_needs_attention_sits_beside_the_running_count(
    running_app: RunningApp, browser: Browser
) -> None:
    with settings_page(browser, running_app, DESKTOP) as page:
        seed_failed_and_running(page)

        expect(count(page)).to_have_text(ONE_RUNNING, timeout=STEP_MS)
        expect_attention(page, ONE)
        name = re.compile(rf"Tasks: {ONE_RUNNING}.*{ONE}")
        expect(page.locator(SLOT).get_by_role("button", name=name)).to_have_id("tasks-toggle")
        assert_no_serious_violations(page)


def test_a_task_that_fails_while_the_panel_is_open_was_looked_at(
    running_app: RunningApp, browser: Browser
) -> None:
    with settings_page(browser, running_app, DESKTOP) as page:
        fail_one(page)
        expect_attention(page, ONE)
        open_panel(page)
        failed = item_with_status(page, "Failed")
        expect(failed).to_have_count(1)

        # Retry fails the same way, in front of the user.
        failed.get_by_role("button", name="Retry").click()
        page.wait_for_function(
            """async () => {
              const token = JSON.parse(document.body.getAttribute("hx-headers"))["X-JP-Token"];
              const response = await fetch("/tasks/indicator", {headers: {"X-JP-Token": token}});
              const finished = JSON.parse(response.headers.get("HX-Trigger"))["tasks:finished"];
              return finished.tasks.some((task) => task.key.endsWith(":2"));
            }""",
            timeout=STEP_MS,
        )
        expect(item_with_status(page, "Failed")).to_have_count(1)
        close_panel(page)
        expect_attention(page, None)


def test_needs_attention_fits_the_phone_top_bar(running_app: RunningApp, browser: Browser) -> None:
    assert PHONE == {"width": 390, "height": 844}
    with settings_page(browser, running_app, PHONE) as page:
        seed_failed_and_running(page)
        expect(count(page)).to_have_text(ONE_RUNNING, timeout=STEP_MS)
        expect_attention(page, ONE)  # in the button's text and its accessible name

        assert_one_row(page, "390 px, one running and one needing attention")
        fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert fits, f"scrollWidth {scroll_width} > {inner_width}"
        assert_no_serious_violations(page)


@pytest.mark.parametrize("width", [360, 390, 430, 480, 599, 600, 767])
def test_needs_attention_with_the_longest_count_stays_one_row(
    running_app: RunningApp, browser: Browser, width: int
) -> None:
    """ "1 running, 1 queued, 1 needs attention": one row and no horizontal scroll just above
    the small-phone width too (the bar used to wrap from 430 px up)."""
    with settings_page(browser, running_app, {"width": width, "height": 844}) as page:
        seed_failed_and_running(page)
        expect(count(page)).to_have_text(ONE_RUNNING, timeout=STEP_MS)
        expect_attention(page, ONE)
        # The longest count, as the server would send it (as test_tasks_panel.py does).
        page.evaluate(
            "(text) => { document.getElementById('tasks-count').textContent = text; }",
            LONGEST_COUNT,
        )
        expect(toggle(page)).to_have_accessible_name(f"Tasks: {LONGEST_COUNT}, {ONE}")

        assert_one_row(page, f"{width} px, {LONGEST_COUNT}, {ONE}")
        fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert fits, f"{width} px: scrollWidth {scroll_width} > {inner_width}"
        expect(attention(page)).to_be_visible()
        running, gap, words = boxes_of(page, RUNNING_PART, GAP, ATTENTION)
        compact = running["width"] <= 1
        assert compact is (width < 600), f"{width} px: the count is shown from 600 px up"
        if not compact:  # all of it on one line of text, with the space after the comma
            assert abs(running["y"] - words["y"]) < 2, (running, words)
            assert running["height"] < 1.5 * words["height"], "the count is one line"
            assert gap["width"] > gap["height"] / 4, "the comma keeps the space after it"


# --- the button's name (M2.26 review) ------------------------------------------------------------


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])
def test_button_name_reads_the_count_a_comma_and_the_attention_words(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int]
) -> None:
    with settings_page(browser, running_app, viewport) as page:
        expect(toggle(page)).to_have_accessible_name(f"Tasks: {NONE_RUNNING}")
        expect(page.locator(GAP)).to_be_hidden()
        seed_failed_and_running(page)
        expect(count(page)).to_have_text(ONE_RUNNING, timeout=STEP_MS)
        expect_attention(page, ONE)

        # The whole name, exactly; the words themselves hold no comma.
        expect(toggle(page)).to_have_accessible_name(f"Tasks: {ONE_RUNNING}, {ONE}")
        expect(toggle(page)).not_to_have_accessible_name(f"Tasks: {ONE_RUNNING}")
        assert attention(page).evaluate("(element) => element.textContent") == ONE
        assert page.locator(GAP).evaluate("(element) => element.textContent") == ", "
        assert page.locator(GAP).evaluate("(element) => element.hidden") is False
        if viewport == DESKTOP:
            expect(page.locator(GAP)).to_be_visible()
            expect(count(page)).to_be_visible()
        else:  # hidden from sight, never from the name
            assert box_of(page, RUNNING_PART)["width"] <= 1
            assert page.locator(RUNNING_PART).evaluate(
                "(element) => getComputedStyle(element).display !== 'none'"
                " && getComputedStyle(element).visibility === 'visible'"
            )
        assert_no_serious_violations(page)

        # Looked at: the comma goes with the words.
        open_panel(page)
        expect(item_with_status(page, "Failed")).to_have_count(1)
        expect_attention(page, None)
        expect(toggle(page)).to_have_accessible_name(f"Tasks: {ONE_RUNNING}")
        assert page.locator(GAP).evaluate("(element) => element.hidden") is True
        expect(count(page)).to_be_visible()


# --- looking is seeing the row (M2.26 review) ----------------------------------------------------


@pytest.mark.parametrize("failure", ["dropped", "error"])
def test_a_panel_that_cannot_load_marks_nothing_as_looked_at(
    running_app: RunningApp, browser: Browser, failure: str
) -> None:
    def fail(route: Route) -> None:
        if failure == "dropped":
            route.abort()
        else:
            route.fulfill(status=500, content_type="text/plain", body="fictional failure")

    with settings_page(browser, running_app, DESKTOP) as page:
        fail_one(page)
        expect_attention(page, ONE)
        page.route(f"**{PANEL_URL}", fail)

        toggle(page).click()
        expect(popover(page)).to_be_visible()
        expect(page.locator(f"{POPOVER} {ERROR_LINE}")).to_be_visible()  # the fetch has failed
        expect(item_with_status(page, "Failed")).to_have_count(0)
        expect_attention(page, ONE)  # nothing was shown, so nothing was looked at
        assert seen_store(page) == []
        close_panel(page)
        expect_attention(page, ONE)

        reopen(page, running_app)
        expect_attention(page, ONE)
        assert seen_store(page) == []

        # Once the panel does show the task, it has been looked at.
        page.unroute(f"**{PANEL_URL}")
        open_panel_by_keyboard(page)
        expect(item_with_status(page, "Failed")).to_have_count(1)
        expect_attention(page, None)
        wait_seen_store(page, listed_keys(page))


def test_only_keys_the_server_still_lists_are_remembered(
    running_app: RunningApp, browser: Browser
) -> None:
    stale = ["task-fictional-gone-0001:1", "task-fictional-gone-0002:3"]
    with settings_page(browser, running_app, DESKTOP) as page:
        page.evaluate(
            "([name, keys]) => window.localStorage.setItem(name, JSON.stringify(keys))",
            [SEEN_STORE, stale],
        )
        fail_one(page)
        expect_attention(page, ONE)
        (first,) = listed_keys(page)
        assert first.endswith(":1")
        assert seen_store(page) == stale, "nothing is written until something is looked at"

        open_panel(page)
        failed = item_with_status(page, "Failed")
        expect(failed).to_have_count(1)
        expect_attention(page, None)
        wait_seen_store(page, [first])  # exactly what the server lists: the stale keys went

        # The retry fails again in front of the user: its key replaces the first.
        second = first.removesuffix(":1") + ":2"
        failed.get_by_role("button", name="Retry").click()
        expect(count(page)).to_have_attribute("data-attention", second, timeout=STEP_MS)
        expect(item_with_status(page, "Failed")).to_have_count(1)
        wait_seen_store(page, [second])
        close_panel(page)
        expect_attention(page, None)


def test_opening_the_panel_in_one_tab_clears_the_words_in_another(
    running_app: RunningApp, browser: Browser
) -> None:
    with settings_page(browser, running_app, DESKTOP) as page:
        fail_one(page)
        expect_attention(page, ONE)
        other = page.context.new_page()
        open_app(other, running_app, SETTINGS)
        other.wait_for_function("() => window.__indicatorAnswers > 0")
        expect_attention(other, ONE)
        asked: list[str] = []
        other.on("request", lambda request: asked.append(urlsplit(request.url).path))

        open_panel(page)
        expect(item_with_status(page, "Failed")).to_have_count(1)
        expect_attention(page, None)

        # No reload and no request: the other tab hears of it through storage alone.
        expect_attention(other, None)
        expect(toggle(other)).to_have_attribute("aria-expanded", "false")
        assert [path for path in asked if path.startswith("/tasks/")] == [], asked
        expect(toggle(other)).to_have_accessible_name(f"Tasks: {NONE_RUNNING}")


def test_a_refetched_panel_that_differs_only_in_the_attempt_is_swapped_in(
    running_app: RunningApp, browser: Browser
) -> None:
    """The retry failed the same way between two fetches: the same ids and words, another
    attempt. The row is replaced, so the new attempt has been looked at."""
    with settings_page(browser, running_app, DESKTOP) as page:
        fail_one(page)
        (first,) = listed_keys(page)
        open_panel(page)
        expect(item_with_status(page, "Failed")).to_have_count(1)
        expect_attention(page, None)
        wait_seen_store(page, [first])
        task_id = first.removesuffix(":1")
        second = f"{task_id}:2"
        row = page.locator(f"#task-{task_id}")
        expect(row).to_have_attribute("data-attention", first)
        assert server_panel(page).count(f'data-attention="{first}"') == 1

        def as_second_attempt(route: Route) -> None:
            response = route.fetch()
            route.fulfill(response=response, body=response.text().replace(first, second))

        page.route(f"**{PANEL_URL}", as_second_attempt)
        page.route(f"**{INDICATOR_URL}", as_second_attempt)
        refetch_panel(page)

        expect(count(page)).to_have_attribute("data-attention", second)
        expect(row).to_have_attribute("data-attention", second)  # swapped in
        wait_seen_store(page, [second])
        expect_attention(page, None)
        close_panel(page)
        expect_attention(page, None)


def test_stale_key_goes_when_the_panel_is_refreshed_before_the_count(
    running_app: RunningApp, browser: Browser
) -> None:
    """The same refetch as above with the count's answer held back until the panel's row is
    swapped in (the order a slow machine can produce): while the count still lists the first
    attempt both keys are kept, and once it lists only the second the first one goes."""
    with settings_page(browser, running_app, DESKTOP) as page:
        fail_one(page)
        (first,) = listed_keys(page)
        open_panel(page)
        wait_seen_store(page, [first])
        task_id = first.removesuffix(":1")
        second = f"{task_id}:2"
        row = page.locator(f"#task-{task_id}")
        held: list[Route] = []

        def as_second_attempt(route: Route) -> None:
            response = route.fetch()
            route.fulfill(response=response, body=response.text().replace(first, second))

        def hold(route: Route) -> None:
            held.append(route)

        page.route(f"**{PANEL_URL}", as_second_attempt)
        page.route(f"**{INDICATOR_URL}", hold)
        refetch_panel(page)
        expect(row).to_have_attribute("data-attention", second)  # the panel came first
        wait_until(lambda: len(held) >= 1, timeout=DEADLINE_S, message="the count was asked")
        expect(count(page)).to_have_attribute("data-attention", first)
        wait_seen_store(page, [first, second])

        for route in held:
            as_second_attempt(route)
        expect(count(page)).to_have_attribute("data-attention", second)
        wait_seen_store(page, [second])
        expect_attention(page, None)


# --- the relative time on an open panel (M2.26 review) -------------------------------------------


def test_relative_words_are_patched_without_replacing_a_node(
    running_app: RunningApp, browser: Browser
) -> None:
    with settings_page(browser, running_app, DESKTOP) as page:
        fail_one(page)
        open_panel(page)
        time = popover(page).locator("time")
        expect(time).to_have_count(1)
        expect(time).to_have_text("just now")
        stamp = time.get_attribute("datetime")
        page.evaluate(
            "() => document.querySelectorAll('#tasks-popover .task-item, #tasks-popover time,"
            " #tasks-popover button').forEach((node) => { node.__sameNode = true; })"
        )

        def a_minute_later(route: Route) -> None:
            response = route.fetch()
            body = response.text()
            assert body.count(">just now</time>") == 1
            route.fulfill(
                response=response, body=body.replace(">just now</time>", ">1 minute ago</time>")
            )

        page.route(f"**{PANEL_URL}", a_minute_later)
        refetch_panel(page)

        expect(time).to_have_text("1 minute ago")
        assert time.get_attribute("datetime") == stamp
        kept: list[bool] = page.evaluate(
            "() => [...document.querySelectorAll('#tasks-popover .task-item, #tasks-popover time,"
            " #tasks-popover button')].map((node) => node.__sameNode === true)"
        )
        assert kept and all(kept), "only the words changed: no node was replaced"


def test_an_open_idle_panel_asks_again_every_minute(
    running_app: RunningApp, browser: Browser
) -> None:
    """On Playwright's page clock: no test waits a real minute."""
    with new_page(browser, DESKTOP) as page:
        page.context.add_init_script(INIT_JS)
        page.clock.install(time=CLOCK_START)
        page.clock.pause_at(CLOCK_START)
        open_app(page, running_app, SETTINGS)
        asked: list[str] = []
        page.on("request", lambda request: asked.append(urlsplit(request.url).path))

        def panel_requests() -> int:
            return asked.count(PANEL_URL)

        def wait_requests(expected: int) -> None:
            wait_until(
                lambda: (
                    panel_requests() == expected
                    and page.evaluate("() => window.__panelAnswers") == expected
                ),
                timeout=DEADLINE_S,
                message=f"{expected} panel request(s) answered, found {panel_requests()}",
            )

        toggle(page).click()
        expect(popover(page).locator("#tasks-empty")).to_be_visible()  # idle: nothing runs
        wait_requests(1)

        page.clock.run_for(MINUTE_MS - 1000)
        assert page.evaluate("() => window.__panelAnswers") == 1
        assert panel_requests() == 1, "an idle panel asks nothing before the minute is up"
        page.clock.run_for(1000)
        wait_requests(2)
        page.clock.run_for(MINUTE_MS)
        wait_requests(3)

        # Closed, it asks nothing however long it stays closed.
        page.keyboard.press("Escape")
        expect(popover(page)).to_be_hidden()
        page.clock.run_for(3 * MINUTE_MS)
        assert (
            page.evaluate(
                """async () => {
              const token = JSON.parse(document.body.getAttribute("hx-headers"))["X-JP-Token"];
              return (await fetch("/health", { headers: { "X-JP-Token": token } })).status;
            }"""
            )
            == 200
        )  # a round trip: anything the page was about to ask has been asked
        assert panel_requests() == 3


def test_interrupted_task_needs_attention_after_a_restart(browser: Browser, tmp_path: Path) -> None:
    """The app is killed under a slow task and started again on the same folders: the closed
    button of a fresh browser says one task needs attention, until the panel is opened."""

    def started(*, restart: bool) -> RunningApp:
        try:
            name = "after-kill" if restart else "before-kill"
            return start_app(tmp_path, restart=restart, output_name=name)
        except AppDidNotStart as error:
            pytest.fail(f"the app did not start: {error}", pytrace=False)

    first = started(restart=False)
    try:
        with settings_page(browser, first, DESKTOP) as page:
            start_task(page, duration="slow")
            wait_server_shows(page, "Running")
        assert first.kill().killed
    finally:
        first.stop()

    second = started(restart=True)
    try:
        with settings_page(browser, second, DESKTOP) as page:
            expect_attention(page, ONE)
            expect(count(page)).to_have_text(NONE_RUNNING)
            open_panel_by_keyboard(page)
            expect(item_with_status(page, "Interrupted")).to_have_count(1)
            expect_attention(page, None)
            close_panel(page)
            reopen(page, second)
            expect_attention(page, None)
    finally:
        stopped = second.stop()
    assert not stopped.killed, second.output_tail()
    assert stopped.returncode == 0, second.output_tail()
