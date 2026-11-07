"""The real CLI opens a usable, fictional demo with no provider credentials."""

from pathlib import Path

import pytest
from playwright.sync_api import Browser, expect

from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_shell import has_no_horizontal_scroll, new_page, open_app
from tests.running_app import start_app

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])
def test_m9_demo_journey(tmp_path: Path, browser: Browser, viewport):
    app = start_app(tmp_path, demo=True, setup="fresh", allow_fakes=False)
    try:
        for theme in ("light", "dark"):
            with new_page(browser, viewport, color_scheme=theme) as page:
                open_app(page, app)
                expect(page.get_by_role("heading", name="Today", exact=True)).to_be_visible()
                assert_no_serious_violations(page)
                page.keyboard.press("ControlOrMeta+k")
                field = page.get_by_role("combobox")
                field.fill("Irrigation")
                expect(page.locator("[data-palette-result]").first).to_be_visible()
                page.keyboard.press("ArrowDown")
                page.keyboard.press("Enter")
                expect(page).to_have_url(__import__("re").compile(r"/jobs/demo-job-"))
                assert has_no_horizontal_scroll(page)
                assert_no_serious_violations(page)
        assert not app.keyring_file.exists()
    finally:
        stopped = app.stop()
    assert not stopped.killed and stopped.returncode == 0
