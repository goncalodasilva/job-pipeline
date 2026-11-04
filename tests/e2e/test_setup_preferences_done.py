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
from playwright.sync_api import Browser, Page, Route, expect

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
FORM = '#setup-step form[hx-post="/setup/preferences"]'
SLOT = "#preferences-form-slot"
FORM_URL = "/setup/preferences/form"
SKIP_CV = '#setup-step button[name="action"][value="skip"]'
# The keyboard order of step 5 once the form is there (it must not change, TD-342).
TAB_ORDER = [
    "target_roles",
    "locations",
    "work_modes=remote",
    "work_modes=hybrid",
    "work_modes=on_site",
    "salary_amount",
    "salary_currency",
    "must_haves",
    "deal_breakers",
    "cv_country",
    "button",
]
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


def wait_for_form(page: Page) -> None:
    """The form arrives by a protected fragment (TD-342): wait for it, then for the placeholder
    to be gone (conditions, no sleeps)."""
    expect(page.locator(FORM)).to_be_visible()
    expect(page.locator(SLOT)).to_have_count(0)


def hold_form(page: Page) -> list[Route]:
    """Keep the form fragment's request pending so the loading state can be inspected."""
    held: list[Route] = []

    def keep(route: Route) -> None:
        held.append(route)

    page.route(f"**{FORM_URL}", keep)
    return held


def wait_for_held(page: Page, held: list[Route]) -> None:
    for _ in range(5000):  # each round trip lets Playwright deliver pending events
        if held:
            return
        page.evaluate("() => 0")
    raise AssertionError("the form fragment was never requested")


def release(page: Page, held: list[Route]) -> None:
    for route in held:
        route.continue_()
    page.unroute(f"**{FORM_URL}")


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

        wait_for_form(page)  # TD-342: the form arrives by its protected fragment
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


def top_of(page: Page, selector: str) -> float:
    box = page.locator(selector).bounding_box()
    assert box is not None, selector
    return box["y"]


def test_td342_form_loads_by_a_token_fragment_without_moving_focus_or_layout(
    running_app: RunningApp, browser: Browser
) -> None:
    """The page is a shell with a visible loading line; the form (empty here: nothing is stored)
    arrives from GET /setup/preferences/form with the launch token, in the place of the
    placeholder. Focus stays where the user put it, nothing above the form moves and nothing is
    announced."""
    with new_page(browser, DESKTOP) as page:
        held = hold_form(page)
        requests: list[dict[str, str]] = []
        page.on("request", lambda r: requests.append(r.headers) if FORM_URL in r.url else None)
        open_app(page, running_app, "/setup/preferences")
        wait_for_held(page, held)
        expect(page.locator(SLOT)).to_be_visible()
        expect(page.locator(SLOT)).to_contain_text("Loading")
        expect(page.locator("#setup-step form")).to_have_count(0)
        assert requests and requests[0].get("x-jp-token"), "the launch token travels"

        page.keyboard.press("Tab")  # the user is somewhere while the form loads
        page.evaluate("() => { window.__focused = document.activeElement; }")
        heading_top = top_of(page, "#step-heading")
        slot_top = top_of(page, SLOT)

        release(page, held)
        wait_for_form(page)

        assert page.evaluate("() => document.activeElement === window.__focused"), "focus jumped"
        expect(page.locator("#setup-step form :focus")).to_have_count(0)
        assert top_of(page, "#step-heading") == heading_top, "the heading moved"
        assert abs(top_of(page, FORM) - slot_top) <= 4, "the form replaces the placeholder"
        expect(page.locator("#announcer")).to_have_text("")
        expect(page).to_have_url(f"{running_app.base_url}/setup/preferences")
        expect(page.locator(ROLES)).to_have_value("")  # nothing stored: the empty form
        expect(page.locator(COUNTRY)).to_have_value("")


def test_td342_tab_order_after_the_fragment_is_the_forms_order(
    running_app: RunningApp, browser: Browser
) -> None:
    with new_page(browser, DESKTOP) as page:
        open_app(page, running_app, "/setup/preferences")
        wait_for_form(page)
        tab_to(page, ROLES)
        seen = ["target_roles"]
        for _ in range(len(TAB_ORDER) - 1):
            page.keyboard.press("Tab")
            seen.append(
                page.evaluate(
                    """() => {
                      const e = document.activeElement;
                      if (e.tagName === 'BUTTON') return 'button';
                      return e.type === 'checkbox' ? e.name + '=' + e.value : e.name;
                    }"""
                )
            )
        assert seen == TAB_ORDER, seen
        assert has_focus(page, CONTINUE)
        # The placeholder offered nothing to tab to, and nothing is left behind.
        expect(page.locator(SLOT)).to_have_count(0)


def test_td342_keyboard_arrival_from_the_previous_step_keeps_focus_and_announcement(
    running_app: RunningApp, browser: Browser
) -> None:
    """ "Skip for now" on the CV step swaps the guide to step 5: the heading takes focus and the
    step is announced; the form arriving afterwards changes neither."""
    said = "CV import skipped. Step 5 of 6: Job preferences"
    with new_page(browser, DESKTOP) as page:
        open_app(page, running_app, "/setup/cv_import")
        held = hold_form(page)
        tab_to(page, SKIP_CV)
        page.keyboard.press("Enter")
        expect(page).to_have_url(f"{running_app.base_url}/setup/preferences")
        wait_for_held(page, held)
        expect(page.locator("#step-heading")).to_be_focused()
        expect(page.locator("#announcer")).to_have_text(said)
        expect(page.locator(SLOT)).to_be_visible()

        release(page, held)
        wait_for_form(page)
        expect(page.locator("#step-heading")).to_be_focused()
        expect(page.locator("#announcer")).to_have_text(said)
        expect(page).to_have_title(re.compile(r"^Job preferences"))


@pytest.mark.parametrize(("viewport", "scheme"), AXE_CASES)
def test_td342_loading_and_prefilled_states_axe_and_fit(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int], scheme: str
) -> None:
    """The shell while the fragment loads, and the form filled from stored values, at desktop
    and 390x844, in light and dark: no serious axe findings, no horizontal scroll."""
    with new_page(browser, viewport, color_scheme=scheme) as page:
        held = hold_form(page)
        open_app(page, running_app, "/setup/preferences")
        wait_for_held(page, held)
        expect(page.locator(SLOT)).to_be_visible()
        assert_no_serious_violations(page)
        assert_fits(page, "preferences loading")
        release(page, held)
        wait_for_form(page)

        fill_minimal_and_continue(page, running_app)  # stores Fictional Archivist and PT
        open_app(page, running_app, "/setup/preferences")
        expect(page.locator(ROLES)).to_have_value("Fictional Archivist")
        expect(page.locator(COUNTRY)).to_have_value("PT")
        assert_no_serious_violations(page)
        assert_fits(page, "preferences prefilled")
