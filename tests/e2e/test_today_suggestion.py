"""M7.8 Today's "Suggested session" block in a real browser (AC-M7.6): Dismiss and Start by
keyboard, the Continue link, and the block at 390x844 with an axe scan in light and dark.

The assumed markup and routes are in ``tests/today_suggestion_pages.py``. The app starts with a
finished setup and the fake provider; the job is fictional and written into its database. The
browser's clock is the real one, so the test moves the stored reset into the past to let the
window pass (no waiting, no sleeping).
"""

import json
import re

import pytest
from playwright.sync_api import Page, expect

from app.modules.storage import Database, sessions_repo
from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import PHONE
from tests.e2e.test_board import desk, rows
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, press_by_keyboard, wait_said
from tests.e2e.test_shell import assert_target_size, has_no_horizontal_scroll, new_page, open_app
from tests.running_app import RunningApp
from tests.today_suggestion_pages import (
    CONTINUE,
    DISMISSED,
    NOTHING_DUE,
    SUGGESTION_KEY,
    TEXT,
    seed_backlog,
)

pytestmark = pytest.mark.e2e

QUESTION_TIMEOUT_MS = 20_000
LONG_AGO = "2020-01-01T00:00:00+00:00"


def seeded(app: RunningApp) -> None:
    with desk(app) as seed:
        seed_backlog(seed)


def age_the_reset(app: RunningApp) -> None:
    """The dismissal happened long ago: the next weekly window has come."""
    with Database.open(app.data_dir / "app.db") as db, db.transaction() as conn:
        conn.execute(
            "UPDATE settings SET value=? WHERE key=?",
            (json.dumps({"last_reset": LONG_AGO}), SUGGESTION_KEY),
        )


def reset_rows(app: RunningApp) -> list[tuple[str]]:
    return rows(app, "SELECT value FROM settings WHERE key=?", (SUGGESTION_KEY,))


def block(page: Page):
    return page.locator("#today-session")


def start_button(page: Page):
    return block(page).get_by_role("button", name=re.compile(r"^Start session"))


def dismiss_button(page: Page):
    return block(page).get_by_role("button", name=re.compile(r"^Dismiss"))


def test_today_suggestion_start_and_dismiss_by_keyboard(running_app: RunningApp, desktop_page):
    page = desktop_page
    seeded(running_app)
    page.add_init_script(RECORD_ANNOUNCEMENTS)
    open_app(page, running_app)

    # The block shows the length and time, with both actions.
    expect(block(page).get_by_role("heading", name="Suggested session", level=2)).to_be_visible()
    expect(block(page)).to_contain_text(TEXT[3])
    expect(start_button(page)).to_have_count(1)
    expect(dismiss_button(page)).to_have_count(1)
    assert reset_rows(running_app) == [], "looking at it stores nothing"
    wait_for_settled_styles(page)
    assert_no_serious_violations(page)

    # Dismiss by keyboard: the block goes, focus lands on the page heading, it is announced and
    # the window starts now.
    press_by_keyboard(page, dismiss_button(page))
    expect(block(page)).to_have_count(0)
    expect(page.locator("#page-heading")).to_be_focused()
    wait_said(page, DISMISSED)
    assert len(reset_rows(running_app)) == 1
    open_app(page, running_app)
    expect(page.locator("#today-content")).to_be_visible()
    expect(block(page)).to_have_count(0)

    # Weeks later it is due again; Start by keyboard opens a session on the most useful item.
    age_the_reset(running_app)
    open_app(page, running_app)
    expect(block(page)).to_contain_text(TEXT[3])
    press_by_keyboard(page, start_button(page))
    expect(page).to_have_url(re.compile(r"/sessions/[A-Za-z0-9_-]+$"), timeout=QUESTION_TIMEOUT_MS)
    expect(page.locator("#session-answer")).to_be_visible(timeout=QUESTION_TIMEOUT_MS)
    expect(page.locator("#session-panel-heading")).to_have_text("Question 1 of 3")
    stored = rows(running_app, "SELECT focus_kind, length, state FROM sessions")
    assert stored == [("most_useful", 3, "open")]
    [(value,)] = reset_rows(running_app)
    assert json.loads(value)["last_reset"] > LONG_AGO, "starting a session resets the window"

    # Back on Today an open session offers to continue instead of a new suggestion.
    session_url = page.url
    open_app(page, running_app)
    expect(block(page)).to_have_count(1)
    expect(block(page)).not_to_contain_text("Suggested session")
    expect(start_button(page)).to_have_count(0)
    expect(dismiss_button(page)).to_have_count(0)
    link = block(page).get_by_role("link", name=CONTINUE)
    press_by_keyboard(page, link)
    expect(page).to_have_url(session_url)
    expect(page.locator("#session-panel-heading")).to_be_visible(timeout=QUESTION_TIMEOUT_MS)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_today_suggestion_phone_390x844_and_axe(running_app: RunningApp, browser, theme):
    seeded(running_app)
    with new_page(browser, PHONE, color_scheme=theme) as page:
        open_app(page, running_app)
        expect(block(page)).to_contain_text(TEXT[3])
        wait_for_settled_styles(page)
        fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert fits, f"the page scrolls sideways: {scroll_width} > {inner_width}"
        assert_no_serious_violations(page)
        assert_target_size(start_button(page), "Start session")
        assert_target_size(dismiss_button(page), "Dismiss")
        # Status never relies on colour alone: the block says what it is in words.
        expect(block(page)).to_contain_text("Suggested session")


def test_today_empty_line_gives_way_to_an_open_session(running_app: RunningApp, desktop_page):
    page = desktop_page
    seeded(running_app)
    open_app(page, running_app)
    # A mere suggestion keeps the line.
    expect(block(page)).to_contain_text("Suggested session")
    expect(page.locator("#today-content")).to_contain_text(NOTHING_DUE)
    open_a_session(running_app)
    open_app(page, running_app)
    expect(block(page).get_by_role("link", name=CONTINUE)).to_be_visible()
    expect(page.locator("#today-content")).not_to_contain_text(NOTHING_DUE)


def open_a_session(app: RunningApp) -> None:
    """A session started elsewhere (another tab): inserted directly, no question is asked."""
    now = "2026-01-01T09:00:00+00:00"
    with Database.open(app.data_dir / "app.db") as db, db.transaction() as conn:
        sessions_repo.insert_session(
            conn, id="elsewhere-session", focus_kind="most_useful", length=3, now=now
        )


def test_today_refused_start_shows_only_the_reason(running_app: RunningApp, desktop_page):
    page = desktop_page
    seeded(running_app)
    open_app(page, running_app)
    expect(start_button(page)).to_have_count(1)
    # Another tab starts a session after this page was read: the Start form is still here.
    open_a_session(running_app)
    headings = page.locator("h1").count()
    press_by_keyboard(page, start_button(page))
    reason = page.locator("#today-session-refusal [role=alert]")
    expect(reason).to_contain_text("open session")
    expect(reason).to_be_focused()
    expect(
        page.locator("#today-session-refusal").get_by_role("link", name=CONTINUE)
    ).to_be_visible()
    # Only the reason is replaced: the block and the page heading stay.
    expect(block(page).get_by_role("heading", name="Suggested session", level=2)).to_be_visible()
    expect(page.locator("#page-heading")).to_have_text("Today")
    assert page.locator("h1").count() == headings, "no heading of its own"
    assert page.locator("#today-session-refusal select").count() == 0, "no focus or length form"
    assert_no_serious_violations(page)
    # A second refusal still finds its slot.
    press_by_keyboard(page, start_button(page))
    expect(page.locator("#today-session-refusal [role=alert]")).to_be_focused()
    assert page.locator("#today-session-refusal").count() == 1
