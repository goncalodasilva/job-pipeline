"""The whole setup guide, end to end, in a real browser (M1.24; AC-M1.2, AC-M1.3, AC-M1.6,
AC-M1.7, AC-M1.8; ADR-053, ADR-066, ADR-068; spec 3.9).

The per-step files (``test_setup_welcome.py``, ``test_setup_provider*.py``,
``test_setup_preferences_done.py``, ``test_setup_guide_shell.py``) start the app part-way
through the guide. These tests start from a fresh app (``running_app`` with ``"fresh"``: an
explicit data folder, no step done; fakes allowed; the file-backed test keyring) and walk every
step in one browser session: Welcome, AI provider (the fake provider, whose fictional key
keyword picks the connection outcome, ADR-054), Web search (optional, M2.25: the walk chooses
"Skip for now"), Job preferences and Done, until the app opens.

- AC-M1.8: the walk by keyboard only at desktop size; at 390x844 (taps) without horizontal
  scroll; and axe on every step state of the walk (desktop and phone, light and dark).
- AC-M1.2: app pages redirect to the guide's current step in the browser, including an htmx
  request (``HX-Redirect``); ``/health`` and static files don't.
- AC-M1.6: the app process is stopped and started again on the same data and config folders
  mid-guide; it resumes at the first unfinished required step with saved values kept.
- AC-M1.3: each fake outcome (Connected, auth, CLI not found, usage limit, network) is shown and
  announced through the live region.
- AC-M1.7: after finishing, a restart with ``--setup`` reopens the guide at its first step with
  every value kept, the Done step lists each step's status, and "Back to the app" leaves.
- ADR-053: after each htmx step swap focus is on the step heading, ``aria-current="step"``
  moves and "Step N of 5: <title>" is announced.
- ADR-066: a finished setup with a broken provider (the saved fake without
  ``JOBPIPELINE_ALLOW_FAKE_PROVIDERS``) shows the banner; by keyboard: skip link, banner link,
  provider step, "Back to the app"; at 390x844 (light) and 360x740 (dark) with axe and no
  horizontal scroll.

The app runs on loopback with temporary folders; every value is fictional and no key-shaped
literal is written here (``fake_key`` builds them at run time).
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from app.domain.errors import (
    NetworkUnavailable,
    ProviderAuthError,
    ProviderNotInstalled,
    UsageLimitReached,
)
from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_setup_preferences_done import (
    ADD_JOB,
    AMOUNT,
    CONTINUE,
    COUNTRY,
    CURRENCY,
    LOCATIONS,
    REMOTE,
    ROLES,
)
from tests.e2e.test_setup_provider import SMALL_PHONE, keyring_values, tab_to
from tests.e2e.test_setup_provider_test import (
    FAKE_RADIO,
    choose_fake_and_type,
    connection_button,
    fake_key,
    tab_to_locator,
)
from tests.e2e.test_shell import has_no_horizontal_scroll, new_page, open_app
from tests.running_app import AppStart, RunningApp, SetupState, start_app

FRESH: SetupState = "fresh"
pytestmark = pytest.mark.e2e

WELCOME_SUBMIT = 'button[name="action"][value="open"]'
GO_TO_TODAY = 'button[name="then"][value="today"]'
SKIP_LINK = "a.skip-link"
CURRENT = '[aria-current="step"]'
SKIP_WEB_SEARCH = '#setup-step button[name="action"][value="skip"]'
STEP_IDS = ("welcome", "provider", "web_search", "preferences", "done")
STEP_TITLES = ("Welcome and data folder", "AI provider", "Web search", "Job preferences", "Done")
# The required steps, which the walk finishes; it skips Web search, the optional one.
REQUIRED_TITLES = ("Welcome and data folder", "AI provider", "Job preferences")
NOT_CONNECTED = "Not connected: "
OUTCOMES = (  # (fake key keyword, expected text or prefix, connected?)
    ("fake-auth-rejected", NOT_CONNECTED + ProviderAuthError().user_message, False),
    ("fake-not-installed", NOT_CONNECTED + ProviderNotInstalled().user_message, False),
    ("fake-usage-limit", NOT_CONNECTED + UsageLimitReached().user_message, False),
    ("fake-offline", NOT_CONNECTED + NetworkUnavailable().user_message, False),
    ("fake-connected", "Connected", True),
)
ROLE_LINES = ("Fictional Archivist", "Fictional Lighthouse Keeper")
LOCATION = "Fictionville"
AXE_CASES = [
    pytest.param(DESKTOP, "light", id="desktop-light"),
    pytest.param(DESKTOP, "dark", id="desktop-dark"),
    pytest.param(PHONE, "light", id="phone-light"),
    pytest.param(PHONE, "dark", id="phone-dark"),
]


# --- helpers -------------------------------------------------------------------------------


def url(app: RunningApp, path: str) -> str:
    return f"{app.base_url}{path}"


def assert_fits(page: Page, state: str) -> None:
    fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
    assert fits, f"{state}: scrollWidth {scroll_width} > {inner_width}"


def assert_on_step(page: Page, app: RunningApp, step_id: str, title: str) -> None:
    """The URL, the one current step in the list, the title and the one ``<h1>``."""
    expect(page).to_have_url(url(app, f"/setup/{step_id}"))
    expect(page.locator(CURRENT)).to_have_count(1)
    expect(page.locator(CURRENT)).to_contain_text(title)
    expect(page).to_have_title(re.compile(rf"^{re.escape(title)}"))
    expect(page.locator("h1")).to_have_count(1)


def assert_swapped_to(page: Page, app: RunningApp, step_id: str, *, said: str = "") -> None:
    """After an htmx step swap (ADR-053): focus on the step heading, aria-current moved, and
    "Step N of 5: <title>" announced in the live region, after ``said`` when the step left
    behind announces its outcome (web search: "Web search skipped.")."""
    position = STEP_IDS.index(step_id)
    title = STEP_TITLES[position]
    assert_on_step(page, app, step_id, title)
    expect(page.locator("#step-heading")).to_have_text(title)
    expect(page.locator("#step-heading")).to_be_focused()
    step = f"Step {position + 1} of 5: {title}"
    expect(page.locator("#announcer")).to_have_text(f"{said} {step}" if said else step)
    expect(page.locator("#announcer")).to_have_attribute("aria-live", "polite")
    others = page.locator("ol.setup-steps > li:not([aria-current])")
    expect(others).to_have_count(len(STEP_TITLES) - 1)


def continue_link(page: Page) -> Locator:
    return page.locator("#setup-step").get_by_role("link", name="Continue")


def pointer_test_connection(page: Page, key: str, *, tap: bool = False) -> None:
    """Pointer (or touch) path, for tests whose subject isn't the keyboard."""
    radio = page.locator(FAKE_RADIO)
    radio.tap() if tap else radio.check()
    expect(radio).to_be_checked()
    page.locator("#credential").fill(key)
    button = connection_button(page)
    button.tap() if tap else button.click()


def pointer_welcome(page: Page, app: RunningApp, *, tap: bool = False) -> None:
    submit = page.locator(WELCOME_SUBMIT)
    submit.tap() if tap else submit.click()
    expect(page).to_have_url(url(app, "/setup/provider"))
    expect(page.locator(CURRENT)).to_contain_text("AI provider")


def pointer_provider_to_web_search(page: Page, app: RunningApp, *, tap: bool = False) -> None:
    pointer_test_connection(page, fake_key("fake-connected"), tap=tap)
    expect(page.locator("#connection-result")).to_contain_text("Connected")
    link = continue_link(page)
    link.tap() if tap else link.click()
    expect(page).to_have_url(url(app, "/setup/web_search"))


def pointer_skip_web_search(page: Page, app: RunningApp, *, tap: bool = False) -> None:
    """Step 3 is optional: "Skip for now" moves on to job preferences."""
    skip = page.locator(SKIP_WEB_SEARCH)
    expect(skip).to_have_text("Skip for now")
    skip.tap() if tap else skip.click()
    expect(page).to_have_url(url(app, "/setup/preferences"))


def pointer_provider_connected(page: Page, app: RunningApp, *, tap: bool = False) -> None:
    pointer_provider_to_web_search(page, app, tap=tap)
    pointer_skip_web_search(page, app, tap=tap)


def pointer_preferences(page: Page, app: RunningApp, *, tap: bool = False) -> None:
    page.locator(ROLES).fill("\n".join(ROLE_LINES))
    page.locator(LOCATIONS).fill(LOCATION)
    remote = page.locator(REMOTE)
    remote.tap() if tap else remote.check()
    expect(remote).to_be_checked()
    page.locator(AMOUNT).fill("45,000")
    page.locator(CURRENCY).select_option("EUR")
    page.locator(COUNTRY).select_option("PT")
    submit = page.locator(CONTINUE)
    submit.tap() if tap else submit.click()
    expect(page).to_have_url(url(app, "/setup/done"))


def walk_to_done(page: Page, app: RunningApp) -> None:
    """From a fresh app to the Done step with the pointer (not the subject of the test)."""
    open_app(page, app, "/")
    expect(page).to_have_url(url(app, "/setup/welcome"))
    pointer_welcome(page, app)
    pointer_provider_connected(page, app)
    pointer_preferences(page, app)


def assert_preferences_kept(page: Page) -> None:
    expect(page.locator(ROLES)).to_have_value("\n".join(ROLE_LINES))
    expect(page.locator(LOCATIONS)).to_have_value(LOCATION)
    expect(page.locator(REMOTE)).to_be_checked()
    expect(page.locator(CURRENCY)).to_have_value("EUR")
    expect(page.locator(COUNTRY)).to_have_value("PT")


def step_item(page: Page, title: str) -> Locator:
    """The step list entry whose title is ``title``: the text starts with it and the status,
    if any, follows (so the "Done" step isn't confused with a "· Done" status)."""
    named = re.compile(rf"^{re.escape(title)}(\s·|$)")
    return page.locator("ol.setup-steps > li").filter(has_text=named)


def step_status(page: Page, title: str) -> Locator:
    """The status words of a step in the list ("· Done", "· Needs attention", ...)."""
    return step_item(page, title).locator(".step-status")


def expect_step_done(page: Page, title: str) -> None:
    expect(step_item(page, title)).to_have_count(1)
    expect(step_status(page, title)).to_have_text("· Done")


def expect_web_search_skipped(page: Page) -> None:
    expect(step_item(page, "Web search")).to_have_count(1)
    expect(step_status(page, "Web search")).to_have_text("· Skipped")


def next_output_name(app: RunningApp) -> str:
    """A name for a restarted app's output files that no earlier run of this test used, so
    the first run's ``app-stdout.txt`` and ``app-stderr.txt`` are kept."""
    folder = app.data_dir.parent
    run = 2
    while (folder / f"app-run{run}-stdout.txt").exists():
        run += 1
    return f"app-run{run}"


@contextmanager
def restarted(app: RunningApp, *, cli_args: tuple[str, ...] = ()) -> Iterator[RunningApp]:
    """Stop ``app`` gracefully and start a new process on the same data, config and keyring
    folders (``start_app`` with ``"fresh"`` writes nothing beforehand). The new process must
    stop cleanly as well."""
    first = app.stop()
    assert not first.killed and first.returncode == 0, app.output_tail()
    again = start_app(
        app.data_dir.parent, setup=FRESH, cli_args=cli_args, output_name=next_output_name(app)
    )
    try:
        assert again.data_dir == app.data_dir and again.config_dir == app.config_dir
        yield again
    finally:
        result = again.stop()
    assert not result.killed, f"the restarted app was killed\n{again.output_tail()}"
    assert result.returncode == 0, f"exit code {result.returncode}\n{again.output_tail()}"


def assert_no_key_leaked(app: RunningApp, keys: list[str]) -> None:
    for key in keys:
        assert key not in keyring_values(app), "the fake needs no key; none is kept"
        for log_file in app.log_files():
            assert key.encode() not in log_file.read_bytes(), log_file
        assert key not in app.stdout_text()
        assert key not in app.stderr_text()


# --- AC-M1.8: keyboard only, phone, axe ----------------------------------------------------


@pytest.mark.parametrize("running_app", [FRESH], indirect=True)
def test_ac_m1_8_guide_completes_keyboard_only_desktop(
    running_app: RunningApp, browser: Browser
) -> None:
    app = running_app
    connected = fake_key("fake-connected")
    with new_page(browser, DESKTOP) as page:
        open_app(page, app, "/")
        assert_on_step(page, app, "welcome", "Welcome and data folder")

        # Step 1: the folder came from the environment, so only the submit button is needed.
        tab_to(page, WELCOME_SUBMIT)
        page.keyboard.press("Enter")
        assert_swapped_to(page, app, "provider")

        # Step 2: the fake provider, a key asking for success, Test connection, Continue.
        choose_fake_and_type(page, connected)
        tab_to_locator(page, connection_button(page))
        page.keyboard.press("Enter")
        result = page.locator("#connection-result")
        expect(result).to_contain_text("Connected")
        expect(result).to_be_focused()
        expect_step_done(page, "AI provider")
        tab_to_locator(page, continue_link(page))
        page.keyboard.press("Enter")
        assert_swapped_to(page, app, "web_search")

        # Step 3: optional; "Skip for now" by keyboard moves on.
        tab_to(page, SKIP_WEB_SEARCH)
        page.keyboard.press("Enter")
        assert_swapped_to(page, app, "preferences", said="Web search skipped.")
        expect_web_search_skipped(page)

        # Step 5: every field by keyboard; Enter in the roles textarea is a new line.
        tab_to(page, ROLES)
        page.keyboard.type(ROLE_LINES[0])
        page.keyboard.press("Enter")
        page.keyboard.type(ROLE_LINES[1])
        expect(page).to_have_url(url(app, "/setup/preferences"))
        tab_to(page, LOCATIONS)
        page.keyboard.type(LOCATION)
        tab_to(page, REMOTE)
        page.keyboard.press("Space")
        expect(page.locator(REMOTE)).to_be_checked()
        tab_to(page, AMOUNT)
        page.keyboard.type("45,000")
        tab_to(page, CURRENCY)
        page.keyboard.type("Euro")
        expect(page.locator(CURRENCY)).to_have_value("EUR")
        tab_to(page, COUNTRY)
        page.keyboard.type("Portugal")
        expect(page.locator(COUNTRY)).to_have_value("PT")
        tab_to(page, CONTINUE)
        page.keyboard.press("Enter")
        assert_swapped_to(page, app, "done")

        # Step 6: every required step done, and the skipped one skipped, in text; "Go to
        # Today" finishes and opens the app.
        for title in REQUIRED_TITLES:
            expect_step_done(page, title)
        expect_web_search_skipped(page)
        tab_to(page, GO_TO_TODAY)
        page.keyboard.press("Enter")
        expect(page).to_have_url(url(app, "/"))
        expect(page.locator("#page-heading")).to_have_text("Today")
        expect(page.locator(CURRENT)).to_have_count(0)
        expect(page.locator(".banners")).to_have_count(0)

        # Finished: other pages open without the guide.
        open_app(page, app, "/inbox")
        expect(page).to_have_url(url(app, "/inbox"))
        expect(page.locator("#page-heading")).to_have_text("Job inbox")

    assert_no_key_leaked(app, [connected])


@pytest.mark.parametrize("running_app", [FRESH], indirect=True)
def test_ac_m1_8_guide_completes_at_390x844(running_app: RunningApp, browser: Browser) -> None:
    app = running_app
    with new_page(browser, PHONE) as page:
        open_app(page, app, "/")
        assert_on_step(page, app, "welcome", "Welcome and data folder")
        assert_fits(page, "welcome")

        pointer_welcome(page, app, tap=True)
        assert_swapped_to(page, app, "provider")
        assert_fits(page, "provider")

        pointer_test_connection(page, fake_key("fake-connected"), tap=True)
        expect(page.locator("#connection-result")).to_contain_text("Connected")
        assert_fits(page, "provider connected")
        continue_link(page).tap()
        assert_swapped_to(page, app, "web_search")
        assert_fits(page, "web search")

        pointer_skip_web_search(page, app, tap=True)
        assert_swapped_to(page, app, "preferences", said="Web search skipped.")
        assert_fits(page, "preferences")

        pointer_preferences(page, app, tap=True)
        assert_swapped_to(page, app, "done")
        assert_fits(page, "done")
        for title in REQUIRED_TITLES:
            expect_step_done(page, title)
        expect_web_search_skipped(page)

        page.locator(ADD_JOB).tap()
        expect(page).to_have_url(url(app, "/inbox"))
        expect(page.locator("#page-heading")).to_have_text("Job inbox")
        assert_fits(page, "inbox after the guide")


@pytest.mark.parametrize("running_app", [FRESH], indirect=True)
@pytest.mark.parametrize(("viewport", "scheme"), AXE_CASES)
def test_ac_m1_8_axe_each_step(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int], scheme: str
) -> None:
    """Every state the walk passes through, reached by htmx swaps as a user would."""
    app = running_app
    with new_page(browser, viewport, color_scheme=scheme) as page:
        open_app(page, app, "/")
        expect(page.locator(WELCOME_SUBMIT)).to_be_visible()
        assert_no_serious_violations(page)

        pointer_welcome(page, app)
        expect(page.locator("#step-heading")).to_be_focused()
        assert_no_serious_violations(page)

        pointer_test_connection(page, fake_key("fake-offline"))
        expect(page.locator("#connection-result")).to_contain_text(NOT_CONNECTED)
        assert_no_serious_violations(page)

        pointer_provider_to_web_search(page, app)
        expect(page.locator(SKIP_WEB_SEARCH)).to_be_visible()
        expect(page.locator("#step-heading")).to_be_focused()
        assert_no_serious_violations(page)

        pointer_skip_web_search(page, app)
        expect(page.locator(ROLES)).to_be_visible()
        assert_no_serious_violations(page)

        pointer_preferences(page, app)
        expect(page.locator(ADD_JOB)).to_be_visible()
        assert_no_serious_violations(page)

        page.locator(GO_TO_TODAY).click()
        expect(page).to_have_url(url(app, "/"))
        expect(page.locator("#page-heading")).to_have_text("Today")
        assert_no_serious_violations(page)


# --- AC-M1.2: redirects in the browser ------------------------------------------------------


@pytest.mark.parametrize("running_app", [FRESH], indirect=True)
def test_ac_m1_2_pages_redirect_to_guide_in_browser(
    running_app: RunningApp, browser: Browser
) -> None:
    app = running_app
    pages = ("/", "/inbox", "/board", "/facts", "/companies", "/settings")
    with new_page(browser, DESKTOP) as page:
        for path in pages:
            open_app(page, app, path)
            assert page.url == url(app, "/setup/welcome"), f"{path} -> {page.url}"
            expect(page.locator(CURRENT)).to_contain_text("Welcome and data folder")

        # Exempt: /health answers itself.
        health = page.goto(url(app, "/health"))
        assert health is not None and health.status == 200
        assert page.url == url(app, "/health")
        # Exempt: static files are served as they are, never redirected.
        static = page.goto(url(app, "/static/css/app.css"))
        assert static is not None and static.status == 200
        assert static.request.redirected_from is None
        assert page.url == url(app, "/static/css/app.css")

        # After step 1 the redirect target follows the progress.
        open_app(page, app, "/setup/welcome")
        pointer_welcome(page, app)
        for path in pages:
            open_app(page, app, path)
            assert page.url == url(app, "/setup/provider"), f"{path} -> {page.url}"

        # An htmx request from a guide page to an app route is sent to the current step too.
        open_app(page, app, "/setup/welcome")
        # Not awaited: the redirect navigates away, which would destroy the pending promise.
        page.evaluate("() => { htmx.ajax('GET', '/inbox', {target: '#main', swap: 'none'}); }")
        expect(page).to_have_url(url(app, "/setup/provider"))
        expect(page.locator(CURRENT)).to_contain_text("AI provider")


# --- AC-M1.6: restart mid-setup -------------------------------------------------------------


@pytest.mark.parametrize("running_app", [FRESH], indirect=True)
def test_ac_m1_6_restart_mid_setup_resumes(running_app: RunningApp, browser: Browser) -> None:
    app = running_app
    with new_page(browser, DESKTOP) as page:
        open_app(page, app, "/")
        pointer_welcome(page, app)
        # The fake is saved (Save stays on the step) but not tested, so step 2 isn't done.
        page.locator(FAKE_RADIO).check()
        page.get_by_role("button", name="Save", exact=True).click()
        expect(page.locator("#announcer")).to_contain_text("Saved")
        expect(page.locator("#credential-status")).to_have_text("The fake provider needs no key.")

    with restarted(app) as second:
        with new_page(browser, DESKTOP) as page:
            open_app(page, second, "/")
            assert_on_step(page, second, "provider", "AI provider")
            expect(page.locator(FAKE_RADIO)).to_be_checked()  # the saved choice
            expect_step_done(page, "Welcome and data folder")
            expect(step_status(page, "AI provider")).to_have_count(0)
            open_app(page, second, "/inbox")
            assert page.url == url(second, "/setup/provider"), page.url

            open_app(page, second, "/setup/provider")
            pointer_provider_connected(page, second)
            pointer_preferences(page, second)

        with restarted(second) as third, new_page(browser, DESKTOP) as page:
            # Every required step is done, the guide isn't finished: Done.
            open_app(page, third, "/")
            assert_on_step(page, third, "done", "Done")
            for title in REQUIRED_TITLES:
                expect_step_done(page, title)
            expect_web_search_skipped(page)
            open_app(page, third, "/setup/preferences")
            assert_preferences_kept(page)
            open_app(page, third, "/setup/provider")
            expect(page.locator(FAKE_RADIO)).to_be_checked()
            expect(page.locator("#connection-done")).to_be_visible()


# --- AC-M1.3: mapped messages ---------------------------------------------------------------


@pytest.mark.parametrize("running_app", [FRESH], indirect=True)
def test_ac_m1_3_mapped_messages_shown_and_announced(
    running_app: RunningApp, browser: Browser
) -> None:
    app = running_app
    keys: list[str] = []
    with new_page(browser, DESKTOP) as page:
        open_app(page, app, "/")
        pointer_welcome(page, app)
        result = page.locator("#connection-result")
        announcer = page.locator("#announcer")
        for keyword, expected, connected in OUTCOMES:
            key = fake_key(keyword)
            keys.append(key)
            pointer_test_connection(page, key)
            if connected:
                expect(result).to_contain_text(expected)
                expect(result).to_have_class(re.compile(r"\bconnection-ok\b"))
                expect(announcer).to_have_text(result.inner_text())
                expect(continue_link(page)).to_have_attribute("href", "/setup/web_search")
                expect_step_done(page, "AI provider")
            else:
                expect(result).to_have_text(expected)
                expect(result).to_have_class(re.compile(r"\bconnection-failed\b"))
                expect(announcer).to_have_text(expected)
                expect(continue_link(page)).to_have_count(0)
                expect(step_status(page, "AI provider")).to_have_count(0)
            expect(result).to_be_focused()
            expect(page).to_have_url(url(app, "/setup/provider"))
            assert key not in page.content()
    assert_no_key_leaked(app, keys)


# --- AC-M1.7: --setup after finishing -------------------------------------------------------


@pytest.mark.parametrize("running_app", [FRESH], indirect=True)
def test_ac_m1_7_setup_flag_reopens_guide(running_app: RunningApp, browser: Browser) -> None:
    app = running_app
    with new_page(browser, DESKTOP) as page:
        walk_to_done(page, app)
        page.locator(GO_TO_TODAY).click()
        expect(page).to_have_url(url(app, "/"))
        expect(page.locator("#page-heading")).to_have_text("Today")

    with restarted(app, cli_args=("--setup",)) as reopened, new_page(browser, DESKTOP) as page:
        open_app(page, reopened, "/")
        assert_on_step(page, reopened, "welcome", "Welcome and data folder")
        back = page.get_by_role("button", name="Back to the app")
        expect(back).to_be_visible()
        for title in (*REQUIRED_TITLES, "Done"):
            expect_step_done(page, title)
        expect_web_search_skipped(page)

        # Nothing was lost.
        open_app(page, reopened, "/setup/provider")
        expect(page.locator(FAKE_RADIO)).to_be_checked()
        expect(page.locator("#connection-done")).to_be_visible()
        open_app(page, reopened, "/setup/preferences")
        assert_preferences_kept(page)

        # Done lists every step with its status in words: the walk skipped Web search, the
        # optional step (M2.25), so it reads "Skipped"; none reads "Not done".
        open_app(page, reopened, "/setup/done")
        rows = page.locator("ul.setup-summary > li")
        expect(rows).to_have_text(
            [
                "Welcome and data folder: Done",
                "AI provider: Done",
                "Web search: Skipped",
                "Job preferences: Done",
            ],
            use_inner_text=True,
        )
        for title in STEP_TITLES[:4]:
            expect(rows.get_by_role("link", name=title)).to_have_count(1)
        expect(rows.get_by_role("link", name="Web search")).to_have_attribute(
            "href", "/setup/web_search"
        )

        # "Back to the app" ends the reopening for this run.
        back.click()
        expect(page).to_have_url(url(reopened, "/"))
        expect(page.locator("#page-heading")).to_have_text("Today")
        open_app(page, reopened, "/inbox")
        expect(page).to_have_url(url(reopened, "/inbox"))


# --- ADR-053: focus, aria-current and the announcement after each swap ----------------------


@pytest.mark.parametrize("running_app", [FRESH], indirect=True)
def test_focus_moves_to_step_heading_after_each_swap(
    running_app: RunningApp, browser: Browser
) -> None:
    """Each step change is made with htmx: Welcome -> AI provider (submit), AI provider -> Web
    search (the "Continue" link), Web search -> Job preferences ("Skip for now") and Job
    preferences -> Done (submit)."""
    app = running_app
    with new_page(browser, DESKTOP) as page:
        open_app(page, app, "/")
        expect(page.locator(CURRENT)).to_contain_text("Welcome and data folder")
        page.locator(WELCOME_SUBMIT).focus()
        page.keyboard.press("Enter")
        assert_swapped_to(page, app, "provider")
        expect_step_done(page, "Welcome and data folder")

        pointer_provider_to_web_search(page, app)
        assert_swapped_to(page, app, "web_search")

        pointer_skip_web_search(page, app)
        assert_swapped_to(page, app, "preferences", said="Web search skipped.")
        expect_web_search_skipped(page)

        page.locator(ROLES).fill(ROLE_LINES[0])
        page.locator(COUNTRY).select_option("PT")
        page.locator(CONTINUE).focus()
        page.keyboard.press("Enter")
        assert_swapped_to(page, app, "done")
        expect_step_done(page, "Job preferences")

        # A refused submit is no swap to another step: focus goes to the field, not a heading.
        open_app(page, app, "/setup/preferences")
        page.locator(ROLES).fill("")
        page.locator(CONTINUE).click()
        expect(page.locator(f'{ROLES}[aria-invalid="true"]')).to_be_focused()
        expect(page.locator(CURRENT)).to_contain_text("Job preferences")


# --- ADR-066: the provider-broken banner and "Back to the app" ------------------------------


@pytest.mark.parametrize(
    "running_app", [AppStart(setup="complete", allow_fakes=False)], indirect=True
)
@pytest.mark.parametrize(
    ("viewport", "scheme"),
    [
        pytest.param(PHONE, "light", id="390x844-light"),
        pytest.param(SMALL_PHONE, "dark", id="360x740-dark"),
    ],
)
def test_provider_broken_banner_keyboard_to_back_to_app(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int], scheme: str
) -> None:
    """A finished setup whose saved provider (the fake) isn't allowed in this run; axe runs in
    the light and the dark scheme."""
    app = running_app
    banner_link = "section.banners a"
    with new_page(browser, viewport, color_scheme=scheme) as page:
        open_app(page, app, "/")
        expect(page.locator("#page-heading")).to_have_text("Today")
        banner = page.locator("section.banners .banner")
        expect(banner).to_have_count(1)
        expect(banner).to_contain_text("Needs attention:")
        expect(banner).to_contain_text("That provider isn't allowed in this workspace.")
        expect(page.locator(banner_link)).to_have_text("Fix the AI provider")
        expect(page.locator(banner_link)).to_have_attribute("href", "/setup/provider")
        assert_no_serious_violations(page)
        assert_fits(page, "Today with the banner")

        # Keyboard: the skip link comes first, then the banner's link.
        page.keyboard.press("Tab")
        expect(page.locator(SKIP_LINK)).to_be_focused()
        expect(page.locator(SKIP_LINK)).to_be_visible()
        tab_to(page, banner_link)
        page.keyboard.press("Enter")

        assert_on_step(page, app, "provider", "AI provider")
        expect(step_status(page, "AI provider")).to_have_text("· Needs attention")
        expect(page.locator(".banners")).to_have_count(0)  # the guide doesn't repeat it
        back = page.get_by_role("button", name="Back to the app")
        expect(back).to_be_visible()
        assert_no_serious_violations(page)
        assert_fits(page, "provider step from the banner")

        tab_to_locator(page, back)
        page.keyboard.press("Enter")
        expect(page).to_have_url(url(app, "/"))
        expect(page.locator("#page-heading")).to_have_text("Today")
        # Nothing was fixed, so the banner is still there; nothing was reset either.
        expect(page.locator("section.banners .banner")).to_have_count(1)
        assert_no_serious_violations(page)
        assert_fits(page, "Today after Back to the app")

        # ADR-068: the Done step says the provider needs attention.
        open_app(page, app, "/setup/done")
        expect(step_status(page, "AI provider")).to_have_text("· Needs attention")
        expect(page.locator("#setup-step")).to_contain_text("Something needs attention")
        assert_no_serious_violations(page)
        assert_fits(page, "Done with a broken provider")
