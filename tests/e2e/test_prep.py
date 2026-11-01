"""Keyboard preparation and explicit web search on desktop and phone; offline CLI fakes."""

import pytest
from playwright.sync_api import Browser, expect

from app.domain.budgets import Budget
from app.domain.clock import SystemClock
from app.domain.fit import Requirement
from app.modules.storage import Database
from app.services.usage import UsageService
from app.wiring import local_timezone
from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_board import desk
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, press_by_keyboard, wait_said
from tests.e2e.test_shell import has_no_horizontal_scroll, new_page, open_app
from tests.fakes.ids import SequentialIdFactory
from tests.running_app import RunningApp
from tests.tailoring_pages import seed_summary

pytestmark = pytest.mark.e2e


def seeded(app):
    with desk(app) as seed:
        made = seed.application("prep-e2e", "Fictional Developer", "Example Orchard", "applied")
        seed_summary(
            seed.db,
            "prep-e2e",
            tuple(
                Requirement(f"prep-r{i}", "key", text)
                for i, text in enumerate(
                    (
                        "Python programming",
                        "Database administration",
                        "Cloud infrastructure",
                        "Machine learning",
                        "Customer support",
                        "Financial forecasting",
                        "Warehouse logistics",
                        "Garden cultivation",
                        "Translation editing",
                        "Network routing",
                        "Software testing",
                        "Public speaking",
                    )
                )
            ),
        )
    return made.id


def usage(app):
    db = Database.open(app.data_dir / "app.db")
    return db, UsageService(db, SystemClock(), SequentialIdFactory("prep-budget"), local_timezone())


def test_ac_m8_1_generate_and_show_more_by_keyboard_announced(
    running_app: RunningApp, browser: Browser
):
    aid = seeded(running_app)
    with new_page(browser, DESKTOP) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        open_app(page, running_app, f"/applications/{aid}?tab=prep")
        press_by_keyboard(page, page.locator("#prep-generate"))
        wait_said(page, "Question generation started.")
        expect(page.locator("#prep .prep-question").first).to_be_visible(timeout=20_000)
        expect(page.locator("#prep-heading")).to_be_focused()
        page.wait_for_function("() => !document.querySelector('#prep [aria-disabled=true]')")
        before = page.locator("#prep .prep-question").count()
        press_by_keyboard(page, page.locator("#prep-more"))
        wait_said(page, "Question generation started.")
        page.wait_for_function(
            "n => document.querySelectorAll('#prep .prep-question').length > n", arg=before
        )
        page.wait_for_function(
            "() => !document.querySelector("
            "'#prep.htmx-swapping, #prep.htmx-settling, #prep .htmx-request')"
        )
        press_by_keyboard(page, page.locator("#prep-regenerate"))
        dialog = page.get_by_role("dialog")
        expect(dialog).to_be_visible()
        expect(dialog.get_by_role("button", name="Not now", exact=True)).to_be_focused()
        page.keyboard.press("Escape")
        expect(page.locator("#prep-regenerate")).to_be_focused()
        # A passive refresh must preserve a typed bank search.
        query = page.locator("#prep-bank-query")
        query.focus()
        query.fill("typed query stays here")
        page.evaluate(
            "() => document.body.dispatchEvent(new CustomEvent('tasks:resync',{bubbles:true}))"
        )
        expect(page.locator("[data-stale-note]")).to_be_visible()
        expect(query).to_have_value("typed query stays here")
        assert_no_serious_violations(page)


def test_ac_m8_2_web_questions_dialog_and_blocked_state(running_app: RunningApp, browser: Browser):
    aid = seeded(running_app)
    db, service = usage(running_app)
    try:
        service.save_budget(
            "web_search", Budget(10, 1, 7, 10, 1), expected_version=service.load_budgets().version
        )
        with new_page(browser, PHONE) as page:
            open_app(page, running_app, f"/applications/{aid}?tab=prep")
            # A press before htmx has processed the form would submit it natively.
            page.wait_for_function(
                "() => !!document.querySelector('#prep-web-start').form['htmx-internal-data']"
            )
            press_by_keyboard(page, page.locator("#prep-web-start"))
            dialog = page.get_by_role("dialog")
            expect(dialog).to_be_visible()
            expect(dialog).to_contain_text("up to 5 searches")
            expect(dialog.get_by_role("button", name="Not now", exact=True)).to_be_focused()
            assert service.status("web_search").local_count == 0
            assert_no_serious_violations(page)
            page.keyboard.press("Escape")
            expect(page.locator("#prep-web-start")).to_be_focused()
            press_by_keyboard(page, page.locator("#prep-web-start"))
            press_by_keyboard(page, dialog.get_by_role("button", name="Continue", exact=True))
            expect(page.locator("#prep")).to_contain_text(
                "Reported online · example.test · found", timeout=20_000
            )
            assert service.status("web_search").local_count == 1
            for _ in range(6):
                record = service.reserve(
                    "web_search", tier="thorough", task_type="interview_web_questions"
                )
                service.complete(record, success=True, tokens=None)
            page.reload()
            expect(page.locator("#prep-web-start")).to_have_attribute("aria-disabled", "true")
            expect(page.locator("#prep-web-reason")).to_contain_text(
                "Monthly search limit reached. Resets on"
            )
            press_by_keyboard(page, page.locator("#prep-web-start"))
            assert service.status("web_search").local_count == 7
            expect(page.locator("#prep-generate")).not_to_have_attribute("aria-disabled", "true")
            assert has_no_horizontal_scroll(page)
            assert_no_serious_violations(page)
    finally:
        db.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone390x844"])
def test_prep_tab_phone_390x844_and_axe(running_app: RunningApp, browser: Browser, theme, viewport):
    aid = seeded(running_app)
    with new_page(browser, viewport) as page:
        page.emulate_media(color_scheme=theme)
        open_app(page, running_app, f"/applications/{aid}?tab=prep")
        wait_for_settled_styles(page)
        expect(page.get_by_role("heading", name="Interview prep", exact=True)).to_be_visible()
        assert has_no_horizontal_scroll(page)
        assert_no_serious_violations(page)
