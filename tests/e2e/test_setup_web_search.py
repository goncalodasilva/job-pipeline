"""Step 3 of the setup guide, web search and its optional budget, in a real browser (M2.25;
AC-M1.7; spec 3.9; ADR-053, ADR-064, ADR-081).

The app starts with steps 1 and 2 done and the fake provider saved (``running_app`` with
``"preferences_step"``). By keyboard only: from the AI provider step, "Continue" opens Web
search (URL, step list, title, focus on the heading, "Step 3 of 6: Web search" announced);
"Skip for now" moves on to the next step, Import CV since M3.7, with focus on its heading, "Web
search skipped." announced before the step, and the step list saying "Skipped". Back on the
step the budget fields are folded away ("Set a monthly budget (optional)", a native
disclosure): it is opened from the keyboard (Enter, and Space), a budget is typed field by
field; a refused save keeps it open and moves focus to the first refused field, whose visible
error is listed in its ``aria-describedby``; the corrected budget is saved with Enter in a
field (Enter saves, it never skips), the guide moves on ("Web search budget saved." announced),
the list says "Done" and the step shows the budget, open, when opened again.

The step folded and open, its field-error state and its reload prompt (the budgets changed in
another tab) cause no horizontal scroll at 390x844 and 360x740 and have no serious or critical
axe findings at desktop and phone size, in light and dark. Folded, on a phone, the primary
button ("Continue") is within the first two screens.

Selectors follow ``tests/integration/test_setup_web_search.py``: the M2.22 field names and
``button[name="action"]`` with ``save`` or ``skip`` inside ``#setup-step``. No fixed waits;
every value is fictional.
"""

import re

import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_setup_provider import SMALL_PHONE, has_focus
from tests.e2e.test_setup_provider_test import tab_to_locator
from tests.e2e.test_shell import has_no_horizontal_scroll, new_page, open_app
from tests.running_app import RunningApp

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.parametrize("running_app", ["preferences_step"], indirect=True),
]

STEP = "/setup/web_search"
TITLE = "Web search"
STEP_ANNOUNCEMENT = "Step 3 of 6: Web search"
# The step after web search: CV import (step 4, M3.7; ADR-089 item 4).
NEXT_PATH = "/setup/cv_import"
NEXT_TITLE = "Import CV"
NEXT_ANNOUNCEMENT = "Step 4 of 6: Import CV"
SECTION = "#setup-step"
SAVE = f'{SECTION} button[name="action"][value="save"]'
SKIP = f'{SECTION} button[name="action"][value="skip"]'
FIRST_FIELD = f'{SECTION} input:not([type="hidden"])'
DISCLOSURE = f"{SECTION} details"
SUMMARY = f"{DISCLOSURE} > summary"
SUMMARY_TEXT = "Set a monthly budget (optional)"
SAVED_SAID = "Web search budget saved."
SKIPPED_SAID = "Web search skipped."
STATUS = "#web-search-budget-status"
# Where the primary button ends, in screens from the top of the page.
SCREENS_TO_END_OF = """(selector) => {
  const box = document.querySelector(selector).getBoundingClientRect();
  return (box.bottom + window.scrollY) / window.innerHeight;
}"""
CURRENT = '[aria-current="step"]'
MAX_TABS = 60
FIELDS = (
    "allowance",
    "first_warning",
    "second_warning",
    "block_at",
    "reset_day",
    "fast_cap",
    "thorough_cap",
)
VALID = {
    "allowance": "100",
    "first_warning": "50",
    "second_warning": "80",
    "block_at": "100",
    "reset_day": "31",
    "fast_cap": "40",
    "thorough_cap": "",
}
# Every rule broken at once: the tallest the form gets.
ALL_WRONG = {
    "allowance": "100",
    "first_warning": "80",
    "second_warning": "80",
    "block_at": "101",
    "reset_day": "32",
    "fast_cap": "101",
    "thorough_cap": "150",
}
ALL_WRONG_FIELDS = ("second_warning", "block_at", "reset_day", "fast_cap", "thorough_cap")
# The focused field is invalid and one of the elements it is described by is a visible error
# (the reset-day note is described-by too, and is not one).
FOCUSED_FIELD_HAS_VISIBLE_ERROR = """() => {
  const el = document.activeElement;
  if (!el || el.getAttribute('aria-invalid') !== 'true') return false;
  const ids = (el.getAttribute('aria-describedby') || '').split(/\\s+/).filter(Boolean);
  return ids.some((id) => {
    const node = document.getElementById(id);
    if (!node || !node.classList.contains('field-error')) return false;
    return node.textContent.trim() !== '' && node.getClientRects().length > 0;
  });
}"""
AXE_CASES = [
    pytest.param(DESKTOP, "light", id="desktop-light"),
    pytest.param(DESKTOP, "dark", id="desktop-dark"),
    pytest.param(PHONE, "light", id="390x844-light"),
    pytest.param(PHONE, "dark", id="390x844-dark"),
    pytest.param(SMALL_PHONE, "dark", id="360x740-dark"),
]


# --- helpers -------------------------------------------------------------------------------


def url(app: RunningApp, path: str) -> str:
    return f"{app.base_url}{path}"


def field(name: str) -> str:
    return f'{SECTION} input[name="{name}"]'


def tab_to(page: Page, selector: str) -> None:
    """Press Tab until ``selector`` has focus (keyboard only, no clicks)."""
    for _ in range(MAX_TABS):
        page.keyboard.press("Tab")
        if has_focus(page, selector):
            return
    raise AssertionError(f"{selector} never received focus within {MAX_TABS} Tab presses")


def replace_text(page: Page, text: str) -> None:
    """Replace the focused field's content from the keyboard."""
    page.keyboard.press("ControlOrMeta+a")
    if text:
        page.keyboard.type(text)
    else:
        page.keyboard.press("Delete")


def fill_by_keyboard(page: Page, values: dict[str, str]) -> None:
    """Tab into the budget form and through it, typing each field's value as it takes focus
    (whatever their order); ends with focus on the last field typed in."""
    tab_to(page, FIRST_FIELD)
    filled: set[str] = set()
    for _ in range(len(values) + 10):
        assert has_focus(page, f"{SECTION} *"), "Tab stays in the step while filling the form"
        name = page.evaluate("() => document.activeElement.getAttribute('name')")
        if name in values and name not in filled:
            replace_text(page, values[name])
            filled.add(name)
        if filled == set(values):
            break
        page.keyboard.press("Tab")
    assert filled == set(values), f"fields never reached by Tab: {sorted(set(values) - filled)}"
    for name, value in values.items():
        expect(page.locator(field(name))).to_have_value(value)


def fill_with_pointer(page: Page, values: dict[str, str]) -> None:
    """Not keyboard-only: for tests whose subject is the layout or axe."""
    for name, value in values.items():
        page.locator(field(name)).fill(value)


def assert_folded(page: Page) -> None:
    expect(page.locator(DISCLOSURE)).to_have_count(1)
    expect(page.locator(SUMMARY)).to_have_text(SUMMARY_TEXT)
    expect(page.locator(DISCLOSURE)).not_to_have_attribute("open", "")
    for name in FIELDS:
        expect(page.locator(field(name))).to_be_hidden()


def assert_unfolded(page: Page) -> None:
    expect(page.locator(DISCLOSURE)).to_have_attribute("open", "")
    for name in FIELDS:
        expect(page.locator(field(name))).to_be_visible()


def unfold_by_keyboard(page: Page, key: str) -> None:
    """Tab to the summary and open the budget fields with ``key`` (Enter or Space)."""
    assert_folded(page)
    tab_to(page, SUMMARY)
    page.keyboard.press(key)
    assert_unfolded(page)
    expect(page.locator(SUMMARY)).to_be_focused()


def unfold_with_pointer(page: Page, *, phone: bool) -> None:
    assert_folded(page)
    summary = page.locator(SUMMARY)
    summary.tap() if phone else summary.click()
    assert_unfolded(page)


def assert_fits(page: Page, state: str) -> None:
    fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
    assert fits, f"{state}: scrollWidth {scroll_width} > {inner_width}"


def step_status(page: Page, title: str) -> Locator:
    named = re.compile(rf"^{re.escape(title)}(\s·|$)")
    return page.locator("ol.setup-steps > li").filter(has_text=named).locator(".step-status")


def assert_on_web_search(page: Page, app: RunningApp) -> None:
    expect(page).to_have_url(url(app, STEP))
    expect(page.locator(CURRENT)).to_have_count(1)
    expect(page.locator(CURRENT)).to_contain_text(TITLE)
    expect(page.locator("#step-heading")).to_have_text(TITLE)
    expect(page.locator("h1")).to_have_count(1)


def assert_moved_to_next_step(page: Page, app: RunningApp, said: str) -> None:
    """The htmx step change (ADR-053): URL, list, title, focus on the heading, and the
    announcement: ``said`` (what the submit did), then the step."""
    expect(page).to_have_url(url(app, NEXT_PATH))
    expect(page.locator(CURRENT)).to_have_count(1)
    expect(page.locator(CURRENT)).to_contain_text(NEXT_TITLE)
    expect(page).to_have_title(re.compile(rf"^{NEXT_TITLE}"))
    expect(page.locator("#step-heading")).to_have_text(NEXT_TITLE)
    expect(page.locator("#step-heading")).to_be_focused()
    expect(page.locator("#announcer")).to_have_text(f"{said} {NEXT_ANNOUNCEMENT}")


# --- keyboard only ---------------------------------------------------------------------------


def test_web_search_keyboard_skip_then_save_a_budget(
    running_app: RunningApp, browser: Browser
) -> None:
    app = running_app
    with new_page(browser, DESKTOP) as page:
        # Reach the step from the AI provider step, by keyboard: "Continue".
        open_app(page, app, "/setup/provider")
        go_on = page.locator(SECTION).get_by_role("link", name="Continue")
        tab_to_locator(page, go_on)
        page.keyboard.press("Enter")
        assert_on_web_search(page, app)
        expect(page).to_have_title(re.compile(rf"^{TITLE}"))
        expect(page.locator("#step-heading")).to_be_focused()
        expect(page.locator("#announcer")).to_have_text(STEP_ANNOUNCEMENT)
        expect(step_status(page, TITLE)).to_have_count(0)
        expect(page.locator(STATUS)).to_have_text("Now: no limit on web searches.")
        expect(page.locator(SAVE)).to_have_text("Continue")

        # The budget fields are folded away; Space on the summary opens them, and Space
        # again folds them back.
        unfold_by_keyboard(page, "Space")
        page.keyboard.press("Space")
        assert_folded(page)

        # "Skip for now" by keyboard: on to the next step, and the list says "Skipped".
        tab_to(page, SKIP)
        expect(page.locator(SKIP)).to_have_text("Skip for now")
        page.keyboard.press("Enter")
        assert_moved_to_next_step(page, app, SKIPPED_SAID)
        expect(step_status(page, TITLE)).to_have_text("· Skipped")

        # Back on the step: nothing was filled in, and it still offers the budget, folded.
        open_app(page, app, STEP)
        assert_on_web_search(page, app)
        expect(step_status(page, TITLE)).to_have_text("· Skipped")
        assert_folded(page)
        for name in FIELDS:
            expect(page.locator(field(name))).to_have_value("")

        # Opened with Enter. A refused budget (reset day 32): Enter on the primary button;
        # the fields stay open and focus goes to the field, whose error is visible and tied
        # to it; what was typed is kept.
        unfold_by_keyboard(page, "Enter")
        fill_by_keyboard(page, {**VALID, "reset_day": "32"})
        tab_to(page, SAVE)
        page.keyboard.press("Enter")
        refused = page.locator(f'{field("reset_day")}[aria-invalid="true"]')
        expect(refused).to_have_count(1)
        assert_unfolded(page)
        expect(refused).to_be_focused()
        expect(page.locator(f'{SECTION} [aria-invalid="true"]')).to_have_count(1)
        assert page.evaluate(FOCUSED_FIELD_HAS_VISIBLE_ERROR), "the error is tied to the field"
        expect(page).to_have_title(re.compile(rf"^Error: {TITLE}"))
        assert_on_web_search(page, app)
        expect(page.locator(field("allowance"))).to_have_value("100")
        expect(step_status(page, TITLE)).to_have_text("· Skipped")

        # Corrected in place; Enter in the field saves (it never skips) and moves on.
        replace_text(page, "31")
        page.keyboard.press("Enter")
        assert_moved_to_next_step(page, app, SAVED_SAID)
        expect(step_status(page, TITLE)).to_have_text("· Done")

        # The step shows the saved budget, unfolded, when opened again.
        open_app(page, app, STEP)
        assert_unfolded(page)
        expect(page.locator(STATUS)).to_have_text("Now: a budget of 100 requests per month.")
        for name, value in VALID.items():
            expect(page.locator(field(name))).to_have_value(value)
        expect(step_status(page, TITLE)).to_have_text("· Done")
        expect(page.locator(f'{SECTION} [aria-invalid="true"]')).to_have_count(0)

        # Done lists it as done, with its link.
        open_app(page, app, "/setup/done")
        row = page.locator("ul.setup-summary > li").filter(has_text=TITLE)
        expect(row).to_have_text(f"{TITLE}: Done", use_inner_text=True)
        expect(row.get_by_role("link", name=TITLE)).to_have_attribute("href", STEP)


def test_web_search_skipped_is_listed_on_done_with_link_by_keyboard(
    running_app: RunningApp, browser: Browser
) -> None:
    """AC-M1.7 in the browser: the skipped step reads "Skipped" on Done, and its link opens
    the step again."""
    app = running_app
    with new_page(browser, DESKTOP) as page:
        open_app(page, app, STEP)
        tab_to(page, SKIP)
        page.keyboard.press("Enter")
        assert_moved_to_next_step(page, app, SKIPPED_SAID)

        open_app(page, app, "/setup/done")
        row = page.locator("ul.setup-summary > li").filter(has_text=TITLE)
        expect(row).to_have_text(f"{TITLE}: Skipped", use_inner_text=True)
        link = row.get_by_role("link", name=TITLE)
        tab_to_locator(page, link)
        page.keyboard.press("Enter")
        assert_on_web_search(page, app)
        expect(page.locator(SAVE)).to_be_visible()
        expect(page.locator(SKIP)).to_be_visible()


# --- layout and axe --------------------------------------------------------------------------


@pytest.mark.parametrize(("viewport", "scheme"), AXE_CASES)
def test_web_search_axe_and_no_horizontal_scroll(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int], scheme: str
) -> None:
    app = running_app
    phone = viewport["width"] < 768
    with new_page(browser, viewport, color_scheme=scheme) as page:
        open_app(page, app, STEP)
        assert_on_web_search(page, app)
        # As most people see it: the budget folded away, both buttons close at hand.
        assert_folded(page)
        expect(page.locator(SAVE)).to_be_visible()
        expect(page.locator(SKIP)).to_be_visible()
        assert_no_serious_violations(page)
        assert_fits(page, "web search, folded")
        if phone:
            screens = page.evaluate(SCREENS_TO_END_OF, SAVE)
            assert screens <= 2, f"the primary button ends {screens:.2f} screens down"

        # Unfolded: every field in sight.
        unfold_with_pointer(page, phone=phone)
        assert_no_serious_violations(page)
        assert_fits(page, "web search, unfolded")

        # Every field error showing: the tallest and widest the step gets.
        fill_with_pointer(page, ALL_WRONG)
        save = page.locator(SAVE)
        save.tap() if phone else save.click()
        first = page.locator(f'{field(ALL_WRONG_FIELDS[0])}[aria-invalid="true"]')
        assert_unfolded(page)
        expect(first).to_be_focused()
        expect(page.locator(f'{SECTION} input[aria-invalid="true"]')).to_have_count(
            len(ALL_WRONG_FIELDS)
        )
        expect(page.locator(f"{SECTION} .field-error")).to_have_count(len(ALL_WRONG_FIELDS))
        for name, value in ALL_WRONG.items():
            expect(page.locator(field(name))).to_have_value(value)
        expect(page).to_have_title(re.compile(rf"^Error: {TITLE}"))
        assert_on_web_search(page, app)
        assert_no_serious_violations(page)
        assert_fits(page, "web search with every field error")

        # Saved: on to the next step, and the list (with "· Done") still fits.
        fill_with_pointer(page, VALID)
        save.tap() if phone else save.click()
        assert_moved_to_next_step(page, app, SAVED_SAID)
        expect(step_status(page, TITLE)).to_have_text("· Done")
        assert_fits(page, "preferences after saving a budget")

        # The step with a stored budget: unfolded from the start.
        open_app(page, app, STEP)
        assert_unfolded(page)
        expect(page.locator(field("allowance"))).to_have_value("100")
        assert_no_serious_violations(page)
        assert_fits(page, "web search with a budget")


@pytest.mark.parametrize(
    ("viewport", "scheme"),
    [
        pytest.param(DESKTOP, "light", id="desktop-light"),
        pytest.param(PHONE, "dark", id="390x844-dark"),
    ],
)
def test_web_search_stale_form_shows_reload_prompt(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int], scheme: str
) -> None:
    """ADR-081: the budgets changed in another tab after this one was opened. Saving here
    changes nothing: the reload prompt shows and takes focus, and its link, by keyboard,
    loads this step again with the budget the other tab saved."""
    app = running_app
    with (
        new_page(browser, viewport, color_scheme=scheme) as page,
        new_page(browser, DESKTOP) as other,
    ):
        open_app(page, app, STEP)
        expect(page.locator(SAVE)).to_be_visible()

        open_app(other, app, STEP)
        unfold_with_pointer(other, phone=False)
        fill_with_pointer(other, VALID)
        other.locator(SAVE).click()
        expect(other).to_have_url(url(app, NEXT_PATH))

        unfold_with_pointer(page, phone=viewport["width"] < 768)
        fill_with_pointer(page, {**VALID, "allowance": "500", "block_at": "400"})
        with page.expect_response(lambda r: r.url.endswith(STEP)) as answer:
            page.locator(SAVE).focus()
            page.keyboard.press("Enter")
        assert answer.value.status == 409, answer.value.status
        prompt = page.locator(f'{SECTION} [role="alert"]')
        expect(prompt).to_have_count(1)
        expect(prompt).to_be_visible()
        expect(prompt).to_be_focused()
        assert_on_web_search(page, app)
        expect(page).to_have_title(re.compile(rf"^Error: {TITLE}"))
        assert_unfolded(page)
        expect(page.locator(field("allowance"))).to_have_value("500")
        assert_no_serious_violations(page)
        assert_fits(page, "web search with the reload prompt")

        reload = prompt.get_by_role("link", name=re.compile("reload", re.IGNORECASE))
        expect(reload).to_have_attribute("href", STEP)
        tab_to_locator(page, reload)
        page.keyboard.press("Enter")
        assert_on_web_search(page, app)
        expect(page.locator(f'{SECTION} [role="alert"]')).to_have_count(0)
        # The other tab's budget, not what was typed here.
        expect(page.locator(field("allowance"))).to_have_value("100")
        expect(page.locator(field("block_at"))).to_have_value("100")
