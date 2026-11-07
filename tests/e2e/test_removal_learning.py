"""Keyboard rule acceptance and reversal, plus the phone-width Hidden view."""

import pytest
from playwright.sync_api import expect

from app.modules.storage import Database
from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_facts_page import press_by_keyboard, tab_until
from tests.e2e.test_inbox_organisation import rows, seed
from tests.e2e.test_shell import has_no_horizontal_scroll, open_app

pytestmark = pytest.mark.e2e


def seed_removals(app):
    seed(app, count=5)
    with Database.open(app.data_dir / "app.db") as db, db.transaction() as conn:
        conn.execute("UPDATE jobs SET work_mode='on_site' WHERE id!='job-0005'")
        conn.execute(
            "UPDATE jobs SET triage_state='removed',removal_reason='location',version=2 "
            "WHERE id IN ('job-0001','job-0002','job-0003')"
        )


def test_ac_m9_1_accept_flag_then_switch_rule_off_by_keyboard(running_app, browser):
    seed_removals(running_app)
    with browser.new_context(viewport=DESKTOP) as context:
        page = context.new_page()
        open_app(page, running_app, "/inbox?selected=job-0004")
        press_by_keyboard(page, page.get_by_role("button", name="Flag them", exact=True))
        expect(page.get_by_text("Matches your rule: Work mode: Onsite")).to_be_visible()
        expect(page.locator("#inbox-card-heading")).to_be_focused()
        assert rows(running_app, "SELECT mode,active FROM removal_rules") == [("flag", 1)]

        open_app(page, running_app, "/settings/preferences")
        section = page.locator("#removal-rules")
        threshold = section.get_by_label("Minimum matching removals for a suggestion")
        tab_until(page, threshold)
        page.keyboard.press("ControlOrMeta+A")
        page.keyboard.type("8")
        press_by_keyboard(page, section.locator("summary", has_text="Work mode: Onsite"))
        toggle = section.get_by_role("checkbox", name="Rule is on")
        tab_until(page, toggle)
        page.keyboard.press("Space")
        press_by_keyboard(page, section.get_by_role("button", name="Save rule", exact=True))
        expect(section.locator("summary", has_text="Work mode: Onsite")).to_be_focused()
        expect(threshold).to_have_value("8")
        expect(section.get_by_text("Rule saved.", exact=True)).to_be_visible()
        assert rows(running_app, "SELECT mode,active FROM removal_rules") == [("flag", 0)]
        press_by_keyboard(page, section.get_by_role("button", name="Delete rule", exact=True))
        dialog = page.get_by_role("dialog", name="Delete removal rule?")
        expect(dialog.get_by_role("button", name="Keep rule")).to_be_focused()
        page.keyboard.press("Escape")
        expect(dialog).not_to_be_visible()
        assert rows(running_app, "SELECT mode,active FROM removal_rules") == [("flag", 0)]


def test_hidden_by_rules_view_phone_390x844_and_axe(running_app, browser):
    seed_removals(running_app)
    with browser.new_context(viewport=PHONE) as context:
        page = context.new_page()
        open_app(page, running_app, "/inbox")
        press_by_keyboard(page, page.get_by_role("button", name="Hide them", exact=True))
        hidden = page.get_by_role("link", name="Hidden by your rules (1)", exact=True)
        expect(hidden).to_be_visible()
        assert_no_serious_violations(page)
        press_by_keyboard(page, hidden)
        expect(page.locator("#inbox-card-heading")).to_contain_text("fixture0004")
        expect(page.locator("#inbox-card-heading")).to_be_focused()
        assert_no_serious_violations(page)
        assert has_no_horizontal_scroll(page)[0]
