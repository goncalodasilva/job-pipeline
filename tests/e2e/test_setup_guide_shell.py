"""The setup guide's page shell in a real browser (M1.9; AC-M1.2; spec 3.9).

A fresh app (no step done) sends ``/`` to the guide's first step. The guide page has one
``<h1>``, an ordered list of every step with ``aria-current="step"`` on the current one, a
title that starts with the step's title, no serious or critical axe findings at desktop and
390x844 in light and dark, and no horizontal scroll at 360 px. The app runs on loopback with
temporary folders; no user data is involved.
"""

import re

import pytest
from playwright.sync_api import Browser, expect

from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_shell import css_token, has_no_horizontal_scroll, new_page, open_app
from tests.running_app import RunningApp

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.parametrize("running_app", ["fresh"], indirect=True),
]

NARROW = {"width": 360, "height": 740}
STEPS = (
    ("welcome", "Welcome and data folder"),
    ("provider", "AI provider"),
    ("web_search", "Web search"),
    ("cv_import", "Import CV"),
    ("preferences", "Job preferences"),
    ("done", "Done"),
)
AXE_CASES = [
    pytest.param(DESKTOP, "light", id="desktop-light"),
    pytest.param(DESKTOP, "dark", id="desktop-dark"),
    pytest.param(PHONE, "light", id="phone-light"),
    pytest.param(PHONE, "dark", id="phone-dark"),
]


def test_root_lands_on_welcome_step(running_app: RunningApp, browser: Browser) -> None:
    with new_page(browser, DESKTOP) as page:
        open_app(page, running_app, "/")

        assert page.url == f"{running_app.base_url}/setup/welcome", page.url
        expect(page.locator("h1")).to_have_count(1)
        current = page.locator('[aria-current="step"]')
        expect(current).to_have_count(1)
        expect(current).to_have_text("Welcome and data folder")
        expect(page.locator("ol.setup-steps > li")).to_have_count(len(STEPS))
        expect(page.locator("#main h2")).to_be_visible()
        assert page.title().startswith("Welcome and data folder"), page.title()


@pytest.mark.parametrize(("step_id", "title"), STEPS, ids=[s for s, _ in STEPS])
def test_title_starts_with_step_title(
    running_app: RunningApp, browser: Browser, step_id: str, title: str
) -> None:
    with new_page(browser, DESKTOP) as page:
        open_app(page, running_app, f"/setup/{step_id}")

        expect(page).to_have_title(re.compile(rf"^{re.escape(title)} · Set up · "))
        expect(page.locator('[aria-current="step"]')).to_have_text(title)


@pytest.mark.parametrize(("viewport", "scheme"), AXE_CASES)
def test_guide_axe_no_serious_or_critical(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int], scheme: str
) -> None:
    with new_page(browser, viewport, color_scheme=scheme) as page:
        open_app(page, running_app, "/setup/welcome")
        assert css_token(page, "--color-bg"), "the colour tokens are not loaded"
        assert_no_serious_violations(page)


def test_guide_has_no_horizontal_scroll_at_360px(running_app: RunningApp, browser: Browser) -> None:
    with new_page(browser, NARROW) as page:
        for step_id, _ in STEPS:
            open_app(page, running_app, f"/setup/{step_id}")
            fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
            assert fits, f"/setup/{step_id}: scrollWidth {scroll_width} > {inner_width}"
