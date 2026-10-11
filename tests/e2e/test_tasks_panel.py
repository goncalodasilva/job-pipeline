"""The top-bar task indicator and its tasks panel in a real browser (M2.20; AC-M2.4, AC-M2.9).

The DOM these tests expect on every app page (and never on a setup guide page):

- ``[data-slot="tasks"]`` holds ``<button type="button" id="tasks-toggle">``: the word "Tasks"
  and, in ``#tasks-count``, the count in words: "No tasks running", "1 running", "1 queued",
  "1 running, 1 queued". The page itself carries the count; it is shown only when scripts run.
  It is a disclosure button: ``aria-expanded`` and ``aria-controls="tasks-popover"``; click,
  Enter and Space toggle it and focus stays on it. The panel closes on Esc (focus goes to the
  button only when it was on it or in the panel), on a pointer press outside, and when
  keyboard focus leaves the button and the panel.
- ``#tasks-popover`` comes right after the button in Tab order, is hidden while closed, and
  holds the M2.19 panel (``#tasks-panel``, items ``.task-item`` with
  ``#task-<id>-status|-progress|-reason|-cancel|-retry``). It is not modal: no focus trap,
  nothing made inert. With no control in it, the panel itself is the Tab stop, so its list
  can be scrolled by keyboard. Cancel and Retry are described by their task's label.
- The count follows ``tasks:resync`` and ``tasks:updated`` without a reload. The panel is
  fetched when it opens and follows those events while open; closed, it asks for nothing.
  While it is open and a task runs, the progress shown moves on (heartbeats publish no
  event, so the panel refetches on a short timer). A refetch that changes nothing replaces
  nothing. When the fetch fails, ``#tasks-error`` in the panel says so.
- A refresh never loses keyboard focus: it stays on the control with the same id, or moves to
  ``#tasks-toggle`` when that control is gone.
- When a task ends, ``#announcer`` says so once: "<label> finished.", "<label> failed. Open
  Tasks to see why." or "<label> cancelled." (label "Practice task" here). Later refreshes,
  a reload and a tab opened afterwards say nothing more about it. A page's first answer
  speaks only of tasks it doesn't know that finished in the last 10 seconds.
- A practice result links nowhere (there is no result page before M3).
- The top bar stays one row at 390 and 360 px.

The practice task (M2.17) is the vehicle: Settings > Testing tools starts it. A slow one
takes 3 s per step (5 steps); a failing outcome fails its first step. Nothing sleeps: every
wait is a condition with a deadline. Every value is fictional.
"""

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Browser, Locator, Page, Response, Route, expect

from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_setup_provider import MAX_TABS, SMALL_PHONE, tab_to
from tests.e2e.test_setup_provider_test import tab_to_locator
from tests.e2e.test_shell import (
    assert_target_size,
    css_token,
    focus_is_inside,
    has_no_horizontal_scroll,
    new_page,
    open_app,
)
from tests.running_app import RunningApp
from tests.waiting import wait_until

pytestmark = pytest.mark.e2e

SETTINGS = "/settings"
EVENTS = "/events"
PANEL_URL = "/tasks/panel"
PRACTICE_FORM = 'form[action="/tasks/practice"]'
SLOT = '[data-slot="tasks"]'
TOGGLE = "#tasks-toggle"
COUNT = "#tasks-count"
POPOVER = "#tasks-popover"
POPOVER_ID = "tasks-popover"
PANEL = "#tasks-panel"
ITEM = f"{POPOVER} .task-item"

NONE_RUNNING = "No tasks running"
ONE_RUNNING = "1 running"
MSG_EMPTY = "No tasks yet. AI work you start shows up here."
MSG_NETWORK = "No connection. Nothing was lost; try again."
LABEL = "Practice task"
PROGRESS_NAME = "Practice task progress"
SAID_FINISHED = "Practice task finished."
SAID_FAILED = "Practice task failed. Open Tasks to see why."
SAID_CANCELLING = "Cancelling the task."
MSG_PANEL_ERROR = (
    "Couldn't load your tasks. Nothing was lost; close and reopen this panel to try again."
)
ERROR_LINE = "#tasks-error"
INDICATOR_URL = "/tasks/indicator"
SAID_CANCELLED = "Practice task cancelled."
SENTINEL = "Fictional sentinel announcement."

STEP_MS = 15_000  # a slow step takes 3 s; a state change is awaited at most this long
RECONNECT_MS = 30_000  # the reader retries after 1, 2, 4 and 8 s
DEADLINE_S = 15.0

# Runs before the page's own scripts: records what #announcer says, counts htmx settles and
# task events, and lets a test end the open event stream (as a dropped connection would).
INIT_JS = """
window.__announced = [];
window.__settles = 0;
window.__resyncs = 0;
window.__indicatorAnswers = 0;
window.__panelAnswers = 0;
document.addEventListener("htmx:afterSettle", () => { window.__settles += 1; });
document.addEventListener("tasks:resync", () => { window.__resyncs += 1; });
document.addEventListener("tasks:finished", () => { window.__indicatorAnswers += 1; });
document.addEventListener("htmx:afterRequest", (event) => {
  const path = event.detail && event.detail.pathInfo && event.detail.pathInfo.requestPath;
  if (path === "/tasks/panel") window.__panelAnswers += 1;
});
document.addEventListener("DOMContentLoaded", () => {
  const region = document.getElementById("announcer");
  if (!region) return;
  new MutationObserver(() => {
    const text = region.textContent.trim();
    if (text) window.__announced.push(text);
  }).observe(region, { childList: true, characterData: true, subtree: true });
});
const realFetch = window.fetch.bind(window);
window.__dropStream = () => {};
window.fetch = function (input, init) {
  if (new URL(String(input), location.href).pathname !== "/events") return realFetch(input, init);
  const dropper = new AbortController();
  window.__dropStream = () => dropper.abort();
  const options = Object.assign({}, init || {});
  if (options.signal) options.signal.addEventListener("abort", () => dropper.abort());
  options.signal = dropper.signal;
  return realFetch(input, options);
};
"""
SERVER_PANEL_JS = """async () => {
  const token = JSON.parse(document.body.getAttribute("hx-headers"))["X-JP-Token"];
  const response = await fetch("/tasks/panel", { headers: { "X-JP-Token": token } });
  return await response.text();
}"""


# --- helpers -----------------------------------------------------------------------------------


@contextmanager
def settings_page(
    browser: Browser, app: RunningApp, viewport: dict[str, int], *, scheme: str = "light"
) -> Iterator[Page]:
    with new_page(browser, viewport, color_scheme=scheme) as page:
        page.context.add_init_script(INIT_JS)  # also for tabs opened later in this browser
        open_app(page, app, SETTINGS)
        # The first answer tells the page what had finished before it opened; a task started
        # earlier than that could end unannounced.
        page.wait_for_function("() => window.__indicatorAnswers > 0")
        yield page


def toggle(page: Page) -> Locator:
    return page.locator(f"{SLOT} {TOGGLE}")


def count(page: Page) -> Locator:
    return page.locator(f"{TOGGLE} {COUNT}")


def popover(page: Page) -> Locator:
    return page.locator(POPOVER)


def items(page: Page) -> Locator:
    return page.locator(ITEM)


def item_with_status(page: Page, word: str) -> Locator:
    status = page.locator(".task-status", has_text=re.compile(f"^{re.escape(word)}$"))
    return items(page).filter(has=status)


def status_of(item: Locator) -> Locator:
    return item.locator(".task-status")


def server_panel(page: Page) -> str:
    """What the server says now, asked directly (for setup and for 'the server is ahead of
    the page' moments; never instead of an assertion on the page)."""
    html: str = page.evaluate(SERVER_PANEL_JS)
    return html


def wait_server_shows(page: Page, word: str) -> None:
    marker = f'class="task-status">{word}<'
    wait_until(
        lambda: marker in server_panel(page),
        timeout=DEADLINE_S,
        message=f"the server shows a task as {word}",
    )


def start_task(page: Page, *, duration: str = "quick", outcome: str = "ok") -> None:
    """Start a practice task from Settings > Testing tools (no task may be active)."""
    form = page.locator(PRACTICE_FORM)
    form.locator('select[name="duration"]').select_option(duration)
    form.locator('select[name="outcome"]').select_option(outcome)

    def is_start(response: Response) -> bool:
        request = response.request
        return urlsplit(request.url).path == "/tasks/practice" and request.method == "POST"

    with page.expect_response(is_start) as started:
        form.get_by_role("button", name="Start").click()
    assert started.value.status == 200
    expect(page.locator("#practice-status")).to_have_text("Practice task started.")


def seed_failed_and_running(page: Page) -> None:
    """One failed task and one running (slow) task, started with the panel closed."""
    start_task(page, outcome="network")
    wait_server_shows(page, "Failed")
    start_task(page, duration="slow")
    wait_server_shows(page, "Running")


def open_panel(page: Page) -> None:
    expect(toggle(page)).to_have_attribute("aria-expanded", "false")
    toggle(page).click()
    expect(toggle(page)).to_have_attribute("aria-expanded", "true")
    expect(popover(page)).to_be_visible()
    expect(popover(page).locator(PANEL)).to_be_visible()


def reopen(page: Page, app: RunningApp) -> None:
    """Load Settings again, so the next Tab starts at the top of the page."""
    open_app(page, app, SETTINGS)
    expect(toggle(page)).to_have_attribute("aria-expanded", "false")


def refresh_tasks(page: Page) -> None:
    """One more ``tasks:resync``, as a reconnect would cause, and the refetch it leads to."""
    with page.expect_response(lambda r: urlsplit(r.url).path.startswith("/tasks/")):
        page.evaluate(
            "() => document.body.dispatchEvent(new CustomEvent('tasks:resync', {bubbles: true}))"
        )


def announced(page: Page) -> list[str]:
    """Everything #announcer has said, after every announcement on its way has arrived: the
    app's own announce event is asked for a sentinel, which is queued behind the others."""
    page.evaluate(
        "(text) => { window.__announced = window.__announced.filter((said) => said !== text);"
        " document.body.dispatchEvent(new CustomEvent('announce', { detail: { value: text } })); }",
        SENTINEL,
    )
    page.wait_for_function("(text) => window.__announced.includes(text)", arg=SENTINEL)
    said: list[str] = page.evaluate("() => window.__announced")
    return [text for text in said if text != SENTINEL]


def wait_announced(page: Page, message: str) -> None:
    page.wait_for_function(
        "(text) => window.__announced.includes(text)", arg=message, timeout=STEP_MS
    )


def assert_focus_not_lost(page: Page) -> None:
    where: str = page.evaluate(
        "() => { const e = document.activeElement;"
        " return !e || e === document.body ? '' : (e.id || e.tagName); }"
    )
    assert where, "keyboard focus was lost (it is on <body>)"


def assert_fits(page: Page, what: str) -> None:
    """No horizontal scroll, and the open panel lies inside the viewport."""
    fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
    assert fits, f"{what}: scrollWidth {scroll_width} > {inner_width}"
    box = popover(page).bounding_box()
    assert box is not None, f"{what}: the panel is not rendered"
    assert box["x"] >= 0 and box["x"] + box["width"] <= inner_width + 0.5, (
        f"{what}: the panel spans {box['x']:.1f} to {box['x'] + box['width']:.1f} px "
        f"in a {inner_width} px viewport"
    )


def assert_no_links(item: Locator) -> None:
    """No result page exists before M3, so a task links nowhere."""
    expect(item.locator("a")).to_have_count(0)


# --- the indicator -----------------------------------------------------------------------------


def test_indicator_shows_running_count_in_text(running_app: RunningApp, browser: Browser) -> None:
    with settings_page(browser, running_app, DESKTOP) as page:
        expect(toggle(page)).to_have_count(1)
        expect(page.locator(SLOT).get_by_role("button", name=re.compile("Tasks"))).to_have_count(1)
        expect(toggle(page)).to_be_visible()
        expect(toggle(page)).to_have_attribute("type", "button")
        expect(toggle(page)).to_have_attribute("aria-expanded", "false")
        expect(toggle(page)).to_have_attribute("aria-controls", POPOVER_ID)
        expect(count(page)).to_have_text(NONE_RUNNING)
        expect(count(page)).to_be_visible()
        expect(popover(page)).to_be_hidden()

        start_task(page, duration="slow")
        # The panel is closed: the count follows the task events by itself, in words.
        expect(count(page)).to_have_text(ONE_RUNNING)
        expect(toggle(page)).to_contain_text("Tasks")
        expect(popover(page)).to_be_hidden()

        page.reload()  # a fresh page knows the count too
        expect(count(page)).to_have_text(ONE_RUNNING)

        open_panel(page)
        items(page).get_by_role("button", name="Cancel").click()
        expect(count(page)).to_have_text(NONE_RUNNING, timeout=STEP_MS)
        expect(page.locator("#app-status")).to_be_hidden()


def test_indicator_and_panel_absent_on_guide_pages(
    running_app: RunningApp, browser: Browser
) -> None:
    with new_page(browser, DESKTOP) as page:
        asked: list[str] = []
        page.on("request", lambda request: asked.append(urlsplit(request.url).path))
        open_app(page, running_app, "/setup/welcome")
        for selector in (SLOT, TOGGLE, COUNT, POPOVER, PANEL):
            expect(page.locator(selector)).to_have_count(0)
        assert page.evaluate("() => fetch('/health').then((r) => r.status)") == 200
        assert not [path for path in asked if path.startswith("/tasks")], asked

        open_app(page, running_app, SETTINGS)  # the positive control
        expect(toggle(page)).to_have_count(1)


# --- the disclosure ----------------------------------------------------------------------------


def test_panel_opens_and_closes_by_keyboard_focus_returns(
    running_app: RunningApp, browser: Browser
) -> None:
    with settings_page(browser, running_app, DESKTOP) as page:
        tab_to(page, TOGGLE)
        expect(popover(page)).to_be_hidden()

        page.keyboard.press("Enter")
        expect(toggle(page)).to_have_attribute("aria-expanded", "true")
        expect(popover(page)).to_be_visible()
        expect(popover(page).locator(PANEL)).to_be_visible()
        expect(popover(page).locator("#tasks-empty")).to_have_text(MSG_EMPTY)
        expect(toggle(page)).to_be_focused()  # a disclosure: opening moves no focus

        page.keyboard.press("Escape")
        expect(toggle(page)).to_have_attribute("aria-expanded", "false")
        expect(popover(page)).to_be_hidden()
        expect(toggle(page)).to_be_focused()

        page.keyboard.press("Space")
        expect(toggle(page)).to_have_attribute("aria-expanded", "true")
        expect(popover(page)).to_be_visible()
        page.keyboard.press("Space")
        expect(toggle(page)).to_have_attribute("aria-expanded", "false")
        expect(popover(page)).to_be_hidden()
        expect(toggle(page)).to_be_focused()

        toggle(page).click()
        expect(popover(page)).to_be_visible()
        toggle(page).click()
        expect(popover(page)).to_be_hidden()

        # With a task in it: Tab goes from the button into the panel; Esc there closes the
        # panel and gives focus back to the button.
        start_task(page, outcome="network")
        wait_server_shows(page, "Failed")
        reopen(page, running_app)
        tab_to(page, TOGGLE)
        page.keyboard.press("Enter")
        retry = items(page).get_by_role("button", name="Retry")
        expect(retry).to_be_visible()
        page.keyboard.press("Tab")
        assert focus_is_inside(page, POPOVER), "the panel follows its button in Tab order"
        expect(retry).to_be_focused()  # the failed task's only control
        page.keyboard.press("Escape")
        expect(toggle(page)).to_have_attribute("aria-expanded", "false")
        expect(popover(page)).to_be_hidden()
        expect(toggle(page)).to_be_focused()
        expect(page.locator("#app-status")).to_be_hidden()


def test_panel_is_not_modal(running_app: RunningApp, browser: Browser) -> None:
    """No focus trap and nothing made inert: Tab leaves the panel for the page."""
    with settings_page(browser, running_app, DESKTOP) as page:
        start_task(page, outcome="network")
        wait_server_shows(page, "Failed")
        reopen(page, running_app)
        tab_to(page, TOGGLE)
        page.keyboard.press("Enter")
        expect(items(page).get_by_role("button", name="Retry")).to_be_visible()

        assert popover(page).get_attribute("aria-modal") != "true"
        assert page.evaluate("() => document.querySelector('dialog:modal') === null")
        blocked: list[str] = page.evaluate(
            "() => [...document.querySelectorAll('[inert], main[aria-hidden], nav[aria-hidden]')]"
            ".map((e) => e.tagName + '#' + e.id)"
        )
        assert blocked == [], f"the page behind the panel stays usable: {blocked}"

        page.keyboard.press("Tab")
        assert focus_is_inside(page, POPOVER)
        for _ in range(MAX_TABS):
            page.keyboard.press("Tab")
            if focus_is_inside(page, "#main") or focus_is_inside(page, "#sidebar"):
                break
        else:
            raise AssertionError("Tab never left the panel for the page (a focus trap)")


def test_panel_closes_on_a_pointer_press_outside(running_app: RunningApp, browser: Browser) -> None:
    with settings_page(browser, running_app, DESKTOP) as page:
        start_task(page, outcome="network")
        wait_server_shows(page, "Failed")
        open_panel(page)
        items(page).first.locator(".task-label").click()  # inside: it stays open
        popover(page).click(position={"x": 4, "y": 4})
        expect(toggle(page)).to_have_attribute("aria-expanded", "true")
        expect(popover(page)).to_be_visible()

        page.locator("#main h1").first.click()
        expect(toggle(page)).to_have_attribute("aria-expanded", "false")
        expect(popover(page)).to_be_hidden()
        assert not focus_is_inside(page, SLOT), "focus is not pulled back to the button"

        open_panel(page)  # and it opens again
        expect(items(page)).to_have_count(1)
        page.mouse.click(5, 300)  # the sidebar's empty space
        expect(popover(page)).to_be_hidden()
        expect(page.locator("#app-status")).to_be_hidden()


def test_panel_closes_when_keyboard_focus_leaves(running_app: RunningApp, browser: Browser) -> None:
    with settings_page(browser, running_app, DESKTOP) as page:
        start_task(page, outcome="network")
        wait_server_shows(page, "Failed")
        reopen(page, running_app)
        tab_to(page, TOGGLE)
        page.keyboard.press("Enter")
        retry = items(page).get_by_role("button", name="Retry")
        expect(retry).to_be_visible()
        page.keyboard.press("Tab")
        expect(retry).to_be_focused()
        expect(popover(page)).to_be_visible()  # moving inside keeps it open
        page.keyboard.press("Shift+Tab")
        expect(toggle(page)).to_be_focused()
        expect(popover(page)).to_be_visible()

        page.keyboard.press("Tab")
        page.keyboard.press("Tab")  # past Retry, the panel's last control
        expect(toggle(page)).to_have_attribute("aria-expanded", "false")
        expect(popover(page)).to_be_hidden()
        assert not focus_is_inside(page, SLOT), "focus stays where the user moved it"
        assert_focus_not_lost(page)

        toggle(page).focus()
        page.keyboard.press("Enter")
        expect(popover(page)).to_be_visible()
        page.keyboard.press("Shift+Tab")  # backwards, out of the top bar's button
        expect(toggle(page)).to_have_attribute("aria-expanded", "false")
        expect(popover(page)).to_be_hidden()


def test_escape_closes_from_anywhere_and_returns_focus_only_from_inside(
    running_app: RunningApp, browser: Browser
) -> None:
    with settings_page(browser, running_app, DESKTOP) as page:
        open_panel(page)
        # Focus is nowhere (as after a click on something that then went away).
        page.evaluate("() => document.activeElement.blur()")
        assert page.evaluate("() => document.activeElement === document.body")
        expect(popover(page)).to_be_visible()
        page.keyboard.press("Escape")
        expect(toggle(page)).to_have_attribute("aria-expanded", "false")
        expect(popover(page)).to_be_hidden()
        assert page.evaluate("() => document.activeElement === document.body"), (
            "focus was not on the button or in the panel, so it is not moved there"
        )

        open_panel(page)
        expect(popover(page).locator("#tasks-empty")).to_have_text(MSG_EMPTY)
        page.keyboard.press("Tab")  # the panel itself: it has no control
        expect(popover(page)).to_be_focused()
        page.keyboard.press("Escape")
        expect(popover(page)).to_be_hidden()
        expect(toggle(page)).to_be_focused()


def test_panel_without_controls_is_a_tab_stop(running_app: RunningApp, browser: Browser) -> None:
    """A list with no button could not be scrolled by keyboard: the panel takes focus itself.
    With a control in it, the first Tab goes to that control."""
    with settings_page(browser, running_app, DESKTOP) as page:
        start_task(page)
        wait_server_shows(page, "Done")
        reopen(page, running_app)
        tab_to(page, TOGGLE)
        page.keyboard.press("Enter")
        expect(items(page)).to_have_count(1)
        expect(items(page).get_by_role("button")).to_have_count(0)
        expect(popover(page)).to_have_attribute("tabindex", "0")
        expect(popover(page)).to_have_role("group")
        expect(popover(page)).to_have_accessible_name("Tasks")
        page.keyboard.press("Tab")
        expect(popover(page)).to_be_focused()
        style: str = popover(page).evaluate("el => getComputedStyle(el).outlineStyle")
        assert style != "none", "the focused panel shows its focus"
        page.keyboard.press("Escape")
        expect(toggle(page)).to_be_focused()

        start_task(page, outcome="network")
        wait_server_shows(page, "Failed")
        reopen(page, running_app)
        tab_to(page, TOGGLE)
        page.keyboard.press("Enter")
        retry = items(page).get_by_role("button", name="Retry")
        expect(retry).to_be_visible()
        page.keyboard.press("Tab")
        expect(retry).to_be_focused()


def test_closed_panel_asks_for_nothing_and_opening_asks_once(
    running_app: RunningApp, browser: Browser
) -> None:
    with settings_page(browser, running_app, DESKTOP) as page:
        asked: list[str] = []
        page.on("request", lambda request: asked.append(urlsplit(request.url).path))
        for _ in range(2):
            answers: int = page.evaluate("() => window.__indicatorAnswers")
            refresh_tasks(page)
            page.wait_for_function("(n) => window.__indicatorAnswers > n", arg=answers)
        start_task(page)
        expect(count(page)).to_have_text(NONE_RUNNING)
        wait_announced(page, SAID_FINISHED)
        assert INDICATOR_URL in asked
        assert PANEL_URL not in asked, "a closed panel fetches nothing"
        assert page.evaluate("() => window.__panelAnswers") == 0

        open_panel(page)
        expect(status_of(items(page).first)).to_have_text("Done")
        assert asked.count(PANEL_URL) == 1, asked
        refetch_panel(page)  # open, it follows the task events
        assert asked.count(PANEL_URL) == 2, asked

        page.keyboard.press("Escape")
        expect(popover(page)).to_be_hidden()
        answers = page.evaluate("() => window.__indicatorAnswers")
        refresh_tasks(page)
        page.wait_for_function("(n) => window.__indicatorAnswers > n", arg=answers)
        assert asked.count(PANEL_URL) == 2, asked


def test_failed_panel_fetch_says_so_in_the_panel(running_app: RunningApp, browser: Browser) -> None:
    with settings_page(browser, running_app, DESKTOP) as page:
        broken = {"on": True}

        def answer(route: Route) -> None:
            if broken["on"]:
                route.fulfill(status=500, content_type="text/plain", body="fictional failure")
            else:
                route.continue_()

        page.route(f"**{PANEL_URL}", answer)
        error = page.locator(f"{POPOVER} {ERROR_LINE}")
        expect(error).to_be_hidden()
        toggle(page).click()
        expect(popover(page)).to_be_visible()
        expect(error).to_be_visible()
        expect(error).to_have_text(MSG_PANEL_ERROR)
        wait_announced(page, MSG_PANEL_ERROR)
        assert "fictional failure" not in popover(page).inner_text()
        # A background refresh is not "that last action": the page-wide banner stays away.
        expect(page.locator("#app-status")).to_be_hidden()

        broken["on"] = False
        page.keyboard.press("Escape")
        toggle(page).click()
        expect(popover(page).locator("#tasks-empty")).to_have_text(MSG_EMPTY)
        expect(error).to_be_hidden()
        assert announced(page).count(MSG_PANEL_ERROR) == 1


# --- tasks in the panel ------------------------------------------------------------------------


def test_progress_has_accessible_name(running_app: RunningApp, browser: Browser) -> None:
    with settings_page(browser, running_app, DESKTOP) as page:
        start_task(page, duration="slow")
        open_panel(page)
        expect(items(page)).to_have_count(1)
        item = items(page).first
        expect(item.locator(".task-label")).to_have_text(LABEL)
        expect(status_of(item)).to_have_text("Running")  # in words, not colour alone
        bar = popover(page).get_by_role("progressbar", name=PROGRESS_NAME, exact=True)
        expect(bar).to_have_count(1)
        expect(bar).to_be_visible()
        expect(bar).to_have_attribute("max", "100")
        assert bar.evaluate("el => el.tagName") == "PROGRESS"
        expect(item.get_by_role("button", name="Cancel")).to_be_visible()
        expect(item.get_by_role("button", name="Retry")).to_have_count(0)
        assert_no_links(item)


def test_progress_increases_while_panel_stays_open(
    running_app: RunningApp, browser: Browser
) -> None:
    """Heartbeats publish no event, so the open panel must fetch the progress itself."""
    with settings_page(browser, running_app, DESKTOP) as page:
        start_task(page, duration="slow")
        open_panel(page)
        bar = popover(page).get_by_role("progressbar", name=PROGRESS_NAME, exact=True)
        expect(bar).to_be_visible()
        first = int(bar.get_attribute("value") or "0")
        page.wait_for_function(
            "([selector, first]) => { const bar = document.querySelector(selector);"
            " return !!bar && Number(bar.getAttribute('value')) > first; }",
            arg=[f"{POPOVER} progress", first],
            timeout=STEP_MS,
        )
        expect(toggle(page)).to_have_attribute("aria-expanded", "true")
        expect(popover(page)).to_be_visible()
        expect(status_of(items(page).first)).to_have_text("Running")
        expect(page.locator("#app-status")).to_be_hidden()


def test_ac_m2_4_cancel_from_panel_ends_cancelled(
    running_app: RunningApp, browser: Browser
) -> None:
    """Keyboard only from the top of the page: open the panel, Cancel. The task ends
    cancelled, with nothing kept from the steps it had done."""
    with settings_page(browser, running_app, DESKTOP) as page:
        start_task(page, duration="slow")
        wait_server_shows(page, "Running")
        reopen(page, running_app)
        expect(count(page)).to_have_text(ONE_RUNNING)

        tab_to(page, TOGGLE)
        page.keyboard.press("Enter")
        expect(items(page)).to_have_count(1)
        item = items(page).first
        cancel = item.get_by_role("button", name="Cancel")
        tab_to_locator(page, cancel)
        page.keyboard.press("Enter")

        expect(status_of(item)).to_have_text("Cancelled", timeout=STEP_MS)
        expect(count(page)).to_have_text(NONE_RUNNING)
        expect(item.get_by_role("button", name="Cancel")).to_have_count(0)
        expect(item.get_by_role("button", name="Retry")).to_be_visible()
        expect(item.locator("progress")).to_have_count(0)
        expect(item.locator('[id$="-result"]')).to_have_count(0)  # partial output discarded
        expect(item.locator('[id$="-reason"]')).to_have_count(0)
        assert_no_links(item)
        expect(toggle(page)).to_have_attribute("aria-expanded", "true")
        assert_focus_not_lost(page)

        wait_announced(page, SAID_CANCELLED)
        refresh_tasks(page)
        said = announced(page)
        assert said.count(SAID_CANCELLED) == 1, said
        assert "cancelled" in server_panel(page).lower()
        expect(page.locator("#app-status")).to_be_hidden()


def test_retry_from_panel_after_failure(running_app: RunningApp, browser: Browser) -> None:
    with settings_page(browser, running_app, DESKTOP) as page:
        start_task(page, duration="slow", outcome="network")
        open_panel(page)
        expect(items(page)).to_have_count(1)
        item = items(page).first
        reason = item.locator('[id$="-reason"]')
        retry = item.get_by_role("button", name="Retry")

        expect(status_of(item)).to_have_text("Failed", timeout=STEP_MS)
        expect(reason).to_have_text(MSG_NETWORK)
        expect(retry).to_be_visible()
        expect(item.get_by_role("button", name="Cancel")).to_have_count(0)
        expect(item.locator("progress")).to_have_count(0)
        expect(count(page)).to_have_text(NONE_RUNNING)
        assert_no_links(item)

        tab_to_locator(page, retry)  # from the indicator button, by keyboard
        page.keyboard.press("Enter")

        # The same task runs again (one failing step of 3 s) and fails again.
        expect(status_of(item)).to_have_text(re.compile(r"^(Queued|Running)$"))
        expect(count(page)).to_have_text(ONE_RUNNING)
        expect(item.locator("progress")).to_be_visible()
        expect(reason).to_have_count(0)
        expect(items(page)).to_have_count(1)

        expect(status_of(item)).to_have_text("Failed", timeout=STEP_MS)
        expect(reason).to_have_text(MSG_NETWORK)
        expect(retry).to_be_visible()
        expect(count(page)).to_have_text(NONE_RUNNING)
        expect(items(page)).to_have_count(1)
        expect(toggle(page)).to_have_attribute("aria-expanded", "true")
        assert_focus_not_lost(page)
        expect(page.locator("#app-status")).to_be_hidden()


def test_done_practice_task_has_no_result_link(running_app: RunningApp, browser: Browser) -> None:
    with settings_page(browser, running_app, DESKTOP) as page:
        start_task(page)
        wait_server_shows(page, "Done")
        open_panel(page)
        item = items(page).first
        expect(status_of(item)).to_have_text("Done")
        assert_no_links(item)
        expect(item.get_by_role("button")).to_have_count(0)
        expect(count(page)).to_have_text(NONE_RUNNING)


# --- focus across refreshes --------------------------------------------------------------------


def refetch_panel(page: Page) -> None:
    """One more ``tasks:resync`` with the panel open, and the panel's answer handled."""
    answers: int = page.evaluate("() => window.__panelAnswers")
    refresh_tasks(page)
    page.wait_for_function("(n) => window.__panelAnswers > n", arg=answers)


def test_identical_refresh_keeps_the_same_nodes_and_focus(
    running_app: RunningApp, browser: Browser
) -> None:
    """A refetch that changes nothing replaces nothing: the very Cancel node that has focus
    is still there (a press under way is not lost), and so is its focus."""
    with settings_page(browser, running_app, DESKTOP) as page:
        start_task(page, duration="slow")
        wait_server_shows(page, "Running")
        reopen(page, running_app)
        tab_to(page, TOGGLE)
        page.keyboard.press("Enter")
        cancel = items(page).get_by_role("button", name="Cancel")
        tab_to_locator(page, cancel)
        focused_id: str = page.evaluate(
            "() => { document.activeElement.__sameNode = true; return document.activeElement.id; }"
        )
        assert focused_id.endswith("-cancel")

        for _ in range(2):
            refetch_panel(page)
            assert page.evaluate(
                "(id) => { const e = document.getElementById(id);"
                " return !!e && e.__sameNode === true && e === document.activeElement; }",
                focused_id,
            ), "the focused Cancel was replaced or lost focus"
        expect(toggle(page)).to_have_attribute("aria-expanded", "true")


def test_focus_stays_on_the_control_with_the_same_id_across_a_real_swap(
    running_app: RunningApp, browser: Browser
) -> None:
    """Retry of a failed task has focus when another task ends: the panel is replaced (the
    node is a new one) and focus is on the new Retry with the same id."""
    with settings_page(browser, running_app, DESKTOP) as page:
        seed_failed_and_running(page)
        reopen(page, running_app)
        tab_to(page, TOGGLE)
        page.keyboard.press("Enter")
        expect(items(page)).to_have_count(2)
        retry = item_with_status(page, "Failed").get_by_role("button", name="Retry")
        tab_to_locator(page, retry)
        focused_id: str = page.evaluate(
            "() => { document.activeElement.__oldNode = true; return document.activeElement.id; }"
        )
        assert focused_id.endswith("-retry")

        # The running task is cancelled from outside the panel, as another tab would do.
        cancel_id = item_with_status(page, "Running").get_by_role("button").get_attribute("id")
        assert cancel_id is not None and cancel_id.endswith("-cancel")
        status: int = page.evaluate(
            """async (taskId) => {
              const token = JSON.parse(document.body.getAttribute("hx-headers"))["X-JP-Token"];
              const response = await fetch("/tasks/" + taskId + "/cancel",
                { method: "POST", headers: { "X-JP-Token": token } });
              return response.status;
            }""",
            cancel_id.removeprefix("task-").removesuffix("-cancel"),
        )
        assert status == 200
        expect(item_with_status(page, "Cancelled")).to_have_count(1, timeout=STEP_MS)
        expect(retry).to_be_focused()
        assert page.evaluate(
            "(id) => { const e = document.getElementById(id);"
            " return e === document.activeElement && e.__oldNode === undefined; }",
            focused_id,
        ), "focus is on the new node with the same id"
        expect(toggle(page)).to_have_attribute("aria-expanded", "true")


def test_focus_moves_to_the_indicator_when_the_focused_control_is_gone(
    running_app: RunningApp, browser: Browser
) -> None:
    """Cancel has focus when the task fails by itself: Cancel is gone, so focus goes to the
    indicator button instead of being dropped on the page."""
    with settings_page(browser, running_app, DESKTOP) as page:
        start_task(page, duration="slow", outcome="network")
        open_panel(page)
        item = items(page).first
        cancel = item.get_by_role("button", name="Cancel")
        tab_to_locator(page, cancel)
        expect(cancel).to_be_focused()

        expect(status_of(item)).to_have_text("Failed", timeout=STEP_MS)
        expect(cancel).to_have_count(0)
        expect(toggle(page)).to_be_focused()
        expect(toggle(page)).to_have_attribute("aria-expanded", "true")


# --- announcements -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "word", "message"),
    [("ok", "Done", SAID_FINISHED), ("network", "Failed", SAID_FAILED)],
    ids=["finished", "failed"],
)
def test_task_finish_announced_once(
    running_app: RunningApp, browser: Browser, outcome: str, word: str, message: str
) -> None:
    with settings_page(browser, running_app, DESKTOP) as page:
        start_task(page, outcome=outcome)  # the panel stays closed
        wait_announced(page, message)
        wait_server_shows(page, word)
        expect(count(page)).to_have_text(NONE_RUNNING)
        for _ in range(3):  # later refreshes say nothing more
            refresh_tasks(page)
        said = announced(page)
        assert said.count(message) == 1, said

        open_panel(page)
        expect(status_of(items(page).first)).to_have_text(word)
        refresh_tasks(page)
        said = announced(page)
        assert said.count(message) == 1, said

        # A tab opened afterwards, and a reload, never saw the task end: nothing to say.
        other = page.context.new_page()
        open_app(other, running_app, SETTINGS)
        other.wait_for_function("() => window.__resyncs > 0")
        refresh_tasks(other)
        assert message not in announced(other)
        page.reload()
        page.wait_for_function("() => window.__resyncs > 0")
        refresh_tasks(page)
        assert message not in announced(page)


def test_task_finish_not_announced_twice_in_another_open_tab(
    running_app: RunningApp, browser: Browser
) -> None:
    with settings_page(browser, running_app, DESKTOP) as page:
        other = page.context.new_page()
        open_app(other, running_app, SETTINGS)
        other.wait_for_function("() => window.__resyncs > 0")

        start_task(page)
        wait_announced(page, SAID_FINISHED)
        expect(count(other)).to_have_text(NONE_RUNNING)
        for tab in (page, other):
            refresh_tasks(tab)
        assert announced(page).count(SAID_FINISHED) == 1
        assert announced(other).count(SAID_FINISHED) <= 1, announced(other)


def test_cancel_is_announced_once_with_the_task_label(
    running_app: RunningApp, browser: Browser
) -> None:
    """The answer to Cancel says only that cancelling began; the end is said once, with the
    task's label. The buttons are described by their task."""
    with settings_page(browser, running_app, DESKTOP) as page:
        start_task(page, duration="slow")
        open_panel(page)
        item = items(page).first
        cancel = item.get_by_role("button", name="Cancel")
        expect(cancel).to_have_accessible_description(LABEL)
        cancel.click()
        expect(status_of(item)).to_have_text("Cancelled", timeout=STEP_MS)
        expect(item.get_by_role("button", name="Retry")).to_have_accessible_description(LABEL)
        wait_announced(page, SAID_CANCELLED)
        refetch_panel(page)
        said = announced(page)
        assert said.count(SAID_CANCELLED) == 1, said
        assert "Task cancelled." not in said, said
        assert [text for text in said if "cancel" in text.lower()] in (
            [SAID_CANCELLING, SAID_CANCELLED],
            [SAID_CANCELLED],  # the task stopped before the answer came
        ), said


def finished_header(*tasks: tuple[str, str, int]) -> str:
    listed = [{"key": key, "message": message, "age": age} for key, message, age in tasks]
    return json.dumps({"tasks:finished": {"tasks": listed}})


def test_first_answer_announces_only_what_just_finished(
    running_app: RunningApp, browser: Browser
) -> None:
    """A browser that has never seen these tasks: its first answer speaks of the one that
    ended seconds ago (it may have ended during a navigation), not of older history; a later
    answer speaks of whatever is new; nothing is said twice, also after a reload."""
    recent = ("fictional-recent:1", "Fictional recent finished.", 3)
    old = ("fictional-old:1", "Fictional old finished.", 11)
    later = ("fictional-later:1", "Fictional later failed. Open Tasks to see why.", 400)
    phase = {"later": False}

    def answer(route: Route) -> None:
        tasks = (later, recent, old) if phase["later"] else (recent, old)
        route.fulfill(
            status=200,
            content_type="text/html; charset=utf-8",
            headers={"HX-Trigger": finished_header(*tasks)},
            body=f'<span id="tasks-count" class="tasks-count">{NONE_RUNNING}</span>',
        )

    with new_page(browser, DESKTOP) as page:
        page.context.add_init_script(INIT_JS)
        page.route(f"**{INDICATOR_URL}", answer)
        open_app(page, running_app, SETTINGS)
        page.wait_for_function("() => window.__indicatorAnswers > 0")
        assert announced(page) == [recent[1]]

        phase["later"] = True
        for _ in range(2):
            answers: int = page.evaluate("() => window.__indicatorAnswers")
            refresh_tasks(page)
            page.wait_for_function("(n) => window.__indicatorAnswers > n", arg=answers)
        said = announced(page)
        assert said == [recent[1], later[1]], said

        page.reload()
        page.wait_for_function("() => window.__indicatorAnswers > 0")
        refresh_tasks(page)
        assert announced(page) == []


# --- AC-M2.9: correct after the stream drops and comes back ------------------------------------


def test_ac_m2_9_panel_correct_after_stream_drop_and_reconnect(
    running_app: RunningApp, browser: Browser
) -> None:
    """The stream drops while a task runs and can't be opened again while the task fails.
    Once it opens, its resync brings the count and the panel up to date: no reload."""
    with settings_page(browser, running_app, DESKTOP) as page:
        refused = {"on": False}

        def answer(route: Route) -> None:
            if refused["on"]:
                route.abort("connectionrefused")
            else:
                route.continue_()

        page.route(f"**{EVENTS}", answer)
        page.evaluate("() => { window.__sameDocument = true; }")
        start_task(page, duration="slow", outcome="network")
        expect(count(page)).to_have_text(ONE_RUNNING)  # live, the panel closed

        refused["on"] = True
        page.evaluate("() => window.__dropStream()")
        wait_server_shows(page, "Failed")  # the server moved on; no event told the page

        def is_stream(response: Response) -> bool:
            return urlsplit(response.url).path == EVENTS and response.status == 200

        with page.expect_response(is_stream, timeout=RECONNECT_MS):
            refused["on"] = False

        expect(count(page)).to_have_text(NONE_RUNNING, timeout=RECONNECT_MS)
        open_panel(page)
        expect(items(page)).to_have_count(1)
        item = items(page).first
        expect(status_of(item)).to_have_text("Failed")
        expect(item.locator('[id$="-reason"]')).to_have_text(MSG_NETWORK)
        expect(item.get_by_role("button", name="Retry")).to_be_visible()
        expect(item.get_by_role("button", name="Cancel")).to_have_count(0)
        assert page.evaluate("() => window.__sameDocument === true"), "the page was reloaded"
        expect(page.locator("#app-status")).to_be_hidden()


# --- layout and accessibility ------------------------------------------------------------------


def test_panel_390x844_and_360_no_horizontal_scroll(
    running_app: RunningApp, browser: Browser
) -> None:
    assert PHONE == {"width": 390, "height": 844}
    assert SMALL_PHONE["width"] == 360
    with settings_page(browser, running_app, PHONE) as page:
        expect(toggle(page)).to_be_visible()
        assert_target_size(toggle(page), "Tasks button")
        seed_failed_and_running(page)
        expect(count(page)).to_have_text(ONE_RUNNING)
        closed = has_no_horizontal_scroll(page)
        assert closed[0], f"390 px with a count: {closed}"
        open_panel(page)
        expect(items(page)).to_have_count(2)
        assert_fits(page, "390 px, panel open")
        running = item_with_status(page, "Running")
        assert_target_size(running.get_by_role("button", name="Cancel"), "Cancel")
        assert_target_size(
            item_with_status(page, "Failed").get_by_role("button", name="Retry"), "Retry"
        )
        # End the running task here, so the next width can start its own.
        running.get_by_role("button", name="Cancel").click()
        expect(item_with_status(page, "Cancelled")).to_have_count(1, timeout=STEP_MS)
        assert_fits(page, "390 px, after a cancel")

    with settings_page(browser, running_app, SMALL_PHONE) as page:
        closed = has_no_horizontal_scroll(page)
        assert closed[0], f"360 px, panel closed: {closed}"
        start_task(page, duration="slow")
        expect(count(page)).to_have_text(ONE_RUNNING)
        open_panel(page)
        expect(items(page)).to_have_count(3)  # failed, cancelled and the one running now
        assert_fits(page, "360 px, panel open")
        assert_target_size(toggle(page), "Tasks button")
        for name in ("Cancel", "Retry"):
            assert_target_size(items(page).get_by_role("button", name=name).first, name)


def assert_one_row(page: Page, what: str) -> None:
    """Menu, the brand and the Tasks button share one row of the top bar."""
    # One read for the three boxes: the page can still be shifting after a task starts,
    # and boxes read one by one could come from different frames.
    bar, menu, tasks = page.evaluate(
        """(selectors) => selectors.map((selector) => {
            const element = document.querySelector(selector);
            if (!element || !element.getClientRects().length) return null;
            const box = element.getBoundingClientRect();
            return { x: box.x, y: box.y, width: box.width, height: box.height };
        })""",
        ["header.topbar", ".sidebar-toggle", f"{SLOT} {TOGGLE}"],
    )
    assert bar is not None and menu is not None and tasks is not None, what
    assert abs((menu["y"] + menu["height"] / 2) - (tasks["y"] + tasks["height"] / 2)) < 2, (
        f"{what}: Menu at y={menu['y']:.0f}, Tasks at y={tasks['y']:.0f}"
    )
    assert tasks["height"] < 60, f"{what}: the Tasks button wraps ({tasks['height']:.0f} px)"
    assert bar["height"] < tasks["height"] + 24, f"{what}: the top bar is {bar['height']:.0f} px"
    assert tasks["x"] + tasks["width"] <= bar["x"] + bar["width"] + 0.5, what


@pytest.mark.parametrize("width", [390, 360])
def test_top_bar_stays_one_row_on_phones(
    running_app: RunningApp, browser: Browser, width: int
) -> None:
    with settings_page(browser, running_app, {"width": width, "height": 844}) as page:
        expect(count(page)).to_have_text(NONE_RUNNING)
        expect(count(page)).to_be_visible()
        expect(
            page.locator(SLOT).get_by_role("button", name=f"Tasks: {NONE_RUNNING}")
        ).to_be_visible()
        assert_one_row(page, f"{width} px, idle")
        start_task(page, duration="slow")
        expect(count(page)).to_have_text(ONE_RUNNING)
        expect(
            page.locator(SLOT).get_by_role("button", name=f"Tasks: {ONE_RUNNING}")
        ).to_be_visible()
        assert_one_row(page, f"{width} px, one running")
        assert has_no_horizontal_scroll(page)[0]
        # The longest count, as the server would send it.
        page.evaluate(
            "() => { document.getElementById('tasks-count').textContent = '3 running, 12 queued'; }"
        )
        assert_one_row(page, f"{width} px, running and queued")
        assert has_no_horizontal_scroll(page)[0]


def test_320_px_has_no_horizontal_scroll(running_app: RunningApp, browser: Browser) -> None:
    with settings_page(browser, running_app, {"width": 320, "height": 700}) as page:
        start_task(page, duration="slow")
        expect(count(page)).to_have_text(ONE_RUNNING)
        assert has_no_horizontal_scroll(page)[0]
        open_panel(page)
        assert_fits(page, "320 px, panel open")


def test_without_javascript_there_is_no_dead_tasks_button(
    running_app: RunningApp, browser: Browser
) -> None:
    with new_page(browser, PHONE, java_script_enabled=False) as page:
        open_app(page, running_app, scripts=False)
        expect(toggle(page)).to_have_count(1)
        expect(toggle(page)).to_be_hidden()
        expect(popover(page)).to_be_hidden()
    with new_page(browser, PHONE) as page:  # the positive control
        open_app(page, running_app)
        expect(toggle(page)).to_be_visible()


AXE_CASES = [
    pytest.param(DESKTOP, "light", id="desktop-light"),
    pytest.param(DESKTOP, "dark", id="desktop-dark"),
    pytest.param(PHONE, "light", id="phone-light"),
    pytest.param(PHONE, "dark", id="phone-dark"),
]


@pytest.mark.parametrize(("viewport", "scheme"), AXE_CASES)
def test_axe_panel(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int], scheme: str
) -> None:
    """The panel open with a running and a failed task: no serious or critical findings."""
    with settings_page(browser, running_app, viewport, scheme=scheme) as page:
        seed_failed_and_running(page)
        open_panel(page)
        expect(item_with_status(page, "Running")).to_have_count(1)
        expect(item_with_status(page, "Failed")).to_have_count(1)
        expect(popover(page).get_by_role("progressbar", name=PROGRESS_NAME)).to_be_visible()
        assert css_token(page, "--color-bg"), "the colour tokens are not loaded"
        assert_no_serious_violations(page)
