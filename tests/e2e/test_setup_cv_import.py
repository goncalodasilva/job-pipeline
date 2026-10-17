"""Setup step 4, Import CV, in a real browser (M3.7; AC-M3.8 Done-screen part; spec 3.9).

The app starts with steps 1 and 2 done and the fake provider saved (``running_app`` with
``"preferences_step"``). Keyboard only, at 390x844 and on desktop: "Skip for now" moves on to
Job preferences ("CV import skipped. Step 5 of 6: Job preferences" announced, focus on the
heading); Done lists "Import CV: Skipped" with a link that opens the step again; a pasted
fictional CV is imported with "Import and continue" ("CV import started." announced first)
and the step reads "Done". A refused upload keeps the step with its error tied to the file
field. Axe finds nothing serious and nothing scrolls sideways, in light and dark.

Selectors follow ``tests/integration/test_setup_cv_import.py``. No fixed waits; every value
is fictional.
"""

import re

import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_facts_page import tab_until
from tests.e2e.test_shell import has_no_horizontal_scroll, new_page, open_app
from tests.running_app import RunningApp

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.parametrize("running_app", ["preferences_step"], indirect=True),
]

STEP = "/setup/cv_import"
TITLE = "Import CV"
SECTION = "#setup-step"
SKIP = f'{SECTION} button[name="action"][value="skip"]'
PASTE = "#setup-cv-text"
FILE = "#setup-cv-file"
START = "#setup-cv-start"
ERROR = "#setup-cv-error"
NEXT = "Step 5 of 6: Job preferences"
SKIPPED_SAID = "CV import skipped."
STARTED_SAID = "CV import started."
FICTIONAL_CV = "Alex Example, fictional route planner at Example Freight Lda, 2019 to 2024."
CURRENT = '[aria-current="step"]'
VIEWPORTS = [
    pytest.param(DESKTOP, "light", id="desktop-light"),
    pytest.param(DESKTOP, "dark", id="desktop-dark"),
    pytest.param(PHONE, "light", id="390x844-light"),
    pytest.param(PHONE, "dark", id="390x844-dark"),
]


def url(app: RunningApp, path: str) -> str:
    return f"{app.base_url}{path}"


def assert_fits(page: Page, state: str) -> None:
    fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
    assert fits, f"{state}: scrollWidth {scroll_width} > {inner_width}"


def assert_on_step(page: Page, app: RunningApp) -> None:
    expect(page).to_have_url(url(app, STEP))
    expect(page.locator(CURRENT)).to_contain_text(TITLE)
    expect(page.locator("#step-heading")).to_have_text(TITLE)
    expect(page.locator("h1")).to_have_count(1)


def assert_moved_to_preferences(page: Page, app: RunningApp, said: str) -> None:
    expect(page).to_have_url(url(app, "/setup/preferences"))
    expect(page.locator(CURRENT)).to_contain_text("Job preferences")
    expect(page.locator("#step-heading")).to_be_focused()
    expect(page.locator("#announcer")).to_have_text(f"{said} {NEXT}")


def step_status(page: Page) -> Locator:
    named = re.compile(rf"^{re.escape(TITLE)}(\s·|$)")
    return page.locator("ol.setup-steps > li").filter(has_text=named).locator(".step-status")


@pytest.mark.parametrize(("viewport", "scheme"), VIEWPORTS)
def test_ac_m3_8_step4_by_keyboard_skip_then_import_with_axe(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int], scheme: str
) -> None:
    app = running_app
    with new_page(browser, viewport, color_scheme=scheme) as page:
        open_app(page, app, STEP)
        assert_on_step(page, app)
        expect(page.locator(START)).to_have_text("Import and continue")
        expect(page.locator(SKIP)).to_have_text("Skip for now")
        assert_no_serious_violations(page)
        assert_fits(page, "step 4")

        # Skip for now, by keyboard.
        tab_until(page, page.locator(SKIP))
        page.keyboard.press("Enter")
        assert_moved_to_preferences(page, app, SKIPPED_SAID)
        expect(step_status(page)).to_have_text("· Skipped")

        # Done lists it as skipped; its link opens the step again, by keyboard.
        open_app(page, app, "/setup/done")
        row = page.locator("ul.setup-summary > li").filter(has_text=TITLE)
        expect(row).to_have_text(f"{TITLE}: Skipped", use_inner_text=True)
        link = row.get_by_role("link", name=TITLE)
        expect(link).to_have_attribute("href", STEP)
        tab_until(page, link)
        page.keyboard.press("Enter")
        assert_on_step(page, app)

        # A refused upload: nothing typed or chosen. The error is tied to the file field.
        tab_until(page, page.locator(START))
        page.keyboard.press("Enter")
        expect(page.locator(f'{FILE}[aria-invalid="true"]')).to_have_count(1)
        expect(page.locator(ERROR)).to_be_visible()
        assert_on_step(page, app)
        assert_no_serious_violations(page)
        assert_fits(page, "step 4 with an error")

        # Paste a fictional CV and import it, by keyboard.
        tab_until(page, page.locator(PASTE))
        page.keyboard.type(FICTIONAL_CV)
        tab_until(page, page.locator(START))
        page.keyboard.press("Enter")
        assert_moved_to_preferences(page, app, STARTED_SAID)
        expect(step_status(page)).to_have_text("· Done")
        assert_no_serious_violations(page)

        open_app(page, app, "/setup/done")
        row = page.locator("ul.setup-summary > li").filter(has_text=TITLE)
        expect(row).to_have_text(f"{TITLE}: Done", use_inner_text=True)
        assert_fits(page, "done")
