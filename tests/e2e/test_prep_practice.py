"""Practice feedback and adding a flagged claim, by keyboard and on a phone (M8.2 part A;
AC-M8.4; spec 3.9).

The app runs with a finished setup and the fake provider, so the questions and the feedback are
its own fictional answers. The claim flagged here is found by code from the answer, not by the
fake: the answer says 17 stores and no confirmed fact does. Browser-facing contract (see
``tests/integration/test_prep_practice.py`` for the routes):

- Each ``.prep-question`` has a link whose name starts "Practice"; it opens the practice view
  inside ``#prep`` without a reload. ``#practice-heading`` (``h3``) takes focus.
- ``textarea#practice-answer`` labelled "Your answer" and the button "Get feedback" (says
  "Feedback started." in the live region; focus returns to the heading).
- The result: five ``[data-check]`` items; each flagged claim is a ``[data-claim]`` holding the
  claim, "This isn't in your fact bank." and a checkbox labelled "This is true; add it"; the
  button "Add to my facts" says "1 draft added to your facts."; the claim then reads "Added as
  a draft".
- The ticked claims post as one ``form[data-claims-form]``; ``a#practice-back`` ("Back to
  questions") returns to the list and focuses ``#prep-heading``.
- The button "Mark ready" in the practice view says "Question marked ready." and shows the
  question under "Already prepared".

Keyboard path: Tab, Space, Enter and typing only. No fixed waits.
"""

import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
from playwright.sync_api import Browser, Page, expect

from app.domain.budgets import Budget
from app.domain.clock import SystemClock
from app.domain.errors import BudgetBlocked
from app.modules.storage import Database
from app.services.facts import FactService
from app.services.usage import UsageService
from app.wiring import local_timezone
from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_board import desk, rows
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, press_by_keyboard, tab_until, wait_said
from tests.e2e.test_shell import assert_target_size, has_no_horizontal_scroll, new_page, open_app
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory
from tests.running_app import RunningApp
from tests.tailoring_pages import seed_facts, seed_summary

pytestmark = pytest.mark.e2e

JOB_ID = "practice-e2e-job"
ANSWER = (
    "I planned 60 delivery routes a week with a team of 3. "
    "Later I managed 17 stores across the region."
)
CLAIM_SENTENCE = "Later I managed 17 stores across the region."
RESULT_TIMEOUT_MS = 20_000


def seeded(app: RunningApp) -> str:
    with desk(app) as seed:
        made = seed.application(JOB_ID, "Route Planner", "Harbor Kite Fictional", "preparing")
        seed_summary(seed.db, JOB_ID)
    with Database.open(app.data_dir / "app.db") as db:
        clock = FrozenClock(datetime(2026, 1, 5, 9, 0, tzinfo=UTC))
        seed_facts(FactService(db, clock, SequentialIdFactory("practice-fact")))
    return str(made.id)


def wait_for_forms(page: Page, selector: str) -> None:
    # A press before htmx has processed the form would submit it natively.
    page.wait_for_function(
        "(selector) => !!document.querySelector(selector)['htmx-internal-data']", arg=selector
    )


def practice_link(page: Page):
    return (
        page.locator("#prep .prep-question")
        .first.get_by_role("link", name=re.compile(r"^Practice"))
        .first
    )


def generate_questions(page: Page, app: RunningApp, aid: str, *, keyboard: bool) -> None:
    open_app(page, app, f"/applications/{aid}?tab=prep")
    wait_for_forms(page, "#prep-generate")
    if keyboard:
        press_by_keyboard(page, page.locator("#prep-generate"))
        wait_said(page, "Question generation started.")
    else:
        page.locator("#prep-generate").click()
    expect(page.locator("#prep .prep-question").first).to_be_visible(timeout=RESULT_TIMEOUT_MS)
    page.wait_for_function("() => !document.querySelector('#prep [aria-disabled=true]')")


def facts_from_practice(app: RunningApp) -> list[tuple]:
    return rows(
        app,
        "SELECT f.status, v.source, v.needs_check, v.original_answer FROM facts f "
        "JOIN fact_versions v ON v.id = f.current_version_id WHERE v.source = 'practice'",
    )


def test_ac_m8_4_practice_feedback_and_add_claim_by_keyboard(
    running_app: RunningApp, browser: Browser
):
    aid = seeded(running_app)
    with new_page(browser, DESKTOP) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        generate_questions(page, running_app, aid, keyboard=True)

        press_by_keyboard(page, practice_link(page))
        expect(page.locator("#practice-heading")).to_be_focused()
        answer = page.get_by_label("Your answer", exact=True)
        tab_until(page, answer)
        page.keyboard.type(ANSWER)
        expect(answer).to_have_value(ANSWER)
        wait_for_forms(page, "#practice-form")
        press_by_keyboard(page, page.get_by_role("button", name="Get feedback", exact=True))
        wait_said(page, "Feedback started.")
        expect(page.locator("#practice-heading")).to_be_focused()

        expect(page.locator("[data-check]")).to_have_count(5, timeout=RESULT_TIMEOUT_MS)
        claim = page.locator("[data-claim]").filter(has_text="17")
        expect(claim).to_have_count(1)
        expect(claim).to_contain_text("This isn't in your fact bank.")
        assert facts_from_practice(running_app) == [], "flagging adds nothing by itself"

        # Tick the claim and add it, with the keyboard only.
        box = claim.get_by_role("checkbox", name="This is true; add it")
        tab_until(page, box)
        page.keyboard.press("Space")
        expect(box).to_be_checked()
        wait_for_forms(page, "form[data-claims-form]")
        press_by_keyboard(page, page.get_by_role("button", name="Add to my facts", exact=True))
        wait_said(page, "1 draft added to your facts.")
        expect(page.locator("#practice-heading")).to_be_focused()
        expect(page.locator("[data-claim]").filter(has_text="17")).to_contain_text(
            "Added as a draft"
        )
        assert facts_from_practice(running_app) == [("draft", "practice", 1, CLAIM_SENTENCE)]
        assert_no_serious_violations(page)

        # Mark the question ready from the practice view.
        press_by_keyboard(page, page.get_by_role("button", name="Mark ready", exact=True))
        wait_said(page, "Question marked ready.")
        expect(page.locator("#prep")).to_contain_text("Already prepared")
        expect(
            page.locator("#prep .prep-question").filter(has_text=re.compile("· Ready"))
        ).to_have_count(1)


def test_practice_view_returns_to_the_questions_by_keyboard(
    running_app: RunningApp, browser: Browser
):
    aid = seeded(running_app)
    with new_page(browser, DESKTOP) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        generate_questions(page, running_app, aid, keyboard=True)
        press_by_keyboard(page, practice_link(page))
        expect(page.locator("#practice-heading")).to_be_focused()

        press_by_keyboard(page, page.locator("#practice-back"))

        expect(page.locator("#prep-heading")).to_be_focused()
        expect(page.locator("#prep .prep-question").first).to_be_visible()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone390x844"])
def test_practice_phone_390x844_and_axe(
    running_app: RunningApp, browser: Browser, theme: str, viewport: dict[str, int]
):
    aid = seeded(running_app)
    with new_page(browser, viewport, color_scheme=theme) as page:
        generate_questions(page, running_app, aid, keyboard=False)
        practice_link(page).click()
        expect(page.locator("#practice-heading")).to_be_visible()
        wait_for_settled_styles(page)
        ok, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert ok, (scroll_width, inner_width)
        assert_no_serious_violations(page)

        page.get_by_label("Your answer", exact=True).fill(ANSWER)
        submit = page.get_by_role("button", name="Get feedback", exact=True)
        assert_target_size(submit, "Get feedback")
        wait_for_forms(page, "#practice-form")
        submit.click()
        expect(page.locator("[data-check]")).to_have_count(5, timeout=RESULT_TIMEOUT_MS)
        claim = page.locator("[data-claim]").filter(has_text="17")
        expect(claim).to_have_count(1)
        wait_for_settled_styles(page)
        ok, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert ok, (scroll_width, inner_width)
        assert_target_size(claim.get_by_role("checkbox"), "claim checkbox")
        assert_target_size(
            page.get_by_role("button", name="Add to my facts", exact=True), "Add to my facts"
        )
        assert_no_serious_violations(page)

        claim.get_by_role("checkbox").check()
        page.get_by_role("button", name="Add to my facts", exact=True).click()
        expect(page.locator("[data-claim]").filter(has_text="17")).to_contain_text(
            "Added as a draft", timeout=RESULT_TIMEOUT_MS
        )
        wait_for_settled_styles(page)
        ok, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert ok, (scroll_width, inner_width)
        assert_no_serious_violations(page)


# --- review fixes: kept answers, blocked and failed states, counter, phone keyboard path ----------

APP_DB = "app.db"
DRAFT = "A draft I am still typing, with 12 words in it and a second sentence."
NODE_KEPT = "() => { window.__box = document.getElementById('practice-answer'); }"
NODE_REPLACED = (
    "() => document.getElementById('practice-answer') !== null"
    " && document.getElementById('practice-answer') !== window.__box"
)
TASKS_UPDATED = (
    "() => document.body.dispatchEvent(new CustomEvent('tasks:updated', {bubbles: true}))"
)
USAGE_CHANGED = (
    "() => document.body.dispatchEvent(new CustomEvent('usage:changed', {bubbles: true}))"
)
FAIL_TRIGGER = (
    "CREATE TRIGGER fictional_feedback_abort BEFORE UPDATE OF status ON tasks "
    "WHEN NEW.type='answer_feedback' AND NEW.status='succeeded' "
    "BEGIN SELECT RAISE(ABORT,'fictional failure'); END"
)


def change_db(app: RunningApp, *statements: str) -> None:
    """Run SQL on the app's database from the test (a failure to inject, a version to move)."""
    with closing(sqlite3.connect(app.data_dir / APP_DB, timeout=10)) as conn:
        for statement in statements:
            conn.execute(statement)
        conn.commit()


def open_practice(page: Page, app: RunningApp, aid: str) -> None:
    generate_questions(page, app, aid, keyboard=False)
    practice_link(page).click()
    expect(page.locator("#practice-heading")).to_be_focused()
    wait_for_forms(page, "#practice-form")


def block_fake_provider(app: RunningApp) -> None:
    """Budget the Fake provider and use it up (whatever was used before counts) until a start
    is refused."""
    db = Database.open(app.data_dir / APP_DB)
    try:
        usage = UsageService(db, SystemClock(), SequentialIdFactory("e2e-block"), local_timezone())
        small = Budget(allowance=10, first_warning=3, second_warning=6, block_at=10, reset_day=1)
        usage.save_budget("fake", small, expected_version=usage.load_budgets().version)
        with pytest.raises(BudgetBlocked):
            for _ in range(12):
                usage.complete(
                    usage.reserve("fake", tier="thorough", task_type="fictional_echo"),
                    success=True,
                    tokens=None,
                )
    finally:
        db.close()


def box(page: Page):
    return page.get_by_label("Your answer", exact=True)


def test_practice_view_starts_with_the_empty_state_and_a_zero_counter(
    running_app: RunningApp, browser: Browser
):
    aid = seeded(running_app)
    with new_page(browser, DESKTOP) as page:
        open_practice(page, running_app, aid)

        expect(page.get_by_text("No tries yet.", exact=False)).to_be_visible()
        expect(page.locator("#practice-count")).to_have_text("0 of 4,000 characters")


def test_practice_counter_counts_and_announces_politely_near_and_at_the_limit(
    running_app: RunningApp, browser: Browser
):
    aid = seeded(running_app)
    with new_page(browser, DESKTOP) as page:
        open_practice(page, running_app, aid)
        count, note = page.locator("#practice-count"), page.locator("#practice-count-note")
        expect(note).to_have_attribute("aria-live", "polite")

        box(page).fill("hello")
        expect(count).to_have_text("5 of 4,000 characters")
        expect(note).to_have_text("")

        box(page).fill("x" * 3900)
        expect(count).to_have_text("3,900 of 4,000 characters")
        expect(note).to_have_text("Fewer than 200 characters left.")

        box(page).fill("x" * 4000)
        expect(count).to_have_text("4,000 of 4,000 characters")
        expect(note).to_have_text("Limit reached: 4,000 characters.")

        box(page).fill("x" * 10)
        expect(count).to_have_text("10 of 4,000 characters")
        expect(note).to_have_text("")


def test_a_draft_in_the_box_survives_a_background_refresh(
    running_app: RunningApp, browser: Browser
):
    aid = seeded(running_app)
    with new_page(browser, DESKTOP) as page:
        open_practice(page, running_app, aid)

        box(page).fill(DRAFT)
        page.evaluate("() => document.activeElement.blur()")  # nothing is focused in the region
        page.evaluate(NODE_KEPT)
        page.evaluate(TASKS_UPDATED)
        page.wait_for_function(NODE_REPLACED)

        expect(box(page)).to_have_value(DRAFT)
        expect(page.locator("#practice-count")).to_have_text(f"{len(DRAFT)} of 4,000 characters")

        # The same for the other background refreshes and for the "Refresh practice" button.
        page.evaluate(NODE_KEPT)
        page.evaluate(USAGE_CHANGED)
        page.wait_for_function(NODE_REPLACED)
        expect(box(page)).to_have_value(DRAFT)


def test_a_failed_task_keeps_the_answer_in_the_box_and_try_again_sends_it_again(
    running_app: RunningApp, browser: Browser
):
    aid = seeded(running_app)
    with new_page(browser, DESKTOP) as page:
        open_practice(page, running_app, aid)
        change_db(running_app, FAIL_TRIGGER)

        box(page).fill(ANSWER)
        page.get_by_role("button", name="Get feedback", exact=True).click()

        expect(page.get_by_role("alert").filter(has_text="Your answer is kept")).to_be_visible(
            timeout=RESULT_TIMEOUT_MS
        )
        expect(box(page)).to_have_value(ANSWER)
        retry = page.get_by_role("button", name="Try again", exact=True)
        expect(retry).to_be_visible()

        change_db(running_app, "DROP TRIGGER fictional_feedback_abort")
        wait_for_forms(page, "#practice-form")
        retry.click()
        expect(page.locator("[data-check]")).to_have_count(5, timeout=RESULT_TIMEOUT_MS)
        expect(page.locator("[data-attempt]")).to_have_count(2)


def test_a_blocked_budget_disables_the_button_with_its_reason_and_keeps_the_answer(
    running_app: RunningApp, browser: Browser
):
    aid = seeded(running_app)
    with new_page(browser, DESKTOP) as page:
        open_practice(page, running_app, aid)
        box(page).fill(DRAFT)
        page.evaluate("() => document.activeElement.blur()")
        block_fake_provider(running_app)

        page.evaluate(USAGE_CHANGED)

        submit = page.get_by_role("button", name="Get feedback", exact=True)
        expect(submit).to_have_attribute("aria-disabled", "true", timeout=RESULT_TIMEOUT_MS)
        expect(page.locator("#practice-reason")).to_contain_text("Monthly limit reached")
        expect(submit).to_have_attribute("aria-describedby", re.compile("practice-reason"))
        expect(box(page)).to_have_value(DRAFT)
        submit.focus()  # a blocked button stays reachable by keyboard
        page.keyboard.press("Enter")
        expect(page.locator("#practice-reason")).to_be_visible()
        expect(box(page)).to_have_value(DRAFT)
        expect(page.get_by_text("No tries yet.", exact=False)).to_be_visible()


def test_a_version_conflict_keeps_the_typed_answer_and_offers_a_reload(
    running_app: RunningApp, browser: Browser
):
    aid = seeded(running_app)
    with new_page(browser, DESKTOP) as page:
        open_practice(page, running_app, aid)
        box(page).fill(ANSWER)
        page.get_by_role("button", name="Get feedback", exact=True).click()
        expect(page.locator("[data-check]")).to_have_count(5, timeout=RESULT_TIMEOUT_MS)
        change_db(running_app, "UPDATE prep_items SET version = version + 1")

        box(page).fill(DRAFT)
        wait_for_forms(page, "#practice-form")
        page.get_by_role("button", name="Get feedback", exact=True).click()

        expect(page.locator("#practice-conflict")).to_be_visible(timeout=RESULT_TIMEOUT_MS)
        expect(box(page)).to_have_value(DRAFT)
        expect(page.locator("[data-attempt]")).to_have_count(1)


def test_practice_feedback_and_add_claim_by_keyboard_on_a_phone(
    running_app: RunningApp, browser: Browser
):
    aid = seeded(running_app)
    with new_page(browser, PHONE) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        generate_questions(page, running_app, aid, keyboard=True)

        press_by_keyboard(page, practice_link(page))
        expect(page.locator("#practice-heading")).to_be_focused()
        tab_until(page, box(page))
        page.keyboard.type(ANSWER)
        expect(box(page)).to_have_value(ANSWER)
        expect(page.locator("#practice-count")).to_have_text(f"{len(ANSWER)} of 4,000 characters")
        wait_for_forms(page, "#practice-form")
        press_by_keyboard(page, page.get_by_role("button", name="Get feedback", exact=True))
        wait_said(page, "Feedback started.")

        expect(page.locator("[data-check]")).to_have_count(5, timeout=RESULT_TIMEOUT_MS)
        claim = page.locator("[data-claim]").filter(has_text="17")
        expect(claim).to_contain_text(CLAIM_SENTENCE)
        checkbox = claim.get_by_role("checkbox", name="This is true; add it")
        tab_until(page, checkbox)
        page.keyboard.press("Space")
        expect(checkbox).to_be_checked()
        wait_for_forms(page, "form[data-claims-form]")
        press_by_keyboard(page, page.get_by_role("button", name="Add to my facts", exact=True))
        wait_said(page, "1 draft added to your facts.")
        assert facts_from_practice(running_app) == [("draft", "practice", 1, CLAIM_SENTENCE)]
        ok, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert ok, (scroll_width, inner_width)
        assert_no_serious_violations(page)
