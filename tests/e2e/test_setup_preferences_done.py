"""Steps 5 and 6 of the setup guide in a real browser (M1.19; AC-M1.6, AC-M1.7; spec 3.9).

The app starts at the job preferences step (``running_app`` with ``"preferences_step"``: steps 1
and 2 done, the fake provider saved). By keyboard only, the user types target roles and a
location, ticks "remote", enters a minimum salary with its currency, chooses the CV country and
continues: the guide moves to Done with htmx (URL, step list, title, focus and announcement).
On Done the step list and summary show every step as done in text, and "Add a job" finishes the
guide and lands on the job inbox; Today then opens without the guide. At desktop and 390x844, in
light and dark, both steps (and the preferences field-error state) have no serious or critical
axe findings and no horizontal scroll. The app runs on loopback with temporary folders; every
value is fictional.
"""

import re

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_shell import has_no_horizontal_scroll, new_page, open_app
from tests.running_app import RunningApp

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.parametrize("running_app", ["preferences_step"], indirect=True),
]

ROLES = '[name="target_roles"]'
LOCATIONS = '[name="locations"]'
REMOTE = 'input[name="work_modes"][value="remote"]'
AMOUNT = '[name="salary_amount"]'
CURRENCY = 'select[name="salary_currency"]'
COUNTRY = 'select[name="cv_country"]'
CONTINUE = '#setup-step form[hx-post="/setup/preferences"] button[type="submit"]'
ADD_JOB = 'button[name="then"][value="add_job"]'
MAX_TABS = 40
AXE_CASES = [
    pytest.param(DESKTOP, "light", id="desktop-light"),
    pytest.param(DESKTOP, "dark", id="desktop-dark"),
    pytest.param(PHONE, "light", id="phone-light"),
    pytest.param(PHONE, "dark", id="phone-dark"),
]


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


def assert_fits(page: Page, state: str) -> None:
    fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
    assert fits, f"{state}: scrollWidth {scroll_width} > {inner_width}"


def fill_minimal_and_continue(page: Page, app: RunningApp) -> None:
    """Not keyboard-only: used by the axe tests to reach the Done step."""
    expect(page.locator(ROLES)).to_be_visible()
    page.locator(ROLES).fill("Fictional Archivist")
    page.locator(COUNTRY).select_option("PT")
    page.locator(CONTINUE).click()
    expect(page).to_have_url(f"{app.base_url}/setup/done")


def test_preferences_and_done_keyboard_add_a_job(running_app: RunningApp, browser: Browser) -> None:
    with new_page(browser, DESKTOP) as page:
        open_app(page, running_app, "/")
        assert page.url == f"{running_app.base_url}/setup/preferences", page.url
        expect(page.locator('[aria-current="step"]')).to_contain_text("Job preferences")
        expect(page.locator("#step-heading")).to_have_text("Job preferences")

        tab_to(page, ROLES)
        page.keyboard.type("Fictional Archivist")
        page.keyboard.press("Enter")  # a new line inside the textarea, not a submit
        page.keyboard.type("Fictional Lighthouse Keeper")
        expect(page).to_have_url(f"{running_app.base_url}/setup/preferences")

        tab_to(page, LOCATIONS)
        page.keyboard.type("Fictionville")

        tab_to(page, REMOTE)
        page.keyboard.press("Space")
        expect(page.locator(REMOTE)).to_be_checked()

        tab_to(page, AMOUNT)
        page.keyboard.type("45,000")

        tab_to(page, CURRENCY)
        page.keyboard.type("Euro")  # type-ahead on the focused select
        expect(page.locator(CURRENCY)).to_have_value("EUR")

        tab_to(page, COUNTRY)
        page.keyboard.type("Portugal")
        expect(page.locator(COUNTRY)).to_have_value("PT")

        tab_to(page, CONTINUE)
        page.keyboard.press("Enter")

        expect(page).to_have_url(f"{running_app.base_url}/setup/done")
        expect(page.locator('[aria-current="step"]')).to_contain_text("Done")
        expect(page).to_have_title(re.compile(r"^Done"))
        expect(page.locator("#step-heading")).to_be_focused()
        expect(page.locator("#announcer")).to_have_text("Step 6 of 6: Done")
        expect(page.locator("h1")).to_have_count(1)
        prefs_item = page.locator("ol.setup-steps > li").filter(has_text="Job preferences")
        expect(prefs_item).to_contain_text("Done")
        for step_id in ("welcome", "provider", "preferences"):
            expect(page.locator(f'#setup-step a[href="/setup/{step_id}"]')).to_have_count(1)

        tab_to(page, ADD_JOB)
        expect(page.locator(ADD_JOB)).to_have_text("Add a job")
        page.keyboard.press("Enter")

        expect(page).to_have_url(f"{running_app.base_url}/inbox")
        expect(page.locator("#page-heading")).to_have_text("Job inbox")

        # Setup is complete: Today opens without the guide.
        open_app(page, running_app, "/")
        assert page.url == f"{running_app.base_url}/", page.url
        expect(page.locator("#page-heading")).to_have_text("Today")

        # Revisiting step 5 shows what was saved.
        open_app(page, running_app, "/setup/preferences")
        expect(page.locator(ROLES)).to_have_value(
            "Fictional Archivist\nFictional Lighthouse Keeper"
        )
        expect(page.locator(REMOTE)).to_be_checked()
        expect(page.locator(CURRENCY)).to_have_value("EUR")
        expect(page.locator(COUNTRY)).to_have_value("PT")


@pytest.mark.parametrize(("viewport", "scheme"), AXE_CASES)
def test_preferences_axe_and_error_state(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int], scheme: str
) -> None:
    with new_page(browser, viewport, color_scheme=scheme) as page:
        open_app(page, running_app, "/setup/preferences")
        expect(page.locator(ROLES)).to_be_visible()
        assert_no_serious_violations(page)
        assert_fits(page, "preferences")

        # The field-error state: no target role, no country chosen and an amount that isn't a
        # number; each error is tied to its field and the first invalid field has focus.
        page.locator(AMOUNT).fill("lots")
        page.locator(CONTINUE).click()
        roles = page.locator(f'{ROLES}[aria-invalid="true"]')
        expect(roles).to_have_count(1)
        expect(roles).to_be_focused()
        expect(page.locator("#target_roles-error")).to_be_visible()
        expect(page.locator("#cv_country-error")).to_be_visible()
        expect(page.locator("#salary_amount-error")).to_be_visible()
        expect(page.locator(AMOUNT)).to_have_value("lots")
        expect(page).to_have_title(re.compile(r"^Error: Job preferences"))
        expect(page).to_have_url(f"{running_app.base_url}/setup/preferences")
        assert_no_serious_violations(page)
        assert_fits(page, "preferences error state")


@pytest.mark.parametrize(("viewport", "scheme"), AXE_CASES)
def test_done_axe(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int], scheme: str
) -> None:
    with new_page(browser, viewport, color_scheme=scheme) as page:
        open_app(page, running_app, "/setup/preferences")
        fill_minimal_and_continue(page, running_app)
        expect(page.locator(ADD_JOB)).to_be_visible()
        assert_no_serious_violations(page)
        assert_fits(page, "done")

        # A fresh load of the Done page (not through htmx) is clean too.
        open_app(page, running_app, "/setup/done")
        expect(page.locator(ADD_JOB)).to_be_visible()
        assert_no_serious_violations(page)
        assert_fits(page, "done (loaded)")
