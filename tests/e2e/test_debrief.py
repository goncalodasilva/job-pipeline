"""The debrief, from Today to a confirmed status move, by keyboard and on a phone (M8.3 part A;
AC-M8.5; spec 3.9; design "Debrief").

The app runs with a finished setup and the fake provider, so the draft facts are its own
fictional answer. Two fictional applications (both Applied) each have an interview that ended
two hours ago on the real clock, which the app runs on. Browser-facing contract (the markup,
routes and announcements are in ``tests/debrief_pages.py``; the behaviour in
``tests/integration/test_debrief.py``):

- Today shows ``#today-debriefs`` with one ``li[data-debrief-event-id]`` per offer: "How did
  the interview go?", a link "Debrief" and a button "Not needed" (the button announces "Debrief
  marked not needed." and the Today heading takes focus).
- The debrief page has ``form#debrief-form`` with the labelled controls of ``LABELS``, and the
  button "Save debrief" (announces "Debrief saved."; ``#debrief-heading`` takes focus). After
  that the texts are read-only, "Turn into draft facts" starts the drafts ("Draft facts
  started."), each draft is a ``[data-debrief-draft]``, and "Move to Interviewing?" shows
  ``#debrief-status-confirm`` ("Moved to Interviewing." when pressed).

Keyboard path: Tab, Enter, Space and typing only. No fixed waits: a press waits until htmx has
processed the swapped form (a press before that submits the form natively, TD-320).
"""

import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import Browser, Page, expect

from app.domain.budgets import Budget
from app.domain.clock import SystemClock
from app.modules.storage import Database
from app.services.usage import UsageService
from app.wiring import local_timezone
from tests.debrief_pages import DISMISSED, DRAFTS_STARTED, LABELS, SAVED, base
from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_board import desk, rows
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, press_by_keyboard, tab_until, wait_said
from tests.e2e.test_prep_practice import block_fake_provider, change_db
from tests.e2e.test_shell import has_no_horizontal_scroll, new_page, open_app
from tests.fakes.ids import SequentialIdFactory
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e

STAMP = "2026-01-05T09:00:00+00:00"
EVENT_A = "debrief-e2e-event-a"
EVENT_B = "debrief-e2e-event-b"
ADD_EVENT = (
    "INSERT INTO events (id, application_id, type, starts_at, ends_at, all_day, time_zone, "
    "thank_you_reminder, status, created_at, updated_at) "
    "VALUES (?, ?, 'interview', ?, ?, 0, 'UTC', 0, 'scheduled', ?, ?)"
)
QUESTIONS = "Walk me through a route you planned.\nHow do you handle a late delivery?"
EXPERIENCE = "I introduced a weekly review of late routes with my team of 3."
RESULT_TIMEOUT_MS = 20_000


def seeded(app: RunningApp) -> dict[str, str]:
    """Application ids by name; each has an interview that ended two hours ago."""
    with desk(app) as seed:
        made = {
            "a": seed.application("debrief-e2e-1", "Seed Buyer", "Fictional Orchards", "applied"),
            "b": seed.application(
                "debrief-e2e-2", "Frost Watcher", "Fictional Orchards", "applied"
            ),
        }
    end = datetime.now(UTC) - timedelta(hours=2)
    start = end - timedelta(hours=1)
    with closing(sqlite3.connect(app.data_dir / "app.db", timeout=10)) as conn:
        for name, event in (("a", EVENT_A), ("b", EVENT_B)):
            conn.execute(
                ADD_EVENT,
                (event, made[name].id, start.isoformat(), end.isoformat(), STAMP, STAMP),
            )
        conn.commit()
    return {name: str(application.id) for name, application in made.items()}


def wait_for_forms(page: Page, selector: str) -> None:
    # A press before htmx has processed the form would submit it natively. The page first holds
    # only a placeholder that loads the region, so the form may not be there yet.
    page.wait_for_function(
        "(selector) => { const form = document.querySelector(selector);"
        " return !!form && !!form['htmx-internal-data']; }",
        arg=selector,
    )


def field(page: Page, name: str):
    return page.get_by_label(LABELS[name], exact=True)


def type_into(page: Page, name: str, text: str) -> None:
    box = field(page, name)
    tab_until(page, box)
    page.keyboard.type(text)
    expect(box).to_have_value(text)


def debrief_facts(app: RunningApp) -> list[tuple]:
    return rows(
        app,
        "SELECT f.status, v.source, v.needs_check, v.original_answer FROM facts f "
        "JOIN fact_versions v ON v.id = f.current_version_id WHERE v.source = 'debrief'",
    )


def test_ac_m8_5_debrief_from_today_by_keyboard(running_app: RunningApp, browser: Browser):
    ids = seeded(running_app)
    with new_page(browser, DESKTOP) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        open_app(page, running_app)
        offers = page.locator("#today-debriefs li[data-debrief-event-id]")
        expect(offers).to_have_count(2)
        offer_a = page.locator(f'#today-debriefs li[data-debrief-event-id="{EVENT_A}"]')
        expect(offer_a).to_contain_text("How did the interview go?")

        # Offer -> page, by keyboard.
        press_by_keyboard(page, offer_a.get_by_role("link", name="Debrief", exact=True))
        page.wait_for_url(running_app.base_url + base(ids["a"], EVENT_A))
        expect(page.locator("#debrief-heading")).to_be_visible()
        wait_for_forms(page, "#debrief-form")

        # Fill the form by keyboard: questions, the company box (Space), how it went, the
        # experience, what happens next (type-ahead on the select).
        type_into(page, "questions", QUESTIONS)
        company_only = field(page, "company_only")
        tab_until(page, company_only)
        page.keyboard.press("Space")
        expect(company_only).to_be_checked()
        type_into(page, "how_it_went", "Calm and friendly.")
        type_into(page, "experiences", EXPERIENCE)
        step = field(page, "next_step")
        tab_until(page, step)
        page.keyboard.type("Next")
        expect(step).to_have_value("next_round")
        press_by_keyboard(page, page.locator("#debrief-save"))
        wait_said(page, SAVED)
        expect(page.locator("#debrief-heading")).to_be_focused()
        expect(page.locator("#debrief textarea")).to_have_count(0)
        expect(page.locator('[data-debrief-text="questions"]')).to_contain_text("late delivery")
        company = rows(
            running_app,
            "SELECT count(*) FROM bank_questions WHERE origin = 'asked' AND asked_count = 1 "
            "AND company_id IS NOT NULL",
        )
        assert company == [(2,)], "both questions are for this company, asked once"
        saved = rows(running_app, "SELECT state, next_step, status_confirmed FROM debriefs")
        assert saved == [("done", "next_round", 0)]

        # Draft facts: explicit, one request per paragraph, kept as drafts.
        expect(page.locator("#debrief-drafts-start")).to_be_visible()
        assert debrief_facts(running_app) == []
        wait_for_forms(page, "#debrief-drafts")
        press_by_keyboard(page, page.locator("#debrief-drafts-start"))
        wait_said(page, DRAFTS_STARTED)
        expect(page.locator("[data-debrief-draft]")).to_have_count(1, timeout=RESULT_TIMEOUT_MS)
        expect(page.locator("#debrief-drafts")).to_have_count(0)
        [draft] = debrief_facts(running_app)
        assert draft[0] == "draft" and draft[1] == "debrief" and draft[3] == EXPERIENCE

        # The suggested status is only applied when it is confirmed.
        status = page.locator("#debrief-status")
        expect(status).to_contain_text("Move to Interviewing?")
        assert rows(
            running_app, "SELECT status_key FROM applications WHERE id = ?", (ids["a"],)
        ) == [("applied",)]
        wait_for_forms(page, "#debrief-status")
        press_by_keyboard(page, page.locator("#debrief-status-confirm"))
        wait_said(page, "Moved to Interviewing.")
        expect(page.locator("#debrief-status")).to_have_count(0)
        assert rows(
            running_app, "SELECT status_key FROM applications WHERE id = ?", (ids["a"],)
        ) == [("interviewing",)]
        last = rows(
            running_app,
            "SELECT from_status, to_status, source FROM status_history WHERE application_id = ? "
            "ORDER BY changed_at DESC, id DESC LIMIT 1",
            (ids["a"],),
        )
        assert last == [("applied", "interviewing", "drawer")]
        assert rows(running_app, "SELECT status_confirmed FROM debriefs") == [(1,)]

        # Back on Today the finished one is gone; "Not needed" dismisses the other.
        open_app(page, running_app)
        expect(page.locator(f'li[data-debrief-event-id="{EVENT_A}"]')).to_have_count(0)
        offer_b = page.locator(f'#today-debriefs li[data-debrief-event-id="{EVENT_B}"]')
        expect(offer_b).to_be_visible()
        wait_for_forms(page, f'li[data-debrief-event-id="{EVENT_B}"] form')
        press_by_keyboard(page, offer_b.get_by_role("button", name="Not needed", exact=True))
        wait_said(page, DISMISSED)
        expect(page.locator("#page-heading")).to_be_focused()
        expect(page.locator("#today-debriefs")).to_have_count(0)
        states = rows(running_app, "SELECT event_id, state FROM debriefs ORDER BY event_id")
        assert states == [(EVENT_A, "done"), (EVENT_B, "dismissed")]
        assert rows(
            running_app, "SELECT status_key FROM applications WHERE id = ?", (ids["b"],)
        ) == [("applied",)]


def settled_checks(page: Page) -> None:
    wait_for_settled_styles(page)
    ok, scroll_width, inner_width = has_no_horizontal_scroll(page)
    assert ok, (scroll_width, inner_width)
    assert_no_serious_violations(page)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone390x844"])
def test_debrief_today_and_page_pass_axe_and_fit_the_screen(
    running_app: RunningApp, browser: Browser, theme: str, viewport: dict[str, int]
):
    ids = seeded(running_app)
    with new_page(browser, viewport, color_scheme=theme) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        open_app(page, running_app)
        expect(page.locator("#today-debriefs")).to_be_visible()
        expect(page.get_by_role("button", name="Not needed").first).to_be_visible()
        settled_checks(page)

        open_app(page, running_app, base(ids["a"], EVENT_A))
        expect(page.locator("#debrief-form")).to_be_visible()
        expect(page.locator("#debrief")).to_contain_text("join your question bank")
        expect(page.locator("#debrief")).to_contain_text("stays on this computer")
        settled_checks(page)

        wait_for_forms(page, "#debrief-form")
        field(page, "questions").fill(QUESTIONS)
        field(page, "how_it_went").fill("Calm and friendly.")
        field(page, "experiences").fill(EXPERIENCE)
        field(page, "next_step").select_option("offer")
        page.locator("#debrief-save").click()
        wait_said(page, SAVED)
        expect(page.locator("#debrief-status")).to_contain_text("Move to Offer?")
        settled_checks(page)

        wait_for_forms(page, "#debrief-drafts")
        page.locator("#debrief-drafts-start").click()
        expect(page.locator("[data-debrief-draft]")).to_have_count(1, timeout=RESULT_TIMEOUT_MS)
        settled_checks(page)
        expect(page.locator("#debrief-status-confirm")).to_have_text(re.compile(r"^Move to Offer"))


# --- review fixes: refusals, the reload prompt, the budget, a failed task, Not needed -------------

FAIL_DEBRIEF = (
    "CREATE TRIGGER fictional_debrief_abort BEFORE UPDATE OF status ON tasks "
    "WHEN NEW.type='debrief_facts' AND NEW.status='succeeded' "
    "BEGIN SELECT RAISE(ABORT,'fictional failure'); END"
)
FOUR_PARAGRAPHS = "One.\n\nTwo.\n\nThree.\n\nFour."


def use_fake_provider(app: RunningApp, uses: int) -> None:
    """A budget of 10 requests for the Fake provider (warnings at 3 and 6) with ``uses`` made."""
    db = Database.open(app.data_dir / "app.db")
    try:
        usage = UsageService(db, SystemClock(), SequentialIdFactory("e2e-use"), local_timezone())
        small = Budget(allowance=10, first_warning=3, second_warning=6, block_at=10, reset_day=1)
        usage.save_budget("fake", small, expected_version=usage.load_budgets().version)
        for _ in range(uses):
            usage.complete(
                usage.reserve("fake", tier="thorough", task_type="fictional_echo"),
                success=True,
                tokens=None,
            )
    finally:
        db.close()


def open_debrief(page: Page, app: RunningApp, ids: dict[str, str]) -> None:
    open_app(page, app, base(ids["a"], EVENT_A))
    expect(page.locator("#debrief-form")).to_be_visible()  # the page loads it on load
    wait_for_forms(page, "#debrief-form")


def saved_with_experience(page: Page, app: RunningApp, ids: dict[str, str]) -> None:
    open_debrief(page, app, ids)
    field(page, "experiences").fill(EXPERIENCE)
    page.locator("#debrief-save").click()
    wait_said(page, SAVED)
    expect(page.locator("#debrief-message")).to_have_text(SAVED)


def test_a_refused_save_keeps_the_typed_text_and_focuses_the_first_invalid_field(
    running_app: RunningApp, browser: Browser
):
    ids = seeded(running_app)
    with new_page(browser, DESKTOP) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        open_debrief(page, running_app, ids)
        field(page, "questions").fill(QUESTIONS)
        field(page, "experiences").fill(FOUR_PARAGRAPHS)

        page.locator("#debrief-save").click()

        errors = page.locator("#debrief-errors")
        expect(errors).to_be_visible()
        expect(errors).to_contain_text("Nothing was saved.")
        expect(field(page, "experiences")).to_be_focused()
        expect(field(page, "experiences")).to_have_value(FOUR_PARAGRAPHS)
        expect(field(page, "questions")).to_have_value(QUESTIONS)
        expect(field(page, "experiences")).to_have_attribute("aria-invalid", "true")
        assert rows(running_app, "SELECT count(*) FROM debriefs") == [(0,)]
        errors.get_by_role("link", name="Experiences you mentioned").click()
        expect(field(page, "experiences")).to_be_focused()


def test_a_debrief_saved_elsewhere_offers_a_reload_and_keeps_what_was_typed(
    running_app: RunningApp, browser: Browser
):
    ids = seeded(running_app)
    with new_page(browser, DESKTOP) as page, new_page(browser, DESKTOP) as other:
        open_debrief(page, running_app, ids)
        open_debrief(other, running_app, ids)
        field(other, "how_it_went").fill("Saved in the other window.")
        other.locator("#debrief-save").click()
        expect(other.locator("#debrief-message")).to_have_text(SAVED)

        field(page, "how_it_went").fill("Typed in the first window.")
        page.locator("#debrief-save").click()

        prompt = page.locator("#debrief-conflict")
        expect(prompt).to_be_visible()
        expect(prompt).to_be_focused()
        expect(prompt).to_contain_text("not saved")
        expect(page.locator("#debrief-unsaved")).to_contain_text("Typed in the first window.")
        assert rows(running_app, "SELECT how_it_went FROM debriefs") == [
            ("Saved in the other window.",)
        ]
        prompt.get_by_role("link", name="Reload the page").click()
        expect(page.locator('[data-debrief-text="how_it_went"]')).to_have_text(
            "Saved in the other window."
        )


def test_a_blocked_budget_shows_its_reason_and_starts_nothing(
    running_app: RunningApp, browser: Browser
):
    ids = seeded(running_app)
    with new_page(browser, DESKTOP) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        saved_with_experience(page, running_app, ids)
        block_fake_provider(running_app)
        page.reload()
        start = page.locator("#debrief-drafts-start")
        expect(start).to_be_visible()
        wait_for_forms(page, "#debrief-drafts")

        expect(start).to_have_attribute("aria-disabled", "true")
        expect(start).to_have_attribute("aria-describedby", re.compile("debrief-reason"))
        expect(page.locator("#debrief-reason")).to_contain_text("Monthly limit reached")
        start.focus()
        page.keyboard.press("Enter")

        expect(page.locator("#debrief-reason")).to_be_visible()
        assert rows(running_app, "SELECT count(*) FROM tasks WHERE type = 'debrief_facts'") == [
            (0,)
        ]


def test_a_budget_warning_asks_first_and_not_now_starts_nothing(
    running_app: RunningApp, browser: Browser
):
    ids = seeded(running_app)
    with new_page(browser, DESKTOP) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        saved_with_experience(page, running_app, ids)
        use_fake_provider(running_app, 4)
        page.reload()
        expect(page.locator("#debrief-drafts-start")).to_be_visible()
        wait_for_forms(page, "#debrief-drafts")

        page.locator("#debrief-drafts-start").click()

        dialog = page.get_by_role("dialog")
        expect(dialog).to_be_visible()
        expect(dialog.get_by_role("button", name="Not now", exact=True)).to_be_focused()
        dialog.get_by_role("button", name="Not now", exact=True).click()
        expect(dialog).to_have_count(0)
        expect(page.locator("#debrief-drafts-start")).to_be_focused()
        assert rows(running_app, "SELECT count(*) FROM tasks WHERE type = 'debrief_facts'") == [
            (0,)
        ]


def test_a_failed_task_shows_an_alert_and_try_again_works(
    running_app: RunningApp, browser: Browser
):
    ids = seeded(running_app)
    with new_page(browser, DESKTOP) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        saved_with_experience(page, running_app, ids)
        change_db(running_app, FAIL_DEBRIEF)
        wait_for_forms(page, "#debrief-drafts")

        page.locator("#debrief-drafts-start").click()

        alert = page.locator("#debrief-task-error")
        expect(alert).to_be_visible(timeout=RESULT_TIMEOUT_MS)
        expect(alert).to_contain_text("Nothing was saved. You can try again.")
        retry = page.get_by_role("button", name="Try again", exact=True)
        expect(retry).to_be_visible()
        assert debrief_facts(running_app) == []

        change_db(running_app, "DROP TRIGGER fictional_debrief_abort")
        wait_for_forms(page, "#debrief-drafts")
        retry.click()

        expect(page.locator("[data-debrief-draft]")).to_have_count(1, timeout=RESULT_TIMEOUT_MS)
        assert len(debrief_facts(running_app)) == 1


def test_not_needed_from_the_page_marks_the_debrief_and_says_so(
    running_app: RunningApp, browser: Browser
):
    ids = seeded(running_app)
    with new_page(browser, DESKTOP) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        open_debrief(page, running_app, ids)
        wait_for_forms(page, "#debrief-dismiss")
        expect(page.locator("#debrief-optional")).to_contain_text("Optional")

        press_by_keyboard(page, page.get_by_role("button", name="Not needed", exact=True))

        wait_said(page, DISMISSED)
        expect(page.locator("#debrief-message")).to_have_text(DISMISSED)
        expect(page.locator("#debrief-dismissed")).to_be_visible()
        expect(page.locator("#debrief-heading")).to_be_focused()
        assert rows(running_app, "SELECT event_id, state FROM debriefs") == [(EVENT_A, "dismissed")]
