"""The app shell in a real browser: tokens, responsive sidebar, focus and axe (M0.18).

Spec 3.9: WCAG 2.2 AA, keyboard reachable, visible focus, 24 by 24 px targets, reduced
motion respected, layouts from 360 px. Design (M0.18):

- ``app/static/css/app.css`` defines colour tokens on ``:root`` (``--color-bg`` and friends),
  with dark values under ``@media (prefers-color-scheme: dark)``.
- At 768 px and wider, ``<nav id="sidebar" aria-label="Main">`` is always visible and the
  toggle is hidden. Below 768 px the sidebar is hidden until the top-bar toggle
  ``<button class="sidebar-toggle" aria-controls="sidebar" aria-expanded="false">`` opens it.
  Opening moves focus to the first link; Escape closes it and returns focus to the toggle.
- The launch token travels in ``<body hx-headers>``; a 403 shows and announces the
  "Reload the page" message (ADR-041).

The app runs on loopback with temporary folders; no fictional or real user data is needed.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e

NARROW = {"width": 360, "height": 740}
PAGE_PATHS = ("/", "/inbox", "/board", "/facts", "/companies", "/settings")
NAV_LINK_NAMES = ("Today", "Job inbox", "Board", "Fact bank", "Companies", "Settings")
MIN_TARGET_PX = 24
RELOAD_TEXT = "Reload the page"
FICTIONAL_STALE_TOKEN = "fictional-stale"  # noqa: S105 - deliberately wrong, not a secret
# A reduced-motion stylesheet may use 0s or the common 0.01ms; anything longer is motion.
MAX_REDUCED_DURATION_S = 0.00001

SIDEBAR = "#sidebar"
TOGGLE = "button.sidebar-toggle"
EVENTS = "/events"  # the live-update stream app pages keep open (M2.1)


# --- helpers -------------------------------------------------------------------------------


@contextmanager
def new_page(
    browser: Browser,
    viewport: dict[str, int],
    *,
    color_scheme: str = "light",
    reduced_motion: str = "no-preference",
    java_script_enabled: bool = True,
) -> Iterator[Page]:
    phone = viewport["width"] < 768
    context = browser.new_context(
        viewport=viewport,  # type: ignore[arg-type]
        is_mobile=phone,
        has_touch=phone,
        color_scheme=color_scheme,  # type: ignore[arg-type]
        reduced_motion=reduced_motion,  # type: ignore[arg-type]
        java_script_enabled=java_script_enabled,
    )
    try:
        yield context.new_page()
    finally:
        context.close()


def open_app(page: Page, app: RunningApp, path: str = "/", *, scripts: bool = True) -> None:
    response = page.goto(app.base_url + path)
    assert response is not None and response.status == 200, path
    if scripts:  # with JavaScript off, htmx never loads
        page.wait_for_function("() => typeof window.htmx !== 'undefined'")


def sidebar(page: Page) -> Locator:
    return page.locator(SIDEBAR)


def toggle(page: Page) -> Locator:
    return page.locator(TOGGLE)


def nav_links(page: Page) -> Locator:
    return page.locator(f"{SIDEBAR} a")


def active_element_description(page: Page) -> str:
    description: str = page.evaluate(
        "() => { const e = document.activeElement;"
        " return e ? `${e.tagName.toLowerCase()}#${e.id}.${e.className} "
        "${(e.textContent || '').trim().slice(0, 40)}` : 'none'; }"
    )
    return description


def focus_is_inside(page: Page, selector: str) -> bool:
    inside: bool = page.evaluate(
        "(sel) => { const c = document.querySelector(sel);"
        " return !!c && c.contains(document.activeElement); }",
        selector,
    )
    return inside


def css_token(page: Page, name: str) -> str:
    value: str = page.evaluate(
        "(n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim()", name
    )
    return value


def body_background(page: Page) -> str:
    value: str = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
    return value


def has_no_horizontal_scroll(page: Page) -> tuple[bool, int, int]:
    scroll_width: int = page.evaluate("() => document.documentElement.scrollWidth")
    inner_width: int = page.evaluate("() => window.innerWidth")
    return scroll_width <= inner_width, scroll_width, inner_width


def assert_target_size(locator: Locator, label: str) -> None:
    box = locator.bounding_box()
    assert box is not None, f"{label} has no bounding box (not rendered)"
    assert box["width"] >= MIN_TARGET_PX and box["height"] >= MIN_TARGET_PX, (
        f"{label} is {box['width']:.1f}x{box['height']:.1f} px; targets must be at least "
        f"{MIN_TARGET_PX}x{MIN_TARGET_PX}"
    )


def open_sidebar_by_keyboard(page: Page) -> None:
    """Tab past the skip link to the toggle and press Enter (phone layout)."""
    page.keyboard.press("Tab")
    page.keyboard.press("Tab")
    expect(toggle(page)).to_be_focused()
    page.keyboard.press("Enter")
    expect(toggle(page)).to_have_attribute("aria-expanded", "true")
    expect(sidebar(page)).to_be_visible()


def durations_in_seconds(value: str) -> list[float]:
    seconds = []
    for part in value.split(","):
        text = part.strip()
        if text.endswith("ms"):
            seconds.append(float(text[:-2]) / 1000)
        elif text.endswith("s"):
            seconds.append(float(text[:-1]))
        else:
            raise AssertionError(f"unexpected duration {value!r}")
    return seconds


def inject_probe(page: Page) -> Locator:
    """Add a button that makes an ordinary htmx request for the inbox fragment into #main.

    allowEval is false (ADR-041), so the probe uses plain hx-get, processed by htmx itself;
    the request inherits ``hx-headers`` (the launch token) from ``<body>``.
    """
    page.evaluate(
        "() => { const b = document.createElement('button');"
        " b.type = 'button'; b.id = 'probe'; b.textContent = 'Probe';"
        " b.setAttribute('hx-get', '/inbox'); b.setAttribute('hx-target', '#main');"
        " document.body.appendChild(b); window.htmx.process(b); }"
    )
    return page.locator("#probe")


# --- layout --------------------------------------------------------------------------------


def test_shell_desktop_sidebar_visible(running_app: RunningApp, desktop_page: Page) -> None:
    open_app(desktop_page, running_app)

    nav = sidebar(desktop_page)
    expect(nav).to_be_visible()
    expect(nav).to_have_attribute("aria-label", "Main")
    links = nav_links(desktop_page)
    expect(links).to_have_count(len(NAV_LINK_NAMES))
    for name in NAV_LINK_NAMES:
        expect(nav.get_by_role("link", name=name, exact=True)).to_be_visible()
    expect(toggle(desktop_page)).to_be_hidden()

    nav_box = nav.bounding_box()
    main_box = desktop_page.locator("#main").bounding_box()
    assert nav_box is not None and main_box is not None
    assert nav_box["x"] + nav_box["width"] <= main_box["x"] + 1, (
        f"the sidebar {nav_box} should sit left of main {main_box}"
    )


def test_shell_phone_390x844_sidebar_collapsed_and_opens_by_keyboard(
    running_app: RunningApp, phone_page: Page
) -> None:
    page = phone_page
    open_app(page, running_app)
    assert page.evaluate("window.innerWidth") == PHONE["width"]

    button = toggle(page)
    expect(button).to_be_visible()
    expect(button).to_have_attribute("type", "button")
    expect(button).to_have_attribute("aria-controls", "sidebar")
    expect(button).to_have_attribute("aria-expanded", "false")
    expect(sidebar(page)).to_be_hidden()

    # Keyboard order from the page start: skip link, then the toggle.
    page.keyboard.press("Tab")
    expect(page.locator("a.skip-link")).to_be_focused()
    page.keyboard.press("Tab")
    expect(button).to_be_focused()
    # The closed sidebar's links are not reachable by Tab.
    page.keyboard.press("Tab")
    assert not focus_is_inside(page, SIDEBAR), (
        f"Tab reached a hidden sidebar link: {active_element_description(page)}"
    )

    # Enter opens it and moves focus to the first link.
    button.focus()
    page.keyboard.press("Enter")
    expect(button).to_have_attribute("aria-expanded", "true")
    expect(sidebar(page)).to_be_visible()
    first_link = sidebar(page).get_by_role("link", name="Today", exact=True)
    expect(first_link).to_be_visible()
    expect(first_link).to_be_focused()

    # Escape closes it and returns focus to the toggle.
    page.keyboard.press("Escape")
    expect(button).to_have_attribute("aria-expanded", "false")
    expect(sidebar(page)).to_be_hidden()
    expect(button).to_be_focused()

    # Space opens it too.
    page.keyboard.press("Space")
    expect(button).to_have_attribute("aria-expanded", "true")
    expect(sidebar(page)).to_be_visible()
    expect(first_link).to_be_focused()
    page.keyboard.press("Escape")
    expect(sidebar(page)).to_be_hidden()
    expect(button).to_be_focused()


def test_no_horizontal_scroll_at_360px(running_app: RunningApp, browser: Browser) -> None:
    with new_page(browser, NARROW) as page:
        for path in PAGE_PATHS:
            open_app(page, running_app, path)
            fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
            assert fits, f"{path} closed sidebar: scrollWidth {scroll_width} > {inner_width}"

            expect(toggle(page)).to_be_visible()
            toggle(page).click()
            expect(sidebar(page)).to_be_visible()
            fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
            assert fits, f"{path} open sidebar: scrollWidth {scroll_width} > {inner_width}"


# --- tokens and themes ---------------------------------------------------------------------


def test_dark_scheme_changes_background_token(running_app: RunningApp, desktop_page: Page) -> None:
    page = desktop_page
    page.emulate_media(color_scheme="light")
    open_app(page, running_app)
    light_token = css_token(page, "--color-bg")
    light_background = body_background(page)
    for name in (
        "--color-bg",
        "--color-text",
        "--color-surface",
        "--color-border",
        "--color-accent",
        "--color-focus",
    ):
        assert css_token(page, name), f"{name} is not defined on :root"

    page.emulate_media(color_scheme="dark")
    dark_token = css_token(page, "--color-bg")
    dark_background = body_background(page)

    assert light_token != dark_token, f"--color-bg is {light_token!r} in both schemes"
    assert light_background != dark_background, (
        f"body background is {light_background!r} in both schemes"
    )


def test_stylesheet_linked_from_base(running_app: RunningApp, desktop_page: Page) -> None:
    open_app(desktop_page, running_app)
    hrefs: list[str] = desktop_page.evaluate(
        "() => Array.from(document.querySelectorAll('link[rel=stylesheet]'))"
        ".map(l => new URL(l.href).pathname)"
    )
    assert any(href.endswith("/css/app.css") for href in hrefs), hrefs
    background = body_background(desktop_page)
    assert background not in ("rgba(0, 0, 0, 0)", "transparent"), (
        "body background should come from var(--color-bg)"
    )


# --- accessibility -------------------------------------------------------------------------


AXE_CASES = [
    pytest.param(DESKTOP, "light", id="desktop-light"),
    pytest.param(DESKTOP, "dark", id="desktop-dark"),
    pytest.param(PHONE, "light", id="phone-light"),
    pytest.param(PHONE, "dark", id="phone-dark"),
]


@pytest.mark.parametrize(("viewport", "scheme"), AXE_CASES)
def test_axe_no_serious_or_critical(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int], scheme: str
) -> None:
    with new_page(browser, viewport, color_scheme=scheme) as page:
        open_app(page, running_app)
        # Contrast is only meaningful once the themed stylesheet applies.
        assert css_token(page, "--color-bg"), "the colour tokens are not loaded"
        assert_no_serious_violations(page)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_axe_phone_sidebar_open(running_app: RunningApp, browser: Browser, scheme: str) -> None:
    with new_page(browser, PHONE, color_scheme=scheme) as page:
        open_app(page, running_app)
        open_sidebar_by_keyboard(page)
        assert_no_serious_violations(page)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_axe_status_banner_shown(running_app: RunningApp, browser: Browser, scheme: str) -> None:
    """The warning banner and its Reload button meet contrast in both schemes."""
    with new_page(browser, DESKTOP, color_scheme=scheme) as page:
        open_app(page, running_app)
        page.evaluate(
            "() => { const s = document.getElementById('app-status');"
            " document.getElementById('app-status-message').textContent ="
            " s.getAttribute('data-msg-restarted');"
            " document.getElementById('app-status-reload').hidden = false; s.hidden = false; }"
        )
        expect(page.locator("#app-status")).to_be_visible()
        expect(page.locator("#app-status-reload")).to_be_visible()
        assert_no_serious_violations(page)


def test_nav_targets_at_least_24px(running_app: RunningApp, browser: Browser) -> None:
    with new_page(browser, DESKTOP) as page:
        open_app(page, running_app)
        links = nav_links(page)
        expect(links).to_have_count(len(NAV_LINK_NAMES))
        for index in range(links.count()):
            assert_target_size(links.nth(index), f"desktop nav link {index}")
        page.keyboard.press("Tab")
        expect(page.locator("a.skip-link")).to_be_focused()
        assert_target_size(page.locator("a.skip-link"), "focused skip link (desktop)")

    with new_page(browser, PHONE) as page:
        open_app(page, running_app)
        page.keyboard.press("Tab")
        assert_target_size(page.locator("a.skip-link"), "focused skip link (phone)")
        assert_target_size(toggle(page), "sidebar toggle (phone)")
        page.keyboard.press("Tab")
        page.keyboard.press("Enter")
        expect(sidebar(page)).to_be_visible()
        links = nav_links(page)
        expect(links).to_have_count(len(NAV_LINK_NAMES))
        for index in range(links.count()):
            assert_target_size(links.nth(index), f"phone nav link {index}")


def _outline(page: Page) -> tuple[str, float]:
    style: str = page.evaluate("() => getComputedStyle(document.activeElement).outlineStyle")
    width: str = page.evaluate("() => getComputedStyle(document.activeElement).outlineWidth")
    return style, float(width.removesuffix("px"))


def test_focus_is_visible_on_keyboard_focus(running_app: RunningApp, desktop_page: Page) -> None:
    page = desktop_page
    open_app(page, running_app)
    page.keyboard.press("Tab")  # skip link
    page.keyboard.press("Tab")  # the top bar's Tasks button (M2.20)
    page.keyboard.press("Tab")  # Notifications (M5.8)
    page.keyboard.press("Tab")  # first nav link
    today = sidebar(page).get_by_role("link", name="Today", exact=True)
    expect(today).to_be_focused()

    style, width = _outline(page)
    assert style != "none", "the focused nav link has no outline"
    assert width >= 2, f"the focus outline is {width}px; it should be at least 2px"


def test_focus_is_visible_on_toggle_phone(running_app: RunningApp, phone_page: Page) -> None:
    page = phone_page
    open_app(page, running_app)
    page.keyboard.press("Tab")
    page.keyboard.press("Tab")
    expect(toggle(page)).to_be_focused()

    style, width = _outline(page)
    assert style != "none", "the focused toggle has no outline"
    assert width >= 2, f"the focus outline is {width}px; it should be at least 2px"


def test_skip_link_hidden_until_focused(running_app: RunningApp, desktop_page: Page) -> None:
    page = desktop_page
    open_app(page, running_app)
    skip = page.locator("a.skip-link")
    on_screen_js = (
        "(e) => { const r = e.getBoundingClientRect();"
        " const inside = r.right > 0 && r.bottom > 0 && r.left < innerWidth"
        " && r.top < innerHeight;"
        " return inside && r.width > 1 && r.height > 1; }"
    )
    assert not skip.evaluate(on_screen_js), "the skip link is visible before it has focus"

    page.keyboard.press("Tab")
    expect(skip).to_be_focused()
    fully_inside: bool = skip.evaluate(
        "(e) => { const r = e.getBoundingClientRect();"
        " return r.left >= 0 && r.top >= 0 && r.right <= innerWidth"
        " && r.bottom <= innerHeight && r.width > 1 && r.height > 1; }"
    )
    assert fully_inside, "the focused skip link should be fully on screen"

    page.keyboard.press("Enter")
    expect(page.locator("#main")).to_be_focused()


def test_reduced_motion_disables_transitions(running_app: RunningApp, browser: Browser) -> None:
    """With reduced motion, no element in the shell keeps a transition or animation.

    Every element is checked, not one container, so the test fails whenever the
    reduced-motion rule is missing and any element declares motion (code review M0.18).
    """
    with new_page(browser, PHONE, reduced_motion="reduce") as page:
        open_app(page, running_app)
        assert css_token(page, "--color-bg"), "the stylesheet is not loaded"
        expect(sidebar(page)).to_have_count(1)

        def assert_no_motion(state: str) -> None:
            styles: list[list[str]] = page.evaluate(
                "() => [...document.querySelectorAll('body, body *')].map(e => {"
                " const s = getComputedStyle(e);"
                " return [e.tagName + (e.id ? '#' + e.id : ''),"
                " s.transitionDuration, s.animationDuration]; })"
            )
            for label, transition, animation in styles:
                for value in (transition, animation):
                    assert all(d <= MAX_REDUCED_DURATION_S for d in durations_in_seconds(value)), (
                        f"{label} has motion {value!r} ({state}) with reduced motion"
                    )

        assert_no_motion("closed")
        open_sidebar_by_keyboard(page)
        assert_no_motion("open")


def test_announcer_visually_hidden(running_app: RunningApp, desktop_page: Page) -> None:
    page = desktop_page
    open_app(page, running_app)
    announcer = page.locator("#announcer")
    expect(announcer).to_have_count(1)
    expect(announcer).to_have_attribute("aria-live", "polite")
    # Even with content, the live region takes no visible space.
    announcer.evaluate("(e) => { e.textContent = 'Fictional announcement for layout'; }")
    size: dict[str, float] = announcer.evaluate(
        "(e) => { const r = e.getBoundingClientRect(); return {w: r.width, h: r.height}; }"
    )
    assert size["w"] <= 1 and size["h"] <= 1, f"#announcer is {size} px; it should be 1x1"
    display: str = announcer.evaluate("(e) => getComputedStyle(e).display")
    assert display != "none", "#announcer must stay in the accessibility tree"


def test_hidden_attribute_wins(running_app: RunningApp, desktop_page: Page) -> None:
    page = desktop_page
    open_app(page, running_app)
    status = page.locator("#app-status")
    expect(status).to_have_attribute("hidden", "")
    expect(status).to_be_hidden()
    # A shown status keeps its hidden Reload button hidden.
    status.evaluate(
        "(e) => { document.getElementById('app-status-message').textContent ="
        " 'Fictional status message'; e.hidden = false; }"
    )
    expect(status).to_be_visible()
    expect(page.locator("#app-status-reload")).to_be_hidden()


# --- htmx behaviour ------------------------------------------------------------------------


def test_stale_token_403_announces_reload(running_app: RunningApp, desktop_page: Page) -> None:
    page = desktop_page
    open_app(page, running_app)
    page.evaluate(
        "(token) => document.body.setAttribute('hx-headers',"
        " JSON.stringify({'X-JP-Token': token}))",
        FICTIONAL_STALE_TOKEN,
    )
    probe = inject_probe(page)

    with page.expect_response(lambda r: r.url.endswith("/inbox")) as response_info:
        probe.click()
    assert response_info.value.status == 403

    expect(page.locator("#announcer")).to_contain_text(RELOAD_TEXT)
    expect(page.locator("#app-status")).to_be_visible()
    expect(page.locator("#app-status-message")).to_contain_text(RELOAD_TEXT)
    expect(page.get_by_role("button", name=RELOAD_TEXT, exact=True)).to_be_visible()
    # Nothing was swapped in.
    expect(page.locator("#main h1")).to_have_text("Today")


def test_htmx_swap_moves_focus_to_heading(running_app: RunningApp, desktop_page: Page) -> None:
    page = desktop_page
    open_app(page, running_app)
    probe = inject_probe(page)

    with page.expect_response(lambda r: r.url.endswith("/inbox")) as response_info:
        probe.click()
    assert response_info.value.status == 200

    heading = page.locator("#main h1")
    expect(heading).to_have_text("Job inbox")
    expect(heading).to_be_focused()
    expect(page.locator("#app-status")).to_be_hidden()


# --- review additions (M0.18) ----------------------------------------------------------------


def test_escape_only_closes_when_focus_is_in_the_menu(
    running_app: RunningApp, phone_page: Page
) -> None:
    """Escape elsewhere on the page must not pull focus back to the toggle."""
    page = phone_page
    open_app(page, running_app)
    open_sidebar_by_keyboard(page)

    page.locator("#main").focus()
    page.keyboard.press("Escape")
    expect(page.locator("#main")).to_be_focused()
    expect(toggle(page)).to_have_attribute("aria-expanded", "true")

    # Another handler that already used Escape keeps it.
    sidebar(page).get_by_role("link", name="Today", exact=True).focus()
    page.evaluate(
        "() => document.addEventListener('keydown', e => { if (e.key === 'Escape')"
        " e.preventDefault(); }, { capture: true, once: true })"
    )
    page.keyboard.press("Escape")
    expect(toggle(page)).to_have_attribute("aria-expanded", "true")

    page.keyboard.press("Escape")
    expect(toggle(page)).to_have_attribute("aria-expanded", "false")
    expect(toggle(page)).to_be_focused()


def test_phone_without_javascript_shows_navigation(
    running_app: RunningApp, browser: Browser
) -> None:
    """If scripts don't run, the plain links stay reachable and the useless toggle is hidden."""
    with new_page(browser, PHONE, java_script_enabled=False) as page:
        open_app(page, running_app, scripts=False)
        expect(sidebar(page)).to_be_visible()
        for name in NAV_LINK_NAMES:
            expect(sidebar(page).get_by_role("link", name=name, exact=True)).to_be_visible()
        expect(toggle(page)).to_be_hidden()


@pytest.mark.parametrize(("width", "collapsed"), [(767, True), (768, False)])
def test_sidebar_breakpoint_is_768px(
    running_app: RunningApp, browser: Browser, width: int, collapsed: bool
) -> None:
    with new_page(browser, {"width": width, "height": 800}) as page:
        open_app(page, running_app)
        if collapsed:
            expect(sidebar(page)).to_be_hidden()
            expect(toggle(page)).to_be_visible()
        else:
            expect(sidebar(page)).to_be_visible()
            expect(toggle(page)).to_be_hidden()


def test_toggle_looks_different_when_open(running_app: RunningApp, phone_page: Page) -> None:
    """Sighted users see the menu state on the button itself, not only through ARIA."""
    page = phone_page
    open_app(page, running_app)
    closed: str = toggle(page).evaluate("(e) => getComputedStyle(e).backgroundColor")
    open_sidebar_by_keyboard(page)
    opened: str = toggle(page).evaluate("(e) => getComputedStyle(e).backgroundColor")
    assert closed != opened, f"the toggle background is {closed} both closed and open"


def test_page_load_has_no_failed_requests(running_app: RunningApp, desktop_page: Page) -> None:
    """Loading a page fetches only things the app serves (no 4xx/5xx, no network failures).

    Headless Chromium never requests a favicon, so the icon itself is covered by
    tests/integration/test_app_shell.py::test_pages_link_a_served_svg_favicon.
    """
    failed: list[str] = []
    desktop_page.on(
        "response",
        lambda response: (
            failed.append(f"{response.status} {response.url}") if response.status >= 400 else None
        ),
    )
    for path in ("/", "/inbox"):
        # App pages keep an event stream open (GET /events, M2.1), so the page never reaches
        # "networkidle". Wait for the load event and for the stream's response headers instead;
        # the listener above then checks the stream's status like every other request.
        with desktop_page.expect_response(lambda response: urlsplit(response.url).path == EVENTS):
            open_app(desktop_page, running_app, path)
        desktop_page.wait_for_load_state("load")

    assert failed == []
