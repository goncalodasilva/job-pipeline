"""M9.5 part C: the real command line opens a usable, fictional demo (AC-M9.4; ADR-131 item 10).

``jobpipeline --demo`` on the app's own demo folder (under the test's temporary home) seeds the
shipped dataset and opens Today with it. By keyboard only: Ctrl/Cmd+K finds a demo job and
opens it, and G then I, B and F visit the Inbox, the Board and the Fact bank, each with no
serious axe violation in light and dark, on a desktop and a phone, and nothing sideways. No
provider credentials are needed and nothing is written to the keychain.
"""

import re
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_shell import has_no_horizontal_scroll, new_page, open_app
from tests.running_app import start_app

pytestmark = pytest.mark.e2e

GO = (
    ("i", "/inbox", "Inbox"),
    ("b", "/board", "Board"),
    ("f", "/facts", "Fact bank"),
)


def checked(page: Page) -> None:
    wait_for_settled_styles(page)
    assert_no_serious_violations(page)
    assert has_no_horizontal_scroll(page)[0]


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])
def test_m9_demo_journey(tmp_path: Path, browser: Browser, viewport: dict[str, int]) -> None:
    app = start_app(tmp_path, demo=True, setup="fresh", allow_fakes=False)
    try:
        for theme in ("light", "dark"):
            with new_page(browser, viewport, color_scheme=theme) as page:
                open_app(page, app)
                expect(page.get_by_role("heading", name="Today", exact=True)).to_be_visible()
                expect(page.locator("main")).to_contain_text("Fictional")
                checked(page)

                page.keyboard.press("ControlOrMeta+k")
                field = page.locator("#palette-input")
                expect(field).to_be_focused()
                page.keyboard.type("Irrigation")
                expect(page.locator("[data-palette-result]").first).to_be_visible()
                checked(page)
                page.keyboard.press("Enter")
                expect(page).to_have_url(re.compile(r"/(jobs|companies|applications)/demo-"))
                page.wait_for_function("() => !!window.htmx")
                checked(page)

                for key, path, heading in GO:
                    page.keyboard.press("g")
                    page.keyboard.press(key)
                    expect(page).to_have_url(app.base_url + path)
                    page.wait_for_function("() => !!window.htmx")
                    expect(page.locator("h1").first).to_contain_text(heading, ignore_case=True)
                    checked(page)
        assert not app.keyring_file.exists(), "the demo needs no saved keys"
    finally:
        stopped = app.stop()
    assert not stopped.killed and stopped.returncode == 0
