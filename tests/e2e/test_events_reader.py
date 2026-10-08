"""The browser's event stream reader (M2.1; ADR-076 item 1; groundwork for AC-M2.9).

``app/static/js/app.js`` reads ``GET /events`` with ``fetch`` (an ``EventSource`` can't send
the ``X-JP-Token`` header), but only on pages whose ``<body>`` has ``data-events-url``: the
app's pages, not the setup guide. It takes the token from the body's ``hx-headers``, turns each
``resync`` event into a bubbling ``tasks:resync`` event on ``document.body``, and on a refused
or wrong response stops without a sound (reconnecting is M2.18). The reader is invisible, so
there is no keyboard path or axe scan of its own: the existing page scans run with a stream open.
The page-wide checks run on a desktop and a phone viewport. Every value here is fictional.

An open stream keeps the page from ever being "network idle": tests here wait on conditions
(``wait_for_function``, request events) and never on ``networkidle``.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Page, Request, Route, expect

from tests.e2e.test_shell import open_app
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e

# Registered on `document` at script start, so it sees every bubbling event from the body.
RECORD_RESYNC = """
window.__resync = 0;
document.addEventListener("tasks:resync", () => { window.__resync += 1; });
"""
READ_HX_HEADERS_JS = "() => JSON.parse(document.body.getAttribute('hx-headers'))['X-JP-Token']"
WAIT_MS = 10_000  # each awaited thing normally happens within milliseconds
AFTER_A_TURN = "() => new Promise((resolve) => setTimeout(() => resolve(true), 0))"
APP_PAGE = "/"
GUIDE_PAGES = ("/setup/welcome", "/setup/no-such-step")
PAGE_FIXTURES = ["desktop_page", "phone_page"]


@dataclass
class Seen:
    """Requests and page errors a page produced."""

    events: list[Request] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def watch(page: Page) -> Seen:
    seen = Seen()

    def on_request(request: Request) -> None:
        seen.urls.append(request.url)
        if urlsplit(request.url).path == "/events":
            seen.events.append(request)

    page.on("request", on_request)
    page.on("pageerror", lambda error: seen.errors.append(str(error)))
    page.add_init_script(RECORD_RESYNC)
    return seen


def wait_until(page: Page, expression: str) -> None:
    page.wait_for_function(expression, timeout=WAIT_MS)


def resyncs(page: Page) -> int:
    count = page.evaluate("() => window.__resync")
    assert isinstance(count, int)
    return count


# --- the real stream: token header, resync event -----------------------------------------------


@pytest.mark.parametrize("page_fixture", PAGE_FIXTURES)
def test_page_receives_resync_event_with_token_header(
    running_app: RunningApp, request: pytest.FixtureRequest, page_fixture: str
) -> None:
    page: Page = request.getfixturevalue(page_fixture)
    seen = watch(page)

    open_app(page, running_app, APP_PAGE)
    wait_until(page, "() => window.__resync >= 1")

    token = page.evaluate(READ_HX_HEADERS_JS)
    (stream_request,) = seen.events
    sent = stream_request.all_headers()
    assert stream_request.method == "GET"
    assert stream_request.resource_type == "fetch"
    assert sent["x-jp-token"] == token, "the reader sends the page's token as a header"
    assert "text/event-stream" in sent["accept"]
    assert resyncs(page) == 1, "one connection, one resync event"
    assert seen.errors == []


def test_token_never_in_events_url(running_app: RunningApp, desktop_page: Page) -> None:
    seen = watch(desktop_page)

    open_app(desktop_page, running_app, APP_PAGE)
    wait_until(desktop_page, "() => window.__resync >= 1")

    token = desktop_page.evaluate(READ_HX_HEADERS_JS)
    assert isinstance(token, str) and token
    (stream_request,) = seen.events
    assert stream_request.url == f"{running_app.base_url}/events", "no query string, no fragment"
    assert seen.urls, "the page made requests"
    for url in seen.urls:
        assert token not in url, f"the launch token is in a URL: {urlsplit(url).path}"


# --- which pages run the reader ----------------------------------------------------------------


def test_guide_pages_do_not_open_event_stream(running_app: RunningApp, desktop_page: Page) -> None:
    """The guide pages (and its not-found page) make no ``/events`` request; the app page
    visited afterwards makes the only one, which also shows the reader works in this run."""
    seen = watch(desktop_page)

    for path in GUIDE_PAGES:
        desktop_page.goto(running_app.base_url + path)  # the not-found page is a 404
        wait_until(desktop_page, "() => typeof window.htmx !== 'undefined'")
        expect(desktop_page.locator("body")).not_to_have_attribute("data-events-url", "/events")
        assert resyncs(desktop_page) == 0

    open_app(desktop_page, running_app, APP_PAGE)
    wait_until(desktop_page, "() => window.__resync >= 1")

    (stream_request,) = seen.events
    referer = stream_request.all_headers()["referer"]
    assert urlsplit(referer).path == APP_PAGE, "the one stream was opened by the app page"


# --- what the reader makes of what it reads ----------------------------------------------------


def fulfil(page: Page, *, status: int, headers: dict[str, str], body: str) -> None:
    """Answer the page's ``/events`` request with a canned response instead of the server."""

    def handle(route: Route) -> None:
        route.fulfill(status=status, headers=headers, body=body)

    page.route("**/events", handle)


def test_reader_dispatches_one_event_per_resync_and_ignores_everything_else(
    running_app: RunningApp, desktop_page: Page
) -> None:
    """Comments, other event names, nameless messages and noise are ignored; both line
    endings the SSE format allows are understood."""
    seen = watch(desktop_page)
    body = (
        ": keepalive\n\n"
        "event: other\ndata: {}\n\n"
        "data: nameless\n\n"
        "event: resync\ndata: {}\n\n"
        "garbage without a colon\n\n"
        "event: resync\r\ndata: {}\r\n\r\n"
        "event: resynchronise\ndata: {}\n\n"
    )
    fulfil(
        desktop_page,
        status=200,
        headers={"content-type": "text/event-stream", "cache-control": "no-store"},
        body=body,
    )

    open_app(desktop_page, running_app, APP_PAGE)
    wait_until(desktop_page, "() => window.__resync >= 2")
    desktop_page.evaluate(AFTER_A_TURN)

    assert resyncs(desktop_page) == 2
    assert seen.errors == []


# name -> (status, headers, body, the browser event that ends the request). A fulfilled redirect
# that is not followed (`redirect: "manual"`) ends as a failed request in Chromium.
REFUSED_RESPONSES = {
    "forbidden": (403, {"content-type": "text/plain"}, "Forbidden.", "requestfinished"),
    "server_error": (
        500,
        {"content-type": "text/plain"},
        "event: resync\ndata: {}\n\n",
        "requestfinished",
    ),
    "unavailable_stream": (
        503,
        {"content-type": "text/event-stream"},
        "event: resync\ndata: {}\n\n",
        "requestfinished",
    ),
    "html_page": (
        200,
        {"content-type": "text/html"},
        "event: resync\ndata: {}\n\n",
        "requestfinished",
    ),
    "json": (200, {"content-type": "application/json"}, '{"event": "resync"}', "requestfinished"),
    "no_content_type": (200, {}, "event: resync\ndata: {}\n\n", "requestfinished"),
    "redirect": (
        303,
        {"location": "/setup/welcome", "content-type": "text/plain"},
        "",
        "requestfailed",
    ),
}


@pytest.mark.parametrize("name", REFUSED_RESPONSES)
def test_reader_stops_quietly_on_a_refused_or_wrong_response(
    running_app: RunningApp, desktop_page: Page, name: str
) -> None:
    """Not OK, or not an event stream: no event, no banner, no error, no second request (and
    no redirect followed). Reconnecting and telling the user come with M2.18."""
    status, headers, body, ended_by = REFUSED_RESPONSES[name]
    seen = watch(desktop_page)
    fulfil(desktop_page, status=status, headers=headers, body=body)

    with desktop_page.expect_event(
        ended_by,
        predicate=lambda request: urlsplit(request.url).path == "/events",
        timeout=WAIT_MS,
    ):
        open_app(desktop_page, running_app, APP_PAGE)
    desktop_page.evaluate(AFTER_A_TURN)

    assert resyncs(desktop_page) == 0, "text in a refused or wrong response is not read"
    assert seen.errors == []
    assert len(seen.events) == 1, "no retry"
    assert not any(urlsplit(url).path == "/setup/welcome" for url in seen.urls), (
        "a redirect is not followed"
    )
    expect(desktop_page.locator("#app-status")).to_be_hidden()


# --- how the reader asks, and when it reads the token -------------------------------------------

# Wraps `fetch` to note the options the page passes for `/events`, then calls the real one.
RECORD_EVENTS_FETCH_OPTIONS = """
window.__eventsFetch = null;
const realFetch = window.fetch.bind(window);
window.fetch = function (input, init) {
  if (new URL(String(input), location.href).pathname === "/events") {
    window.__eventsFetch = {
      input: String(input), mode: init.mode, credentials: init.credentials, cache: init.cache,
      redirect: init.redirect, headers: Object.assign({}, init.headers),
    };
  }
  return realFetch(input, init);
};
"""
# Counts the reads of `hx-headers` (which holds the token) that come from app.js itself; htmx
# reads it too, when it sends a request, so the call stack tells the two apart.
RECORD_APP_JS_HEADER_READS = """
window.__appJsTokenReads = 0;
const realGetAttribute = Element.prototype.getAttribute;
Element.prototype.getAttribute = function (name) {
  if (name === "hx-headers" && new Error().stack.includes("/static/js/app.js")) {
    window.__appJsTokenReads += 1;
  }
  return realGetAttribute.call(this, name);
};
"""


def test_reader_fetch_is_same_origin_without_credentials_cache_or_redirects(
    running_app: RunningApp, desktop_page: Page
) -> None:
    desktop_page.add_init_script(RECORD_EVENTS_FETCH_OPTIONS)
    seen = watch(desktop_page)

    open_app(desktop_page, running_app, APP_PAGE)
    wait_until(desktop_page, "() => window.__resync >= 1")

    token = desktop_page.evaluate(READ_HX_HEADERS_JS)
    assert desktop_page.evaluate("() => window.__eventsFetch") == {
        "input": "/events",
        "mode": "same-origin",
        "credentials": "omit",
        "cache": "no-store",
        "redirect": "manual",
        "headers": {"X-JP-Token": token, "Accept": "text/event-stream"},
    }
    assert seen.errors == []


def test_app_js_reads_the_token_only_when_it_starts_the_reader(
    running_app: RunningApp, desktop_page: Page
) -> None:
    desktop_page.add_init_script(RECORD_APP_JS_HEADER_READS)
    watch(desktop_page)

    # A guide page has no data-events-url: nothing to start, so nothing reads the token.
    desktop_page.goto(running_app.base_url + GUIDE_PAGES[0])
    wait_until(desktop_page, "() => typeof window.htmx !== 'undefined'")
    assert desktop_page.evaluate("() => window.__appJsTokenReads") == 0

    # The positive control: on an app page the reader starts, and it does read it.
    open_app(desktop_page, running_app, APP_PAGE)
    wait_until(desktop_page, "() => window.__resync >= 1")
    assert desktop_page.evaluate("() => window.__appJsTokenReads") >= 1


# --- chunk boundaries and the size cap -----------------------------------------------------------

# Answers the page's /events request with CHUNKS (a JSON list of latin-1 strings, one per byte),
# handing out one chunk per read. highWaterMark 0 means a chunk is made only when the reader asks
# for it, so `__drained` (the reader asked after the last chunk) shows every earlier chunk has been
# handled, and `__cancelled` shows the reader gave up on the stream.
CHUNKED_EVENTS_JS = """
const chunks = __CHUNKS__.map((text) => Uint8Array.from(text, (c) => c.charCodeAt(0)));
const realFetch = window.fetch.bind(window);
window.__drained = false;
window.__cancelled = false;
window.fetch = function (input, init) {
  if (new URL(String(input), location.href).pathname !== "/events") return realFetch(input, init);
  let next = 0;
  const stream = new ReadableStream({
    pull(controller) {
      if (next < chunks.length) {
        controller.enqueue(chunks[next]);
        next += 1;
      } else {
        window.__drained = true;
        controller.close();
      }
    },
    cancel() { window.__cancelled = true; },
  }, { highWaterMark: 0 });
  const headers = { "content-type": "text/event-stream" };
  return Promise.resolve(new Response(stream, { status: 200, headers }));
};
"""
RESYNC_MESSAGE = b"event: resync\ndata: {}\n\n"
CAP_BYTES = 64 * 1024
COMMENT_LINE = b": " + b"x" * 1022 + b"\n"  # 1 KiB; lines like this never make a blank line


def comment_lines(size: int) -> bytes:
    """At least ``size`` bytes of complete comment lines: text without a blank line."""
    return COMMENT_LINE * (size // len(COMMENT_LINE) + 1)


def deliver_in_chunks(page: Page, chunks: Sequence[bytes]) -> None:
    """Make the page's own ``/events`` request come back as these chunks, not from the server."""
    payload = json.dumps([chunk.decode("latin-1") for chunk in chunks])
    page.add_init_script(CHUNKED_EVENTS_JS.replace("__CHUNKS__", payload))


def flag(page: Page, name: str) -> bool:
    return bool(page.evaluate(f"() => window.{name}"))


# name -> (the chunks the stream hands out, the "tasks:resync" events the page must see)
BOUNDARY_CASES = {
    "crlf_and_multibyte_character_split": (
        [b"event: resync\r", b"\ndata: \xc3", b"\xa9\r\n\r", b"\n"],
        1,
    ),
    "byte_by_byte": ([bytes([byte]) for byte in b"event: resync\r\ndata: \xc3\xa9\r\n\r\n"], 1),
    "two_messages_in_one_chunk": ([RESYNC_MESSAGE * 2], 2),
    "lf_blank_line_split": ([b"event: resync\ndata: {}\n", b"\n"], 1),
    "crlf_blank_line_split": ([b"event: resync\r\ndata: {}\r\n", b"\r\n"], 1),
    "bare_cr_line_ends": ([b"event: resync\rdata: {}\r\r"], 1),
    "bare_cr_then_bare_cr_in_the_next_chunk": ([b"event: resync\rdata: {}\r", b"\r"], 1),
    "bare_cr_blank_line_at_the_end_of_a_chunk": ([b"event: resync\r\r", b"data: {}\n\n"], 1),
    # CR and LF in two chunks are one line break, so "event: other" stays in the same message
    # (the last event field wins) and only the resync after it counts.
    "crlf_split_is_one_line_break": (
        [b"event: resync\r", b"\nevent: other\r\n\r\n", RESYNC_MESSAGE],
        1,
    ),
    "message_without_its_blank_line_yet": ([b"event: resync\ndata: {}\n"], 0),
    "a_big_chunk_of_complete_messages_is_not_capped": ([RESYNC_MESSAGE * 4000], 4000),
    "just_under_the_cap_still_dispatches": ([comment_lines(60_000), RESYNC_MESSAGE], 1),
}


@pytest.mark.parametrize("name", BOUNDARY_CASES)
def test_reader_dispatches_the_same_events_wherever_the_chunks_are_cut(
    running_app: RunningApp, desktop_page: Page, name: str
) -> None:
    chunks, expected = BOUNDARY_CASES[name]
    seen = watch(desktop_page)
    deliver_in_chunks(desktop_page, chunks)

    open_app(desktop_page, running_app, APP_PAGE)
    wait_until(desktop_page, "() => window.__drained")

    assert resyncs(desktop_page) == expected
    assert not flag(desktop_page, "__cancelled")
    assert seen.errors == []


OVER_THE_CAP_CASES = {
    "one_big_chunk": [comment_lines(CAP_BYTES + 5_000), RESYNC_MESSAGE],
    "many_small_chunks": [COMMENT_LINE] * (CAP_BYTES // len(COMMENT_LINE) + 5) + [RESYNC_MESSAGE],
}


@pytest.mark.parametrize("name", OVER_THE_CAP_CASES)
def test_reader_gives_up_when_a_message_never_ends_within_64_kib(
    running_app: RunningApp, desktop_page: Page, name: str
) -> None:
    """Text that goes on for more than 64 KiB without a blank line is not an event stream we
    know: the reader cancels the stream and stays quiet, even if a valid resync follows."""
    seen = watch(desktop_page)
    deliver_in_chunks(desktop_page, OVER_THE_CAP_CASES[name])

    open_app(desktop_page, running_app, APP_PAGE)
    wait_until(desktop_page, "() => window.__cancelled || window.__drained")
    desktop_page.evaluate(AFTER_A_TURN)

    assert flag(desktop_page, "__cancelled"), "the reader should have cancelled the stream"
    assert not flag(desktop_page, "__drained"), "and not have read on to the end"
    assert resyncs(desktop_page) == 0
    assert seen.errors == []
    expect(desktop_page.locator("#app-status")).to_be_hidden()
