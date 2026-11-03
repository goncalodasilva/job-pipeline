"""M8.3 part C: the whole M8 journey in a browser with the real CLI and the fake AI provider, by
keyboard, at desktop and phone (390x844) width, with an axe scan in light and dark and a
sideways-scroll check on the screens named in the ``checked`` calls: the Prep tab with generated
questions, a practice answer with its feedback, a drafted narrative card, the debrief form, the
saved debrief with its draft facts and suggested status, and the company page with its brief.
Nothing here uses a real provider, the network or real data; every person, job, fact and note is
fictional.

The app starts with a completed setup (the fake provider saved) and the fictional rows of the
M8.2 practice tests written into its database (an application with a job summary, confirmed facts
F1 to F5) plus an interview that ended two hours ago on the real clock. Then, with Tab, Enter,
Space and typing only:

1. The Prep tab generates questions (AC-M8.1), announced.
2. A question is practised: a typed answer gets feedback with its five checks (AC-M8.4).
3. The Prep tab's narrative cards draft "Tell me about yourself" (AC-M8.2), as a draft to review.
4. Today offers "How did the interview go?"; its Debrief page saves the questions asked (they
   join the question bank, for this company), how it went and an experience; "Turn into draft
   facts" makes a draft fact from the experience only; the suggested status is applied only when
   confirmed (AC-M8.5).
5. The company page gets a brief, with a source link and a found date (AC-M8.3).

The details of each criterion are named integration and unit tests; this is the one scenario that
runs them together. A press waits until htmx has processed the swapped form (a press before that
submits the form natively, without the launch token: TD-320). No fixed waits.
"""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.debrief_pages import DRAFTS_STARTED, SAVED, base
from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_board import rows
from tests.e2e.test_company_brief import STARTED as BRIEF_STARTED
from tests.e2e.test_company_brief import bound as brief_button
from tests.e2e.test_debrief import ADD_EVENT, EXPERIENCE, QUESTIONS, STAMP, field, type_into
from tests.e2e.test_debrief import wait_for_forms as wait_for_debrief_forms
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, press_by_keyboard, tab_until, wait_said
from tests.e2e.test_prep_cards import KIND, card, draft_by_keyboard, open_cards
from tests.e2e.test_prep_practice import (
    ANSWER,
    RESULT_TIMEOUT_MS,
    generate_questions,
    practice_link,
    wait_for_forms,
)
from tests.e2e.test_prep_practice import seeded as practice_seeded
from tests.e2e.test_shell import assert_target_size, has_no_horizontal_scroll, new_page, open_app
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e

EVENT = "m8-journey-event"
COMPANY = "Harbor Kite Fictional"


def seeded(app: RunningApp) -> dict[str, str]:
    """The practice tests' application, facts and summary, and an interview that ended two hours
    ago. Returns the application id and the company id."""
    aid = practice_seeded(app)
    end = datetime.now(UTC) - timedelta(hours=2)
    start = end - timedelta(hours=1)
    with closing(sqlite3.connect(app.data_dir / "app.db", timeout=10)) as conn:
        conn.execute(ADD_EVENT, (EVENT, aid, start.isoformat(), end.isoformat(), STAMP, STAMP))
        conn.commit()
    company = rows(app, "SELECT id FROM companies WHERE name = ?", (COMPANY,))
    assert len(company) == 1
    return {"application": aid, "company": str(company[0][0])}


def checked(page: Page, label: str) -> None:
    """The screen fits the width and has no serious accessibility problem in light and in dark;
    the page ends in light again."""
    for scheme in ("light", "dark"):
        page.emulate_media(color_scheme=scheme)
        wait_for_settled_styles(page)
        fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert fits, f"{label} ({scheme}): scrollWidth {scroll_width} > {inner_width}"
        assert_no_serious_violations(page)
    page.emulate_media(color_scheme="light")
    wait_for_settled_styles(page)


def generate_and_practise(page: Page, app: RunningApp, aid: str) -> None:
    generate_questions(page, app, aid, keyboard=True)
    expect(page.locator("#prep-heading")).to_be_focused()
    checked(page, "the Prep tab with generated questions")

    press_by_keyboard(page, practice_link(page))
    expect(page.locator("#practice-heading")).to_be_focused()
    answer = page.get_by_label("Your answer", exact=True)
    tab_until(page, answer)  # Enter would type a newline
    page.keyboard.type(ANSWER)
    expect(answer).to_have_value(ANSWER)
    wait_for_forms(page, "#practice-form")
    submit = page.get_by_role("button", name="Get feedback", exact=True)
    assert_target_size(submit, "Get feedback")
    press_by_keyboard(page, submit)
    wait_said(page, "Feedback started.")
    expect(page.locator("#practice-heading")).to_be_focused()
    expect(page.locator("[data-check]")).to_have_count(5, timeout=RESULT_TIMEOUT_MS)
    attempts = rows(app, "SELECT answer_text FROM practice_attempts")
    assert attempts == [(ANSWER,)], "the answer is kept as typed"
    assert rows(app, "SELECT count(*) FROM facts WHERE number > 5") == [(0,)], (
        "flagging adds nothing"
    )
    checked(page, "a practice answer with feedback")


def draft_a_card(page: Page, app: RunningApp, aid: str) -> None:
    open_cards(page, app, aid, keyboard=True)
    draft_by_keyboard(page)
    expect(card(page)).to_have_attribute("data-state", "draft")
    units = card(page).locator("li[data-unit]")
    expect(units.first).to_be_visible()
    assert rows(app, "SELECT count(*) FROM prep_document_versions WHERE status = 'approved'") == [
        (0,)
    ], f"the {KIND} card is a draft until the person approves it"
    checked(page, "a drafted narrative card")


def debrief_from_today(page: Page, app: RunningApp, ids: dict[str, str]) -> None:
    open_app(page, app, "/")
    offer = page.locator(f'#today-debriefs li[data-debrief-event-id="{EVENT}"]')
    expect(offer).to_contain_text("How did the interview go?")
    checked(page, "Today with a debrief offer")
    press_by_keyboard(page, offer.get_by_role("link", name="Debrief", exact=True))
    page.wait_for_url(app.base_url + base(ids["application"], EVENT))
    expect(page.locator("#debrief-heading")).to_be_visible()
    wait_for_debrief_forms(page, "#debrief-form")
    checked(page, "the debrief form")

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
    assert_target_size(page.locator("#debrief-save"), "Save debrief")
    press_by_keyboard(page, page.locator("#debrief-save"))
    wait_said(page, SAVED)
    expect(page.locator("#debrief-heading")).to_be_focused()
    expect(page.locator("#debrief textarea")).to_have_count(0)
    asked = rows(
        app,
        "SELECT count(*) FROM bank_questions WHERE origin = 'asked' AND asked_count = 1 "
        "AND company_id IS NOT NULL",
    )
    assert asked == [(2,)], "both questions joined the bank, for this company, asked once"
    assert rows(app, "SELECT state, how_it_went FROM debriefs") == [("done", "Calm and friendly.")]
    checked(page, "a saved debrief")

    # Draft facts: explicit, from the experience only, kept as drafts to review.
    assert rows(app, "SELECT count(*) FROM fact_versions WHERE source = 'debrief'") == [(0,)]
    wait_for_debrief_forms(page, "#debrief-drafts")
    assert_target_size(page.locator("#debrief-drafts-start"), "Draft facts")
    press_by_keyboard(page, page.locator("#debrief-drafts-start"))
    wait_said(page, DRAFTS_STARTED)
    expect(page.locator("[data-debrief-draft]")).to_have_count(1, timeout=RESULT_TIMEOUT_MS)
    made = rows(
        app,
        "SELECT f.status, v.source, v.original_answer FROM facts f "
        "JOIN fact_versions v ON v.id = f.current_version_id WHERE v.source = 'debrief'",
    )
    assert made == [("draft", "debrief", EXPERIENCE)]

    # The suggested status moves only when it is confirmed.
    expect(page.locator("#debrief-status")).to_contain_text("Move to Interviewing?")
    assert rows(app, "SELECT status_key FROM applications WHERE id = ?", (ids["application"],)) == [
        ("preparing",)
    ]
    checked(page, "a debrief with draft facts and a suggested status")
    wait_for_debrief_forms(page, "#debrief-status")
    assert_target_size(page.locator("#debrief-status-confirm"), "Move the status")
    press_by_keyboard(page, page.locator("#debrief-status-confirm"))
    wait_said(page, "Moved to Interviewing.")
    assert rows(app, "SELECT status_key FROM applications WHERE id = ?", (ids["application"],)) == [
        ("interviewing",)
    ]


def company_brief(page: Page, app: RunningApp, ids: dict[str, str]) -> None:
    open_app(page, app, f"/companies/{ids['company']}")
    start = brief_button(page)
    expect(start).to_have_text("Get a company brief")
    assert_target_size(start, "Get a company brief")
    press_by_keyboard(page, start)
    wait_said(page, BRIEF_STARTED)
    expect(page.locator("#company-brief-heading")).to_be_focused()
    sections = page.locator("#company-brief [data-brief-section]")
    expect(sections.first).to_be_visible(timeout=RESULT_TIMEOUT_MS)
    expect(sections.first.get_by_role("link")).to_have_attribute(
        "rel", "noopener noreferrer nofollow"
    )
    expect(page.locator("#company-brief-found")).to_contain_text("Found ")
    page.wait_for_function("() => !document.querySelector('#company-brief [aria-disabled=true]')")
    saved = rows(app, "SELECT count(*) FROM company_briefs WHERE company_id = ?", (ids["company"],))
    assert saved == [(1,)], "the brief is kept with its sources and date"
    checked(page, "the company page with a brief")


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone-390x844"])
def test_m8_journey_generate_practice_debrief(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int]
) -> None:
    app = running_app
    ids = seeded(app)
    with new_page(browser, viewport) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        generate_and_practise(page, app, ids["application"])
        draft_a_card(page, app, ids["application"])
        debrief_from_today(page, app, ids)
        company_brief(page, app, ids)
        # Every task the journey started ended well, one of each kind it exercised.
        done = rows(app, "SELECT DISTINCT type, status FROM tasks ORDER BY type")
        assert done == [
            ("answer_feedback", "succeeded"),
            ("company_brief", "succeeded"),
            ("debrief_facts", "succeeded"),
            ("draft_narrative", "succeeded"),
            ("interview_questions", "succeeded"),
        ]
