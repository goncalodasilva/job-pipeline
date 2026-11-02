"""The company brief in a browser (M8.3 part B; AC-M8.2 and AC-M8.3 for the brief; design
Companies "Research"): get a brief from the company page by keyboard, the budget dialog and the
blocked state, light and dark, desktop and 390 x 844 phone, with the offline fake provider of the
running app (it searches once and cites one fictional source).

The markup hooks and announcement texts are written in ``tests/brief_pages.py``.
"""

import json

import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from app.domain.budgets import Budget
from app.domain.clock import SystemClock
from app.modules.storage import Database
from app.services.usage import UsageService
from app.wiring import local_timezone
from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_board import desk
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, press_by_keyboard, wait_said
from tests.e2e.test_shell import has_no_horizontal_scroll, new_page, open_app
from tests.fakes.ids import SequentialIdFactory
from tests.running_app import AppStart, RunningApp

pytestmark = pytest.mark.e2e
STARTED = "Company brief started."
LONG_WORD = "unbrokenfictionalwordthatkeepsgoing" * 6
LONG_URL = "https://example.test/" + "path-segment-" * 30


def seeded(app: RunningApp) -> tuple[str, str]:
    """An application and its company; returns (application id, company id)."""
    with desk(app) as seed:
        made = seed.application("brief-e2e", "Fictional Developer", "Example Orchard", "applied")
    with Database.open(app.data_dir / "app.db") as db:
        cid = str(
            db.connection.execute(
                "SELECT id FROM companies WHERE name=?", ("Example Orchard",)
            ).fetchone()[0]
        )
    return made.id, cid


def usage(app: RunningApp):
    db = Database.open(app.data_dir / "app.db")
    return db, UsageService(
        db, SystemClock(), SequentialIdFactory("brief-budget"), local_timezone()
    )


def save_brief(app: RunningApp, cid: str) -> None:
    """A saved brief with unbroken text and a long source url, to check narrow screens."""
    content = {
        "sections": [
            {
                "heading": "what_they_do",
                "text": f"Plans delivery routes for small depots. {LONG_WORD}",
                "source_url": LONG_URL,
                "source_date": "March 2024",
            },
            {
                "heading": "recent_news",
                "text": "Opened a second fictional depot this year.",
                "source_url": "https://example.test/news",
                "source_date": None,
            },
        ]
    }
    sources = [s["source_url"] for s in content["sections"]]
    with Database.open(app.data_dir / "app.db") as db, db.transaction() as conn:
        conn.execute(
            "INSERT INTO company_briefs(id, company_id, content, sources, found_at, provider, "
            "model, prompt_id, prompt_version, created_at, updated_at, version) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
            (
                "brief-e2e-1",
                cid,
                json.dumps(content),
                json.dumps(sources),
                "2026-10-20T09:00:00+00:00",
                "fake",
                "fake-thorough",
                "company_brief",
                "1",
                "2026-10-20T09:00:00+00:00",
                "2026-10-20T09:00:00+00:00",
            ),
        )


def bound(page: Page) -> Locator:
    """The start button, once htmx has processed its form (a press before that would submit the
    form natively, without the launch token: TD-320)."""
    page.wait_for_function(
        "() => !!document.querySelector('#company-brief-start').form['htmx-internal-data']"
    )
    return page.locator("#company-brief-start")


def test_ac_m8_3_get_a_company_brief_by_keyboard_announced(
    running_app: RunningApp, browser: Browser
):
    _aid, cid = seeded(running_app)
    with new_page(browser, DESKTOP) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        open_app(page, running_app, f"/companies/{cid}")
        start = bound(page)
        expect(start).to_have_text("Get a company brief")
        expect(page.locator("#company-brief [data-brief-section]")).to_have_count(0)
        press_by_keyboard(page, start)
        wait_said(page, STARTED)
        expect(page.locator("#company-brief-heading")).to_be_focused()
        sections = page.locator("#company-brief [data-brief-section]")
        expect(sections.first).to_be_visible(timeout=20_000)
        link = sections.first.get_by_role("link")
        expect(link).to_have_attribute("rel", "noopener noreferrer nofollow")
        expect(page.locator("#company-brief-found")).to_contain_text("Found ")
        expect(page.locator("#company-brief-start")).to_have_text("Refresh the brief")
        page.wait_for_function(
            "() => !document.querySelector('#company-brief [aria-disabled=true]')"
        )
        # The rest of the page is still the company page, in the same order.
        order = page.evaluate(
            "() => ['company-history','company-brief','company-edit']"
            ".map((id) => document.getElementById(id).getBoundingClientRect().top)"
        )
        assert order == sorted(order)
        assert has_no_horizontal_scroll(page)[0]
        assert_no_serious_violations(page)


def test_ac_m8_3_brief_budget_dialog_then_blocked_state_by_keyboard(
    running_app: RunningApp, browser: Browser
):
    _aid, cid = seeded(running_app)
    db, service = usage(running_app)
    try:
        service.save_budget(
            "web_search", Budget(10, 1, 7, 10, 1), expected_version=service.load_budgets().version
        )
        with new_page(browser, PHONE) as page:
            page.add_init_script(RECORD_ANNOUNCEMENTS)
            open_app(page, running_app, f"/companies/{cid}")
            start = bound(page)
            press_by_keyboard(page, start)
            dialog = page.get_by_role("dialog")
            expect(dialog).to_be_visible()
            expect(dialog).to_contain_text("up to 5 searches")
            expect(dialog.get_by_role("button", name="Not now", exact=True)).to_be_focused()
            assert service.status("web_search").local_count == 0
            assert_no_serious_violations(page)
            page.keyboard.press("Escape")
            expect(page.locator("#company-brief-start")).to_be_focused()
            press_by_keyboard(page, page.locator("#company-brief-start"))
            press_by_keyboard(page, dialog.get_by_role("button", name="Continue", exact=True))
            wait_said(page, STARTED)
            expect(page.locator("#company-brief [data-brief-section]").first).to_be_visible(
                timeout=20_000
            )
            assert service.status("web_search").local_count == 1
            for _ in range(6):
                record = service.reserve("web_search", tier="thorough", task_type="company_brief")
                service.complete(record, success=True, tokens=None)
            page.reload()
            start = bound(page)
            expect(start).to_have_attribute("aria-disabled", "true")
            expect(page.locator("#company-brief-reason")).to_contain_text(
                "Monthly search limit reached. Resets on"
            )
            press_by_keyboard(page, start)
            assert service.status("web_search").local_count == 7, "a blocked press starts nothing"
            expect(page.locator("#company-edit-heading")).to_be_visible()
            assert has_no_horizontal_scroll(page)[0]
            assert_no_serious_violations(page)
    finally:
        db.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone390x844"])
def test_company_page_with_a_saved_brief_390x844_and_axe(
    running_app: RunningApp, browser: Browser, theme, viewport
):
    _aid, cid = seeded(running_app)
    save_brief(running_app, cid)
    with new_page(browser, viewport) as page:
        page.emulate_media(color_scheme=theme)
        open_app(page, running_app, f"/companies/{cid}")
        wait_for_settled_styles(page)
        expect(page.locator("#company-brief [data-brief-section]")).to_have_count(2)
        expect(page.locator("#company-brief-found")).to_contain_text("Found 20 October 2026")
        expect(page.locator("#company-brief")).to_contain_text(
            "March 2024 (as stated by the source)"
        )
        ok, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert ok, f"horizontal scroll: {scroll_width} > {inner_width}"
        assert_no_serious_violations(page)


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone390x844"])
def test_prep_tab_research_section_shows_the_brief_and_starts_it_by_keyboard(
    running_app: RunningApp, browser: Browser, viewport
):
    aid, _cid = seeded(running_app)
    with new_page(browser, viewport) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        open_app(page, running_app, f"/applications/{aid}?tab=prep")
        research = page.locator("section[aria-labelledby=prep-research-heading]")
        expect(research.locator("#company-brief")).to_have_count(1)
        start = bound(page)
        press_by_keyboard(page, start)
        wait_said(page, STARTED)
        expect(research.locator("#company-brief [data-brief-section]").first).to_be_visible(
            timeout=20_000
        )
        expect(page.locator("#prep-heading")).to_be_visible()
        ok, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert ok, f"horizontal scroll: {scroll_width} > {inner_width}"
        assert_no_serious_violations(page)


# --- review fixes: refresh safety, a company edit, and the outcome states on a phone ------------

NOTHING = "Nothing with a source was found this time."


def seed_task(app: RunningApp, cid: str, *, status: str, error=None, result_ref=None) -> None:
    """A finished ``company_brief`` task row of this company, too old to be announced."""
    stamp = "2026-10-20T09:00:00+00:00"
    with Database.open(app.data_dir / "app.db") as db, db.transaction() as conn:
        conn.execute(
            "INSERT INTO tasks(id, type, status, dedupe_key, input_ref, result_ref, error, "
            "created_at, updated_at, finished_at, version) VALUES(?,?,?,?,?,?,?,?,?,?,1)",
            (
                "brief-task-e2e",
                "company_brief",
                status,
                f"company_brief:{cid}",
                f"{cid}/default",
                result_ref,
                error,
                stamp,
                stamp,
                stamp,
            ),
        )


def fire(page: Page, *events: str) -> None:
    """What the task indicator and the usage panel do: tell the body something changed, then
    give htmx a moment to act on it."""
    page.evaluate(
        "async (names) => { names.forEach((n) => document.body.dispatchEvent("
        "new CustomEvent(n))); await new Promise((r) => setTimeout(r, 400)); }",
        list(events),
    )


def test_an_open_budget_dialog_survives_a_refresh_event_and_keeps_its_focus(
    running_app: RunningApp, browser: Browser
):
    _aid, cid = seeded(running_app)
    db, service = usage(running_app)
    try:
        service.save_budget(
            "web_search", Budget(10, 1, 7, 10, 1), expected_version=service.load_budgets().version
        )
        with new_page(browser, PHONE) as page:
            requested: list[str] = []
            page.on(
                "request",
                lambda r: (
                    requested.append(r.url)
                    if r.url.endswith(f"/companies/{cid}/brief") and r.method == "GET"
                    else None
                ),
            )
            open_app(page, running_app, f"/companies/{cid}")
            press_by_keyboard(page, bound(page))
            dialog = page.get_by_role("dialog")
            expect(dialog).to_be_visible()
            not_now = dialog.get_by_role("button", name="Not now", exact=True)
            expect(not_now).to_be_focused()
            fire(page, "tasks:updated", "usage:changed")
            expect(dialog).to_be_visible()
            expect(not_now).to_be_focused()
            assert requested == [], "a refresh never replaces the open question"
            expect(page.locator("#company-brief [data-nested-stale-note]")).to_be_visible()
            assert has_no_horizontal_scroll(page)[0]
            assert_no_serious_violations(page)
    finally:
        db.close()


def test_the_heading_keeps_focus_when_the_brief_finishes(running_app: RunningApp, browser: Browser):
    _aid, cid = seeded(running_app)
    with new_page(browser, DESKTOP) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        open_app(page, running_app, f"/companies/{cid}")
        press_by_keyboard(page, bound(page))
        wait_said(page, STARTED)
        heading = page.locator("#company-brief-heading")
        expect(heading).to_be_focused()
        expect(page.locator("#company-brief [data-brief-section]").first).to_be_visible(
            timeout=20_000
        )
        expect(heading).to_be_focused()
        expect(page.locator("#company-brief [data-nested-stale-note]")).to_be_hidden()


def test_a_brief_started_after_a_company_edit_is_not_a_conflict(
    running_app: RunningApp, browser: Browser
):
    _aid, cid = seeded(running_app)
    with new_page(browser, DESKTOP) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        open_app(page, running_app, f"/companies/{cid}")
        version = page.locator("#company-brief input[name=version]")
        before = version.input_value()
        bound(page)
        page.wait_for_function(
            "() => !!document.querySelector('#company-edit form')['htmx-internal-data']"
        )
        page.locator("#company-edit-notes").fill("A fictional note.")
        press_by_keyboard(page, page.get_by_role("button", name="Save company"))
        wait_said(page, "Saved.")
        page.wait_for_function(
            "(was) => document.querySelector('#company-brief input[name=version]').value !== was",
            arg=before,
        )
        press_by_keyboard(page, bound(page))
        wait_said(page, STARTED)
        expect(page.locator("#company-brief-conflict")).to_have_count(0)
        expect(page.locator("#company-brief [data-brief-section]").first).to_be_visible(
            timeout=20_000
        )


@pytest.mark.parametrize(
    ("state", "kwargs"),
    [
        ("nothing", {"status": "succeeded", "result_ref": "none"}),
        ("failed", {"status": "failed", "error": "InvalidModelOutput"}),
    ],
)
def test_nothing_found_and_failed_outcomes_at_390x844_without_a_live_alert(
    running_app: RunningApp, browser: Browser, state, kwargs
):
    _aid, cid = seeded(running_app)
    seed_task(running_app, cid, **kwargs)
    with new_page(browser, PHONE) as page:
        open_app(page, running_app, f"/companies/{cid}")
        wait_for_settled_styles(page)
        brief = page.locator("#company-brief")
        if state == "nothing":
            expect(page.locator("#company-brief-nothing")).to_have_text(NOTHING)
        else:
            expect(brief.locator(".field-error")).to_contain_text("Try starting again.")
        expect(brief.locator("[role=alert]")).to_have_count(0)
        expect(page.locator("#company-brief-start")).to_have_text("Get a company brief")
        ok, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert ok, f"horizontal scroll: {scroll_width} > {inner_width}"
        assert_no_serious_violations(page)


@pytest.mark.parametrize(
    "running_app", [AppStart(setup="complete", allow_fakes=False)], indirect=True
)
def test_no_search_provider_state_at_390x844(running_app: RunningApp, browser: Browser):
    _aid, cid = seeded(running_app)
    with new_page(browser, PHONE) as page:
        open_app(page, running_app, f"/companies/{cid}")
        wait_for_settled_styles(page)
        brief = page.locator("#company-brief")
        expect(brief).to_contain_text("needs a search provider")
        expect(brief.get_by_role("link", name="Settings")).to_have_attribute(
            "href", "/settings#providers"
        )
        expect(page.locator("#company-brief-start")).to_have_count(0)
        ok, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert ok, f"horizontal scroll: {scroll_width} > {inner_width}"
        assert_no_serious_violations(page)


def test_the_unchecked_line_and_new_tab_links_show_on_a_phone(
    running_app: RunningApp, browser: Browser
):
    _aid, cid = seeded(running_app)
    save_brief(running_app, cid)
    with new_page(browser, PHONE) as page:
        open_app(page, running_app, f"/companies/{cid}")
        wait_for_settled_styles(page)
        expect(page.locator("#company-brief-unchecked")).to_contain_text("not checked against")
        expect(page.locator("#company-brief-sent")).to_contain_text("are sent to the AI")
        link = page.locator("#company-brief [data-brief-section] a").first
        expect(link).to_have_attribute("target", "_blank")
        expect(link).to_contain_text("https://example.test/")
        expect(page.locator("#company-brief h3.brief-section-label").first).to_be_visible()
        assert has_no_horizontal_scroll(page)[0]
        assert_no_serious_violations(page)
