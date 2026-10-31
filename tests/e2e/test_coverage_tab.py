"""M7.7 Coverage tab in a real browser (AC-M7.5): keyboard path, phone width and axe.

The assumed markup is listed in ``tests/coverage_pages.py``. The app starts with a finished setup
and the fake provider; the jobs and facts are fictional and written into its database.
"""

import re
from datetime import UTC, datetime

import pytest
from playwright.sync_api import Page, expect

from app.services.facts import FactService
from tests.coverage_pages import ORDER, ROUTES, TAB_URL, TERRAFORM, TERRAFORM_FACT, seed_coverage
from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import PHONE
from tests.e2e.test_board import desk, rows
from tests.e2e.test_facts_page import press_by_keyboard, tab_link
from tests.e2e.test_shell import assert_target_size, has_no_horizontal_scroll, new_page, open_app
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e

QUESTION_TIMEOUT_MS = 20_000


def seeded(app: RunningApp) -> dict[str, str]:
    with desk(app) as seed:
        clock = FrozenClock(datetime(2026, 1, 5, 9, 0, tzinfo=UTC))
        return seed_coverage(seed, FactService(seed.db, clock, SequentialIdFactory("seedfact")))


def table_row(page: Page, label: str):
    return page.locator("#coverage tbody tr").filter(has_text=label)


def start_button(row):
    return row.get_by_role("button", name=re.compile(r"^Start session"))


def strengthen(row):
    return row.locator("button, a").filter(has_text=re.compile(r"^Strengthen"))


def test_ac_m7_5_coverage_row_starts_session_by_keyboard(running_app: RunningApp, desktop_page):
    page = desktop_page
    made = seeded(running_app)
    open_app(page, running_app, "/facts")

    # The fourth tab is reached by keyboard and shows the clusters, biggest gap first.
    press_by_keyboard(page, tab_link(page, "coverage"))
    expect(page).to_have_url(re.compile(r"/facts\?tab=coverage$"))
    expect(page.locator("#coverage tbody tr")).to_have_count(len(ORDER))
    shown = [
        page.locator("#coverage tbody tr")
        .nth(n)
        .locator('[data-cell="label"]')
        .inner_text()
        .strip()
        for n in range(len(ORDER))
    ]
    assert shown == list(ORDER)
    expect(table_row(page, TERRAFORM)).to_contain_text("Thin")
    expect(table_row(page, ROUTES)).to_contain_text("None")
    expect(table_row(page, ROUTES)).to_contain_text("Asked by 3 saved jobs")

    # Strengthen (Thin rows only) opens the related facts with their Refine with AI links.
    assert strengthen(table_row(page, ROUTES)).count() == 0
    press_by_keyboard(page, strengthen(table_row(page, TERRAFORM)))
    detail = page.locator("#coverage-detail")
    expect(detail).to_be_visible()
    expect(page.locator("#coverage-detail-heading")).to_be_focused()
    expect(detail).to_contain_text(TERRAFORM_FACT)
    refine = detail.get_by_role("link", name="Refine with AI")
    expect(refine).to_have_count(1)
    expect(refine).to_have_attribute("href", f"/facts/{made['terraform']}/edit#fact-refine")

    # Start session on the biggest gap, by keyboard: a session focused on that cluster.
    row_id = table_row(page, ROUTES).get_attribute("data-cluster-id")
    press_by_keyboard(page, start_button(table_row(page, ROUTES)))
    expect(page).to_have_url(re.compile(r"/sessions/[A-Za-z0-9_-]+$"), timeout=QUESTION_TIMEOUT_MS)
    expect(page.locator("#session-answer")).to_be_visible(timeout=QUESTION_TIMEOUT_MS)
    expect(page.locator("#session-panel-heading")).to_have_text("Question 1 of 3")
    stored = rows(running_app, "SELECT focus_kind, focus_ref, length FROM sessions")
    assert stored == [("cluster", row_id, 3)]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_coverage_phone_390x844_and_axe(running_app: RunningApp, browser, theme):
    seeded(running_app)
    with new_page(browser, PHONE, color_scheme=theme) as page:
        open_app(page, running_app, TAB_URL)
        expect(page.locator("#coverage tbody tr")).to_have_count(len(ORDER))
        wait_for_settled_styles(page)
        fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert fits, f"the page scrolls sideways: {scroll_width} > {inner_width}"
        assert_no_serious_violations(page)
        for label in (ROUTES, TERRAFORM):
            assert_target_size(start_button(table_row(page, label)), f"Start session ({label})")
        assert_target_size(strengthen(table_row(page, TERRAFORM)), "Strengthen")

        # The related-facts view fits and passes the scan too.
        press_by_keyboard(page, strengthen(table_row(page, TERRAFORM)))
        expect(page.locator("#coverage-detail")).to_be_visible()
        wait_for_settled_styles(page)
        fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert fits, f"the page scrolls sideways: {scroll_width} > {inner_width}"
        assert_no_serious_violations(page)

        # Status never relies on colour alone: the word is in the text of every row.
        for label in ORDER:
            expect(table_row(page, label)).to_contain_text(re.compile(r"None|Thin|Strong"))


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_coverage_rows_are_readable_cards_at_390(running_app: RunningApp, browser, theme):
    seeded(running_app)
    with new_page(browser, PHONE, color_scheme=theme) as page:
        open_app(page, running_app, TAB_URL)
        expect(page.locator("#coverage tbody tr")).to_have_count(len(ORDER))
        wait_for_settled_styles(page)
        for label in ORDER:
            row = table_row(page, label)
            cells = {
                name: row.locator(f'[data-cell="{name}"]').bounding_box()
                for name in ("label", "asked", "strength", "actions")
            }
            assert all(cells.values()), f"{label}: a cell is not rendered"
            label_box = cells["label"]
            assert label_box is not None
            # Not a column squeezed beside three others: the cell has the card's width and the
            # label is on one or two lines, never broken into fragments.
            assert label_box["width"] >= 200, f"{label} cell is {label_box['width']:.0f} px wide"
            line = row.locator('[data-cell="label"]').evaluate(
                "(el) => parseFloat(getComputedStyle(el).lineHeight)"
            )
            assert label_box["height"] <= 2 * line + 1, f"{label} wraps into fragments"
            # Stacked in reading order: label, asked by, strength, then the buttons.
            tops = [cells[n]["y"] for n in ("label", "asked", "strength", "actions")]  # type: ignore[index]
            assert tops == sorted(tops) and len(set(tops)) == 4, (label, tops)
        # The region around the table has nothing to scroll here, so it is not a tab stop.
        region = page.locator("#coverage .table-scroll")
        assert region.evaluate("(el) => getComputedStyle(el).display") == "contents"
        fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert fits, f"the page scrolls sideways: {scroll_width} > {inner_width}"
        assert_no_serious_violations(page)
        for label in (ROUTES, TERRAFORM):
            assert_target_size(start_button(table_row(page, label)), f"Start session ({label})")
        assert_target_size(strengthen(table_row(page, TERRAFORM)), "Strengthen")


def test_coverage_table_region_scrolls_by_keyboard_on_desktop(
    running_app: RunningApp, desktop_page
):
    page = desktop_page
    seeded(running_app)
    open_app(page, running_app, TAB_URL)
    region = page.locator("#coverage .table-scroll")
    expect(region).to_have_attribute("role", "region")
    assert region.evaluate("(el) => getComputedStyle(el).display") != "contents"
    region.focus()
    expect(region).to_be_focused()


def test_coverage_detail_closes_by_keyboard_and_returns_focus(
    running_app: RunningApp, desktop_page
):
    page = desktop_page
    seeded(running_app)
    open_app(page, running_app, TAB_URL)
    expect(page.locator("#coverage tbody tr")).to_have_count(len(ORDER))
    press_by_keyboard(page, strengthen(table_row(page, TERRAFORM)))
    expect(page.locator("#coverage-detail-heading")).to_be_focused()
    expect(page.locator("#announcer")).to_have_text(f"Showing the facts behind {TERRAFORM}.")
    close = page.locator("#coverage-detail").get_by_role("button", name="Close")
    press_by_keyboard(page, close)
    expect(page.locator("#coverage-detail")).to_have_count(0)
    expect(page.locator("#coverage-detail-slot")).to_be_empty()
    expect(strengthen(table_row(page, TERRAFORM))).to_be_focused()
    expect(strengthen(table_row(page, TERRAFORM))).to_be_enabled()


def test_coverage_refused_start_shows_only_the_reason(running_app: RunningApp, desktop_page):
    page = desktop_page
    seeded(running_app)
    open_app(page, running_app, TAB_URL)
    expect(page.locator("#coverage tbody tr")).to_have_count(len(ORDER))
    press_by_keyboard(page, start_button(table_row(page, ROUTES)))
    expect(page).to_have_url(re.compile(r"/sessions/[A-Za-z0-9_-]+$"), timeout=QUESTION_TIMEOUT_MS)
    # A session is open now: starting another from the tab is refused, in the tab.
    open_app(page, running_app, TAB_URL)
    expect(page.locator("#coverage tbody tr")).to_have_count(len(ORDER))
    headings = page.locator("h1").count()
    press_by_keyboard(page, start_button(table_row(page, TERRAFORM)))
    reason = page.locator("#coverage-start [role=alert]")
    expect(reason).to_contain_text("open session")
    expect(reason).to_be_focused()
    expect(
        page.locator("#coverage-start").get_by_role("link", name="Continue your session")
    ).to_be_visible()
    assert page.locator("h1").count() == headings, "no heading of its own"
    assert page.locator("#coverage-start select").count() == 0, "no focus or length form"
    assert_no_serious_violations(page)
    # A second refusal still finds its slot.
    press_by_keyboard(page, start_button(table_row(page, ROUTES)))
    expect(page.locator("#coverage-start [role=alert]")).to_be_focused()
