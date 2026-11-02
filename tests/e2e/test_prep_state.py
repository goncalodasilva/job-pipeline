"""Prep state as text on a board card, in the drawer and in Today (M8.2 part D; design "Prep
state"): desktop and a 390x844 phone, light and dark, with axe and no sideways scrolling.

The app runs with a finished setup. Three fictional applications are written into its database:
one with a future interview and a practiced answer ("Practiced for the interview"), one with a
future interview and no prep ("Prep not started") and one with prep questions but no interview
("Questions ready").
Interviews are two days after the real clock, since the app runs on it. Browser-facing contract
(the markup is in ``tests/prep_state_pages.py``): the state is one ``[data-prep-state]`` element of
plain text on ``li.board-card``, inside the drawer and on ``#today-interviews li``; it takes no
focus, so Tab visits the same controls with or without prep.
"""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_board import card, desk, open_board
from tests.e2e.test_facts_page import press_by_keyboard
from tests.e2e.test_shell import has_no_horizontal_scroll, new_page, open_app
from tests.prep_state_pages import ADD_ATTEMPT, ADD_ITEM, ADD_QUESTION, LABELS
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e

STAMP = "2026-01-05T09:00:00+00:00"
ADD_EVENT = (
    "INSERT INTO events (id, application_id, type, starts_at, ends_at, all_day, time_zone, "
    "thank_you_reminder, status, created_at, updated_at) "
    "VALUES (?, ?, 'interview', ?, ?, 0, 'UTC', 0, 'scheduled', ?, ?)"
)


def seeded(app: RunningApp) -> dict[str, str]:
    """Application ids by the state each one shows."""
    with desk(app) as seed:
        made = {
            "practiced": seed.application(
                "ps-e2e-1", "Seed Buyer", "Fictional Orchards", "applied"
            ),
            "not_started": seed.application(
                "ps-e2e-2", "Frost Watcher", "Fictional Orchards", "applied"
            ),
            "questions_ready": seed.application(
                "ps-e2e-3", "Pruning Lead", "Fictional Orchards", "applied"
            ),
        }
    start = datetime.now(UTC) + timedelta(days=2)
    end = start + timedelta(hours=1)
    with closing(sqlite3.connect(app.data_dir / "app.db", timeout=10)) as conn:
        for name in ("practiced", "not_started"):
            conn.execute(
                ADD_EVENT,
                (
                    f"ps-event-{name}",
                    made[name].id,
                    start.isoformat(),
                    end.isoformat(),
                    STAMP,
                    STAMP,
                ),
            )
        for name, practiced in (("practiced", True), ("questions_ready", False)):
            aid = made[name].id
            conn.execute(ADD_QUESTION, (f"ps-q-{name}", "Describe a fictional task.", STAMP, STAMP))
            conn.execute(ADD_ITEM, (f"ps-i-{name}", aid, f"ps-q-{name}", STAMP, STAMP))
            if practiced:
                conn.execute(
                    ADD_ATTEMPT,
                    (f"ps-a-{name}", f"ps-i-{name}", "A fictional answer.", STAMP, STAMP),
                )
        conn.commit()
    return {name: str(application.id) for name, application in made.items()}


def mark_of_card(page: Page, application_id: str):
    return card(page, application_id).locator("[data-prep-state]")


def check_board(page: Page, app: RunningApp, ids: dict[str, str]) -> None:
    open_board(page, app)
    for name, application_id in ids.items():
        mark = mark_of_card(page, application_id)
        expect(mark).to_have_count(1)
        expect(mark).to_have_attribute("data-prep-state", name)
        expect(mark).to_have_text(LABELS[name])
        expect(mark).to_be_visible()


def check_today(page: Page, app: RunningApp) -> None:
    open_app(page, app)
    expect(page.locator("#today-interviews")).to_be_visible()
    for name in ("practiced", "not_started"):
        mark = page.locator(
            f'#today-interviews li[data-interview-id="ps-event-{name}"] [data-prep-state]'
        )
        expect(mark).to_have_count(1)
        expect(mark).to_have_text(LABELS[name])
    expect(page.locator("#today-interviews [data-prep-state]")).to_have_count(2)


def settled_checks(page: Page) -> None:
    wait_for_settled_styles(page)
    ok, scroll_width, inner_width = has_no_horizontal_scroll(page)
    assert ok, (scroll_width, inner_width)
    assert_no_serious_violations(page)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone390x844"])
def test_prep_state_is_text_on_the_board_and_in_today_and_axe(
    running_app: RunningApp, browser: Browser, theme: str, viewport: dict[str, int]
):
    ids = seeded(running_app)
    with new_page(browser, viewport, color_scheme=theme) as page:
        check_board(page, running_app, ids)
        settled_checks(page)
        check_today(page, running_app)
        settled_checks(page)


def test_prep_state_in_the_drawer_by_keyboard_adds_no_tab_stop(
    running_app: RunningApp, browser: Browser
):
    ids = seeded(running_app)
    with new_page(browser, DESKTOP) as page:
        open_board(page, running_app)
        # The state is not focusable on the card, and nothing inside it is.
        for application_id in ids.values():
            expect(mark_of_card(page, application_id)).to_have_count(1)
            expect(mark_of_card(page, application_id)).not_to_have_attribute("tabindex", "0")
            assert (
                mark_of_card(page, application_id)
                .locator("a, button, input, select, textarea, [tabindex]")
                .count()
                == 0
            )
        opener = card(page, ids["practiced"]).get_by_role("link", name="Open application")
        press_by_keyboard(page, opener)
        dialog = page.get_by_role("dialog", name="Application details")
        expect(dialog).to_be_visible()
        mark = dialog.locator("[data-prep-state]")
        expect(mark).to_have_count(1)
        expect(mark).to_have_text(LABELS["practiced"])
        assert mark.locator("a, button, input, select, textarea, [tabindex]").count() == 0
        assert_no_serious_violations(page)
