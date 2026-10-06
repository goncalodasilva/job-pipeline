"""Settings > Providers in a real browser (M1.21; AC-M1.3; spec 3.9).

The app starts with a completed setup (``running_app`` default: the fake provider saved; fakes
allowed). On ``/settings`` the Providers section (``#providers``) has no serious or critical
axe findings at desktop size and at 390x844 (no horizontal scroll there). By keyboard only the
user reaches the provider choice, the key field and "Test connection": a fictional key asking
for a usage-limit failure shows the mapped message, focused and announced; one asking for
success shows "Connected". The page stays on ``/settings`` (not the guide), and neither key is
shown, logged or printed. Every value is fictional.
"""

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_setup_provider import MAX_TABS, SMALL_PHONE, has_focus, tab_to
from tests.e2e.test_setup_provider_test import USAGE_LIMIT, fake_key, tab_to_locator
from tests.e2e.test_shell import has_no_horizontal_scroll, new_page, open_app
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e

SECTION = "#providers"
FAKE_RADIO = f"{SECTION} #kind-fake"


def assert_layout(page: Page, viewport: dict[str, int]) -> None:
    assert_no_serious_violations(page)
    if viewport == PHONE:
        fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert fits, f"scrollWidth {scroll_width} > {inner_width}"


def press_test_connection_with(page: Page, key: str) -> None:
    """Keyboard only: the fake radio (checked, so Tab lands on it), the key field, then
    "Test connection" and Enter."""
    tab_to(page, f'{SECTION} input[name="kind"]')
    for _ in range(MAX_TABS):
        if has_focus(page, FAKE_RADIO):
            break
        page.keyboard.press("ArrowDown")
    page.keyboard.press("Space")
    expect(page.locator(FAKE_RADIO)).to_be_checked()
    tab_to(page, f"{SECTION} #credential")
    page.keyboard.type(key)
    button = page.locator(SECTION).get_by_role("button", name="Test connection")
    tab_to_locator(page, button)
    page.keyboard.press("Enter")


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])
def test_settings_providers_keyboard_and_axe(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int]
) -> None:
    settings_url = f"{running_app.base_url}/settings"
    with new_page(browser, viewport) as page:
        open_app(page, running_app, "/settings")
        providers = page.locator(SECTION)
        expect(providers.get_by_role("heading", name="Providers", level=2)).to_be_visible()
        expect(page.locator(FAKE_RADIO)).to_be_checked()
        expect(providers.locator("#credential-status")).to_have_text(
            "The fake provider needs no key."
        )
        assert_layout(page, viewport)

        failing = fake_key("fake-usage-limit")
        press_test_connection_with(page, failing)
        result = providers.locator("#connection-result")
        expect(result).to_have_text(USAGE_LIMIT)
        expect(page.locator("#announcer")).to_have_text(USAGE_LIMIT)
        expect(result).to_be_focused()
        expect(page).to_have_url(settings_url)
        expect(page.locator('[aria-current="step"]')).to_have_count(0)
        for button in page.locator("#provider-form button").all():
            expect(button).to_be_enabled()  # re-enabled once the request ends
        assert failing not in page.content()
        assert_layout(page, viewport)

        connected = fake_key("fake-connected")
        press_test_connection_with(page, connected)
        expect(result).to_contain_text("Connected")
        expect(page.locator("#announcer")).to_contain_text("Connected")
        expect(result).to_be_focused()
        expect(page).to_have_url(settings_url)
        expect(providers.get_by_role("link", name="Continue")).to_have_count(0)
        assert connected not in page.content()
        assert_layout(page, viewport)

    for key in (failing, connected):
        for log_file in running_app.log_files():
            assert key.encode() not in log_file.read_bytes(), log_file
        assert key not in running_app.stdout_text()
        assert key not in running_app.stderr_text()


def assert_phone_layout(page: Page) -> None:
    """Every phone width: no serious axe findings and no horizontal scroll."""
    assert_no_serious_violations(page)
    fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
    assert fits, f"scrollWidth {scroll_width} > {inner_width}"


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("viewport", [PHONE, SMALL_PHONE], ids=["390x844", "360x740"])
def test_settings_providers_phone_axe(
    running_app: RunningApp, browser: Browser, scheme: str, viewport: dict[str, int]
) -> None:
    """At 390x844 and 360x740, light and dark: the section, its field-error state and a
    connection result have no serious axe findings and no horizontal scroll (M1.21 review)."""
    refused = "FICTIONAL-short"
    failing = fake_key("fake-usage-limit")
    with new_page(browser, viewport, color_scheme=scheme) as page:
        open_app(page, running_app, "/settings")
        providers = page.locator(SECTION)
        expect(providers.get_by_role("heading", name="Providers", level=2)).to_be_visible()
        assert_phone_layout(page)

        # The field-error state: a too-short API key is refused and not shown back.
        page.locator(f"{SECTION} #kind-claude_api_key").check()
        page.locator(f"{SECTION} #credential").fill(refused)
        providers.get_by_role("button", name="Save", exact=True).click()
        field = page.locator(f'{SECTION} #credential[aria-invalid="true"]')
        expect(field).to_have_count(1)
        expect(field).to_be_focused()
        expect(field).to_have_value("")
        expect(page).to_have_url(f"{running_app.base_url}/settings")
        assert refused not in page.content()
        assert_phone_layout(page)

        # A connection result.
        page.locator(FAKE_RADIO).check()
        page.locator(f"{SECTION} #credential").fill(failing)
        providers.get_by_role("button", name="Test connection").click()
        result = providers.locator("#connection-result")
        expect(result).to_have_text(USAGE_LIMIT)
        expect(result).to_be_focused()
        assert failing not in page.content()
        assert_phone_layout(page)
