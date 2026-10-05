"""Step 2b of the setup guide in a real browser (M1.18; AC-M1.3; spec 3.9).

The app starts at the AI provider step (``running_app`` with ``"provider_step"``: step 1 done,
no provider saved; fakes allowed). By keyboard only, the user chooses the fake provider, pastes
a fictional key whose keyword asks for a usage-limit failure and presses "Test connection": the
mapped message (spec 2.11) is shown, announced in the live region and focused, with no way on.
Then a key asking for success: "Connected" is shown, announced and focused, the step list says
the step is done and a Continue link leads to the next step. Both states have no serious or
critical axe findings, at desktop size and at 390x844 (no horizontal scroll there). The app
runs on loopback with temporary folders; every value is fictional.
"""

import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_setup_provider import MAX_TABS, PROVIDER_STEP, has_focus, tab_to
from tests.e2e.test_shell import has_no_horizontal_scroll, new_page, open_app
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e

FAKE_RADIO = "#kind-fake"
# Failure lines start with "Not connected: " (M1.18 review); the fake keeps the domain text.
USAGE_LIMIT = (
    "Not connected: Your Claude plan's usage limit was reached. Try later or switch provider."
)


def fake_key(keyword: str) -> str:
    """A fictional key for the fake provider: the keyword picks the outcome (ADR-054)."""
    return "-".join([keyword, "FICTIONAL", "e118", "Wq5Zt3Hn"])


def is_focused(locator: Locator) -> bool:
    focused: bool = locator.evaluate("(element) => element === document.activeElement")
    return focused


def tab_to_locator(page: Page, locator: Locator) -> None:
    """Press Tab until ``locator`` has focus (keyboard only, no clicks)."""
    for _ in range(MAX_TABS):
        page.keyboard.press("Tab")
        if is_focused(locator):
            return
    raise AssertionError(f"{locator} never received focus within {MAX_TABS} Tab presses")


def choose_fake_and_type(page: Page, key: str) -> None:
    tab_to(page, 'input[name="kind"]')
    for _ in range(MAX_TABS):
        if has_focus(page, FAKE_RADIO):
            break
        page.keyboard.press("ArrowDown")
    page.keyboard.press("Space")
    expect(page.locator(FAKE_RADIO)).to_be_checked()
    tab_to(page, "#credential")
    page.keyboard.type(key)


def connection_button(page: Page) -> Locator:
    return page.get_by_role("button", name="Test connection")


def assert_layout(page: Page, viewport: dict[str, int]) -> None:
    assert_no_serious_violations(page)
    if viewport == PHONE:
        fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert fits, f"scrollWidth {scroll_width} > {inner_width}"


@pytest.mark.parametrize("running_app", [PROVIDER_STEP], indirect=True)
@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])
def test_provider_test_connection_messages(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int]
) -> None:
    preferences = f"{running_app.base_url}/setup/preferences"
    with new_page(browser, viewport) as page:
        open_app(page, running_app, "/setup/provider")
        expect(connection_button(page)).to_be_visible()
        assert_layout(page, viewport)

        # A failure: the mapped message, announced and focused; no way on.
        failing = fake_key("fake-usage-limit")
        choose_fake_and_type(page, failing)
        tab_to_locator(page, connection_button(page))
        page.keyboard.press("Enter")

        result = page.locator("#connection-result")
        expect(result).to_have_text(USAGE_LIMIT)
        expect(page.locator("#announcer")).to_have_text(USAGE_LIMIT)
        expect(result).to_be_focused()
        expect(page).to_have_url(f"{running_app.base_url}/setup/provider")
        expect(page.get_by_role("link", name="Continue")).to_have_count(0)
        expect(page.locator('[aria-current="step"]')).not_to_contain_text("Done")
        assert failing not in page.content()
        for button in page.locator("#provider-form button").all():
            expect(button).to_be_enabled()  # re-enabled once the request ends
        assert_layout(page, viewport)

        # Success: "Connected", announced and focused; the step is done; Continue goes on.
        connected = fake_key("fake-connected")
        choose_fake_and_type(page, connected)
        tab_to_locator(page, connection_button(page))
        page.keyboard.press("Enter")

        expect(result).to_contain_text("Connected")
        expect(page.locator("#announcer")).to_contain_text("Connected")
        expect(result).to_be_focused()
        expect(page.locator('[aria-current="step"]')).to_contain_text("Done")
        assert connected not in page.content()
        assert_layout(page, viewport)

        go_on = page.get_by_role("link", name="Continue")
        expect(go_on).to_have_attribute("href", "/setup/preferences")
        tab_to_locator(page, go_on)
        page.keyboard.press("Enter")
        expect(page).to_have_url(preferences)
        expect(page.locator('[aria-current="step"]')).to_contain_text("Job preferences")

    for key in (failing, connected):
        for log_file in running_app.log_files():
            assert key.encode() not in log_file.read_bytes(), log_file
        assert key not in running_app.stdout_text()
        assert key not in running_app.stderr_text()


@pytest.mark.parametrize("running_app", [PROVIDER_STEP], indirect=True)
def test_provider_testing_note_is_hidden_until_a_test_runs(
    running_app: RunningApp, browser: Browser
) -> None:
    with new_page(browser, DESKTOP) as page:
        open_app(page, running_app, "/setup/provider")
        note = page.get_by_text("Testing the connection…")
        expect(note).to_have_count(1)
        expect(note).to_be_hidden()
