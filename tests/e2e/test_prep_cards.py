"""Narrative cards in a real browser: review, edit, confirm a claim as a fact, approve and build
the salary card, by keyboard and on a phone (M8.2 part B; spec 3.9; ADR-124 item 8).

The app runs with a finished setup and the fake provider, so a drafted card is the fake's own
answer: one or two paragraphs, each starting "Practice data.", each citing a confirmed fact and
saying nothing those facts do not. The claim flagged here is found by code from the person's
edit, not by the fake. Browser-facing contract (the markup hooks, ids and announcement texts are
written in the docstring of ``tests/prep_card_pages.py``; the routes are exercised by
``tests/integration/test_prep_cards.py``):

- The Prep tab has the link ``#prep-cards-link`` ("Narrative cards"); it opens the cards inside
  ``#prep`` without a reload and ``#cards-heading`` takes focus.
- Seven ``section[data-card]`` follow, each with ``data-state``; an action on one card moves
  focus to that card's heading ``#card-<kind>-heading``.
- Drafting: ``button#card-<kind>-generate`` says "Card drafting started." and the card reaches
  ``data-state="draft"``. Editing: ``details[data-unit-edit]`` "Edit" on a paragraph, a textarea
  labelled "Edit this paragraph", the button "Save paragraph"; a paragraph that claims something
  no cited fact states says "Paragraph saved. Save this as a fact?", is ``data-flagged="true"``,
  disables ``#card-<kind>-approve`` (``aria-disabled``) with ``#card-<kind>-approve-reason``
  "1 paragraph needs your check.", and offers the button "This is true — save it as a fact"
  ("Saved as a fact. The paragraph now cites it."). No dialog opens.
- Approving: "Card approved." and ``#card-<kind>-approved`` ("Approved. Changing a paragraph
  starts a new draft.").
- The salary card: ``button#card-salary-build`` says "Salary card built and approved." and the
  card is ``data-state="approved"`` at once; no AI task runs for it.

Keyboard path: Tab, Space, Enter and typing only. No fixed waits (htmx has processed a form
before it is pressed).
"""

import re

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_board import rows
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, press_by_keyboard, tab_until, wait_said
from tests.e2e.test_prep_practice import (
    JOB_ID,
    RESULT_TIMEOUT_MS,
    change_db,
    seeded,
    wait_for_forms,
)
from tests.e2e.test_shell import assert_target_size, has_no_horizontal_scroll, new_page, open_app
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e

KIND = "tell_me_about_yourself"
NEW_CLAIM = "I led 9 people and saved $2m."
assert JOB_ID == "practice-e2e-job"
PAY = (
    "UPDATE jobs SET salary_min = 55000, salary_max = 65000, salary_currency = 'EUR', "
    "salary_period = 'year' WHERE id = 'practice-e2e-job'"
)
TWO_COUNT = "SELECT count(*) FROM tasks WHERE type = 'draft_narrative'"


def card(page: Page, kind: str = KIND):
    return page.locator(f"#card-{kind}")


def open_cards(page: Page, app: RunningApp, aid: str, *, keyboard: bool) -> None:
    open_app(page, app, f"/applications/{aid}?tab=prep")
    expect(page.locator("#prep-cards-link")).to_be_visible()
    wait_for_forms(page, "#prep-cards-link")
    if keyboard:
        press_by_keyboard(page, page.locator("#prep-cards-link"))
    else:
        page.locator("#prep-cards-link").click()
    expect(page.locator("#cards-heading")).to_be_focused()
    expect(page.locator("section[data-card]")).to_have_count(7)


def draft_by_keyboard(page: Page, kind: str = KIND) -> None:
    wait_for_forms(page, f"#card-{kind}-generate-form")
    press_by_keyboard(page, page.locator(f"#card-{kind}-generate"))
    wait_said(page, "Card drafting started.")
    expect(page.locator(f"#card-{kind}-heading")).to_be_focused()
    expect(card(page, kind)).to_have_attribute("data-state", "draft", timeout=RESULT_TIMEOUT_MS)


def edit_first_by_keyboard(page: Page, text: str, kind: str = KIND) -> None:
    unit = card(page, kind).locator("li[data-unit]").first
    press_by_keyboard(page, unit.locator("details[data-unit-edit] > summary"))
    box = unit.get_by_label("Edit this paragraph", exact=True)
    tab_until(page, box)
    page.keyboard.press("ControlOrMeta+A")
    page.keyboard.type(text)
    expect(box).to_have_value(text)
    wait_for_forms(page, f"#card-{kind} form[data-unit-form=edit]")
    press_by_keyboard(
        page, unit.locator("form[data-unit-form=edit]").get_by_role("button", name="Save paragraph")
    )


def test_narrative_card_review_and_approve_by_keyboard(running_app: RunningApp, browser: Browser):
    aid = seeded(running_app)
    change_db(running_app, PAY)
    with new_page(browser, DESKTOP) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        open_cards(page, running_app, aid, keyboard=True)
        expect(card(page)).to_have_attribute("data-state", "none")
        assert_no_serious_violations(page)

        # Draft the card: the fake provider's practice answer, every paragraph cited.
        draft_by_keyboard(page)
        expect(page.locator(f"#card-{KIND}-practice")).to_contain_text("Practice data")
        units = card(page).locator("li[data-unit]")
        expect(units.first).to_be_visible()
        assert 1 <= units.count() <= 2
        expect(units.first.locator("[data-unit-text]")).to_contain_text("Practice data.")
        expect(card(page).locator('li[data-unit][data-flagged="true"]')).to_have_count(0)
        expect(page.locator(f"#card-{KIND}-approve")).not_to_have_attribute("aria-disabled", "true")

        # The facts behind the paragraph are reachable by keyboard.
        disclosure = units.first.locator("details[data-unit-facts]")
        press_by_keyboard(page, disclosure.locator("summary"))
        link = disclosure.get_by_role("link").first
        tab_until(page, link)
        assert str(link.get_attribute("href")).startswith("/facts/")

        # Say something no confirmed fact states: it is flagged and blocks approval.
        edit_first_by_keyboard(page, NEW_CLAIM)
        wait_said(page, "Paragraph saved. Save this as a fact?")
        expect(page.locator(f"#card-{KIND}-heading")).to_be_focused()
        flagged = card(page).locator('li[data-unit][data-flagged="true"]')
        expect(flagged).to_have_count(1)
        expect(flagged).to_contain_text(NEW_CLAIM)
        expect(page.locator("dialog[open]")).to_have_count(0)
        approve = page.locator(f"#card-{KIND}-approve")
        expect(approve).to_have_attribute("aria-disabled", "true")
        expect(page.locator(f"#card-{KIND}-approve-reason")).to_have_text(
            "1 paragraph needs your check."
        )
        assert rows(running_app, "SELECT count(*) FROM facts WHERE number > 5") == [(0,)]
        assert_no_serious_violations(page)

        # "This is true": the claim becomes a confirmed fact (source document) the paragraph cites.
        wait_for_forms(page, f"#card-{KIND} form[data-unit-form=confirm]")
        press_by_keyboard(
            page, page.get_by_role("button", name="This is true — save it as a fact", exact=True)
        )
        wait_said(page, "Saved as a fact. The paragraph now cites it.")
        expect(card(page).locator('li[data-unit][data-flagged="true"]')).to_have_count(0)
        expect(approve).not_to_have_attribute("aria-disabled", "true")
        assert rows(
            running_app,
            "SELECT f.status, v.source, v.text FROM facts f JOIN fact_versions v "
            "ON v.fact_id = f.id WHERE f.number > 5",
        ) == [("confirmed", "document", NEW_CLAIM)]

        # Approve.
        wait_for_forms(page, f"#card-{KIND}-approve-form")
        press_by_keyboard(page, approve)
        wait_said(page, "Card approved.")
        expect(page.locator(f"#card-{KIND}-heading")).to_be_focused()
        expect(page.locator(f"#card-{KIND}-approved")).to_be_visible()
        expect(card(page)).to_have_attribute("data-state", "approved")
        assert rows(
            running_app,
            "SELECT v.status FROM prep_document_versions v JOIN prep_documents d "
            "ON d.id = v.document_id WHERE d.kind = ? ORDER BY v.number DESC LIMIT 1",
            (KIND,),
        ) == [("approved",)]
        assert_no_serious_violations(page)

        # The salary card is built in code: no task, approved at once.
        wait_for_forms(page, "#card-salary-form")
        press_by_keyboard(page, page.locator("#card-salary-build"))
        wait_said(page, "Salary card built and approved.")
        expect(page.locator("#card-salary-heading")).to_be_focused()
        expect(card(page, "salary")).to_have_attribute("data-state", "approved")
        expect(card(page, "salary")).to_contain_text(
            "The posting states 55,000 to 65,000 EUR per year."
        )
        assert rows(running_app, TWO_COUNT) == [(1,)], "only the written card ran a task"
        assert_no_serious_violations(page)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone390x844"])
def test_prep_cards_phone_390x844_and_axe(
    running_app: RunningApp, browser: Browser, theme: str, viewport: dict[str, int]
):
    aid = seeded(running_app)
    change_db(running_app, PAY)
    with new_page(browser, viewport, color_scheme=theme) as page:
        open_cards(page, running_app, aid, keyboard=False)
        wait_for_settled_styles(page)
        ok, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert ok, (scroll_width, inner_width)
        assert_no_serious_violations(page)

        generate = page.locator(f"#card-{KIND}-generate")
        assert_target_size(generate, "Draft this card")
        wait_for_forms(page, f"#card-{KIND}-generate-form")
        generate.click()
        expect(card(page)).to_have_attribute("data-state", "draft", timeout=RESULT_TIMEOUT_MS)
        wait_for_settled_styles(page)
        ok, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert ok, (scroll_width, inner_width)
        assert_target_size(page.locator(f"#card-{KIND}-approve"), "Approve card")
        assert_target_size(
            card(page).locator("details[data-unit-edit] > summary").first, "Edit paragraph"
        )
        assert_no_serious_violations(page)

        # A flagged paragraph and a disabled approve button keep the layout and the contrast.
        unit = card(page).locator("li[data-unit]").first
        unit.locator("details[data-unit-edit] > summary").click()
        unit.get_by_label("Edit this paragraph", exact=True).fill(NEW_CLAIM)
        wait_for_forms(page, f"#card-{KIND} form[data-unit-form=edit]")
        unit.locator("form[data-unit-form=edit]").get_by_role(
            "button", name="Save paragraph"
        ).click()
        expect(card(page).locator('li[data-unit][data-flagged="true"]')).to_have_count(
            1, timeout=RESULT_TIMEOUT_MS
        )
        wait_for_settled_styles(page)
        ok, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert ok, (scroll_width, inner_width)
        assert_no_serious_violations(page)
        confirm = page.get_by_role("button", name="This is true — save it as a fact", exact=True)
        assert_target_size(confirm, "This is true")
        wait_for_forms(page, f"#card-{KIND} form[data-unit-form=confirm]")
        confirm.click()
        expect(card(page).locator('li[data-unit][data-flagged="true"]')).to_have_count(
            0, timeout=RESULT_TIMEOUT_MS
        )

        page.locator(f"#card-{KIND}-approve").click()
        expect(page.locator(f"#card-{KIND}-approved")).to_be_visible(timeout=RESULT_TIMEOUT_MS)
        wait_for_forms(page, "#card-salary-form")
        salary = page.locator("#card-salary-build")
        assert_target_size(salary, "Build salary card")
        salary.click()
        expect(card(page, "salary")).to_have_attribute(
            "data-state", "approved", timeout=RESULT_TIMEOUT_MS
        )
        wait_for_settled_styles(page)
        ok, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert ok, (scroll_width, inner_width)
        assert_no_serious_violations(page)


def test_a_card_no_fact_supports_links_to_a_session_by_keyboard(
    running_app: RunningApp, browser: Browser
):
    """With no usable confirmed fact every written card says so and offers a session."""
    aid = seeded(running_app)
    change_db(running_app, "UPDATE facts SET privacy = 'never_use'")
    with new_page(browser, DESKTOP) as page:
        open_cards(page, running_app, aid, keyboard=True)

        empty = page.locator(f"#card-{KIND}-empty")
        expect(empty).to_have_text(re.compile(r"No confirmed facts support this card yet\."))
        link = page.locator(f"#card-{KIND}-sessions")
        tab_until(page, link)
        assert link.get_attribute("href") == "/sessions/new"
        assert rows(running_app, TWO_COUNT) == [(0,)]
        assert_no_serious_violations(page)
