"""M7.10: the whole M7 journey in a browser with the real CLI and the fake AI provider, by keyboard,
at desktop and phone (390x844) width, with an axe scan in light and dark and a sideways-scroll check
on the screens named in the ``checked`` calls: Today with a suggestion, a question, an answered
question, the end-of-session summary, the Coverage tab and the gap check's offer. Nothing here uses
a real provider, the network or real data; every person, job, fact and note is fictional.

The app starts with a completed setup (the fake provider saved) and the fictional rows of the M7.7
(Coverage) and M7.9 (entry points) end-to-end tests written into its database: saved jobs that ask
for the same eight things, an application with a summary, an upcoming interview, confirmed facts
(one tagged Terraform, a story with no result), a draft and a capture note. Then, with Tab, Enter
and typing only:

1. Today shows "Suggested session: 3 questions, about 5 minutes" (AC-M7.6). Start opens a session;
   Today then offers to continue it instead.
2. The session: an answer (a draft appears and the original is kept, AC-M7.3), Next, Skip, Next,
   "I don't remember" (AC-M7.4: the item is rescheduled), End session. The summary lists the draft,
   which is confirmed. Today no longer suggests a session (starting reset the window).
3. Fact bank > Coverage lists the clusters, None before Thin (AC-M7.5).
4. Start session on a row opens a session focused on that cluster (AC-M7.5); it is ended.
5. The entry points (AC-M7.1 capture notes; scope "entry points"): the gap check's "Answer a few
   questions about these gaps", the drawer's "Talk through this interview in a session" and the
   capture inbox's "Expand in a session", each opening a session focused on its own item.

The details of each criterion (backlog order, cooldown arithmetic, refusals, escaping) are named
integration and unit tests; this is the one scenario that runs them together. No fixed waits.
"""

import re
from datetime import UTC, datetime

import pytest
from playwright.sync_api import Page, expect

from app.domain.facts import FactType
from app.modules.storage import capture_repo
from app.services.facts import FactInput, FactService
from tests.coverage_pages import ORDER, ROUTES, TERRAFORM, seed_coverage
from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_board import desk, open_board, rows
from tests.e2e.test_coverage_tab import start_button as row_start_button
from tests.e2e.test_coverage_tab import table_row
from tests.e2e.test_entry_points import (
    JOB_ID,
    NOTE_ID,
    end_session,
    offer_button,
    region,
    run_gap_check,
    session_rows,
)
from tests.e2e.test_facts_page import (
    RECORD_ANNOUNCEMENTS,
    press_by_keyboard,
    tab_link,
    tab_until,
    wait_said,
)
from tests.e2e.test_sessions_screen import press
from tests.e2e.test_shell import assert_target_size, has_no_horizontal_scroll, new_page, open_app
from tests.entry_point_pages import CONTINUE, LABELS, NOTE_TEXT, STORY_NO_RESULT, UPCOMING
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory
from tests.reminder_desk import add_event
from tests.running_app import RunningApp
from tests.tailoring_pages import seed_facts, seed_summary
from tests.today_suggestion_pages import TEXT

pytestmark = pytest.mark.e2e

QUESTION_TIMEOUT_MS = 20_000
SESSION_URL = re.compile(r"/sessions/[A-Za-z0-9_-]+$")
ANSWER = "I planned the fictional depot routes every Friday and tracked late parcels."


def seeded(app: RunningApp) -> dict[str, str]:
    """The fictional rows of the Coverage and entry point tests in one database: eight clusters
    asked for by saved jobs, an application with a summary and an upcoming interview, confirmed
    facts (one tagged Terraform, a story with no result), a draft and a capture note."""
    with desk(app) as seed:
        clock = FrozenClock(datetime(2026, 1, 5, 9, 0, tzinfo=UTC))
        application = seed.application(
            JOB_ID, "Route Planner", "Harbor Kite Fictional", "preparing"
        )
        seed_summary(seed.db, JOB_ID)
        add_event(seed.db, "m7-journey-event", application.id, "interview", UPCOMING)
        facts = FactService(seed.db, clock, SequentialIdFactory("journeyfact"))
        seed_facts(facts)
        situation, action, _ = STORY_NO_RESULT
        facts.create(FactInput(type=FactType.STORY, situation=situation, action=action))
        with seed.db.transaction() as conn:
            capture_repo.insert_note(
                conn, note_id=NOTE_ID, text=NOTE_TEXT, now="2026-10-30T10:00:00+00:00"
            )
        seed_coverage(seed, FactService(seed.db, clock, SequentialIdFactory("seedfact")))
    return {"application": str(application.id)}


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


def block(page: Page):
    return page.locator("#today-session")


def asked(page: Page) -> None:
    """The question screen after a start: the URL is the session's and the question is shown."""
    expect(page).to_have_url(SESSION_URL, timeout=QUESTION_TIMEOUT_MS)
    expect(page.locator("#session-answer")).to_be_visible(timeout=QUESTION_TIMEOUT_MS)


def today_start_and_continue(page: Page, app: RunningApp) -> None:
    open_app(page, app, "/")
    expect(block(page).get_by_role("heading", name="Suggested session", level=2)).to_be_visible()
    expect(block(page)).to_contain_text(TEXT[3])
    start = block(page).get_by_role("button", name=re.compile(r"^Start session"))
    dismiss = block(page).get_by_role("button", name=re.compile(r"^Dismiss"))
    assert_target_size(start, "Start session")
    assert_target_size(dismiss, "Dismiss")
    checked(page, "today with a suggestion")
    assert rows(app, "SELECT count(*) FROM sessions") == [(0,)], "looking at it started nothing"
    press_by_keyboard(page, start)
    asked(page)
    assert session_rows(app) == [("most_useful", None, 3, "open")]

    # With a session open, Today offers to continue it and nothing else about sessions.
    open_app(page, app, "/")
    expect(block(page)).not_to_contain_text("Suggested session")
    press_by_keyboard(page, block(page).get_by_role("link", name=CONTINUE))
    asked(page)


def run_session_by_keyboard(page: Page, app: RunningApp) -> None:
    """Answer the first question, skip the second, say "not sure" to the third, end, confirm."""
    expect(page.locator("#session-panel-heading")).to_have_text("Question 1 of 3")
    checked(page, "a question")
    tab_until(page, page.get_by_label("Your answer", exact=True))  # Enter would type a newline
    page.keyboard.type(ANSWER)
    press(page, "Submit answer")
    expect(page.locator(".session-draft")).to_be_visible(timeout=QUESTION_TIMEOUT_MS)
    expect(page.locator("#session-answer")).to_have_value(ANSWER)
    wait_said(page, "Your answer was sent.")
    expect(page.locator("#session-status")).to_be_focused()
    checked(page, "an answered question")
    press(page, "Next question")
    expect(page.locator("#session-panel-heading")).to_have_text("Question 2 of 3")
    press(page, "Skip")
    wait_said(page, "Question skipped.")
    press(page, "Next question")
    expect(page.locator("#session-panel-heading")).to_have_text("Question 3 of 3")
    press(page, "I don't remember")
    wait_said(page, "Not sure recorded.")
    # A session of three has no fourth question.
    expect(
        page.locator("#session-panel").get_by_role("button", name="Next question")
    ).to_have_count(0)
    press(page, "End session")
    expect(page.get_by_role("heading", name="Session summary")).to_be_visible()
    expect(page.locator(".session-draft")).to_have_count(1)
    expect(page.get_by_role("link", name="Leave in Drafts to review")).to_be_visible()
    checked(page, "the session summary")
    press(page, "Confirm")
    wait_said(page, "Draft confirmed.")
    expect(page.locator(".session-draft")).to_contain_text("Confirmed")

    # AC-M7.3: the draft exists and the original answer is kept as typed.
    answers = rows(
        app,
        "SELECT a.position, a.outcome, a.answer_text, a.fallback FROM session_answers a "
        "ORDER BY a.position",
    )
    assert [(p, o) for p, o, _, _ in answers] == [(1, "answered"), (2, "skipped"), (3, "not_sure")]
    assert answers[0][2] == ANSWER
    assert [fallback for *_, fallback in answers] == [0, 0, 0], "the fake's questions were kept"
    drafts = rows(
        app,
        "SELECT v.original_answer FROM session_answers a JOIN facts f ON f.id=a.draft_fact_id "
        "JOIN fact_versions v ON v.fact_id=f.id WHERE a.position=1 ORDER BY v.version",
    )
    assert drafts and all(original == ANSWER for (original,) in drafts)
    # AC-M7.4: a skip and a "not sure" are rescheduled (the exact days are named unit tests).
    scheduled = rows(
        app,
        "SELECT last_outcome, cooldown_until IS NOT NULL FROM enrichment_questions "
        "WHERE last_outcome IN ('skipped','not_sure') ORDER BY last_outcome",
    )
    assert scheduled == [("not_sure", 1), ("skipped", 1)]
    assert session_rows(app) == [("most_useful", None, 3, "ended")]

    # AC-M7.6: starting reset the window, so Today suggests nothing now.
    open_app(page, app, "/")
    expect(page.locator("#today-content")).to_be_visible()
    expect(block(page)).to_have_count(0)


def coverage_and_row_start(page: Page, app: RunningApp) -> None:
    open_app(page, app, "/facts")
    press_by_keyboard(page, tab_link(page, "coverage"))
    expect(page).to_have_url(re.compile(r"/facts\?tab=coverage$"))
    labels = page.locator("#coverage tbody tr [data-cell='label']")
    expect(labels.first).to_be_visible()
    shown = [text.strip() for text in labels.all_inner_texts()]
    assert set(ORDER) <= set(shown), "every cluster the jobs ask for is listed"
    assert shown.index(ROUTES) < shown.index(TERRAFORM), "None comes before Thin (AC-M7.5)"
    expect(table_row(page, ROUTES)).to_contain_text("None")
    expect(table_row(page, TERRAFORM)).to_contain_text("Thin")
    for label in ORDER:  # status is a word, not only a colour
        expect(table_row(page, label)).to_contain_text(re.compile(r"None|Thin|Strong"))
    assert_target_size(row_start_button(table_row(page, ROUTES)), "Start session (row)")
    checked(page, "coverage")

    cluster = table_row(page, ROUTES).get_attribute("data-cluster-id")
    press_by_keyboard(page, row_start_button(table_row(page, ROUTES)))
    asked(page)
    assert session_rows(app)[-1] == ("cluster", cluster, 3, "open")
    end_session(page)


def entry_points(page: Page, app: RunningApp, made: dict[str, str]) -> None:
    # The gap check: no offer before a check; after one with gaps, one offer.
    open_app(page, app, f"/applications/{made['application']}?tab=tailoring")
    expect(offer_button(region(page), "gap")).to_have_count(0)
    run_gap_check(page)
    expect(offer_button(region(page), "gap")).to_be_visible()
    assert_target_size(offer_button(region(page), "gap"), LABELS["gap"])
    checked(page, "the gap check with its offer")
    press_by_keyboard(page, offer_button(region(page), "gap"))
    asked(page)
    assert session_rows(app)[-1] == ("job", JOB_ID, 3, "open")
    end_session(page)

    # The drawer: an upcoming interview offers a session about its job.
    open_board(page, app)
    link = page.locator(f'[data-application-id="{made["application"]}"] a[hx-get$="/detail"]')
    press_by_keyboard(page, link)
    dialog = page.get_by_role("dialog", name="Application details")
    expect(dialog).to_be_visible()
    press_by_keyboard(page, offer_button(dialog, "interview"))
    asked(page)
    assert session_rows(app)[-1] == ("job", JOB_ID, 3, "open")
    end_session(page)

    # The capture inbox: a note is expanded in a session focused on it.
    open_app(page, app, "/facts?tab=capture")
    press_by_keyboard(page, offer_button(page.locator(f"#note-{NOTE_ID}"), "note"))
    asked(page)
    assert session_rows(app)[-1] == ("capture_note", NOTE_ID, 3, "open")
    end_session(page)

    # Nothing was left open and every start came from a person's key press: five sessions.
    assert [state for *_, state in session_rows(app)] == ["ended"] * 5


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone-390x844"])
def test_m7_journey_today_session_summary_coverage_entry_points(
    running_app: RunningApp, browser, viewport: dict[str, int]
) -> None:
    app = running_app
    made = seeded(app)
    with new_page(browser, viewport) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        today_start_and_continue(page, app)
        run_session_by_keyboard(page, app)
        coverage_and_row_start(page, app)
        entry_points(page, app, made)
