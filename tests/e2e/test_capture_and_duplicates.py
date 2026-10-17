"""The capture inbox and possible duplicates in a real browser (M3.7).

The app starts with a completed setup and the fake provider saved (``running_app`` default), so
a pasted CV gives the four fictional "Alex Example" drafts. Keyboard only after any seeding:
a note is added, turned into a fact through the editor and leaves the inbox; another is
deleted and brought back with Undo. On the Drafts tab, "Find possible duplicates" lists a draft
that repeats a confirmed fact, and its "Compare and merge" link opens the M3.6 merge screen.
Axe runs in light and dark at desktop and 390x844; nothing scrolls sideways on the phone.
Names, ids and copy come from ``tests/facts_page.py``. Everything is fictional.
"""

import re

import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_facts_page import (
    AXE_CASES,
    ROUTES,
    draft,
    expect_draft_count,
    facts_page,
    import_drafts,
    named_button,
    open_drafts,
    press_by_keyboard,
    tab_until,
    wait_said,
)
from tests.e2e.test_shell import has_no_horizontal_scroll, open_app
from tests.facts_page import (
    ADD_NOTE,
    CAPTURE_EMPTY,
    CAPTURE_ID,
    CAPTURE_LIST_ID,
    COMPARE_AND_MERGE,
    CONFIRM,
    DELETE_NOTE,
    DUPLICATES_ID,
    EDITOR_ID,
    FIND_DUPLICATES,
    MERGE_COMPARE_ID,
    MERGE_DRAFT_ID,
    MERGE_TARGET_ID,
    NOT_A_DUPLICATE,
    SAID_ADDED,
    SAID_CONFIRMED,
    SAID_DISMISSED,
    SAID_NOTE_ADDED,
    SAID_NOTE_DELETED,
    SAID_NOTE_RESTORED,
    SAVE,
    TURN_INTO_FACT,
    UNDO,
    tab_url,
)
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e

NOTE = "Ran the fictional spring stocktake at the Example Freight depot in two days."
OTHER = "Ask Example Freight about the forklift course."
MERGE_PAGE = re.compile(r"/facts/[^/?#]+/merge\?target=")


def assert_fits(page: Page, state: str) -> None:
    fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
    assert fits, f"{state}: scrollWidth {scroll_width} > {inner_width}"


def note_item(page: Page, text: str) -> Locator:
    return page.locator(f"#{CAPTURE_LIST_ID} [id^='note-']").filter(has_text=text)


@pytest.mark.parametrize(("viewport", "scheme"), AXE_CASES)
def test_capture_note_to_fact_by_keyboard_axe(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int], scheme: str
) -> None:
    with facts_page(browser, viewport, color_scheme=scheme) as page:
        open_app(page, running_app, tab_url("capture"))
        inbox = page.locator(f"#{CAPTURE_ID}")
        expect(inbox).to_contain_text(CAPTURE_EMPTY)
        assert_no_serious_violations(page)
        assert_fits(page, "empty inbox")

        # Two notes, typed and added by keyboard.
        for text in (NOTE, OTHER):
            tab_until(page, inbox.locator('textarea[name="text"]'))
            page.keyboard.type(text)
            press_by_keyboard(page, named_button(inbox, ADD_NOTE))
            wait_said(page, SAID_NOTE_ADDED, times=1 if text == NOTE else 2)
            expect(note_item(page, text)).to_have_count(1)
        assert_no_serious_violations(page)
        assert_fits(page, "inbox with notes")

        # Delete one and bring it back with Undo.
        other = note_item(page, OTHER)
        press_by_keyboard(page, named_button(other, DELETE_NOTE))
        wait_said(page, SAID_NOTE_DELETED)
        expect(note_item(page, OTHER)).to_have_count(0)
        press_by_keyboard(page, named_button(inbox, UNDO))
        wait_said(page, SAID_NOTE_RESTORED)
        expect(note_item(page, OTHER)).to_have_count(1)

        # Turn the first into a fact: the editor holds its text; Save adds it.
        item = note_item(page, NOTE)
        press_by_keyboard(page, item.get_by_role("link", name=TURN_INTO_FACT))
        form = page.locator(f"form#{EDITOR_ID}")
        expect(form.locator('[name="text"]')).to_have_value(NOTE)
        assert_no_serious_violations(page)
        press_by_keyboard(page, named_button(form, SAVE))
        wait_said(page, SAID_ADDED)

        open_app(page, running_app, tab_url("capture"))
        expect(note_item(page, NOTE)).to_have_count(0)
        expect(note_item(page, OTHER)).to_have_count(1)
        open_app(page, running_app, tab_url("library"))
        expect(page.locator("main")).to_contain_text(NOTE)


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "390x844"])
def test_duplicate_suggestion_opens_merge(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int]
) -> None:
    """Seed (mouse): import the fictional CV, confirm its routes draft, import it again. Then by
    keyboard: find possible duplicates, dismiss nothing, open "Compare and merge"."""
    with facts_page(browser, viewport) as page:
        import_drafts(page, running_app)
        named_button(draft(page, ROUTES), CONFIRM).click()
        wait_said(page, SAID_CONFIRMED)
        import_drafts(page, running_app, count=7)

        open_drafts(page, running_app)
        press_by_keyboard(page, named_button(page, FIND_DUPLICATES))
        listing = page.locator(f"#{DUPLICATES_ID}")
        pairs = listing.locator("[id^='duplicate-']")
        expect(pairs).to_have_count(1)
        expect(pairs).to_contain_text(ROUTES)
        expect(pairs.get_by_role("button", name=NOT_A_DUPLICATE)).to_have_count(1)
        assert_no_serious_violations(page)
        assert_fits(page, "duplicates listed")

        press_by_keyboard(page, pairs.get_by_role("link", name=COMPARE_AND_MERGE))
        expect(page).to_have_url(MERGE_PAGE)
        expect(page.locator(f"#{MERGE_COMPARE_ID}")).to_be_visible()
        expect(page.locator(f"#{MERGE_DRAFT_ID}")).to_contain_text(ROUTES)
        expect(page.locator(f"#{MERGE_TARGET_ID}")).to_contain_text(ROUTES)
        assert_no_serious_violations(page)

        # Back on the Drafts tab: dismissing the pair is remembered.
        open_drafts(page, running_app)
        expect_draft_count(page, 7)
        press_by_keyboard(page, named_button(page, FIND_DUPLICATES))
        press_by_keyboard(page, named_button(pairs, NOT_A_DUPLICATE))
        wait_said(page, SAID_DISMISSED)
        expect(pairs).to_have_count(0)
        open_drafts(page, running_app)
        press_by_keyboard(page, named_button(page, FIND_DUPLICATES))
        expect(listing).to_be_visible()
        expect(pairs).to_have_count(0)
