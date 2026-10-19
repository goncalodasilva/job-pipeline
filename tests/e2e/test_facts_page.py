"""The Fact bank page in a real browser (M3.5; AC-M3.1, AC-M3.2, AC-M3.6).

The app starts with a completed setup (``running_app`` default: the fake provider saved and
allowed), so a CV import is answered from the fake's fictional scripts: any text gets the four
"Alex Example" drafts, and a keyword in the text asks for another answer (``fake-cv-malformed``
fails, ``fake-cv-invented-quote`` returns a draft that needs a check). Facts are seeded through
the page itself. The keyboard tests use the mouse only to seed; from there every step is Tab,
typing and Enter. Names, ids and copy come from ``tests/facts_page.py``. Everything is
fictional and nothing leaves loopback.
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from tests.cv_fixtures import fixture_path
from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_settings_usage_budgets import replace_text
from tests.e2e.test_setup_provider import MAX_TABS, SMALL_PHONE
from tests.e2e.test_setup_provider_test import is_focused
from tests.e2e.test_shell import assert_target_size, has_no_horizontal_scroll, new_page, open_app
from tests.facts_page import (
    BADGE_NEVER_USE,
    CHOOSE_ONE,
    CONFIRM,
    CONFIRM_ALL,
    COUNT_ID,
    DISCARD,
    DISCARDED_LINE,
    DRAFTS_ID,
    EDIT,
    EMPTY_LIBRARY,
    EMPTY_LINK,
    FILTERS_ID,
    IMPORT,
    IMPORT_ERROR_ID,
    IMPORT_ID,
    IMPORT_LIST_ID,
    IMPORT_PRIVACY_ID,
    IMPORT_START_ID,
    IMPORT_STATUS_ID,
    NEEDS_CHECK_NOTE,
    NO_DRAFTS,
    NO_MATCH,
    PRACTICE_BADGE,
    QUOTE_LABEL,
    QUOTE_LABEL_UNCHECKED,
    RELOAD,
    REMOVE,
    RESULTS_ID,
    SAID_CONFIRMED,
    SAID_DISCARDED,
    SAID_EDITED,
    SAID_IMPORT_FAILED,
    SAID_RESTORED,
    SAVE_CONFIRM,
    SEARCH,
    STALE_NOTE,
    STALE_NOTE_ID,
    START,
    STARTED_LINE,
    TABS,
    TABS_LABEL,
    TRY_AGAIN,
    UNDO,
    batch_hint,
    batch_said,
    drafts_tab_text,
    empty_with_drafts,
    found_said,
    skip_text,
    tab_url,
)
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e

CV_TEXT = fixture_path("cv.txt").read_text(encoding="utf-8")
CV_PDF = fixture_path("single_column.pdf")
MALFORMED_CV = f"{CV_TEXT}\nfake-cv-malformed\n"
FLAGGED_CV = f"{CV_TEXT}\nfake-cv-invented-quote\n"
ROUTES = "Planned 60 delivery routes a week with a team of 3."
INVOICES = "Cut invoice processing time by 35% across 4 warehouses."
STORY = "Cut invoice processing time"
DEGREE = "BSc in Industrial Management, Example University, 2018"
ENGLISH = "English"
TRAINED = "Trained new planners on the routing tool."
REWORDED = "BSc in Industrial Management, Example University"
THE_FOUR = (ROUTES, STORY, DEGREE, ENGLISH)
MSG_INVALID_OUTPUT = "The AI response didn't have the expected shape. Try again."
MSG_REMOVED = "The uploaded CV was removed."
VERSION_CONFLICT = "This item changed elsewhere. Reload to see the latest version."
LABELS = {"q": "Search facts", "role": "Role", "skill": "Skill", "status": "Status"}
LONG = 20_000  # an import parses the stored file in a child process and then calls the fake
AXE_CASES = [
    pytest.param(DESKTOP, "light", id="desktop-light"),
    pytest.param(DESKTOP, "dark", id="desktop-dark"),
    pytest.param(PHONE, "light", id="phone-light"),
    pytest.param(PHONE, "dark", id="phone-dark"),
]

# Keeps what the live region said, in order: later words replace earlier ones on the page.
RECORD_ANNOUNCEMENTS = """
window.__said = [];
document.addEventListener("DOMContentLoaded", () => {
  const region = document.getElementById("announcer");
  if (!region) return;
  new MutationObserver(() => {
    const text = region.textContent.trim();
    if (text) window.__said.push(text);
  }).observe(region, { childList: true, characterData: true, subtree: true });
});
"""
FOCUS_INSIDE = """(id) => {
  const block = document.getElementById(id);
  return !!block && (block === document.activeElement || block.contains(document.activeElement));
}"""


# --- helpers -----------------------------------------------------------------------------------


@contextmanager
def facts_page(
    browser: Browser, viewport: dict[str, int], *, color_scheme: str = "light"
) -> Iterator[Page]:
    with new_page(browser, viewport, color_scheme=color_scheme) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        yield page


def wait_said(page: Page, text: str, *, times: int = 1) -> None:
    """The live region has said ``text`` (at least ``times`` times) since the page loaded."""
    page.wait_for_function(
        "([text, times]) => (window.__said || []).filter((said) => said === text).length >= times",
        arg=[text, times],
    )


def tab_link(page: Page, tab: str) -> Locator:
    tabs = page.get_by_role("navigation", name=TABS_LABEL)
    return tabs.get_by_role("link", name=re.compile(rf"^{re.escape(TABS[tab])}"))


def expect_draft_count(page: Page, count: int) -> None:
    expect(tab_link(page, "drafts")).to_have_text(drafts_tab_text(count))


def block(page: Page, region_id: str, text: str) -> Locator:
    """The fact's own block (``#fact-<id>``) holding ``text``, located by its id from then on,
    so it is still found after an action replaced what it shows."""
    found = page.locator(f"#{region_id} [id^='fact-']").filter(has_text=text).first
    expect(found).to_be_visible(timeout=LONG)
    block_id = found.get_attribute("id")
    assert block_id, "a fact's block has an id"
    return page.locator(f'[id="{block_id}"]')


def draft(page: Page, text: str) -> Locator:
    return block(page, DRAFTS_ID, text)


def library_fact(page: Page, text: str) -> Locator:
    return block(page, RESULTS_ID, text)


def named_button(scope: Locator | Page, name: str) -> Locator:
    return scope.get_by_role("button", name=name, exact=True)


def edit_control(scope: Locator) -> Locator:
    """The control that opens a draft's inline form: a ``<summary>`` or a button."""
    return scope.locator("summary, button").filter(has_text=re.compile(rf"^\s*{EDIT}\s*$"))


def tab_until(page: Page, locator: Locator) -> None:
    """Press Tab until ``locator`` has focus, unless it has it already (keyboard only)."""
    expect(locator).to_have_count(1)
    for _ in range(MAX_TABS + 1):
        if is_focused(locator):
            return
        page.keyboard.press("Tab")
    raise AssertionError(f"{locator} never received focus within {MAX_TABS} Tab presses")


def press_by_keyboard(page: Page, locator: Locator) -> None:
    tab_until(page, locator)
    page.keyboard.press("Enter")


def expect_focus_inside(page: Page, locator: Locator) -> None:
    page.wait_for_function(FOCUS_INSIDE, arg=locator.get_attribute("id"))


def open_drafts(page: Page, app: RunningApp) -> None:
    open_app(page, app, tab_url("drafts"))
    expect(page.locator(f"#{IMPORT_ID}")).to_be_visible()


def paste_field(page: Page) -> Locator:
    return page.locator(f'#{IMPORT_ID} textarea[name="text"]')


def file_field(page: Page) -> Locator:
    return page.locator(f'#{IMPORT_ID} input[type="file"][name="file"]')


def start_import(page: Page, text: str) -> None:
    paste_field(page).fill(text)
    page.locator(f"#{IMPORT_START_ID}").click()


def import_drafts(page: Page, app: RunningApp, text: str = CV_TEXT, *, count: int = 4) -> None:
    """Seed: open the Drafts tab, paste a fictional CV and wait for its drafts."""
    open_drafts(page, app)
    start_import(page, text)
    expect(draft(page, ROUTES)).to_be_visible(timeout=LONG)
    expect_draft_count(page, count)


def assert_fits(page: Page) -> None:
    fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
    assert fits, f"scrollWidth {scroll_width} > {inner_width}"


def option_text(select: Locator, containing: str) -> str:
    """The label of the one option whose label holds ``containing``."""
    labels: list[str] = select.locator("option").evaluate_all(
        "(options) => options.map((option) => option.textContent.trim())"
    )
    found = [label for label in labels if containing in label]
    assert len(found) == 1, (containing, labels)
    return found[0]


def choose_by_keyboard(page: Page, select: Locator, label: str) -> None:
    """Tab to a select and choose an option by typing its label (type-ahead works on every
    platform; on macOS ArrowDown would open the popup instead)."""
    tab_until(page, select)
    page.keyboard.type(label)
    chosen = select.evaluate("(el) => el.options[el.selectedIndex].textContent.trim()")
    assert chosen == label, (chosen, label)


def first_option(select: Locator) -> str:
    label: str = select.evaluate("(el) => el.options[0].textContent.trim()")
    assert label, "the option for no filter has words"
    return label


# --- AC-M3.6: the Library ----------------------------------------------------------------------


def test_ac_m3_6_search_and_filters_by_keyboard(running_app: RunningApp, browser: Browser) -> None:
    """Three confirmed facts and one draft; then, by keyboard only: a search, a role, a role
    with a skill, a skill alone and the Draft status each show the right facts."""
    with facts_page(browser, DESKTOP) as page:
        import_drafts(page, running_app)
        for text in (ROUTES, STORY, ENGLISH):  # seeding: DEGREE stays a draft
            named_button(draft(page, text), CONFIRM).click()
            wait_said(page, SAID_CONFIRMED, times=(ROUTES, STORY, ENGLISH).index(text) + 1)
        expect_draft_count(page, 1)

        open_app(page, running_app, tab_url("library"))
        results = page.locator(f"#{RESULTS_ID}")
        filters = page.locator(f"#{FILTERS_ID}")
        fields = {name: filters.get_by_label(label, exact=True) for name, label in LABELS.items()}
        submit = named_button(filters, SEARCH)
        for text in (ROUTES, STORY, ENGLISH):
            expect(library_fact(page, text)).to_be_visible()
        expect(results).not_to_contain_text(DEGREE)
        expect(page.locator(f"#{COUNT_ID}")).to_contain_text("3")

        # Search: type and press Enter. Focus stays in the box and the result is announced.
        tab_until(page, fields["q"])
        page.keyboard.type("routes")
        page.keyboard.press("Enter")
        wait_said(page, found_said(1))
        expect(library_fact(page, ROUTES)).to_be_visible()
        expect(results).not_to_contain_text(STORY)
        expect(results).not_to_contain_text(ENGLISH)
        expect(fields["q"]).to_be_focused()
        expect(fields["q"]).to_have_value("routes")

        # A search that finds nothing says so, without offering the import.
        replace_text(page, "xylophone")
        page.keyboard.press("Enter")
        wait_said(page, NO_MATCH)
        expect(results).to_contain_text(NO_MATCH)
        expect(results).not_to_contain_text(EMPTY_LIBRARY)

        # Role alone.
        replace_text(page, "")
        widgets = option_text(fields["role"], "Example Widgets Ltd")
        choose_by_keyboard(page, fields["role"], widgets)
        press_by_keyboard(page, submit)
        expect(library_fact(page, STORY)).to_be_visible()
        expect(results).not_to_contain_text(ROUTES)
        expect(results).not_to_contain_text(ENGLISH)

        # Role and skill together: nothing has both.
        choose_by_keyboard(page, fields["skill"], option_text(fields["skill"], ENGLISH))
        press_by_keyboard(page, submit)
        expect(results).to_contain_text(NO_MATCH)
        expect(results).not_to_contain_text(STORY)

        # The skill alone.
        choose_by_keyboard(page, fields["role"], first_option(fields["role"]))
        press_by_keyboard(page, submit)
        expect(library_fact(page, ENGLISH)).to_be_visible()
        expect(results).not_to_contain_text(ROUTES)
        expect(results).not_to_contain_text(STORY)

        # Status: the draft, said in words.
        choose_by_keyboard(page, fields["skill"], first_option(fields["skill"]))
        draft_label = fields["status"].evaluate(
            "(el) => [...el.options].find((o) => o.value === 'draft').textContent.trim()"
        )
        choose_by_keyboard(page, fields["status"], draft_label)
        press_by_keyboard(page, submit)
        expect(library_fact(page, DEGREE)).to_be_visible()
        expect(library_fact(page, DEGREE)).to_contain_text("Draft")
        expect(results).not_to_contain_text(ROUTES)
        expect(page.get_by_text("Something went wrong")).to_have_count(0)

        # The filters are a plain GET form too: the same query as a link shows the same.
        open_app(page, running_app, tab_url("library") + "&q=routes")
        expect(library_fact(page, ROUTES)).to_be_visible()
        expect(page.locator(f"#{RESULTS_ID}")).not_to_contain_text(STORY)
        expect(page.locator(f"#{FILTERS_ID}").get_by_label(LABELS["q"], exact=True)).to_have_value(
            "routes"
        )


def test_library_works_without_scripts(running_app: RunningApp, browser: Browser) -> None:
    """With JavaScript off the tabs are links and the filter form a plain GET."""
    with facts_page(browser, DESKTOP) as page:
        import_drafts(page, running_app)
        named_button(page, CONFIRM_ALL).click()
        wait_said(page, batch_said(4, 0))
    with new_page(browser, DESKTOP, java_script_enabled=False) as page:
        open_app(page, running_app, tab_url("library"), scripts=False)
        filters = page.locator(f"#{FILTERS_ID}")
        filters.get_by_label(LABELS["q"], exact=True).fill("routes")
        named_button(filters, SEARCH).click()
        expect(page).to_have_url(re.compile(r"/facts\?.*q=routes"))
        expect(page.locator(f"#{RESULTS_ID}")).to_contain_text(ROUTES)
        expect(page.locator(f"#{RESULTS_ID}")).not_to_contain_text(DEGREE)
        tab_link(page, "drafts").click()
        expect(page).to_have_url(re.compile(r"/facts\?tab=drafts$"))
        expect(page.locator(f"#{DRAFTS_ID}")).to_contain_text(NO_DRAFTS)


# --- AC-M3.1: importing ------------------------------------------------------------------------


def test_ac_m3_1_import_paste_shows_drafts_none_confirmed(
    running_app: RunningApp, browser: Browser
) -> None:
    with facts_page(browser, DESKTOP) as page:
        open_app(page, running_app, "/facts")
        expect(page.get_by_role("heading", name="Fact bank", level=1)).to_be_visible()
        results = page.locator(f"#{RESULTS_ID}")
        expect(results).to_contain_text(EMPTY_LIBRARY)
        expect_draft_count(page, 0)

        # The empty state leads to the import form, which says where the CV's text goes.
        results.get_by_role("link", name=EMPTY_LINK).click()
        expect(page).to_have_url(re.compile(r"/facts\?tab=drafts#cv-import$"))
        section = page.locator(f"#{IMPORT_ID}")
        expect(section).to_be_visible()
        notice = page.locator(f"#{IMPORT_PRIVACY_ID}")
        expect(notice).to_be_visible()
        expect(notice).to_contain_text("AI provider")
        for field in (paste_field(page), file_field(page)):
            assert field.evaluate(
                "(el) => el.labels.length > 0 && !!el.labels[0].textContent.trim()"
            )
        start = page.locator(f"#{IMPORT_START_ID}")
        expect(start).to_have_text(IMPORT)
        expect(page.locator(f"#{DRAFTS_ID}")).to_contain_text(NO_DRAFTS)

        page.evaluate("() => { window.__stayed = true; }")
        start_import(page, CV_TEXT)

        # Focus goes to the started import, and the drafts arrive on their own: no reload.
        expect_focus_inside(page, section)
        for text in THE_FOUR:
            expect(draft(page, text)).to_be_visible(timeout=LONG)
        expect_draft_count(page, 4)
        assert page.evaluate("() => window.__stayed === true"), "the page was not reloaded"
        expect(page.locator(f"#{DRAFTS_ID}")).not_to_contain_text(NO_DRAFTS)

        # Each draft: the CV quote beside it and its own actions.
        routes = draft(page, ROUTES)
        expect(routes.locator("blockquote, q")).to_have_text(ROUTES)
        expect(routes).to_contain_text(QUOTE_LABEL)
        expect(draft(page, STORY).locator("blockquote, q")).to_have_text(INVOICES)
        expect(routes).to_contain_text("Sample Logistics SA")
        for text in THE_FOUR:
            expect(named_button(draft(page, text), CONFIRM)).to_be_visible()
            expect(named_button(draft(page, text), DISCARD)).to_be_visible()
        expect(named_button(page, CONFIRM_ALL)).to_have_count(1)
        # The kept upload is listed, with Remove and nothing to start again.
        listing = page.locator(f"#{IMPORT_LIST_ID}")
        expect(named_button(listing, REMOVE)).to_have_count(1)
        expect(named_button(listing, START)).to_have_count(0)
        expect(paste_field(page)).to_have_value("")

        # Nothing was confirmed: the Library is still empty, also after a reload, and says
        # that drafts wait.
        tab_link(page, "library").click()
        expect(page.locator(f"#{RESULTS_ID}")).to_contain_text(empty_with_drafts(4))
        expect(page.locator(f"#{RESULTS_ID}")).not_to_contain_text(EMPTY_LIBRARY)
        open_drafts(page, running_app)
        for text in THE_FOUR:
            expect(draft(page, text)).to_be_visible()
        expect_draft_count(page, 4)


def test_import_upload_fictional_pdf(running_app: RunningApp, browser: Browser) -> None:
    with facts_page(browser, DESKTOP) as page:
        open_drafts(page, running_app)
        file_field(page).set_input_files(str(CV_PDF))
        page.locator(f"#{IMPORT_START_ID}").click()

        for text in THE_FOUR:
            expect(draft(page, text)).to_be_visible(timeout=LONG)
        expect_draft_count(page, 4)
        expect(page.get_by_text(NEEDS_CHECK_NOTE)).to_have_count(0)
        expect(draft(page, ROUTES).locator("blockquote, q")).to_have_text(ROUTES)
        listing = page.locator(f"#{IMPORT_LIST_ID}")
        expect(named_button(listing, REMOVE)).to_have_count(1)
        expect(page.locator("main")).not_to_contain_text(CV_PDF.name)
        expect(page.get_by_text("Something went wrong")).to_have_count(0)

        # Removing the stored file keeps the drafts.
        named_button(listing, REMOVE).click()
        wait_said(page, MSG_REMOVED)
        expect(named_button(page.locator(f"#{IMPORT_LIST_ID}"), REMOVE)).to_have_count(0)
        open_drafts(page, running_app)
        expect_draft_count(page, 4)
        expect(draft(page, DEGREE)).to_be_visible()


def test_failed_import_shows_reason_and_retry(running_app: RunningApp, browser: Browser) -> None:
    with facts_page(browser, DESKTOP) as page:
        open_drafts(page, running_app)
        start_import(page, MALFORMED_CV)

        listing = page.locator(f"#{IMPORT_LIST_ID}")
        expect(listing).to_contain_text(MSG_INVALID_OUTPUT, timeout=LONG)
        expect(listing).to_contain_text("Failed")
        again = named_button(listing, TRY_AGAIN)
        expect(again).to_have_count(1)
        expect(named_button(listing, REMOVE)).to_have_count(1)
        expect(page.locator(f"#{DRAFTS_ID}")).to_contain_text(NO_DRAFTS)
        expect_draft_count(page, 0)
        expect(page.locator("main")).not_to_contain_text("fake-cv-malformed")

        # Try again by keyboard: the stored upload is started once more (and fails again,
        # since it holds the same text); the reason is back and nothing was drafted.
        tab_until(page, again)
        with page.expect_response(
            lambda response: response.request.method == "POST" and response.url.endswith("/start")
        ) as started:
            page.keyboard.press("Enter")
        assert started.value.status == 200
        # The new attempt shows as queued or running (nothing to try again), then fails.
        again = named_button(page.locator(f"#{IMPORT_LIST_ID}"), TRY_AGAIN)
        expect(again).to_have_count(0)
        expect(again).to_have_count(1, timeout=LONG)
        expect(page.locator(f"#{IMPORT_LIST_ID}")).to_contain_text(MSG_INVALID_OUTPUT)
        expect_draft_count(page, 0)

        # Remove clears it; the form is ready for another CV, which then imports.
        named_button(page.locator(f"#{IMPORT_LIST_ID}"), REMOVE).click()
        wait_said(page, MSG_REMOVED)
        expect(named_button(page.locator(f"#{IMPORT_LIST_ID}"), REMOVE)).to_have_count(0)
        start_import(page, CV_TEXT)
        expect(draft(page, ROUTES)).to_be_visible(timeout=LONG)
        expect_draft_count(page, 4)


def test_fake_drafts_labelled_practice_data(running_app: RunningApp, browser: Browser) -> None:
    """ADR-089 item 8: the fake's drafts say so, and keep saying so once confirmed."""
    with facts_page(browser, DESKTOP) as page:
        import_drafts(page, running_app)
        for text in THE_FOUR:
            expect(draft(page, text).get_by_text(PRACTICE_BADGE, exact=True)).to_be_visible()

        named_button(draft(page, ROUTES), CONFIRM).click()
        wait_said(page, SAID_CONFIRMED)
        tab_link(page, "library").click()
        confirmed = library_fact(page, ROUTES)
        expect(confirmed.get_by_text(PRACTICE_BADGE, exact=True)).to_be_visible()
        expect(confirmed).not_to_contain_text(BADGE_NEVER_USE)


# --- AC-M3.2: reviewing drafts -----------------------------------------------------------------


def test_ac_m3_2_confirm_edit_discard_batch_by_keyboard(
    running_app: RunningApp, browser: Browser
) -> None:
    """Keyboard only after the seed: confirm one draft, reword and confirm another, discard
    and undo a third, then confirm the rest together. Focus and the live region follow."""
    with facts_page(browser, DESKTOP) as page:
        import_drafts(page, running_app)
        page.evaluate("() => { window.__stayed = true; }")

        # Confirm.
        routes = draft(page, ROUTES)
        press_by_keyboard(page, named_button(routes, CONFIRM))
        wait_said(page, SAID_CONFIRMED)
        expect_draft_count(page, 3)
        expect(named_button(routes, CONFIRM)).to_have_count(0)
        expect_focus_inside(page, routes)

        # Edit, then confirm.
        degree = draft(page, DEGREE)
        press_by_keyboard(page, edit_control(degree))
        wording = degree.locator('textarea[name="text"]')
        expect(wording).to_be_visible()
        expect(wording).to_have_value(DEGREE)
        tab_until(page, wording)
        replace_text(page, REWORDED)
        press_by_keyboard(page, named_button(degree, SAVE_CONFIRM))
        wait_said(page, SAID_EDITED)
        expect_draft_count(page, 2)
        expect(named_button(degree, SAVE_CONFIRM)).to_have_count(0)
        expect_focus_inside(page, degree)

        # Discard, with undo one key press away.
        english = draft(page, ENGLISH)
        press_by_keyboard(page, named_button(english, DISCARD))
        wait_said(page, SAID_DISCARDED)
        expect_draft_count(page, 1)
        undo = named_button(english, UNDO)
        expect(undo).to_be_focused()
        page.keyboard.press("Enter")
        wait_said(page, SAID_RESTORED)
        expect_draft_count(page, 2)
        expect(named_button(english, CONFIRM)).to_be_visible()
        expect(english).to_contain_text(ENGLISH)
        expect_focus_inside(page, english)

        # A blank rewording is refused in place: the field is marked and takes focus.
        press_by_keyboard(page, edit_control(english))
        field = english.locator('textarea[name="text"]')
        tab_until(page, field)
        replace_text(page, "")
        press_by_keyboard(page, named_button(english, SAVE_CONFIRM))
        expect(field).to_have_attribute("aria-invalid", "true")
        expect(field).to_be_focused()
        assert field.evaluate(
            "(el) => (el.getAttribute('aria-describedby') || '').split(' ').some((id) => {"
            " const note = document.getElementById(id);"
            " return !!note && !!note.textContent.trim() && note.offsetParent !== null; })"
        ), "a visible message is tied to the field"
        expect_draft_count(page, 2)

        # Confirm all from this import: it leaves the draft whose inline form is open with
        # changed wording, and what was typed there stays.
        press_by_keyboard(page, named_button(page, CONFIRM_ALL))
        wait_said(page, batch_said(1, 0))
        status = page.locator(f"#{DRAFTS_ID} [id^='draft-group-'][id$='-status']")
        expect(status).to_have_text(batch_said(1, 0))
        expect(status).to_be_focused()
        expect_draft_count(page, 1)
        expect(field).to_have_attribute("aria-invalid", "true")
        expect(field).to_have_value("")
        expect(page.locator(f"#{DRAFTS_ID}")).not_to_contain_text(STORY)

        # Finishing the rewording confirms the last one.
        tab_until(page, field)
        page.keyboard.type(ENGLISH)
        press_by_keyboard(page, named_button(english, SAVE_CONFIRM))
        wait_said(page, SAID_EDITED, times=2)
        expect_draft_count(page, 0)
        assert page.evaluate("() => window.__stayed === true"), "every step was a swap"

        # All four are in the Library, the reworded one with its new wording.
        press_by_keyboard(page, tab_link(page, "library"))
        for text in (ROUTES, STORY, ENGLISH):
            expect(library_fact(page, text)).to_be_visible()
        expect(library_fact(page, REWORDED)).not_to_contain_text("2018")
        expect(page.locator(f"#{RESULTS_ID}")).not_to_contain_text(EMPTY_LIBRARY)
        expect(page.get_by_text("Something went wrong")).to_have_count(0)


def test_flagged_draft_note_and_batch_says_what_it_left(
    running_app: RunningApp, browser: Browser
) -> None:
    """ADR-091: a draft that needs a check says so, "Confirm all" leaves it and says how many
    it left, and its own Confirm still works."""
    with facts_page(browser, DESKTOP) as page:
        import_drafts(page, running_app, FLAGGED_CV, count=2)
        flagged = draft(page, TRAINED)
        expect(flagged.get_by_text(NEEDS_CHECK_NOTE, exact=True)).to_be_visible()
        expect(draft(page, ROUTES)).not_to_contain_text(NEEDS_CHECK_NOTE)
        expect(flagged.locator("blockquote, q")).to_have_text(TRAINED)

        press_by_keyboard(page, named_button(page, CONFIRM_ALL))
        said = batch_said(1, 1)
        wait_said(page, said)
        status = page.locator(f"#{DRAFTS_ID} [id^='draft-group-'][id$='-status']")
        expect(status).to_have_text(said)
        expect(status).to_be_focused()
        expect_draft_count(page, 1)
        expect(flagged.get_by_text(NEEDS_CHECK_NOTE, exact=True)).to_be_visible()

        press_by_keyboard(page, named_button(flagged, CONFIRM))
        wait_said(page, SAID_CONFIRMED)
        expect_draft_count(page, 0)
        tab_link(page, "library").click()
        expect(library_fact(page, TRAINED)).to_be_visible()
        expect(library_fact(page, TRAINED)).not_to_contain_text(NEEDS_CHECK_NOTE)


def test_stale_draft_reload_prompt_in_browser(running_app: RunningApp, browser: Browser) -> None:
    """Two tabs: the draft is confirmed in one; the other's Confirm gets the reload prompt in
    the draft's own block, focused, and its link shows the current state."""
    with facts_page(browser, DESKTOP) as page, facts_page(browser, DESKTOP) as other:
        import_drafts(page, running_app)
        open_drafts(other, running_app)
        stale = draft(other, ROUTES)
        named_button(draft(page, ROUTES), CONFIRM).click()
        wait_said(page, SAID_CONFIRMED)

        press_by_keyboard(other, named_button(stale, CONFIRM))

        prompt = stale.get_by_role("alert")
        expect(prompt).to_be_visible()
        expect(prompt).to_contain_text(VERSION_CONFLICT)
        expect(prompt).to_be_focused()
        expect(other.get_by_text("Something went wrong")).to_have_count(0)
        reload = prompt.get_by_role("link", name=RELOAD)
        press_by_keyboard(other, reload)
        expect(other).to_have_url(re.compile(r"/facts\?tab=drafts"))
        expect(draft(other, DEGREE)).to_be_visible()
        expect(other.locator(f"#{DRAFTS_ID}")).not_to_contain_text(ROUTES)
        expect_draft_count(other, 3)


# --- review fixes: nothing the user is doing is lost -------------------------------------------


def draft_blocks(page: Page) -> Locator:
    """Every fact's block in the drafts region, whatever it shows now."""
    return page.locator(f"#{DRAFTS_ID} .fact-card")


def test_typing_in_a_draft_survives_another_import(
    running_app: RunningApp, browser: Browser
) -> None:
    """Keep-edits: while an inline form is open, an import that finishes elsewhere replaces
    nothing. The typing stays, and a note says new drafts are ready."""
    with facts_page(browser, DESKTOP) as page, facts_page(browser, DESKTOP) as other:
        import_drafts(page, running_app)
        degree = draft(page, DEGREE)
        edit_control(degree).click()
        wording = degree.locator('textarea[name="text"]')
        wording.fill(REWORDED)
        page.evaluate(
            "() => { window.__stayed = true;"
            f" document.getElementById('{DRAFTS_ID}').dataset.probe = 'kept'; }}"
        )
        note = page.locator(f"#{STALE_NOTE_ID}")
        expect(note).to_be_hidden()

        # Another import finishes (started from a second tab).
        open_drafts(other, running_app)
        start_import(other, CV_TEXT)
        expect(draft_blocks(other)).to_have_count(8, timeout=LONG)

        wait_said(page, STALE_NOTE)
        expect(note).to_be_visible()
        expect(note).to_have_text(STALE_NOTE)
        expect(wording).to_have_value(REWORDED)
        expect(wording).to_be_focused()
        expect(draft_blocks(page)).to_have_count(4)
        region = page.locator(f"#{DRAFTS_ID}")
        expect(region).to_have_attribute("data-probe", "kept")
        assert page.evaluate("() => window.__stayed === true"), "nothing was reloaded"
        assert (
            page.evaluate(
                "(text) => window.__said.filter((said) => said === text).length", STALE_NOTE
            )
            == 1
        ), "the note is said once"

        # The typing can still be saved, and the note's link shows the new drafts.
        named_button(degree, SAVE_CONFIRM).click()
        wait_said(page, SAID_EDITED)
        note.get_by_role("link").click()
        expect(page).to_have_url(re.compile(r"/facts\?tab=drafts$"))
        expect(draft_blocks(page)).to_have_count(7)


def test_undo_survives_another_import_and_confirm_all(
    running_app: RunningApp, browser: Browser
) -> None:
    """A discarded draft keeps its Undo: a finished import doesn't refresh it away, and
    "Confirm all" neither drops it nor confirms the draft being reworded beside it."""
    with facts_page(browser, DESKTOP) as page, facts_page(browser, DESKTOP) as other:
        import_drafts(page, running_app)
        heading = page.locator(f"#{DRAFTS_ID} [id^='draft-group-'][id$='-heading']")
        expect(heading).to_contain_text("\u00b7 4 drafts")
        confirm_all = named_button(page, CONFIRM_ALL)
        expect(page.get_by_text(batch_hint(4), exact=True)).to_be_visible()
        assert confirm_all.evaluate(
            "(button, id) => !!(button.compareDocumentPosition("
            "document.querySelector(`#${id} [role=list]`)) & Node.DOCUMENT_POSITION_PRECEDING)",
            DRAFTS_ID,
        ), "the batch button comes after the group's drafts"
        expect(page.get_by_role("link", name=skip_text(4))).to_be_visible()

        english = draft(page, ENGLISH)
        named_button(english, DISCARD).click()
        wait_said(page, SAID_DISCARDED)
        undo = named_button(english, UNDO)
        expect(english).to_contain_text(DISCARDED_LINE)
        expect(heading).to_contain_text("\u00b7 3 drafts")
        expect(page.get_by_role("link", name=skip_text(3))).to_be_visible()
        # Focus leaves the drafts: only the Undo on the page holds the refresh now.
        page.locator(f"#{IMPORT_ID} textarea[name='text']").focus()

        open_drafts(other, running_app)
        start_import(other, CV_TEXT)
        expect(draft_blocks(other)).to_have_count(7, timeout=LONG)
        wait_said(page, STALE_NOTE)
        expect(undo).to_be_visible()
        expect(draft_blocks(page)).to_have_count(4)

        # Reword one draft without saving, then confirm all.
        degree = draft(page, DEGREE)
        edit_control(degree).click()
        wording = degree.locator('textarea[name="text"]')
        wording.fill(REWORDED)
        confirm_all.click()
        wait_said(page, batch_said(2, 0))
        expect(undo).to_be_visible()
        expect(wording).to_have_value(REWORDED)
        expect(named_button(degree, SAVE_CONFIRM)).to_be_visible()
        expect(draft(page, DEGREE)).to_have_count(1)
        expect(page.locator(f"#{DRAFTS_ID}")).not_to_contain_text(ROUTES)
        expect_draft_count(page, 5)  # this import's reworded one and the other import's four
        expect(heading).to_contain_text("\u00b7 1 draft")

        # Undo still works, and the reworded draft is saved with what was typed.
        undo.click()
        wait_said(page, SAID_RESTORED)
        expect(named_button(english, CONFIRM)).to_be_visible()
        named_button(degree, SAVE_CONFIRM).click()
        wait_said(page, SAID_EDITED)
        tab_link(page, "library").click()
        expect(library_fact(page, REWORDED)).not_to_contain_text("2018")
        expect(page.get_by_text("Something went wrong")).to_have_count(0)


def test_refused_upload_shows_its_reason_in_the_page(
    running_app: RunningApp, browser: Browser
) -> None:
    """A file and a pasted text together: the import section says to choose one, in place,
    with focus on the reason. Nothing was uploaded."""
    with facts_page(browser, DESKTOP) as page:
        open_drafts(page, running_app)
        page.evaluate("() => { window.__stayed = true; }")
        file_field(page).set_input_files(str(CV_PDF))
        paste_field(page).fill(CV_TEXT)
        with page.expect_response(
            lambda response: (
                response.request.method == "POST" and response.url.endswith("/facts/import")
            )
        ) as refused:
            page.locator(f"#{IMPORT_START_ID}").click()
        assert refused.value.status == 400

        reason = page.locator(f"#{IMPORT_ERROR_ID}")
        expect(reason).to_be_visible()
        expect(reason).to_have_text(CHOOSE_ONE)
        expect(reason).to_be_focused()
        assert page.evaluate("() => window.__stayed === true"), "shown in place"
        expect(page.get_by_text("Something went wrong")).to_have_count(0)
        listing = page.locator(f"#{IMPORT_LIST_ID}")
        expect(named_button(listing, REMOVE)).to_have_count(0)
        expect(page.locator(f"#{DRAFTS_ID}")).to_contain_text(NO_DRAFTS)
        # The form is ready again: pasting alone imports.
        start_import(page, CV_TEXT)
        expect(page.locator(f"#{IMPORT_STATUS_ID}")).to_have_text(STARTED_LINE)
        expect(draft(page, ROUTES)).to_be_visible(timeout=LONG)


def test_failed_import_is_announced_with_where_the_reason_is(
    running_app: RunningApp, browser: Browser
) -> None:
    """On this page a failed import's reason is in its own row: the live region says that,
    not "Open Tasks to see why"."""
    with facts_page(browser, DESKTOP) as page:
        open_drafts(page, running_app)
        start_import(page, MALFORMED_CV)
        expect(page.locator(f"#{IMPORT_LIST_ID}")).to_contain_text(MSG_INVALID_OUTPUT, timeout=LONG)
        wait_said(page, SAID_IMPORT_FAILED)
        said = page.evaluate("() => window.__said")
        assert not [words for words in said if "Open Tasks" in words], said


def test_flagged_quote_caption_in_the_browser(running_app: RunningApp, browser: Browser) -> None:
    with facts_page(browser, DESKTOP) as page:
        import_drafts(page, running_app, FLAGGED_CV, count=2)
        flagged = draft(page, TRAINED)
        expect(flagged.locator("figcaption")).to_have_text(QUOTE_LABEL_UNCHECKED)
        expect(draft(page, ROUTES).locator("figcaption")).to_have_text(QUOTE_LABEL)


# --- phone widths and axe ----------------------------------------------------------------------


def review_states(page: Page, app: RunningApp) -> Iterator[str]:
    """Walk the page through its states, yielding at each one to be checked: the empty
    Library, the import form, drafts with a flagged one, an open inline form with a refused
    wording, a discarded draft with Undo, the batch result, the Library with facts and with
    no match, and the Capture inbox."""
    open_app(page, app, "/facts")
    expect(page.locator(f"#{RESULTS_ID}")).to_contain_text(EMPTY_LIBRARY)
    yield "empty library"
    open_drafts(page, app)
    yield "import form"
    start_import(page, FLAGGED_CV)
    flagged = draft(page, TRAINED)
    routes = draft(page, ROUTES)
    expect(flagged.get_by_text(NEEDS_CHECK_NOTE, exact=True)).to_be_visible()
    expect_draft_count(page, 2)
    yield "drafts with a flagged one"
    edit_control(flagged).click()
    field = flagged.locator('textarea[name="text"]')
    expect(field).to_be_visible()
    field.fill("")
    named_button(flagged, SAVE_CONFIRM).click()
    expect(field).to_have_attribute("aria-invalid", "true")
    yield "inline form with a refused wording"
    named_button(routes, DISCARD).click()
    expect(named_button(routes, UNDO)).to_be_visible()
    yield "discarded with undo"
    named_button(routes, UNDO).click()
    expect(named_button(routes, CONFIRM)).to_be_visible()
    named_button(page, CONFIRM_ALL).click()
    expect(page.locator(f"#{DRAFTS_ID} [id^='draft-group-'][id$='-status']")).to_have_text(
        batch_said(1, 1)
    )
    yield "batch result"
    open_app(page, app, tab_url("library"))
    expect(library_fact(page, ROUTES)).to_be_visible()
    yield "library with a fact"
    open_app(page, app, tab_url("library") + "&q=xylophone")
    expect(page.locator(f"#{RESULTS_ID}")).to_contain_text(NO_MATCH)
    yield "library with no match"
    open_app(page, app, tab_url("capture"))
    expect(page.get_by_role("heading", name=TABS["capture"], level=2)).to_be_visible()
    yield "capture inbox"


@pytest.mark.parametrize("viewport", [PHONE, SMALL_PHONE], ids=["390x844", "360x740"])
def test_facts_390x844_no_horizontal_scroll(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int]
) -> None:
    """At 390x844 (and at 360 px, the narrowest supported width) no state of the page scrolls
    sideways, and its controls are at least 24 by 24 px."""
    assert PHONE == {"width": 390, "height": 844}
    with facts_page(browser, viewport) as page:
        for state in review_states(page, running_app):
            fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
            assert fits, f"{state}: scrollWidth {scroll_width} > {inner_width}"
            controls = page.locator("main").locator("button:visible, summary:visible")
            for index in range(controls.count()):
                control = controls.nth(index)
                assert_target_size(control, f"{state}: {control.inner_text().strip()[:30]!r}")
        for tab in TABS:
            expect(tab_link(page, tab)).to_be_visible()


@pytest.mark.parametrize(("viewport", "scheme"), AXE_CASES)
def test_axe_facts(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int], scheme: str
) -> None:
    """No serious or critical axe findings in any state of the page, on desktop and phone, in
    light and dark."""
    with facts_page(browser, viewport, color_scheme=scheme) as page:
        seen = []
        for state in review_states(page, running_app):
            try:
                assert_no_serious_violations(page)
            except AssertionError as error:
                raise AssertionError(f"{state}: {error}") from None
            seen.append(state)
        assert len(seen) == 9


def test_phone_review_by_touch_sized_controls(running_app: RunningApp, browser: Browser) -> None:
    """At 390x844 the whole review works: import, confirm one, confirm the rest."""
    with facts_page(browser, PHONE) as page:
        import_drafts(page, running_app)
        assert_fits(page)
        named_button(draft(page, ROUTES), CONFIRM).click()
        wait_said(page, SAID_CONFIRMED)
        named_button(page, CONFIRM_ALL).click()
        wait_said(page, batch_said(3, 0))
        expect_draft_count(page, 0)
        assert_fits(page)
        tab_link(page, "library").click()
        for text in THE_FOUR:
            expect(library_fact(page, text)).to_be_visible()
        assert_fits(page)
