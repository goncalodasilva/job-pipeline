"""Step 2a of the setup guide in a real browser (M1.17; AC-M1.4, AC-M1.5; spec 3.9).

The app starts at the AI provider step (``running_app`` with ``"provider_step"``: step 1 done,
no provider saved). By keyboard only, the user chooses the subscription token, pastes a
fictional token and saves: the guide stays on the provider step, announces the save, shows
that a token is saved in the system keychain without any of its characters, and the token is
in the test keyring file (the app's keychain), not in the page. Removing it asks first, inline
and by keyboard: Esc or "Keep it" keeps it and returns focus to the Remove control; "Remove
token" deletes it (M1.21 follow-up). At 390x844 and 360x740, in light
and dark, the step and its field-error state have no serious or critical axe findings and no
horizontal scroll. With ``JOBPIPELINE_ALLOW_FAKE_PROVIDERS`` unset the fake choice is not
offered (ADR-059). The app runs on loopback with temporary folders; every value is fictional.
"""

import json
import re

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_shell import has_no_horizontal_scroll, new_page, open_app
from tests.running_app import AppStart, RunningApp, SetupState

pytestmark = pytest.mark.e2e

PROVIDER_STEP: SetupState = "provider_step"
SUBSCRIPTION_RADIO = "#kind-claude_subscription_token"
API_KEY_RADIO = "#kind-claude_api_key"
SAVE = 'button[name="action"][value="save"]'
REMOVE_TOKEN = "#remove-claude_subscription_token"  # noqa: S105 - a selector, not a secret
KEEP_IT = 'button[name="keep"]'
MAX_TABS = 120  # Settings has more than 40 tab stops since the usage budgets (M2.22)
SMALL_PHONE = {"width": 360, "height": 740}  # the narrowest supported layout (360 px)


def anthropic_shaped(kind: str, body: str) -> str:
    """An Anthropic-shaped fictional credential, built at run time."""
    return "-".join(["sk", "ant", kind, body])


FICTIONAL_TOKEN = anthropic_shaped("oat01", "FICTIONALe2eQz7Wv3Kp9Xr2Lm5Jd8Tb")


def has_focus(page: Page, selector: str) -> bool:
    focused: bool = page.evaluate("(s) => document.activeElement?.matches(s) ?? false", selector)
    return focused


def tab_to(page: Page, selector: str) -> None:
    """Press Tab until ``selector`` has focus (keyboard only, no clicks)."""
    for _ in range(MAX_TABS):
        page.keyboard.press("Tab")
        if has_focus(page, selector):
            return
    raise AssertionError(f"{selector} never received focus within {MAX_TABS} Tab presses")


def keyring_values(app: RunningApp) -> list[str]:
    """Every value in the app's test keyring file (``tests/running_app.py``)."""
    path = app.data_dir.parent / "keyring.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [value for items in data.values() for value in items.values()]


@pytest.mark.parametrize("running_app", [PROVIDER_STEP], indirect=True)
def test_provider_desktop_keyboard_choose_paste_save(
    running_app: RunningApp, browser: Browser
) -> None:
    with new_page(browser, DESKTOP) as page:
        open_app(page, running_app, "/")
        assert page.url == f"{running_app.base_url}/setup/provider", page.url
        expect(page.locator('[aria-current="step"]')).to_have_text("AI provider")
        expect(page.locator("#kind-fake")).to_have_count(1)  # fakes allowed in this run
        expect(page.locator("#terms-note a")).to_have_attribute(
            "href", "https://docs.claude.com/en/docs/agent-sdk/overview"
        )

        # Tab into the radio group; arrow keys move within it, Space selects.
        tab_to(page, 'input[name="kind"]')
        for _ in range(MAX_TABS):
            if has_focus(page, SUBSCRIPTION_RADIO):
                break
            page.keyboard.press("ArrowDown")
        page.keyboard.press("Space")
        expect(page.locator(SUBSCRIPTION_RADIO)).to_be_checked()

        tab_to(page, "#credential")
        page.keyboard.type(FICTIONAL_TOKEN)
        tab_to(page, SAVE)
        page.keyboard.press("Enter")

        expect(page.locator("#announcer")).to_contain_text("Saved")
        expect(page).to_have_url(f"{running_app.base_url}/setup/provider")
        expect(page.locator('[aria-current="step"]')).to_have_text("AI provider")
        expect(page).to_have_title(re.compile(r"^AI provider"))
        expect(page.locator("#credential-status")).to_contain_text("system keychain")
        expect(page.locator("#credential")).to_have_value("")
        expect(page.locator(SUBSCRIPTION_RADIO)).to_be_checked()
        expect(page.locator("#credential-status")).to_be_focused()
        expect(
            page.locator('button[name="remove"][value="claude_subscription_token"]')
        ).to_have_text("Remove saved token")
        expect(page.locator("h1")).to_have_count(1)
        assert FICTIONAL_TOKEN not in page.content()
        assert "sk-ant" not in page.content()

        # Reloading shows the same state, still without the token.
        open_app(page, running_app, "/setup/provider")
        expect(page.locator("#credential-status")).to_contain_text("system keychain")
        assert FICTIONAL_TOKEN not in page.content()

        # Removing the saved token asks first, in the page, by keyboard (no browser dialog).
        page.on("dialog", lambda dialog: pytest.fail(f"unexpected {dialog.type} dialog"))
        tab_to(page, REMOVE_TOKEN)
        page.keyboard.press("Enter")
        question = page.locator("#remove-question-claude_subscription_token")
        expect(question).to_have_text("Remove the saved token? You'd have to paste it again.")
        expect(question).to_be_focused()
        assert FICTIONAL_TOKEN in keyring_values(running_app), "asking deletes nothing"
        assert_no_serious_violations(page)
        page.set_viewport_size(PHONE)
        fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert fits, f"confirm at 390 px: scrollWidth {scroll_width} > {inner_width}"
        assert_no_serious_violations(page)
        page.set_viewport_size(DESKTOP)

        # Esc answers like "Keep it": the question goes and focus returns to Remove.
        page.keyboard.press("Escape")
        expect(page.locator(KEEP_IT)).to_have_count(0)
        expect(page.locator(REMOVE_TOKEN)).to_be_focused()
        expect(page.locator("#announcer")).to_have_text("The saved token was kept.")
        assert FICTIONAL_TOKEN in keyring_values(running_app)

        # "Keep it": the question goes, focus returns to the Remove control, nothing deleted.
        page.keyboard.press("Enter")
        expect(question).to_be_focused()
        tab_to(page, KEEP_IT)
        page.keyboard.press("Enter")
        expect(page.locator(KEEP_IT)).to_have_count(0)
        expect(page.locator(REMOVE_TOKEN)).to_be_focused()
        expect(page.locator("#announcer")).to_have_text("The saved token was kept.")
        assert FICTIONAL_TOKEN in keyring_values(running_app)
        # The Remove control stays in the Tab order after taking the focus.
        page.keyboard.press("Shift+Tab")
        tab_to(page, REMOVE_TOKEN)

        # Asked again and confirmed: the token is removed from the keychain.
        page.keyboard.press("Enter")
        expect(question).to_be_focused()
        page.keyboard.press("Tab")
        confirm = page.locator(".remove-confirm").get_by_role(
            "button", name="Remove token", exact=True
        )
        expect(confirm).to_be_focused()
        page.keyboard.press("Enter")
        expect(page.locator("#announcer")).to_have_text("The saved token was removed.")
        expect(page.locator(REMOVE_TOKEN)).to_have_count(0)
        expect(page.locator("#credential-status")).to_have_text("No token saved yet.")
        assert_no_serious_violations(page)

    assert FICTIONAL_TOKEN not in keyring_values(running_app), "removed once confirmed"
    for log_file in running_app.log_files():
        assert FICTIONAL_TOKEN.encode() not in log_file.read_bytes(), log_file
    assert FICTIONAL_TOKEN not in running_app.stdout_text()
    assert FICTIONAL_TOKEN not in running_app.stderr_text()
    database = running_app.data_dir / "app.db"
    for path in (database, database.with_name("app.db-wal")):
        if path.exists():
            assert FICTIONAL_TOKEN.encode() not in path.read_bytes(), path


@pytest.mark.parametrize("running_app", [PROVIDER_STEP], indirect=True)
@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("viewport", [PHONE, SMALL_PHONE], ids=["390x844", "360x740"])
def test_provider_phone_axe(
    running_app: RunningApp, browser: Browser, scheme: str, viewport: dict[str, int]
) -> None:
    refused = "FICTIONAL-short"
    with new_page(browser, viewport, color_scheme=scheme) as page:
        open_app(page, running_app, "/setup/provider")
        expect(page.locator("#credential")).to_be_visible()
        expect(page.locator("#env-fallback")).to_be_attached()
        assert_no_serious_violations(page)
        fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert fits, f"scrollWidth {scroll_width} > {inner_width}"

        # The field-error state: a too-short API key is refused, tied to the field and not
        # shown back.
        page.locator(API_KEY_RADIO).check()
        page.locator("#credential").fill(refused)
        page.locator(SAVE).click()
        field = page.locator('#credential[aria-invalid="true"]')
        expect(field).to_have_count(1)
        expect(field).to_have_value("")
        described = field.get_attribute("aria-describedby") or ""
        assert described.split(), "the error is tied to the field"
        expect(page.locator(f"#{described.split()[0]}")).to_be_visible()
        expect(page.locator(API_KEY_RADIO)).to_be_checked()
        expect(page).to_have_url(f"{running_app.base_url}/setup/provider")
        assert refused not in page.content()
        assert_no_serious_violations(page)
        fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert fits, f"error state: scrollWidth {scroll_width} > {inner_width}"

    assert refused not in keyring_values(running_app)


@pytest.mark.parametrize(
    "running_app", [AppStart(setup=PROVIDER_STEP, allow_fakes=False)], indirect=True
)
def test_provider_fakes_off_shows_no_fake_choice(running_app: RunningApp, browser: Browser) -> None:
    with new_page(browser, DESKTOP) as page:
        open_app(page, running_app, "/setup/provider")
        expect(page.locator('input[name="kind"]')).to_have_count(3)
        expect(page.locator("#kind-fake")).to_have_count(0)
        assert "Fake (testing only)" not in page.content()
        assert_no_serious_violations(page)
