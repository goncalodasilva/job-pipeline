"""M6.9 the cover letter step (step 4) in a real browser with the real CLI, fictional facts, the
keyboard, a phone viewport and axe. Markup hooks are in the docstring of ``tests/letter_pages.py``.

The app starts with a completed setup (the fake provider saved), so the letter is the fake
provider's practice answer: one paragraph for each of the first two facts, each starting "Practice
data.". The note keyword ``fake-letter-invented`` makes it write an invented paragraph instead.
A fictional application with a current summary and confirmed facts is written into the app's
temporary database first (``tests/e2e/test_tailoring_workspace.py::seeded``). No CV and no CV
template are needed. Keyboard path: Tab, Enter, Space and typing only. No fixed waits.
"""

import re

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, press_by_keyboard, tab_until, wait_said
from tests.e2e.test_shell import has_no_horizontal_scroll, new_page, open_app
from tests.e2e.test_tailoring_workspace import heading, region, seeded, stepper_link
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e
NEW_CLAIM = "I led 9 people and saved $2m."
WORDED = "I planned 60 delivery routes a week with a team of 3, mostly at night."
GENERATED = '#letter-step[data-state="draft"]'


def open_letter(page: Page, app: RunningApp, aid: str, note: str = "") -> None:
    """Open step 4 from its address and draft the letter with the keyboard."""
    page.add_init_script(RECORD_ANNOUNCEMENTS)
    open_app(page, app, f"/applications/{aid}?tab=tailoring&step=4")
    if note:
        page.locator("#letter-note").fill(note)
    press_by_keyboard(page, page.locator("#letter-generate-start"))
    expect(page.locator(GENERATED)).to_be_visible(timeout=20_000)


def edit_first(page: Page, text: str) -> None:
    line = page.locator("li[data-unit-id]").first
    press_by_keyboard(page, line.locator("details[data-line-edit] > summary"))
    box = line.get_by_label("Edit this paragraph", exact=True)
    tab_until(page, box)
    page.keyboard.press("ControlOrMeta+A")
    page.keyboard.type(text)


def test_cover_letter_draft_edit_approve_by_keyboard(running_app: RunningApp, browser: Browser):
    aid = seeded(running_app)
    with new_page(browser, DESKTOP) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        open_app(page, running_app, f"/applications/{aid}?tab=tailoring")
        # From the stepper, by keyboard only: the step is optional, says so and loads its frame.
        press_by_keyboard(page, stepper_link(page, "Cover letter"))
        expect(heading(page, 4, "Cover letter")).to_be_visible()
        expect(page.locator("#letter-optional")).to_contain_text("optional")
        expect(page.locator("#letter-next")).to_have_text("Continue without a letter")
        expect(region(page)).not_to_contain_text(re.compile("later step", re.IGNORECASE))
        # The greeting, the opening line (it names the role and the company), the closing and
        # the name are the application's, loaded after the page; there is nothing to type in them.
        opening = page.locator("[data-letter-opening]")
        expect(opening).to_contain_text("Route Planner")
        expect(opening).to_contain_text("Harbor Kite Fictional")
        expect(page.locator("[data-letter-greeting]")).to_be_visible()
        expect(page.locator("[data-letter-closing]")).to_be_visible()
        expect(page.locator("#letter-frame").locator("input, textarea, button")).to_have_count(0)

        # Tone and length with the keyboard (typeahead on the focused select), then draft.
        tab_until(page, page.locator("#letter-tone"))
        page.keyboard.type("f")
        expect(page.locator("#letter-tone")).to_have_value("formal")
        tab_until(page, page.locator("#letter-length"))
        page.keyboard.type("s")
        expect(page.locator("#letter-length")).to_have_value("short")
        press_by_keyboard(page, page.locator("#letter-generate-start"))
        wait_said(page, "Letter drafting started.")
        expect(page.locator(GENERATED)).to_be_visible(timeout=20_000)
        wait_said(page, "Letter ready to review.")
        expect(page.locator("#letter-tone")).to_have_value("formal")  # the choice is kept
        expect(page.locator("li[data-unit-id]")).to_have_count(2)
        expect(page.locator("#letter-practice")).to_contain_text("Practice data")

        # The facts behind each paragraph are reachable by keyboard.
        disclosure = page.locator("details[data-line-facts]").first
        press_by_keyboard(page, disclosure.locator("summary"))
        link = disclosure.get_by_role("link").first
        tab_until(page, link)
        assert str(link.get_attribute("href")).startswith("/facts/")

        # Edit the first paragraph, save, approve: all by keyboard.
        edit_first(page, WORDED)
        press_by_keyboard(page, page.locator("[data-line-form=edit]").first.get_by_role("button"))
        wait_said(page, "Paragraph saved.")
        expect(page.locator('li[data-origin="user"]')).to_have_count(1)
        expect(page.locator("#letter-approve")).not_to_have_attribute("aria-disabled", "true")
        press_by_keyboard(page, page.locator("#letter-approve"))
        wait_said(page, "Letter approved.")
        expect(page.locator("#letter-approved")).to_be_visible()
        expect(page.locator("#letter-heading")).to_be_focused()
        expect(page.locator("#letter-next")).to_have_text("Continue to Submit")
        assert_no_serious_violations(page)

        press_by_keyboard(page, page.locator("#letter-next"))
        expect(heading(page, 5, "Submit")).to_be_visible()


def test_ac_m6_1_letter_flagged_paragraph_blocks_approval_until_resolved(
    running_app: RunningApp, browser: Browser
):
    aid = seeded(running_app)
    with new_page(browser, DESKTOP) as page:
        open_letter(page, running_app, aid, "fake-letter-invented")
        expect(page.locator("#letter-approve")).to_have_attribute("aria-disabled", "true")
        expect(page.locator("#letter-approve-reason")).to_have_text("1 paragraph needs your check.")
        expect(page.locator("[data-flags]")).to_contain_text("Check this number")
        press_by_keyboard(page, page.get_by_role("button", name="This is true — save it as a fact"))
        wait_said(page, "Saved as a fact. The paragraph now cites it.")
        expect(page.locator("#letter-approve")).not_to_have_attribute("aria-disabled", "true")
        press_by_keyboard(page, page.locator("#letter-approve"))
        expect(page.locator("#letter-approved")).to_be_visible()


def test_ac_m6_4_letter_typing_new_claim_shows_save_as_fact_by_keyboard(
    running_app: RunningApp, browser: Browser
):
    aid = seeded(running_app)
    with new_page(browser, DESKTOP) as page:
        open_letter(page, running_app, aid)
        edit_first(page, NEW_CLAIM)
        press_by_keyboard(page, page.locator("[data-line-form=edit]").first.get_by_role("button"))
        dialog = page.locator("#letter-save-fact")
        expect(dialog).to_be_visible()
        expect(dialog.get_by_role("button", name="Not now")).to_be_focused()
        page.keyboard.press("Escape")
        expect(dialog).to_have_count(0)
        expect(page.locator("#letter-approve")).to_have_attribute("aria-disabled", "true")


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone-390x844"])
def test_letter_step_phone_390x844_and_axe(
    running_app: RunningApp, browser: Browser, viewport, scheme
):
    aid = seeded(running_app)
    with new_page(browser, viewport, color_scheme=scheme) as page:
        open_letter(page, running_app, aid, "fake-letter-invented")
        expect(page.locator("[data-letter-opening]")).to_be_visible()
        edit_first(page, NEW_CLAIM)
        wait_for_settled_styles(page)
        assert has_no_horizontal_scroll(page)[0]
        assert_no_serious_violations(page)
        # And with the "Save this as a fact?" question open.
        press_by_keyboard(page, page.locator("[data-line-form=edit]").first.get_by_role("button"))
        expect(page.locator("#letter-save-fact")).to_be_visible()
        wait_for_settled_styles(page)
        assert has_no_horizontal_scroll(page)[0]
        assert_no_serious_violations(page)
