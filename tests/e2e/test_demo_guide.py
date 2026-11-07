"""The demo's setup guide, step 2, in a real browser (M2.26 review; user decision of
2026-10-11): in the demo the practice provider needs nothing pasted.

The real app runs as a subprocess with ``--demo --setup`` (no ``JOBPIPELINE_DATA_DIR``, no
``JOBPIPELINE_ALLOW_FAKE_PROVIDERS``) on a folder that doesn't exist yet: the demo folder the
app chooses under the test's temporary home (``tests/running_app.py``). Since M9.5 a new demo
folder is seeded with the demo dataset, which already holds the practice provider and a
finished setup, so ``--setup`` reopens the guide over it: here "already chosen" and "the step
is done" come from the dataset. The rule that the fake is preselected while nothing is saved
(ADR-088 item 2) is covered on an emptied demo, as Delete all data leaves it, by
``tests/integration/test_demo_guide_provider.py`` (``demo_dataset=None``; ADR-138 item 5).

By keyboard only:

1. Step 1 (the folder is the demo's, so only its button is needed) leads to step 2.
2. Step 2 says "You're in the demo. Choose the practice provider below; it needs no account
   or key." above the choices, and "Fake (testing only)" is already chosen. While it is, the
   credential field, its keychain hint and the environment-variable note are not shown.
   Arrow keys to another choice show them again; back on the fake they go.
3. Test connection, with nothing typed, answers "Connected (fake-fast)": the result takes
   focus, the step is done, and the note shown while testing said nothing of plan or credit.
4. Continue leads to step 3.

Axe finds nothing serious or critical on step 2 in light and in dark, before and after the
test; at 390x844 nothing scrolls sideways.

A second test shows whose keychain the demo uses: a fictional token pasted and saved in the
demo lands in the test's keyring file under the demo's own service name, and nowhere else.

No fixed waits. Every value is fictional; the token is built at run time.
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from app.config import BOOTSTRAP_FILE, DEMO_APP_NAME, ENV_DATA_DIR
from app.wiring import ALLOW_FAKES_VARIABLE
from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_setup_guide_full import (
    WELCOME_SUBMIT,
    assert_fits,
    assert_on_step,
    assert_swapped_to,
    continue_link,
    expect_step_done,
)
from tests.e2e.test_setup_provider import (
    API_KEY_RADIO,
    SAVE,
    SUBSCRIPTION_RADIO,
    anthropic_shaped,
    has_focus,
    tab_to,
)
from tests.e2e.test_setup_provider_test import (
    FAKE_RADIO,
    connection_button,
    tab_to_locator,
)
from tests.e2e.test_shell import new_page, open_app
from tests.running_app import (
    DEMO_FLAG,
    KEYRING_BACKEND_VARIABLE,
    TEST_KEYRING_BACKEND,
    AppDidNotStart,
    RunningApp,
    start_app,
)

pytestmark = pytest.mark.e2e

DEMO_NOTE = "You're in the demo. Choose the practice provider below; it needs no account or key."
FAKE_LABEL = "Fake (testing only)"
CONNECTED = "Connected (fake-fast)"
NOTE = "#demo-note"
FIELD = "#credential"
FIELD_LABEL = 'label[for="credential"]'
HINT = "#credential-hint"
VARIABLE_NOTE = "#env-fallback"
STATUS = "#credential-status"
TESTING_NOTE = "#provider-testing"
CREDIT = f"{TESTING_NOTE} .uses-credit"
RESULT = "#connection-result"
PROVIDER = "/setup/provider"
DEMO_KEYCHAIN_SERVICE = "jobpipeline-demo"
TOKEN_NAME = "claude_code_oauth_token"  # noqa: S105 - a key name, not a secret
MAX_ARROWS = 6
# Records how the testing note looked each time it was shown (it is shown only while the
# request runs, too briefly to ask for afterwards).
WATCH_TESTING_NOTE_JS = """() => {
  window.__testingNote = [];
  const note = document.querySelector("#provider-testing");
  const look = () => {
    if (!note.classList.contains("htmx-request")) return;
    const credit = note.querySelector(".uses-credit");
    window.__testingNote.push({
      shown: getComputedStyle(note).display !== "none",
      credit: getComputedStyle(credit).display !== "none",
      text: note.innerText.replace(/\\s+/g, " ").trim(),
    });
  };
  new MutationObserver(look).observe(note, { attributes: true, attributeFilter: ["class"] });
}"""


@pytest.fixture
def demo_app(tmp_path: Path) -> Iterator[RunningApp]:
    """``--demo`` alone, on a demo folder that doesn't exist yet."""
    try:
        app = start_app(
            tmp_path,
            demo=True,
            setup="fresh",
            allow_fakes=False,
            cli_args=("--setup",),
            output_name="demo",
        )
    except AppDidNotStart as error:
        pytest.fail(f"the demo did not start: {error}", pytrace=False)
    try:
        yield app
    finally:
        result = app.stop()
    assert not result.killed, app.output_tail()
    assert result.returncode == 0, app.output_tail()


def assert_a_real_demo_run(app: RunningApp, tmp_path: Path) -> None:
    command = app.process.args
    assert isinstance(command, list) and DEMO_FLAG in command
    assert ENV_DATA_DIR not in app.env and ALLOW_FAKES_VARIABLE not in app.env
    folder = app.data_dir.resolve()
    assert folder.is_relative_to((tmp_path / "home").resolve())
    assert DEMO_APP_NAME in folder.parts
    assert app.env[KEYRING_BACKEND_VARIABLE] == TEST_KEYRING_BACKEND


def keychain(app: RunningApp) -> dict[str, dict[str, str]]:
    """Everything in the test keyring file the app was pointed at, by service."""
    if not app.keyring_file.exists():
        return {}
    stored: dict[str, dict[str, str]] = json.loads(app.keyring_file.read_text(encoding="utf-8"))
    return {service: items for service, items in stored.items() if items}


def to_step_2_by_keyboard(page: Page, app: RunningApp) -> None:
    open_app(page, app, "/")
    assert_on_step(page, app, "welcome", "Welcome and data folder")
    tab_to(page, WELCOME_SUBMIT)
    page.keyboard.press("Enter")
    assert_swapped_to(page, app, "provider")


def arrow_to(page: Page, selector: str, key: str) -> None:
    """Move within the radio group with an arrow key until ``selector`` is the one chosen
    (arrow keys choose as they move)."""
    for _ in range(MAX_ARROWS):
        if has_focus(page, selector):
            break
        page.keyboard.press(key)
    assert has_focus(page, selector), f"{selector} was not reached with {key}"
    expect(page.locator(selector)).to_be_checked()


def expect_nothing_to_paste(page: Page) -> None:
    """The practice provider is chosen: no field, no hint, no variable note."""
    expect(page.locator(FAKE_RADIO)).to_be_checked()
    for selector in (FIELD, FIELD_LABEL, HINT, VARIABLE_NOTE):
        expect(page.locator(selector)).to_be_hidden()
    expect(page.locator(STATUS)).to_be_visible()
    expect(page.locator(STATUS)).to_have_text("The fake provider needs no key.")


def expect_something_to_paste(page: Page) -> None:
    for selector in (FIELD, FIELD_LABEL, HINT, VARIABLE_NOTE):
        expect(page.locator(selector)).to_be_visible()


def demo_note(page: Page) -> Locator:
    return page.locator(NOTE)


def test_demo_guide_step_2_by_keyboard(
    demo_app: RunningApp, browser: Browser, tmp_path: Path
) -> None:
    app = demo_app
    assert_a_real_demo_run(app, tmp_path)
    with new_page(browser, DESKTOP) as page:
        to_step_2_by_keyboard(page, app)

        # The line above the choices, and the practice provider already chosen.
        expect(demo_note(page)).to_be_visible()
        expect(demo_note(page)).to_have_text(DEMO_NOTE)
        note_box, choices_box = (
            demo_note(page).bounding_box(),
            page.locator("fieldset.choices").bounding_box(),
        )
        assert note_box is not None and choices_box is not None
        assert note_box["y"] + note_box["height"] <= choices_box["y"] + 1, "above the choices"
        expect(page.locator(f'label[for="{FAKE_RADIO.removeprefix("#")}"]')).to_have_text(
            FAKE_LABEL
        )
        expect_nothing_to_paste(page)
        assert_no_serious_violations(page)

        # Tab reaches the chosen radio; arrows show what another choice needs, and hide it
        # again on the way back.
        tab_to(page, 'input[name="kind"]')
        assert has_focus(page, FAKE_RADIO), "Tab lands on the chosen provider"
        arrow_to(page, API_KEY_RADIO, "ArrowUp")
        expect_something_to_paste(page)
        expect(page.locator(CREDIT)).to_have_count(1)
        arrow_to(page, FAKE_RADIO, "ArrowDown")
        expect_nothing_to_paste(page)

        # Test connection with nothing typed: the next Tab stops are the two buttons.
        page.evaluate(WATCH_TESTING_NOTE_JS)
        page.keyboard.press("Tab")
        assert has_focus(page, SAVE), "no hidden field takes a Tab stop"
        tab_to_locator(page, connection_button(page))
        page.keyboard.press("Enter")

        result = page.locator(RESULT)
        expect(result).to_have_text(CONNECTED)
        expect(result).to_be_focused()
        expect(page.locator("#announcer")).to_have_text(CONNECTED)
        expect_step_done(page, "AI provider")
        expect_nothing_to_paste(page)
        seen: list[dict[str, object]] = page.evaluate("() => window.__testingNote")
        assert seen, "the testing note was shown while the test ran"
        for look in seen:
            assert look["shown"] is True and look["credit"] is False, look
            assert look["text"] == "Testing the connection… This can take up to a minute.", look
        assert_no_serious_violations(page)

        tab_to_locator(page, continue_link(page))
        page.keyboard.press("Enter")
        assert_swapped_to(page, app, "web_search")

    # Dark, and a phone: the step as it is now (the practice provider saved and tested).
    with new_page(browser, DESKTOP, color_scheme="dark") as dark:
        open_app(dark, app, PROVIDER)
        expect(demo_note(dark)).to_have_text(DEMO_NOTE)
        expect_nothing_to_paste(dark)
        expect(dark.locator(RESULT)).to_have_text(CONNECTED)
        assert_no_serious_violations(dark)
    assert PHONE == {"width": 390, "height": 844}
    for scheme in ("light", "dark"):
        with new_page(browser, PHONE, color_scheme=scheme) as phone:
            open_app(phone, app, PROVIDER)
            expect(demo_note(phone)).to_be_visible()
            expect_nothing_to_paste(phone)
            assert_fits(phone, f"demo step 2 at 390 px, {scheme}")
            assert_no_serious_violations(phone)

    assert keychain(app) == {}, "the practice provider keeps nothing in any keychain"
    assert list(tmp_path.rglob(BOOTSTRAP_FILE)) == [], "the demo writes no settings file"


def test_demo_saves_a_pasted_token_in_the_demo_keychain_only(
    demo_app: RunningApp, browser: Browser, tmp_path: Path
) -> None:
    """Whose keychain the demo uses: the test's file (never the computer's), under the
    demo's own service name, so a real install's saved token is never read or replaced."""
    app = demo_app
    assert_a_real_demo_run(app, tmp_path)
    token = anthropic_shaped("oat01", "FICTIONALdemoGuideQz7Wv3Kp9Xr2Lm5")
    with new_page(browser, DESKTOP) as page:
        to_step_2_by_keyboard(page, app)
        tab_to(page, 'input[name="kind"]')
        arrow_to(page, SUBSCRIPTION_RADIO, "ArrowUp")
        expect_something_to_paste(page)
        tab_to(page, FIELD)
        page.keyboard.type(token)
        tab_to(page, SAVE)
        page.keyboard.press("Enter")

        expect(page.locator("#announcer")).to_contain_text("Saved")
        expect(page.locator(STATUS)).to_contain_text("system keychain")
        expect(page.locator(SUBSCRIPTION_RADIO)).to_be_checked()  # the saved choice stays
        expect_something_to_paste(page)
        assert token not in page.content()

    assert keychain(app) == {DEMO_KEYCHAIN_SERVICE: {TOKEN_NAME: token}}
    assert app.keyring_file.resolve().is_relative_to(tmp_path.resolve())
