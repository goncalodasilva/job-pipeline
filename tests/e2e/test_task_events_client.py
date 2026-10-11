"""The browser's event stream client (M2.18; AC-M2.9; ADR-077 follow-ups).

What ``app/static/js/app.js`` is expected to do on pages with ``<body data-events-url>``:

- Connect: every connection's ``resync`` becomes a ``tasks:resync`` event and every
  ``task-updated`` becomes a ``tasks:updated`` event: bubbling ``CustomEvent`` objects on
  ``document.body`` with NO detail (``detail === null``). Changes can arrive out of commit
  order (M2.7), so a listener refetches; it is given no status it could trust by mistake.
- Reconnect when the stream drops or can't be opened: after 1 s, then 2, 4, 8, 16 and 30 s
  (the cap), with no jitter; the delay goes back to 1 s after a successful connect. The token
  is read again from ``hx-headers`` for every attempt.
- Stop for good on a 403 and show the existing "restarted" message with Reload, and stop
  quietly on an answer that isn't an event stream.
- Pause while the tab is hidden: close the stream on ``visibilitychange`` to hidden, open no
  stream while hidden, connect at once when visible again.
- One stream per browser: the tab that holds a Web Lock reads the stream and passes each
  event on over a ``BroadcastChannel``; the other tabs dispatch the same DOM events, and
  one ``tasks:resync`` when they join. When the leader closes or is hidden, another visible
  tab takes over. Without Web Locks or ``BroadcastChannel`` every visible tab reads its own.

No test waits for real time: the backoff runs on Playwright's page clock (``page.clock``),
paused, so app.js needs no test hook. While the clock is paused nothing here uses in-page
timers; waits poll from Python with a deadline (``tests.waiting.wait_until``). Real task
changes come from the practice task (M2.17). Every value is fictional.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, Request, Route, expect

from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_setup_provider_test import tab_to_locator
from tests.e2e.test_shell import (
    FICTIONAL_STALE_TOKEN,
    RELOAD_TEXT,
    has_no_horizontal_scroll,
)
from tests.running_app import RunningApp
from tests.waiting import wait_until

pytestmark = pytest.mark.e2e

EVENTS = "/events"
DEADLINE_S = 10.0  # each awaited thing normally happens within milliseconds
CLOCK_START = datetime(2026, 4, 7, 9, 0, 0, tzinfo=UTC)
BACKOFF_MS = (1_000, 2_000, 4_000, 8_000, 16_000, 30_000, 30_000, 30_000)
LONG_MS = 120_000  # far beyond the 30 s cap
RESYNC = "event: resync\ndata: {}\n\n"
STREAM_HEADERS = {"content-type": "text/event-stream", "cache-control": "no-store"}
RESTARTED_TEXT = "The app was restarted"
PRACTICE_FORM = 'form[action="/tasks/practice"]'

# Runs before the page's own scripts. Records every tasks:* event that reaches `document`
# (so it bubbled from wherever it was dispatched) and lets a test change what the page
# believes about its visibility (a headless page is always "visible").
RECORD_JS = """
window.__events = [];
for (const name of ["tasks:resync", "tasks:updated"]) {
  document.addEventListener(name, (event) => {
    window.__events.push({
      name,
      onBody: event.target === document.body,
      bubbles: event.bubbles,
      custom: event instanceof CustomEvent,
      detail: event.detail === undefined ? "undefined" : event.detail,
    });
  });
}
window.__setVisibility = (state) => {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
  Object.defineProperty(document, "hidden", { configurable: true, get: () => state === "hidden" });
  document.dispatchEvent(new Event("visibilitychange"));
};
"""
START_HIDDEN_JS = 'window.__setVisibility("hidden");'
# Answers the page's /events requests with streams that say "resync" and then stay open, and
# notes for each whether the page closed it (cancelled the reader or aborted the request).
OPEN_STREAMS_JS = """
window.__streams = [];
const realFetch = window.fetch.bind(window);
window.fetch = function (input, init) {
  if (new URL(String(input), location.href).pathname !== "/events") return realFetch(input, init);
  const entry = { closed: false };
  window.__streams.push(entry);
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode("event: resync\\ndata: {}\\n\\n"));
      const signal = init && init.signal;
      if (signal) {
        signal.addEventListener("abort", () => {
          entry.closed = true;
          try { controller.error(new DOMException("Aborted", "AbortError")); } catch (e) {}
        });
      }
    },
    cancel() { entry.closed = true; },
  });
  const headers = { "content-type": "text/event-stream" };
  return Promise.resolve(new Response(stream, { status: 200, headers }));
};
"""
NO_WEB_LOCKS_JS = """
Object.defineProperty(Navigator.prototype, "locks", { configurable: true, get: () => undefined });
"""
NO_BROADCAST_CHANNEL_JS = "delete window.BroadcastChannel; window.BroadcastChannel = undefined;"
# Web Locks exist but refuse to work (as in a context the browser doesn't trust).
LOCKS_REJECT_JS = """
navigator.locks.request = function () {
  return Promise.reject(new DOMException("fictional refusal", "SecurityError"));
};
"""
# The page has no launch token by the time app.js runs (deferred scripts run after parsing;
# the observer's callback runs as soon as the parser has made <body>).
EMPTY_HX_HEADERS_JS = """
new MutationObserver((records, observer) => {
  if (!document.body) return;
  document.body.setAttribute("hx-headers", "{}");
  observer.disconnect();
}).observe(document, { childList: true, subtree: true });
"""
ROUND_TRIP_JS = "() => fetch('/health', { cache: 'no-store' }).then((response) => response.status)"


# --- helpers -----------------------------------------------------------------------------------


@dataclass
class Tabs:
    """One browser (a context) with every ``/events`` request its tabs made."""

    context: BrowserContext
    app: RunningApp
    requests: list[Request] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def new_tab(self, *scripts: str) -> Page:
        page = self.context.new_page()
        page.add_init_script(RECORD_JS)
        for script in scripts:
            page.add_init_script(script)
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        return page

    def open(self, page: Page, path: str = "/") -> Page:
        response = page.goto(self.app.base_url + path)
        assert response is not None and response.status == 200, path
        until(page, "htmx loaded", "() => typeof window.htmx !== 'undefined'")
        return page

    def streams(self, page: Page | None = None) -> list[Request]:
        return [r for r in self.requests if page is None or r.frame.page == page]

    def count(self) -> int:
        """How many ``/events`` requests so far. The sync API hands out request events only
        while a Playwright call runs, so this asks an open tab something first."""
        for page in self.context.pages:
            if not page.is_closed():
                page.evaluate("() => 0")
                break
        return len(self.requests)


@contextmanager
def browser_with_tabs(
    browser: Browser, app: RunningApp, viewport: dict[str, int] | None = None
) -> Iterator[Tabs]:
    size = viewport or DESKTOP
    phone = size["width"] < 768
    context = browser.new_context(viewport=size, is_mobile=phone, has_touch=phone)  # type: ignore[arg-type]
    tabs = Tabs(context=context, app=app)

    def note(request: Request) -> None:
        if urlsplit(request.url).path == EVENTS:
            tabs.requests.append(request)

    context.on("request", note)
    try:
        yield tabs
    finally:
        context.close()


def pause_clock(page: Page) -> None:
    """Fake timers from the first script on, standing still until the test moves them."""
    page.clock.install(time=CLOCK_START)
    page.clock.pause_at(CLOCK_START)


def poll(what: str, check: Callable[[], Any]) -> Any:
    return wait_until(check, timeout=DEADLINE_S, message=what)


def until(page: Page, what: str, expression: str) -> None:
    """Wait for a page condition by asking from Python (works while the page clock stands)."""
    poll(what, lambda: page.evaluate(expression))


def round_trip(page: Page) -> None:
    """One request to the server and back: anything the page was about to do after its last
    network event has had its turn. Used before asserting that something did NOT happen."""
    assert page.evaluate(ROUND_TRIP_JS) == 200


def seen(page: Page, name: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = page.evaluate("() => window.__events")
    return [event for event in events if event["name"] == name]


def wait_seen(page: Page, name: str, count: int) -> None:
    poll(f"{count} {name} event(s) on the page", lambda: len(seen(page, name)) >= count)


def assert_plain_body_events(page: Page) -> None:
    events: list[dict[str, Any]] = page.evaluate("() => window.__events")
    assert events, "the page saw events"
    for event in events:
        assert event["onBody"] and event["bubbles"] and event["custom"], event
        assert event["detail"] is None, f"no detail (listeners refetch): {event}"


def wait_streams(tabs: Tabs, count: int) -> None:
    poll(f"{count} /events request(s) in this browser", lambda: tabs.count() >= count)


def advance(page: Page, milliseconds: int) -> None:
    page.clock.run_for(milliseconds)


def assert_no_retry_before(tabs: Tabs, page: Page, delay_ms: int) -> None:
    """Move the clock to 1 ms before ``delay_ms``: no new request; then the last 1 ms: one."""
    made = len(tabs.requests)
    advance(page, delay_ms - 1)
    round_trip(page)
    assert len(tabs.requests) == made, f"retried sooner than {delay_ms} ms"
    advance(page, 1)
    wait_streams(tabs, made + 1)
    round_trip(page)
    assert len(tabs.requests) == made + 1, "one retry per delay"


class Answers:
    """Decides how each ``/events`` request of a page is answered."""

    def __init__(self, page: Page, mode: str) -> None:
        self.mode = mode
        page.route(f"**{EVENTS}", self._handle)

    def _handle(self, route: Route) -> None:
        if self.mode == "refuse":  # the app is down: no connection
            route.abort("connectionrefused")
        elif self.mode == "drop":  # a stream that says resync and then ends
            route.fulfill(status=200, headers=STREAM_HEADERS, body=RESYNC)
        elif self.mode == "html":  # not a stream
            route.fulfill(
                status=200, headers={"content-type": "text/html"}, body="<p>event: resync</p>"
            )
        else:  # "server": the real app
            route.continue_()


def start_practice(page: Page) -> None:
    """Start a quick practice task from Settings: real task changes on the stream."""
    page.locator(PRACTICE_FORM).get_by_role("button", name="Start").click()
    expect(page.locator("#practice-status")).to_have_text("Practice task started.")


# --- AC-M2.9: reconnect with backoff, resync on every connect ----------------------------------


def test_reader_reconnects_and_dispatches_resync(running_app: RunningApp, browser: Browser) -> None:
    """The stream can't be opened (1 s, then 2 s, then 4 s between attempts), then opens and
    drops (resync; the delay is back at 1 s), then stays up (resync again, no more attempts)."""
    with browser_with_tabs(browser, running_app) as tabs:
        page = tabs.new_tab()
        pause_clock(page)
        answers = Answers(page, "refuse")

        tabs.open(page)
        wait_streams(tabs, 1)
        round_trip(page)
        assert seen(page, "tasks:resync") == []
        assert_no_retry_before(tabs, page, 1_000)
        assert_no_retry_before(tabs, page, 2_000)

        answers.mode = "drop"
        assert_no_retry_before(tabs, page, 4_000)
        wait_seen(page, "tasks:resync", 1)

        answers.mode = "server"
        assert_no_retry_before(tabs, page, 1_000)  # reset by the successful connect
        wait_seen(page, "tasks:resync", 2)
        token = page.evaluate("() => JSON.parse(document.body.getAttribute('hx-headers'))")
        assert tabs.requests[-1].all_headers()["x-jp-token"] == token["X-JP-Token"]

        made = len(tabs.requests)
        advance(page, LONG_MS)
        round_trip(page)
        assert len(tabs.requests) == made == 5, "a healthy stream is left alone"
        assert len(seen(page, "tasks:resync")) == 2, "one tasks:resync per connection"
        assert_plain_body_events(page)
        expect(page.locator("#app-status")).to_be_hidden()
        assert tabs.errors == []


def test_reader_backoff_doubles_to_30_seconds_and_stops_on_a_non_stream(
    running_app: RunningApp, browser: Browser
) -> None:
    with browser_with_tabs(browser, running_app) as tabs:
        page = tabs.new_tab()
        pause_clock(page)
        answers = Answers(page, "refuse")

        tabs.open(page)
        wait_streams(tabs, 1)
        for delay in BACKOFF_MS:
            assert_no_retry_before(tabs, page, delay)
        expect(page.locator("#app-status")).to_be_hidden()  # retrying is not worth a banner

        answers.mode = "html"  # an answer that isn't an event stream: stop, quietly
        assert_no_retry_before(tabs, page, 30_000)
        made = len(tabs.requests)
        advance(page, LONG_MS)
        round_trip(page)
        assert len(tabs.requests) == made, "no retry after an answer that isn't a stream"
        assert seen(page, "tasks:resync") == [], "its text is not read as events"
        expect(page.locator("#app-status")).to_be_hidden()
        assert tabs.errors == []


# --- a stale token: stop and say so --------------------------------------------------------------


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])
def test_stale_token_on_reconnect_shows_restart_message(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int]
) -> None:
    """The stream drops and the page's token is no longer the app's (the app restarted): the
    reconnect is refused with 403. The reader stops and the page shows the restart message with
    Reload, which works by keyboard and brings the stream back."""
    with browser_with_tabs(browser, running_app, viewport) as tabs:
        page = tabs.new_tab()
        pause_clock(page)
        answers = Answers(page, "drop")
        tabs.open(page)
        wait_seen(page, "tasks:resync", 1)
        expect(page.locator("#app-status")).to_be_hidden()

        page.evaluate(
            "(token) => document.body.setAttribute('hx-headers',"
            " JSON.stringify({'X-JP-Token': token}))",
            FICTIONAL_STALE_TOKEN,
        )
        answers.mode = "server"
        with page.expect_response(lambda r: urlsplit(r.url).path == EVENTS) as refused:
            advance(page, 1_000)
        assert refused.value.status == 403

        expect(page.locator("#app-status")).to_be_visible()
        expect(page.locator("#app-status-message")).to_contain_text(RESTARTED_TEXT)
        reload = page.get_by_role("button", name=RELOAD_TEXT, exact=True)
        expect(reload).to_be_visible()
        made = len(tabs.requests)
        advance(page, LONG_MS)  # also lets the announcement's short timer run
        round_trip(page)
        assert len(tabs.requests) == made == 2, "no retry after a 403"
        expect(page.locator("#announcer")).to_contain_text(RESTARTED_TEXT)
        assert len(seen(page, "tasks:resync")) == 1

        page.clock.resume()  # nothing is scheduled any more; axe needs timers
        assert_no_serious_violations(page)
        if viewport == PHONE:
            assert viewport == {"width": 390, "height": 844}
            fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
            assert fits, f"scrollWidth {scroll_width} > {inner_width}"

        # Keyboard only: Tab to Reload, Enter. The fresh page has the app's token again.
        tab_to_locator(page, reload)
        with page.expect_navigation():
            page.keyboard.press("Enter")
        wait_streams(tabs, 3)
        wait_seen(page, "tasks:resync", 1)  # counted afresh on the reloaded page
        expect(page.locator("#app-status")).to_be_hidden()
        assert tabs.errors == []


# --- hidden tabs hold no stream ------------------------------------------------------------------


def stream_count(page: Page) -> int:
    count: int = page.evaluate("() => window.__streams.length")
    return count


def test_hidden_tab_closes_stream_and_reconnects_when_visible(
    running_app: RunningApp, browser: Browser
) -> None:
    with browser_with_tabs(browser, running_app) as tabs:
        page = tabs.new_tab(OPEN_STREAMS_JS)
        pause_clock(page)
        tabs.open(page)
        wait_seen(page, "tasks:resync", 1)
        assert stream_count(page) == 1
        assert page.evaluate("() => window.__streams[0].closed") is False

        page.evaluate("() => window.__setVisibility('hidden')")

        until(page, "the hidden tab closed its stream", "() => window.__streams[0].closed")
        advance(page, LONG_MS)
        round_trip(page)
        assert stream_count(page) == 1, "no reconnect while hidden, however long"
        expect(page.locator("#app-status")).to_be_hidden()

        page.evaluate("() => window.__setVisibility('visible')")

        until(page, "a new stream once visible", "() => window.__streams.length === 2")
        wait_seen(page, "tasks:resync", 2)
        assert page.evaluate("() => window.__streams[1].closed") is False
        advance(page, LONG_MS)
        round_trip(page)
        assert stream_count(page) == 2
        assert len(seen(page, "tasks:resync")) == 2
        assert tabs.errors == []


def test_tab_loaded_hidden_opens_no_stream_until_visible(
    running_app: RunningApp, browser: Browser
) -> None:
    """A tab opened in the background (a middle click) waits until the user looks at it."""
    with browser_with_tabs(browser, running_app) as tabs:
        page = tabs.new_tab(OPEN_STREAMS_JS, START_HIDDEN_JS)
        pause_clock(page)
        tabs.open(page)
        assert page.evaluate("() => document.visibilityState") == "hidden"
        advance(page, LONG_MS)
        round_trip(page)
        assert stream_count(page) == 0, "a hidden tab opens no stream"
        assert seen(page, "tasks:resync") == []

        page.evaluate("() => window.__setVisibility('visible')")

        until(page, "a stream once visible", "() => window.__streams.length === 1")
        wait_seen(page, "tasks:resync", 1)
        assert tabs.errors == []


# --- ADR-077: one stream per browser -------------------------------------------------------------


def test_two_tabs_share_one_stream_and_both_get_task_updates(
    running_app: RunningApp, browser: Browser
) -> None:
    with browser_with_tabs(browser, running_app) as tabs:
        first = tabs.open(tabs.new_tab(), "/")
        wait_seen(first, "tasks:resync", 1)
        second = tabs.open(tabs.new_tab(), "/settings")
        wait_seen(second, "tasks:resync", 1)  # a follower resyncs when it joins
        third = tabs.open(tabs.new_tab(), "/inbox")
        wait_seen(third, "tasks:resync", 1)
        for page in (first, second, third):
            round_trip(page)
        assert len(tabs.requests) == 1, "one /events stream for the whole browser"
        assert len(tabs.streams(first)) == 1, "held by the first tab (the leader)"

        start_practice(second)

        for page in (first, second, third):
            wait_seen(page, "tasks:updated", 1)
            assert_plain_body_events(page)
            round_trip(page)
        assert len(tabs.requests) == 1, "still one stream"
        assert tabs.errors == []


def test_leader_tab_close_hands_stream_to_another_tab(
    running_app: RunningApp, browser: Browser
) -> None:
    with browser_with_tabs(browser, running_app) as tabs:
        first = tabs.open(tabs.new_tab(), "/")
        wait_seen(first, "tasks:resync", 1)
        second = tabs.open(tabs.new_tab(), "/settings")
        wait_seen(second, "tasks:resync", 1)
        round_trip(second)
        assert len(tabs.requests) == 1 and len(tabs.streams(first)) == 1
        joined = len(seen(second, "tasks:resync"))

        first.close()

        wait_streams(tabs, 2)
        assert len(tabs.streams(second)) == 1, "the remaining tab opened the stream"
        wait_seen(second, "tasks:resync", joined + 1)  # a new connection: resync
        start_practice(second)
        wait_seen(second, "tasks:updated", 1)
        round_trip(second)
        assert len(tabs.requests) == 2
        assert tabs.errors == []


def test_hidden_leader_hands_stream_to_a_visible_tab(
    running_app: RunningApp, browser: Browser
) -> None:
    """Only one tab of a window is visible at a time, so a leader that kept the stream while
    hidden would starve the tab the user is looking at: a hidden leader gives the stream up
    and a visible tab takes it. Back in view, the first tab follows."""
    with browser_with_tabs(browser, running_app) as tabs:
        first = tabs.open(tabs.new_tab(), "/")
        wait_seen(first, "tasks:resync", 1)
        second = tabs.open(tabs.new_tab(), "/settings")
        wait_seen(second, "tasks:resync", 1)
        round_trip(second)
        assert len(tabs.requests) == 1 and len(tabs.streams(first)) == 1
        joined = len(seen(second, "tasks:resync"))

        first.evaluate("() => window.__setVisibility('hidden')")

        wait_streams(tabs, 2)
        assert len(tabs.streams(second)) == 1, "the visible tab took the stream over"
        wait_seen(second, "tasks:resync", joined + 1)

        first.evaluate("() => window.__setVisibility('visible')")
        start_practice(second)
        for page in (first, second):
            wait_seen(page, "tasks:updated", 1)
            round_trip(page)
        assert len(tabs.requests) == 2, "the first tab follows; it opens no second stream"
        assert tabs.errors == []


@pytest.mark.parametrize(
    "missing", [NO_WEB_LOCKS_JS, NO_BROADCAST_CHANNEL_JS], ids=["no_web_locks", "no_channel"]
)
def test_without_locks_or_channel_each_tab_reads_its_own_stream(
    running_app: RunningApp, browser: Browser, missing: str
) -> None:
    with browser_with_tabs(browser, running_app) as tabs:
        first = tabs.open(tabs.new_tab(missing), "/")
        second = tabs.open(tabs.new_tab(missing), "/settings")
        for page in (first, second):
            wait_seen(page, "tasks:resync", 1)
            round_trip(page)
        assert len(tabs.streams(first)) == 1 and len(tabs.streams(second)) == 1

        start_practice(second)

        for page in (first, second):
            wait_seen(page, "tasks:updated", 1)
            assert_plain_body_events(page)
            round_trip(page)
            assert len(seen(page, "tasks:resync")) == 1, "one connection each"
        assert len(tabs.requests) == 2, "one stream per tab, no more"
        assert tabs.errors == []


def test_tab_without_a_token_does_not_keep_the_stream_from_another_tab(
    running_app: RunningApp, browser: Browser
) -> None:
    """A leader that can't even ask (its page has no token) stops at once, and must let go of
    the lock: the next tab opens the stream."""
    with browser_with_tabs(browser, running_app) as tabs:
        first = tabs.open(tabs.new_tab(EMPTY_HX_HEADERS_JS), "/")
        assert first.evaluate("() => document.body.getAttribute('hx-headers')") == "{}"
        round_trip(first)
        assert len(tabs.requests) == 0, "no token, no request"
        expect(first.locator("#app-status")).to_be_hidden()

        second = tabs.open(tabs.new_tab(), "/settings")

        wait_streams(tabs, 1)
        assert len(tabs.streams(second)) == 1, "the second tab reads the stream"
        start_practice(second)
        wait_seen(second, "tasks:updated", 1)
        round_trip(second)
        assert len(tabs.requests) == 1
        assert tabs.errors == []


def test_when_the_lock_request_is_refused_each_tab_reads_its_own_stream(
    running_app: RunningApp, browser: Browser
) -> None:
    with browser_with_tabs(browser, running_app) as tabs:
        first = tabs.open(tabs.new_tab(LOCKS_REJECT_JS), "/")
        second = tabs.open(tabs.new_tab(LOCKS_REJECT_JS), "/settings")
        for page in (first, second):
            wait_seen(page, "tasks:resync", 1)
            round_trip(page)
        assert len(tabs.streams(first)) == 1 and len(tabs.streams(second)) == 1

        start_practice(second)

        for page in (first, second):
            wait_seen(page, "tasks:updated", 1)
            assert_plain_body_events(page)
            round_trip(page)
            assert len(seen(page, "tasks:resync")) == 1, "one connection each, nothing shared"
        assert len(tabs.requests) == 2, "one stream per tab, no more"
        assert tabs.errors == []


def test_tab_showing_the_restart_message_dispatches_nothing_more(
    running_app: RunningApp, browser: Browser
) -> None:
    """A tab that stopped (its token is stale; it shows the restart message) is done until it
    is reloaded: events another tab passes on are not dispatched there, so nothing on it
    refetches with the stale token."""
    with browser_with_tabs(browser, running_app) as tabs:
        first = tabs.open(tabs.new_tab(), "/")
        wait_seen(first, "tasks:resync", 1)
        stale = tabs.open(tabs.new_tab(), "/inbox")
        wait_seen(stale, "tasks:resync", 1)
        stale.evaluate(
            "(token) => document.body.setAttribute('hx-headers',"
            " JSON.stringify({'X-JP-Token': token}))",
            FICTIONAL_STALE_TOKEN,
        )

        first.close()  # the stale tab takes the stream over and is refused

        expect(stale.locator("#app-status")).to_be_visible()
        expect(stale.locator("#app-status-message")).to_contain_text(RESTARTED_TEXT)
        assert len(tabs.streams(stale)) == 1
        before = stale.evaluate("() => window.__events.length")

        fresh = tabs.open(tabs.new_tab(), "/settings")  # leads now: its resync is passed on
        wait_streams(tabs, 3)
        wait_seen(fresh, "tasks:resync", 1)
        start_practice(fresh)
        wait_seen(fresh, "tasks:updated", 1)
        assert len(tabs.streams(fresh)) == 1, "the stopped tab had let go of the lock"

        round_trip(stale)
        assert stale.evaluate("() => window.__events.length") == before, (
            "a stopped tab dispatches nothing"
        )
        assert len(tabs.requests) == 3
        assert tabs.errors == []
