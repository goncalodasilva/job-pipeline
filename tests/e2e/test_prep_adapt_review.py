"""Adapting an earlier answer, review fixes (M8.2 part C), in a real browser: nothing the person
typed is lost by an adapt action, and every state of the section is reachable and readable.
The first journey (keyboard, phone, axe) is ``tests/e2e/test_prep_adapt.py``; the markup is pinned
by ``tests/integration/test_prep_adapt_review.py``.

- A half-typed practice answer survives Adapt, an edit, "This is true", Remove and Approve (the
  section's forms swap only ``#adapt``), and an unsaved Edit in another paragraph survives
  Remove, "This is true" and Approve.
- States: no usable fact (the reason, a disabled button, the way out), an empty edit (422), a change
  made in another window (409: reload prompt, read-only copy of what was typed), a running task
  (editing paused, the section follows it to the end), a failed task, a blocked budget and an
  answer that needs a re-check.
"""

import re
import sqlite3
from contextlib import closing

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP
from tests.e2e.test_board import rows
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, wait_said
from tests.e2e.test_prep_adapt import (
    ADAPT_DONE,
    NEW_CLAIM,
    open_in_second,
    practice_in_first,
    section,
    seeded_two,
)
from tests.e2e.test_prep_practice import (
    RESULT_TIMEOUT_MS,
    TASKS_UPDATED,
    USAGE_CHANGED,
    block_fake_provider,
    change_db,
)
from tests.e2e.test_shell import new_page
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e

TYPED = "A half-typed practice answer that must stay where it is."
OPEN_EDIT = "A paragraph I am still rewriting and have not saved."
ADAPT_FAIL_TRIGGER = (
    "CREATE TRIGGER fictional_adapt_abort BEFORE UPDATE OF status ON tasks "
    "WHEN NEW.type='adapt_answer' AND NEW.status='succeeded' "
    "BEGIN SELECT RAISE(ABORT,'fictional failure'); END"
)
# Posts a form of the page from the page itself: a second window of the same person.
POST_FROM_PAGE = """async ([selector, text]) => {
    const headers = JSON.parse(document.body.getAttribute('hx-headers'));
    const form = document.querySelector(selector);
    const body = new URLSearchParams(text === null ? {} : {text});
    await fetch(form.action, {method: 'POST', headers, body});
}"""


def insert_running_task(app: RunningApp, key: str) -> None:
    """A task of this application and question that is running, with a beat in the future so
    the watchdog leaves it alone."""
    future = "2999-01-01T00:00:00+00:00"
    with closing(sqlite3.connect(app.data_dir / "app.db", timeout=10)) as conn:
        conn.execute(
            "INSERT INTO tasks (id, type, status, dedupe_key, heartbeat_at, created_at, "
            "updated_at) VALUES ('e2e-adapt-run', 'adapt_answer', 'running', ?, ?, ?, ?)",
            (key, future, future, future),
        )
        conn.commit()


def typed_box(page: Page):
    return page.get_by_label("Your answer", exact=True)


def forms_ready(page: Page) -> None:
    """Every form of the adapt section is processed by htmx (a press before is a native post)."""
    page.wait_for_function(
        "() => [...document.querySelectorAll('#adapt form')]"
        ".every((form) => !!form['htmx-internal-data'])"
    )


def second_screen(
    page: Page, app: RunningApp, *, practice_text: str | None = TYPED
) -> tuple[str, str]:
    """Practice in the first application, open the same question in the second and (when
    ``practice_text``) leave a half-typed answer in its practice box. Returns the second
    application's id and the question's text."""
    first, second = seeded_two(app)
    question = practice_in_first(page, app, first, keyboard=False)
    open_in_second(page, app, second, question, keyboard=False)
    if practice_text is not None:
        typed_box(page).fill(practice_text)
    return second, question


def adapt_now(page: Page) -> None:
    forms_ready(page)
    page.get_by_role("button", name="Adapt to this role", exact=True).click()
    expect(section(page)).to_have_attribute("data-state", "draft", timeout=RESULT_TIMEOUT_MS)
    expect(page.locator("#adapt-heading")).to_be_focused()


def unit_at(page: Page, index: int):
    return page.locator("#adapt-units li[data-unit]").nth(index)


def save_edit(page: Page, index: int, text: str) -> None:
    unit = unit_at(page, index)
    unit.locator("details[data-unit-edit] > summary").click()
    unit.get_by_label("Edit this paragraph", exact=True).fill(text)
    forms_ready(page)
    unit.get_by_role("button", name="Save paragraph").click()


def test_a_half_typed_practice_answer_survives_every_adapt_action(
    running_app: RunningApp, browser: Browser
):
    with new_page(browser, DESKTOP) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        second_screen(page, running_app)

        adapt_now(page)
        expect(typed_box(page)).to_have_value(TYPED)

        save_edit(page, 0, NEW_CLAIM)
        wait_said(page, "Paragraph saved. Save this as a fact?")
        expect(page.locator("#adapt-heading")).to_be_focused()
        expect(typed_box(page)).to_have_value(TYPED)

        forms_ready(page)
        page.get_by_role("button", name="This is true — save it as a fact", exact=True).click()
        wait_said(page, "Saved as a fact. The paragraph now cites it.")
        expect(page.locator("#adapt-heading")).to_be_focused()
        expect(typed_box(page)).to_have_value(TYPED)

        unit_at(page, 1).get_by_role("button", name="Remove").click()
        wait_said(page, "Paragraph removed.")
        expect(page.locator("#adapt-heading")).to_be_focused()
        expect(typed_box(page)).to_have_value(TYPED)

        forms_ready(page)
        page.locator("#adapt-approve").click()
        wait_said(page, "Answer approved.")
        expect(page.locator("#adapt-heading")).to_be_focused()
        expect(typed_box(page)).to_have_value(TYPED)
        expect(page.locator("#practice-count")).to_have_text(f"{len(TYPED)} of 4,000 characters")


def test_an_open_edit_in_another_paragraph_survives_remove_confirm_and_approve(
    running_app: RunningApp, browser: Browser
):
    with new_page(browser, DESKTOP) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        second_screen(page, running_app, practice_text=None)
        adapt_now(page)
        other = unit_at(page, 1).locator("[data-unit-text]").inner_text()

        def keep_open_edit() -> None:
            unit = page.locator("#adapt-units li[data-unit]").filter(has_text=other)
            expect(unit.locator("details[data-unit-edit]")).to_have_attribute("open", "")
            expect(unit.get_by_label("Edit this paragraph", exact=True)).to_have_value(OPEN_EDIT)

        # A flagged paragraph elsewhere, then "This is true" on it with the edit still open.
        save_edit(page, 0, NEW_CLAIM)
        wait_said(page, "Paragraph saved. Save this as a fact?")
        unit = unit_at(page, 1)
        unit.locator("details[data-unit-edit] > summary").click()
        unit.get_by_label("Edit this paragraph", exact=True).fill(OPEN_EDIT)
        forms_ready(page)
        page.get_by_role("button", name="This is true — save it as a fact", exact=True).click()
        wait_said(page, "Saved as a fact. The paragraph now cites it.")
        expect(page.locator("#adapt-heading")).to_be_focused()
        keep_open_edit()

        # Remove the other paragraph: the open edit is still there.
        forms_ready(page)
        unit_at(page, 0).get_by_role("button", name="Remove").click()
        wait_said(page, "Paragraph removed.")
        expect(page.locator("#adapt-units li[data-unit]")).to_have_count(1)
        keep_open_edit()

        # Approving with an edit open keeps it too; saving it afterwards still works.
        forms_ready(page)
        page.locator("#adapt-approve").click()
        wait_said(page, "Answer approved.")
        keep_open_edit()
        forms_ready(page)
        unit_at(page, 0).get_by_role("button", name="Save paragraph").click()
        wait_said(page, "Saved as a new draft. The approved version is unchanged.")


def test_with_no_usable_fact_the_reason_and_the_way_out_are_shown(
    running_app: RunningApp, browser: Browser
):
    with new_page(browser, DESKTOP) as page:
        first, second = seeded_two(running_app)
        question = practice_in_first(page, running_app, first, keyboard=False)
        change_db(running_app, "UPDATE facts SET status = 'archived'")
        open_in_second(page, running_app, second, question, keyboard=False)

        expect(page.locator("#adapt-empty")).to_have_text(
            "No confirmed facts to adapt it with yet."
        )
        unavailable = page.locator("#adapt-unavailable")
        expect(unavailable).to_have_attribute("aria-disabled", "true")
        expect(unavailable).to_have_attribute("aria-describedby", "adapt-empty")
        expect(section(page).locator("#adapt-form")).to_have_count(0)
        expect(page.locator("#adapt-sessions")).to_have_attribute("href", "/sessions/new")
        unavailable.focus()
        page.keyboard.press("Enter")
        assert rows(running_app, ADAPT_DONE) == [(0,)]
        assert_no_serious_violations(page)


def test_an_empty_edit_is_refused_in_place_and_fixing_it_works(
    running_app: RunningApp, browser: Browser
):
    with new_page(browser, DESKTOP) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        second_screen(page, running_app)
        adapt_now(page)

        save_edit(page, 0, "   ")

        unit = unit_at(page, 0)
        alert = unit.get_by_role("alert").filter(has_text="Write the paragraph before saving.")
        expect(alert).to_be_visible(timeout=RESULT_TIMEOUT_MS)
        box = unit.get_by_label("Edit this paragraph", exact=True)
        expect(box).to_have_attribute("aria-invalid", "true")
        expect(box).to_be_focused()
        expect(typed_box(page)).to_have_value(TYPED)
        assert_no_serious_violations(page)
        box.fill("I planned 60 delivery routes a week.")
        forms_ready(page)
        unit.get_by_role("button", name="Save paragraph").click()
        wait_said(page, "Paragraph saved.")
        expect(section(page).get_by_role("alert")).to_have_count(0)


def test_a_change_made_in_another_window_gives_a_reload_prompt_and_keeps_what_was_typed(
    running_app: RunningApp, browser: Browser
):
    with new_page(browser, DESKTOP) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        second_screen(page, running_app)
        adapt_now(page)

        # Another window removes a paragraph: this page now holds a stale version.
        page.evaluate(POST_FROM_PAGE, ["#adapt form[data-unit-form=remove]", None])
        forms_ready(page)
        page.get_by_role("button", name="Adapt again", exact=True).click()
        prompt = page.locator("#adapt-conflict")
        expect(prompt).to_be_visible(timeout=RESULT_TIMEOUT_MS)
        expect(prompt).to_contain_text("Nothing was started.")
        expect(prompt).not_to_contain_text("Your change was not saved")
        expect(typed_box(page)).to_have_value(TYPED)
        assert rows(running_app, ADAPT_DONE) == [(1,)]

        # A second window edits once more; this page then saves an edit of the old version:
        # what it typed is kept in a read-only box.
        page.evaluate(
            POST_FROM_PAGE, ["#adapt form[data-unit-form=edit]", "Edited in the other window."]
        )
        save_edit(page, 0, OPEN_EDIT)
        expect(page.locator("#adapt-conflict")).to_contain_text("Your change was not saved.")
        recovered = page.locator("#adapt-recovered")
        expect(recovered).to_have_value(OPEN_EDIT)
        expect(recovered).to_have_attribute("readonly", "")
        expect(typed_box(page)).to_have_value(TYPED)
        assert_no_serious_violations(page)


def test_a_running_adaptation_pauses_editing_and_the_section_follows_it_to_the_end(
    running_app: RunningApp, browser: Browser
):
    with new_page(browser, DESKTOP) as page:
        second, text = second_screen(page, running_app)
        (question,) = rows(running_app, "SELECT id FROM bank_questions WHERE text = ?", (text,))
        key = f"adapt_answer:{second}/{question[0]}"
        insert_running_task(running_app, key)
        typed_box(page).blur()

        page.evaluate(TASKS_UPDATED)

        running = page.locator("#adapt-running")
        expect(running).to_contain_text("Editing is paused", timeout=RESULT_TIMEOUT_MS)
        expect(section(page)).to_have_attribute("data-state", "running")
        submit = page.locator("#adapt-submit")
        expect(submit).to_have_attribute("aria-disabled", "true")
        expect(submit).to_have_attribute("aria-describedby", re.compile("adapt-running"))
        expect(typed_box(page)).to_have_value(TYPED)
        assert_no_serious_violations(page)

        # The task ends: the section notices by itself (its own poll); the typing is untouched.
        typed_box(page).focus()
        change_db(running_app, "DELETE FROM tasks WHERE id = 'e2e-adapt-run'")
        expect(running).to_have_count(0, timeout=RESULT_TIMEOUT_MS)
        expect(page.locator("#adapt-submit")).not_to_have_attribute("aria-disabled", "true")
        expect(typed_box(page)).to_have_value(TYPED)
        expect(typed_box(page)).to_be_focused()


def test_a_failed_adaptation_alerts_and_can_be_started_again(
    running_app: RunningApp, browser: Browser
):
    with new_page(browser, DESKTOP) as page:
        second_screen(page, running_app)
        change_db(running_app, ADAPT_FAIL_TRIGGER)
        forms_ready(page)

        page.get_by_role("button", name="Adapt to this role", exact=True).click()

        failed = page.locator("#adapt-error")
        expect(failed).to_be_visible(timeout=RESULT_TIMEOUT_MS)
        expect(failed).to_have_attribute("role", "alert")
        expect(failed).to_contain_text("Try adapting again.")
        expect(section(page)).to_have_attribute("data-state", "failed")
        expect(typed_box(page)).to_have_value(TYPED)
        assert_no_serious_violations(page)

        change_db(running_app, "DROP TRIGGER fictional_adapt_abort")
        forms_ready(page)
        page.get_by_role("button", name="Adapt to this role", exact=True).click()
        expect(section(page)).to_have_attribute("data-state", "draft", timeout=RESULT_TIMEOUT_MS)
        expect(typed_box(page)).to_have_value(TYPED)


def test_a_blocked_budget_disables_adapt_with_its_reason_and_starts_nothing(
    running_app: RunningApp, browser: Browser
):
    with new_page(browser, DESKTOP) as page:
        second_screen(page, running_app)
        typed_box(page).blur()
        block_fake_provider(running_app)

        page.evaluate(USAGE_CHANGED)

        submit = page.locator("#adapt-submit")
        expect(submit).to_have_attribute("aria-disabled", "true", timeout=RESULT_TIMEOUT_MS)
        expect(page.locator("#adapt-reason")).to_contain_text("Monthly limit reached")
        expect(submit).to_have_attribute("aria-describedby", re.compile("adapt-reason"))
        expect(typed_box(page)).to_have_value(TYPED)
        submit.focus()  # reachable by keyboard; pressing it does nothing
        page.keyboard.press("Enter")
        expect(page.locator("#adapt-reason")).to_be_visible()
        assert rows(running_app, ADAPT_DONE) == [(0,)]
        assert_no_serious_violations(page)


def test_an_answer_that_needs_a_recheck_says_what_to_do(running_app: RunningApp, browser: Browser):
    with new_page(browser, DESKTOP) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        second_screen(page, running_app, practice_text=None)
        adapt_now(page)
        forms_ready(page)
        page.locator("#adapt-approve").click()
        wait_said(page, "Answer approved.")
        change_db(running_app, "UPDATE facts SET privacy = 'never_use' WHERE number = 1")

        page.evaluate("() => document.activeElement.blur()")
        page.evaluate(TASKS_UPDATED)

        recheck = page.locator("#adapt-recheck")
        expect(recheck).to_be_visible(timeout=RESULT_TIMEOUT_MS)
        expect(recheck).to_have_text(
            "A fact this answer cites changed. Edit, remove or confirm the flagged paragraphs: "
            "that saves a new draft, and you approve it again. You can also adapt again."
        )
        expect(section(page)).to_have_attribute("data-state", "recheck")
        expect(page.get_by_role("button", name="Adapt again", exact=True)).to_be_visible()
        assert_no_serious_violations(page)
