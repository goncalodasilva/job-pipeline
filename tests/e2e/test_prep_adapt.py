"""Adapting an earlier answer in a real browser: practice an answer in one application, open the
same question in a second one, adapt, review, fix a flagged paragraph and approve, by keyboard and
on a phone (M8.2 part C; AC-M8.1 carrying answers over; spec 3.9; ADR-124 items 7 and 8).

The app runs with a finished setup and the fake provider, so the questions and the feedback are
its own fictional answers, and the adapted answer is the fake's own: one or two paragraphs, each
starting "Practice data.", each citing a confirmed fact and saying nothing those facts do not.
The claim flagged here is found by code from the person's edit, not by the fake. Browser-facing
contract (the markup hooks, ids and announcement texts are written in the docstring of
``tests/prep_adapt_pages.py``; the routes are exercised by
``tests/integration/test_prep_adapt.py``):

- The practice view of a question that was practiced in ANOTHER application shows
  ``section#adapt`` with ``h4#adapt-heading`` "From your earlier answer", the old answer in
  ``#adapt-earlier`` and ``button#adapt-submit`` "Adapt to this role". In the application where the
  answer was practiced there is no ``#adapt``.
- The button says "Adapting your earlier answer." in the live region, focus moves to
  ``#adapt-heading`` and ``#adapt`` reaches ``data-state="draft"`` without a reload.
- Editing: ``details[data-unit-edit]`` "Edit" on a paragraph, a textarea labelled "Edit this
  paragraph", the button "Save paragraph"; a paragraph that claims something no cited fact states
  says "Paragraph saved. Save this as a fact?", is ``data-flagged="true"``, disables
  ``#adapt-approve`` (``aria-disabled``) with ``#adapt-approve-reason`` "1 paragraph needs your
  check." and offers the button "This is true — save it as a fact" ("Saved as a fact. The
  paragraph now cites it."). No dialog opens.
- Approving: "Answer approved." and ``#adapt-approved`` ("Approved. Changing a paragraph starts a
  new draft."); ``#adapt-submit`` is gone.

Keyboard path: Tab, Space, Enter and typing only. No fixed waits (htmx has processed a form
before it is pressed).
"""

import re
from datetime import UTC, datetime

import pytest
from playwright.sync_api import Browser, Page, expect

from app.domain.fit import Requirement
from app.modules.storage import Database
from app.services.facts import FactService
from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_board import desk, rows
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, press_by_keyboard, tab_until, wait_said
from tests.e2e.test_prep_practice import (
    ANSWER,
    JOB_ID,
    RESULT_TIMEOUT_MS,
    generate_questions,
    practice_link,
    wait_for_forms,
)
from tests.e2e.test_shell import (
    assert_target_size,
    has_no_horizontal_scroll,
    new_page,
    open_app,
)
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory
from tests.running_app import RunningApp
from tests.tailoring_ai_inputs import REQUIREMENTS
from tests.tailoring_pages import seed_facts, seed_summary

pytestmark = pytest.mark.e2e

JOB_TWO = "adapt-e2e-job"
NEW_CLAIM = "I led 9 people and saved $2m."
ADAPT_DONE = "SELECT count(*) FROM tasks WHERE type = 'adapt_answer'"


def seeded_two(app: RunningApp) -> tuple[str, str]:
    """Two applications (the second at another company) with a summary each, and facts F1 to F5.
    Both are made by one seeder: it numbers the companies it makes."""
    other = tuple(
        Requirement(f"{JOB_TWO}-{item.id}", item.kind, item.text) for item in REQUIREMENTS
    )
    with desk(app) as seed:
        first = seed.application(JOB_ID, "Route Planner", "Harbor Kite Fictional", "preparing")
        second = seed.application(JOB_TWO, "Other Planner", "Other Fictional Co", "preparing")
        seed_summary(seed.db, JOB_ID)
        seed_summary(seed.db, JOB_TWO, other, summary_id=f"summary-{JOB_TWO}")
    with Database.open(app.data_dir / "app.db") as db:
        clock = FrozenClock(datetime(2026, 1, 5, 9, 0, tzinfo=UTC))
        seed_facts(FactService(db, clock, SequentialIdFactory("practice-fact")))
    return str(first.id), str(second.id)


def section(page: Page):
    return page.locator("#adapt")


def practice_in_first(page: Page, app: RunningApp, aid: str, *, keyboard: bool) -> str:
    """Practice the first question of the first application and return the question's text."""
    generate_questions(page, app, aid, keyboard=keyboard)
    if keyboard:
        press_by_keyboard(page, practice_link(page))
    else:
        practice_link(page).click()
    expect(page.locator("#practice-heading")).to_be_focused()
    answer = page.get_by_label("Your answer", exact=True)
    wait_for_forms(page, "#practice-form")
    if keyboard:
        tab_until(page, answer)
        page.keyboard.type(ANSWER)
        expect(answer).to_have_value(ANSWER)
        press_by_keyboard(page, page.get_by_role("button", name="Get feedback", exact=True))
        wait_said(page, "Feedback started.")
    else:
        answer.fill(ANSWER)
        page.get_by_role("button", name="Get feedback", exact=True).click()
    expect(page.locator("[data-check]")).to_have_count(5, timeout=RESULT_TIMEOUT_MS)
    # Nothing to adapt here: the only earlier answer is this application's own.
    expect(section(page)).to_have_count(0)
    found = rows(
        app,
        "SELECT q.text FROM prep_items p JOIN bank_questions q ON q.id = p.question_id "
        "JOIN practice_attempts a ON a.prep_item_id = p.id WHERE p.application_id = ?",
        (aid,),
    )
    assert len(found) == 1
    return str(found[0][0])


def open_in_second(page: Page, app: RunningApp, aid: str, text: str, *, keyboard: bool) -> None:
    """Open the practice view of the same question in the second application. The bank already
    holds the question (same requirements), so the Prep tab lists it without a generation."""
    open_app(page, app, f"/applications/{aid}?tab=prep")
    expect(page.locator("#prep .prep-question").filter(has_text=text).first).to_be_visible(
        timeout=RESULT_TIMEOUT_MS
    )
    link = (
        page.locator("#prep .prep-question")
        .filter(has_text=text)
        .first.get_by_role("link", name=re.compile(r"^Practice"))
        .first
    )
    if keyboard:
        press_by_keyboard(page, link)
    else:
        link.click()
    expect(page.locator("#practice-heading")).to_be_focused()
    expect(section(page)).to_have_attribute("data-state", "none")
    expect(page.locator("#adapt-heading")).to_have_text("From your earlier answer")
    expect(page.locator("#adapt-earlier")).to_contain_text(ANSWER)


def edit_first_by_keyboard(page: Page, text: str) -> None:
    unit = page.locator("#adapt-units li[data-unit]").first
    press_by_keyboard(page, unit.locator("details[data-unit-edit] > summary"))
    box = unit.get_by_label("Edit this paragraph", exact=True)
    tab_until(page, box)
    page.keyboard.press("ControlOrMeta+A")
    page.keyboard.type(text)
    expect(box).to_have_value(text)
    wait_for_forms(page, "#adapt form[data-unit-form=edit]")
    press_by_keyboard(
        page, unit.locator("form[data-unit-form=edit]").get_by_role("button", name="Save paragraph")
    )


def test_adapt_an_earlier_answer_review_and_approve_by_keyboard(
    running_app: RunningApp, browser: Browser
):
    first, second = seeded_two(running_app)
    with new_page(browser, DESKTOP) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        question = practice_in_first(page, running_app, first, keyboard=True)
        open_in_second(page, running_app, second, question, keyboard=True)
        assert_no_serious_violations(page)

        # Adapt: the fake provider's practice answer, every paragraph cited.
        wait_for_forms(page, "#adapt-form")
        press_by_keyboard(page, page.get_by_role("button", name="Adapt to this role", exact=True))
        wait_said(page, "Adapting your earlier answer.")
        expect(page.locator("#adapt-heading")).to_be_focused()
        expect(section(page)).to_have_attribute("data-state", "draft", timeout=RESULT_TIMEOUT_MS)
        expect(page.locator("#adapt-practice")).to_contain_text("Practice data")
        units = page.locator("#adapt-units li[data-unit]")
        expect(units.first).to_be_visible()
        assert 1 <= units.count() <= 2
        expect(units.first.locator("[data-unit-text]")).to_contain_text("Practice data.")
        expect(section(page).locator('li[data-unit][data-flagged="true"]')).to_have_count(0)
        expect(page.locator("#adapt-approve")).not_to_have_attribute("aria-disabled", "true")
        expect(page.locator("#adapt-earlier")).to_contain_text(ANSWER)
        assert rows(running_app, ADAPT_DONE) == [(1,)]
        assert_no_serious_violations(page)

        # The facts behind the paragraph are reachable by keyboard.
        disclosure = units.first.locator("details[data-unit-facts]")
        press_by_keyboard(page, disclosure.locator("summary"))
        link = disclosure.get_by_role("link").first
        tab_until(page, link)
        assert str(link.get_attribute("href")).startswith("/facts/")

        # Say something no confirmed fact states: it is flagged and blocks approval.
        edit_first_by_keyboard(page, NEW_CLAIM)
        wait_said(page, "Paragraph saved. Save this as a fact?")
        expect(page.locator("#adapt-heading")).to_be_focused()
        flagged = section(page).locator('li[data-unit][data-flagged="true"]')
        expect(flagged).to_have_count(1)
        expect(flagged).to_contain_text(NEW_CLAIM)
        expect(page.locator("dialog[open]")).to_have_count(0)
        approve = page.locator("#adapt-approve")
        expect(approve).to_have_attribute("aria-disabled", "true")
        expect(page.locator("#adapt-approve-reason")).to_have_text("1 paragraph needs your check.")
        assert rows(running_app, "SELECT count(*) FROM facts WHERE number > 5") == [(0,)]
        assert_no_serious_violations(page)

        # "This is true": the claim becomes a confirmed fact (source document) the paragraph cites.
        wait_for_forms(page, "#adapt form[data-unit-form=confirm]")
        press_by_keyboard(
            page, page.get_by_role("button", name="This is true — save it as a fact", exact=True)
        )
        wait_said(page, "Saved as a fact. The paragraph now cites it.")
        expect(section(page).locator('li[data-unit][data-flagged="true"]')).to_have_count(0)
        expect(approve).not_to_have_attribute("aria-disabled", "true")
        assert rows(
            running_app,
            "SELECT f.status, v.source, v.text FROM facts f JOIN fact_versions v "
            "ON v.fact_id = f.id WHERE f.number > 5",
        ) == [("confirmed", "document", NEW_CLAIM)]

        # Approve.
        wait_for_forms(page, "#adapt-approve-form")
        press_by_keyboard(page, approve)
        wait_said(page, "Answer approved.")
        expect(page.locator("#adapt-heading")).to_be_focused()
        expect(page.locator("#adapt-approved")).to_be_visible()
        expect(section(page)).to_have_attribute("data-state", "approved")
        expect(page.locator("#adapt-submit")).to_have_count(0)
        assert rows(
            running_app,
            "SELECT v.status FROM prep_document_versions v JOIN prep_documents d "
            "ON d.id = v.document_id WHERE d.kind = 'adapted_answer' "
            "ORDER BY v.number DESC LIMIT 1",
        ) == [("approved",)]
        assert rows(running_app, ADAPT_DONE) == [(1,)], "one request for the whole flow"
        assert_no_serious_violations(page)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone390x844"])
def test_adapt_phone_390x844_and_axe(
    running_app: RunningApp, browser: Browser, theme: str, viewport: dict[str, int]
):
    first, second = seeded_two(running_app)
    with new_page(browser, viewport, color_scheme=theme) as page:
        question = practice_in_first(page, running_app, first, keyboard=False)
        open_in_second(page, running_app, second, question, keyboard=False)
        wait_for_settled_styles(page)
        ok, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert ok, (scroll_width, inner_width)
        assert_no_serious_violations(page)

        adapt = page.get_by_role("button", name="Adapt to this role", exact=True)
        assert_target_size(adapt, "Adapt to this role")
        wait_for_forms(page, "#adapt-form")
        adapt.click()
        expect(section(page)).to_have_attribute("data-state", "draft", timeout=RESULT_TIMEOUT_MS)
        wait_for_settled_styles(page)
        ok, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert ok, (scroll_width, inner_width)
        assert_target_size(page.locator("#adapt-approve"), "Approve answer")
        assert_target_size(
            section(page).locator("details[data-unit-edit] > summary").first, "Edit paragraph"
        )
        assert_no_serious_violations(page)

        # A flagged paragraph and a disabled approve button keep the layout and the contrast.
        unit = section(page).locator("li[data-unit]").first
        unit.locator("details[data-unit-edit] > summary").click()
        unit.get_by_label("Edit this paragraph", exact=True).fill(NEW_CLAIM)
        wait_for_forms(page, "#adapt form[data-unit-form=edit]")
        unit.locator("form[data-unit-form=edit]").get_by_role(
            "button", name="Save paragraph"
        ).click()
        expect(section(page).locator('li[data-unit][data-flagged="true"]')).to_have_count(
            1, timeout=RESULT_TIMEOUT_MS
        )
        wait_for_settled_styles(page)
        ok, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert ok, (scroll_width, inner_width)
        assert_no_serious_violations(page)
        confirm = page.get_by_role("button", name="This is true — save it as a fact", exact=True)
        assert_target_size(confirm, "This is true")
        wait_for_forms(page, "#adapt form[data-unit-form=confirm]")
        confirm.click()
        expect(section(page).locator('li[data-unit][data-flagged="true"]')).to_have_count(
            0, timeout=RESULT_TIMEOUT_MS
        )

        page.locator("#adapt-approve").click()
        expect(page.locator("#adapt-approved")).to_be_visible(timeout=RESULT_TIMEOUT_MS)
        wait_for_settled_styles(page)
        ok, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert ok, (scroll_width, inner_width)
        assert_no_serious_violations(page)
