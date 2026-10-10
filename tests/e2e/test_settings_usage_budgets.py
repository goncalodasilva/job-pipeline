"""Settings > Usage budgets in a real browser (M2.22; AC-M2.7, AC-M2.10; spec 3.7, 3.9).

The app starts with a completed setup (``running_app`` default: the fake provider saved; fakes
allowed), so ``#usage-budgets`` shows four services, each "No limit". By keyboard only the user
tabs through the Fake provider's form (``#budget-fake``), types a budget and presses Enter on
its Save button: the live region announces it was saved, focus lands inside ``#budget-fake`` on
something that is not a control, a visible line in the block says "Saved.", the page stays on
``/settings`` and a reload shows the budget.
A refused save moves focus to the first invalid field (like the other Settings forms), whose
error is listed in its ``aria-describedby``; correcting it and pressing Enter saves.
"Background tasks at once" is changed by keyboard too. The section, also with every field
error showing and with a saved budget, causes no horizontal scroll at 390x844 and 360x740 and
has no serious or critical axe findings at desktop and phone size, in light and dark.

Selectors follow the integration tests (``tests/integration/test_settings_usage_budgets.py``):
field names, ``button[name="action"]`` values, ``#budget-<service>`` and
``#budget-<service>-status``. No fixed waits; every value is fictional.
"""

import re

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_setup_provider import SMALL_PHONE, has_focus
from tests.e2e.test_shell import has_no_horizontal_scroll, new_page, open_app
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e

SECTION = "#usage-budgets"
BLOCK = "#budget-fake"
STATUS = "#budget-fake-status"
OTHER_STATUS = "#budget-web_search-status"
SAVE = f'{BLOCK} button[name="action"][value="save"]'
REMOVE = f'{BLOCK} button[name="action"][value="remove"]'
FIRST_FIELD = f'{BLOCK} input:not([type="hidden"])'
CONCURRENCY_FORM = 'form[action="/settings/usage/concurrency"]'
CONCURRENCY = f'{CONCURRENCY_FORM} [name="max_concurrent"]'
CONCURRENCY_SAVE = f'{CONCURRENCY_FORM} button[type="submit"]'
NO_LIMIT = "No limit"
SAVED = re.compile("saved", re.IGNORECASE)
DONE = "#budget-fake-done"
CONFLICT = "#budget-fake-conflict"
CONCURRENCY_DONE = "#usage-concurrency-done"
CONCURRENCY_CONFLICT = "#usage-concurrency-conflict"
BUDGETS_URL = "/settings/usage/budgets"
CONCURRENCY_URL = "/settings/usage/concurrency"
OTHER = "#budget-web_search"
OTHER_SAVE = f'{OTHER} button[name="action"][value="save"]'
# The Settings page has many tab stops before this section.
MAX_TABS = 200

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

# Focus after a save: inside the service's block, on a heading or status, not on a control.
FOCUS_IN_BLOCK = """(block) => {
  const el = document.activeElement;
  if (!el || !el.closest(block)) return false;
  return !el.matches('input, select, textarea, button, a');
}"""
# The focused field is invalid and one of the elements it is described by is a visible error
# (a field's hint is described-by too, and is not one).
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
FITS = """(selector) => {
  const block = document.querySelector(selector);
  return block.scrollWidth <= block.clientWidth;
}"""


def field(name: str, block: str = BLOCK) -> str:
    return f'{block} input[name="{name}"]'


def save_again_is_refused(page: Page, button: str, url: str) -> None:
    """Pressing Save again in a tab that is behind is a conflict again (409)."""
    with page.expect_response(lambda r: r.url.endswith(url)) as answer:
        page.locator(button).click()
    assert answer.value.status == 409, answer.value.status


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
    """Tab into the Fake provider's form and through it, typing each field's value as it
    takes focus (whatever their order), ending with focus on its Save button."""
    tab_to(page, FIRST_FIELD)
    filled: set[str] = set()
    for _ in range(len(values) + 10):
        assert has_focus(page, f"{BLOCK} *"), "Tab stays in the form until its Save button"
        if has_focus(page, SAVE):
            break
        name = page.evaluate("() => document.activeElement.getAttribute('name')")
        if name in values:
            replace_text(page, values[name])
            filled.add(name)
        page.keyboard.press("Tab")
    assert has_focus(page, SAVE), "the Save button follows the fields"
    assert filled == set(values), f"fields reached by Tab: {sorted(filled)}"


def fill_fields(page: Page, values: dict[str, str]) -> None:
    """Fill the Fake provider's form (pointer path, for the layout and axe scenarios)."""
    fill_fields_in(page, BLOCK, values)


def fill_fields_in(page: Page, block: str, values: dict[str, str]) -> None:
    for name, value in values.items():
        page.locator(field(name, block)).fill(value)


def open_settings(page: Page, app: RunningApp) -> None:
    open_app(page, app, "/settings")
    section = page.locator(SECTION)
    expect(section.get_by_role("heading", name="Usage budgets", level=2)).to_be_visible()
    expect(page.locator(STATUS)).to_contain_text(NO_LIMIT)


def assert_fits(page: Page) -> None:
    fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
    assert fits, f"scrollWidth {scroll_width} > {inner_width}"
    assert page.evaluate(FITS, SECTION), "the section fits its own width"
    assert page.evaluate(FITS, BLOCK), "the service's block fits its own width"


def show_every_error(page: Page) -> None:
    fill_fields(page, ALL_WRONG)
    page.locator(SAVE).click()
    for name in ALL_WRONG_FIELDS:
        expect(page.locator(f'{field(name)}[aria-invalid="true"]')).to_have_count(1)
    expect(page.locator(f'{BLOCK} [aria-invalid="true"]')).to_have_count(len(ALL_WRONG_FIELDS))
    for name, value in ALL_WRONG.items():
        expect(page.locator(field(name))).to_have_value(value)


def save_valid(page: Page) -> None:
    fill_fields(page, VALID)
    page.locator(SAVE).click()
    expect(page.locator("#announcer")).to_contain_text(SAVED)
    expect(page.locator(STATUS)).not_to_contain_text(NO_LIMIT)
    expect(page.locator(REMOVE)).to_be_visible()
    expect(page.locator(f'{BLOCK} [aria-invalid="true"]')).to_have_count(0)


# --- keyboard ----------------------------------------------------------------------------------


def test_usage_budget_saved_by_keyboard(running_app: RunningApp, browser: Browser) -> None:
    """AC-M2.10 then a save: "No limit" for every service, a budget typed and saved by
    keyboard for one, kept after a reload; the other services still say "No limit"."""
    settings_url = f"{running_app.base_url}/settings"
    with new_page(browser, DESKTOP) as page:
        open_settings(page, running_app)
        for service in ("claude_subscription", "claude_api_key", "web_search", "fake"):
            expect(page.locator(f"#budget-{service}-status")).to_contain_text(NO_LIMIT)
        for name in VALID:
            expect(page.locator(field(name))).to_have_value("")
        expect(page.locator(REMOVE)).to_have_count(0)

        fill_by_keyboard(page, VALID)
        page.keyboard.press("Enter")

        expect(page.locator("#announcer")).to_contain_text(SAVED)
        page.wait_for_function(FOCUS_IN_BLOCK, arg=BLOCK)
        expect(page.locator(DONE)).to_be_visible()
        expect(page.locator(DONE)).to_have_text("Saved.")
        expect(page).to_have_url(settings_url)
        expect(page.locator(STATUS)).not_to_contain_text(NO_LIMIT)
        expect(page.locator(OTHER_STATUS)).to_contain_text(NO_LIMIT)

        page.reload()
        page.wait_for_function("() => typeof window.htmx !== 'undefined'")
        expect(page.locator(DONE)).to_have_count(0)
        for name, value in VALID.items():
            expect(page.locator(field(name))).to_have_value(value)
        expect(page.locator(STATUS)).not_to_contain_text(NO_LIMIT)
        expect(page.locator(OTHER_STATUS)).to_contain_text(NO_LIMIT)

        # Remove by keyboard: back to "No limit", announced, focus kept inside the block.
        tab_to(page, REMOVE)
        page.keyboard.press("Enter")
        expect(page.locator(STATUS)).to_contain_text(NO_LIMIT)
        page.wait_for_function(FOCUS_IN_BLOCK, arg=BLOCK)
        expect(page.locator(DONE)).to_be_visible()
        expect(page.locator(DONE)).to_have_text("Removed. This service has no limit now.")
        expect(page.locator(REMOVE)).to_have_count(0)
        expect(page).to_have_url(settings_url)
        page.reload()
        expect(page.locator(STATUS)).to_contain_text(NO_LIMIT)
        expect(page.locator(field("allowance"))).to_have_value("")


def test_ac_m2_7_invalid_save_focuses_first_invalid_field(
    running_app: RunningApp, browser: Browser
) -> None:
    """A blocking point above the allowance, saved by keyboard: nothing is stored, focus is
    on that field, its error is visible and tied to it, what was typed is kept; correcting it
    and pressing Enter in the field saves."""
    wrong = {**VALID, "block_at": "101"}
    with new_page(browser, DESKTOP) as page:
        open_settings(page, running_app)
        fill_by_keyboard(page, wrong)
        page.keyboard.press("Enter")

        invalid = page.locator(f'{field("block_at")}[aria-invalid="true"]')
        expect(invalid).to_have_count(1)
        expect(invalid).to_be_focused()
        page.wait_for_function(FOCUSED_FIELD_HAS_VISIBLE_ERROR)
        expect(page.locator("#announcer")).to_have_text(
            "Budget for Fake provider not saved. 1 field needs a change."
        )
        expect(page.locator(DONE)).to_have_count(0)
        expect(page.locator(f'{BLOCK} [aria-invalid="true"]')).to_have_count(1)
        for name, value in wrong.items():
            expect(page.locator(field(name))).to_have_value(value)
        expect(page.locator(STATUS)).to_contain_text(NO_LIMIT)
        expect(page).to_have_url(f"{running_app.base_url}/settings")

        # The reset-day note stays tied to its field next to the errors.
        note_ids = page.locator(field("reset_day")).get_attribute("aria-describedby") or ""
        notes = [page.locator(f"[id='{i}']").inner_text() for i in note_ids.split()]
        assert any("29" in n and "31" in n and "last day" in n.lower() for n in notes), notes

        replace_text(page, "100")
        page.keyboard.press("Enter")
        expect(page.locator("#announcer")).to_contain_text(SAVED)
        page.wait_for_function(FOCUS_IN_BLOCK, arg=BLOCK)
        expect(page.locator(DONE)).to_be_visible()
        expect(page.locator(f'{BLOCK} [aria-invalid="true"]')).to_have_count(0)
        expect(page.locator(STATUS)).not_to_contain_text(NO_LIMIT)

        page.reload()
        expect(page.locator(field("block_at"))).to_have_value("100")


def test_concurrency_saved_by_keyboard(running_app: RunningApp, browser: Browser) -> None:
    """ "Background tasks at once" starts at 1, is set to 3 by keyboard (typing works for an
    input and for a select) and is kept after a reload."""
    with new_page(browser, DESKTOP) as page:
        open_settings(page, running_app)
        control = page.locator(CONCURRENCY)
        expect(page.get_by_label(re.compile("Background tasks at once"))).to_have_count(1)
        expect(control).to_have_value("1")

        tab_to(page, CONCURRENCY)
        if control.evaluate("(el) => el.tagName") == "SELECT":
            page.keyboard.type("3")  # type-ahead
        else:
            replace_text(page, "3")
        expect(control).to_have_value("3")
        tab_to(page, CONCURRENCY_SAVE)
        page.keyboard.press("Enter")

        expect(page.locator("#announcer")).to_have_text(
            "Saved. Up to 3 background tasks from the next start."
        )
        expect(page.locator(CONCURRENCY_DONE)).to_be_visible()
        expect(page.locator(CONCURRENCY_DONE)).to_have_text(
            "Saved. It applies the next time you start the app."
        )
        page.wait_for_function(FOCUS_IN_BLOCK, arg="#usage-concurrency")
        expect(page).to_have_url(f"{running_app.base_url}/settings")
        expect(page.locator(CONCURRENCY)).to_have_value("3")
        expect(page.locator(f'{CONCURRENCY}[aria-invalid="true"]')).to_have_count(0)
        page.reload()
        expect(page.locator(CONCURRENCY_DONE)).to_have_count(0)
        expect(page.locator(CONCURRENCY)).to_have_value("3")
        expect(page.locator(STATUS)).to_contain_text(NO_LIMIT)


def test_stale_version_reload_prompt_in_browser(running_app: RunningApp, browser: Browser) -> None:
    """Two tabs: the second saves with the version the first made stale. It is told the
    budgets changed elsewhere in an alert that takes focus, nothing of its own is saved
    however often it presses Save, and its Reload link shows the first tab's budget."""
    with new_page(browser, DESKTOP) as first, new_page(browser, DESKTOP) as second:
        open_settings(first, running_app)
        open_settings(second, running_app)
        save_valid(first)

        fill_fields(second, {**VALID, "allowance": "500"})
        second.locator(SAVE).click()
        prompt = second.locator(SECTION).get_by_text("changed elsewhere")
        expect(prompt).to_be_visible()
        alert = second.locator(SECTION).get_by_role("alert")
        expect(alert).to_have_count(1)
        expect(alert).to_have_id(CONFLICT.removeprefix("#"))
        expect(alert).to_be_focused()
        expect(alert).to_contain_text("What you typed here isn't saved.")
        expect(second.locator(field("allowance"))).to_have_value("500")
        reload_link = alert.get_by_role("link", name=re.compile("Reload"))
        expect(reload_link).to_be_visible()
        assert_no_serious_violations(second)

        # Still behind: a second Save is refused too, and so is another form's.
        save_again_is_refused(second, SAVE, BUDGETS_URL)
        expect(second.locator(CONFLICT)).to_be_focused()
        expect(second.locator(field("allowance"))).to_have_value("500")
        fill_fields_in(second, OTHER, VALID)
        save_again_is_refused(second, OTHER_SAVE, BUDGETS_URL)
        expect(second.locator(f"{OTHER}-conflict")).to_be_focused()
        first.reload()
        expect(first.locator(field("allowance"))).to_have_value("100")
        expect(first.locator(OTHER_STATUS)).to_contain_text(NO_LIMIT)

        reload_link = second.locator(CONFLICT).get_by_role("link", name=re.compile("Reload"))
        reload_link.focus()
        second.keyboard.press("Enter")
        expect(second.locator(field("allowance"))).to_have_value("100")
        expect(second.get_by_text("changed elsewhere")).to_have_count(0)
        expect(second.locator(STATUS)).not_to_contain_text(NO_LIMIT)


def test_stale_concurrency_reload_prompt_in_browser(
    running_app: RunningApp, browser: Browser
) -> None:
    """The same for "Background tasks at once": the tab that is behind gets the alert with
    focus on it, a second Save is refused too, and its Reload link shows what was saved."""
    with new_page(browser, DESKTOP) as first, new_page(browser, DESKTOP) as second:
        open_settings(first, running_app)
        open_settings(second, running_app)
        first.locator(CONCURRENCY).select_option("2")
        first.locator(CONCURRENCY_SAVE).click()
        expect(first.locator(CONCURRENCY_DONE)).to_be_visible()

        second.locator(CONCURRENCY).select_option("3")
        second.locator(CONCURRENCY_SAVE).click()
        alert = second.locator(SECTION).get_by_role("alert")
        expect(alert).to_have_count(1)
        expect(alert).to_have_id(CONCURRENCY_CONFLICT.removeprefix("#"))
        expect(alert).to_be_focused()
        expect(alert).to_contain_text("changed elsewhere")
        expect(alert).to_contain_text("What you chose here isn't saved.")
        assert_no_serious_violations(second)

        second.locator(CONCURRENCY).select_option("3")
        save_again_is_refused(second, CONCURRENCY_SAVE, CONCURRENCY_URL)
        expect(second.locator(CONCURRENCY_CONFLICT)).to_be_focused()

        reload_link = second.locator(CONCURRENCY_CONFLICT).get_by_role(
            "link", name=re.compile("Reload")
        )
        reload_link.focus()
        second.keyboard.press("Enter")
        expect(second.locator(CONCURRENCY)).to_have_value("2")
        expect(second.get_by_text("changed elsewhere")).to_have_count(0)


def test_saving_one_budget_keeps_another_form_usable(
    running_app: RunningApp, browser: Browser
) -> None:
    """One version for every budget (ADR-079): values typed for one service stay while
    another's budget is saved, and are then saved from the same tab without a reload."""
    typed = {**VALID, "allowance": "250", "reset_day": "7"}
    with new_page(browser, DESKTOP) as page:
        open_settings(page, running_app)
        fill_fields_in(page, OTHER, typed)

        save_valid(page)
        for name, value in typed.items():  # untouched by the other form's answer
            expect(page.locator(field(name, OTHER))).to_have_value(value)
        expect(page.locator(OTHER_STATUS)).to_contain_text(NO_LIMIT)

        with page.expect_response(lambda r: r.url.endswith(BUDGETS_URL)) as answer:
            page.locator(OTHER_SAVE).click()
        assert answer.value.status == 200, answer.value.status
        expect(page.locator(f"{OTHER}-done")).to_have_text("Saved.")
        expect(page.locator(OTHER_STATUS)).to_contain_text("250")
        expect(page.get_by_role("alert")).to_have_count(0)

        # And back: the first form took the new version too.
        page.locator(field("allowance")).fill("120")
        with page.expect_response(lambda r: r.url.endswith(BUDGETS_URL)) as answer:
            page.locator(SAVE).click()
        assert answer.value.status == 200, answer.value.status
        expect(page.locator(STATUS)).to_contain_text("120")

        page.reload()
        for name, value in typed.items():
            expect(page.locator(field(name, OTHER))).to_have_value(value)
        expect(page.locator(field("allowance"))).to_have_value("120")


# --- phone widths ------------------------------------------------------------------------------


@pytest.mark.parametrize("viewport", [PHONE, SMALL_PHONE], ids=["390x844", "360x740"])
def test_usage_budgets_390x844_and_360_no_horizontal_scroll(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int]
) -> None:
    """No horizontal scroll with "No limit" everywhere, with every field error showing, and
    with a saved budget (its Remove button showing)."""
    with new_page(browser, viewport) as page:
        open_settings(page, running_app)
        assert_fits(page)

        show_every_error(page)
        assert_fits(page)
        for name in ALL_WRONG_FIELDS:  # an error is never clipped out of sight
            ids = page.locator(field(name)).get_attribute("aria-describedby") or ""
            assert ids.split(), name
            for element_id in ids.split():
                expect(page.locator(f"[id='{element_id}']")).to_be_visible()

        save_valid(page)
        assert_fits(page)


# --- axe -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])
def test_axe_usage_budgets(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int], scheme: str
) -> None:
    """No serious or critical axe findings: the default state, a form showing every field
    error, a refused "Background tasks at once" value, and a saved budget."""
    with new_page(browser, viewport, color_scheme=scheme) as page:
        open_settings(page, running_app)
        assert_no_serious_violations(page)

        show_every_error(page)
        assert_no_serious_violations(page)

        # A value outside 1-3 (only reachable by a tampered form when the control is a select).
        page.evaluate(
            """(selector) => {
              const control = document.querySelector(selector);
              if (control.tagName === 'SELECT') control.add(new Option('4', '4'));
              control.value = '4';
            }""",
            CONCURRENCY,
        )
        page.locator(CONCURRENCY_SAVE).click()
        expect(page.locator(f'{CONCURRENCY}[aria-invalid="true"]')).to_have_count(1)
        assert_no_serious_violations(page)

        page.reload()
        page.wait_for_function("() => typeof window.htmx !== 'undefined'")
        expect(page.locator(CONCURRENCY)).to_have_value("1")
        save_valid(page)
        assert_no_serious_violations(page)
