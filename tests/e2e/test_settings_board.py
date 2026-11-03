"""Settings > Board in a real browser (M5.7; AC-M5.1; ADR-102 item 10): statuses (add with a phase,
rename, move up and down, delete) and reminder rules per status, by keyboard only; a refused add
with no phase, a refused number of days, and a tab that is behind; the section at 390x844 with no
sideways scroll, and an axe scan at desktop and phone size in light and dark.

The app starts with a completed setup (``running_app`` default). Selectors and messages follow
``tests/settings_board_pages.py`` and ``tests/integration/test_settings_board.py``. No fixed waits;
every value is fictional.
"""

import re
from typing import Any

import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_board import rows
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, wait_said
from tests.e2e.test_setup_provider_test import is_focused
from tests.e2e.test_shell import (
    assert_target_size,
    has_no_horizontal_scroll,
    new_page,
    open_app,
)
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e

SECTION = "#board-settings"
ADD_FORM = "#status-add-form"
ADD_LABEL = f'{ADD_FORM} input[name="label"]'
ADD_PHASE = f'{ADD_FORM} select[name="phase"]'
ADD_BUTTON = f'{ADD_FORM} button[type="submit"]'
ROWS = "#status-list li[data-status-key]"
STATUSES_HEADING = "#board-statuses-heading"
# The Settings page has many tab stops before and inside this section.
MAX_TABS = 400
FOCUS_INSIDE = """(id) => {
  const element = document.getElementById(id);
  return !!element && element.contains(document.activeElement);
}"""
FITS = """(selector) => {
  const box = document.querySelector(selector);
  return box.scrollWidth <= box.clientWidth;
}"""
ONE_FIELD = "1 field needs a change."


def row(key: str) -> str:
    return f"#status-{key}"


def rule(key: str) -> str:
    return f"#reminder-{key}"


def tab_to(page: Page, locator: Locator) -> None:
    """Press Tab until ``locator`` has focus, unless it has it already (keyboard only)."""
    expect(locator).to_have_count(1)
    for _ in range(MAX_TABS + 1):
        if is_focused(locator):
            return
        page.keyboard.press("Tab")
    raise AssertionError(f"{locator} never received focus within {MAX_TABS} Tab presses")


def replace_text(page: Page, text: str) -> None:
    page.keyboard.press("ControlOrMeta+a")
    page.keyboard.type(text)


def focus_inside(page: Page, element_id: str) -> None:
    page.wait_for_function(FOCUS_INSIDE, arg=element_id)


def status_keys(page: Page) -> list[str]:
    keys: list[str] = page.evaluate(
        "(rows) => [...document.querySelectorAll(rows)].map((li) => li.dataset.statusKey)", ROWS
    )
    return keys


def db_keys(app: RunningApp, phase: str) -> list[str]:
    found = rows(
        app, "SELECT key FROM application_statuses WHERE phase=? ORDER BY position, key", (phase,)
    )
    return [str(key) for (key,) in found]


def label_in_db(app: RunningApp, label: str) -> list[tuple[Any, ...]]:
    return rows(
        app,
        "SELECT key, phase, built_in, version FROM application_statuses WHERE label=?",
        (label,),
    )


def open_settings(page: Page, app: RunningApp, statuses: int = 9) -> None:
    page.add_init_script(RECORD_ANNOUNCEMENTS)
    open_app(page, app, "/settings/lists")
    section = page.locator(SECTION)
    expect(section.get_by_role("heading", name="Board", level=2, exact=True)).to_be_visible()
    expect(page.locator(ROWS)).to_have_count(statuses)


def assert_fits(page: Page) -> None:
    fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
    assert fits, f"scrollWidth {scroll_width} > {inner_width}"
    assert page.evaluate(FITS, SECTION), "the Board section fits its own width"


def test_settings_board_statuses_and_rules_by_keyboard_phone_axe(
    running_app: RunningApp, browser: Browser
) -> None:
    app = running_app
    with new_page(browser, DESKTOP) as page:
        open_settings(page, app)
        section = page.locator(SECTION)
        wait_for_settled_styles(page)
        assert_no_serious_violations(page)
        # Built-in statuses can be renamed and moved, never deleted; custom ones can.
        expect(page.locator(f'{row("applied")} form[action$="/delete"]')).to_have_count(0)
        expect(page.locator(f'{row("applied")} form[action$="/rename"]')).to_have_count(1)
        expect(
            page.locator(rule("applied")).get_by_label(re.compile("^Remind me if"))
        ).to_be_checked()
        expect(page.locator(f'{rule("applied")} input[name="days"]')).to_have_value("14")

        # --- add: a name with no phase is refused, by keyboard ---------------------------------
        label = page.locator(ADD_LABEL)
        tab_to(page, label)
        page.keyboard.type("Waiting for reply")
        page.keyboard.press("Enter")
        phase = page.locator(ADD_PHASE)
        expect(phase).to_have_attribute("aria-invalid", "true")
        expect(phase).to_be_focused()  # focus goes to the first invalid field
        wait_said(page, f"Status not added. {ONE_FIELD}")
        expect(section.locator(f"{ADD_FORM} [aria-invalid='true']")).to_have_count(1)
        errors = (phase.get_attribute("aria-describedby") or "").split()
        assert any("Active" in page.locator(f"[id='{i}']").inner_text() for i in errors), (
            "the error says what to choose"
        )
        expect(page.locator(ADD_LABEL)).to_have_value("Waiting for reply")  # the typing stays
        assert label_in_db(app, "Waiting for reply") == []
        expect(page.locator(ROWS)).to_have_count(9)

        # --- add: choose the phase by keyboard and press Add status -------------------------
        page.keyboard.type("Active")
        expect(page.locator(ADD_PHASE)).to_have_value("active")
        tab_to(page, page.locator(ADD_BUTTON))
        page.keyboard.press("Enter")
        wait_said(page, "Added the status Waiting for reply.")
        expect(page.locator(ROWS)).to_have_count(10)
        [(key, phase_name, built_in, _version)] = label_in_db(app, "Waiting for reply")
        assert (phase_name, built_in) == ("active", 0)
        assert status_keys(page)[5] == key, "last of the active statuses, before the closed ones"
        expect(page.locator(row(key))).to_contain_text("Active")
        focus_inside(page, f"status-{key}")
        expect(page.locator("#board-settings-done")).to_have_text(
            "Added the status Waiting for reply."
        )
        expect(page.locator(f'{row(key)} form[action$="/delete"]')).to_have_count(1)

        # --- rename by keyboard --------------------------------------------------------------
        name = page.locator(f'{row(key)} input[name="label"]')
        tab_to(page, name)
        replace_text(page, "Reply pending")
        page.keyboard.press("Enter")
        wait_said(page, "Renamed to Reply pending.")
        assert label_in_db(app, "Reply pending")[0][0] == key
        focus_inside(page, f"status-{key}")
        expect(page.locator(f'{row(key)} input[name="label"]')).to_have_value("Reply pending")

        # --- move up, then down, by keyboard -------------------------------------------------
        up = page.locator(f'{row(key)} button[name="direction"][value="up"]')
        tab_to(page, up)
        page.keyboard.press("Enter")
        wait_said(page, "Moved Reply pending up.")
        assert db_keys(app, "active") == ["applied", "screening", "interviewing", key, "offer"]
        assert status_keys(page)[1:6] == ["applied", "screening", "interviewing", key, "offer"]
        focus_inside(page, f"status-{key}")
        down = page.locator(f'{row(key)} button[name="direction"][value="down"]')
        tab_to(page, down)
        page.keyboard.press("Enter")
        wait_said(page, "Moved Reply pending down.")
        assert db_keys(app, "active")[-1] == key

        # --- a reminder rule: refused days, then saved, by keyboard ---------------------------
        enabled = page.locator(f'{rule(key)} input[name="enabled"]')
        days = page.locator(f'{rule(key)} input[name="days"]')
        expect(enabled).not_to_be_checked()
        tab_to(page, enabled)
        page.keyboard.press("Space")
        expect(enabled).to_be_checked()
        page.keyboard.press("Tab")
        expect(days).to_be_focused()
        replace_text(page, "0")
        page.keyboard.press("Enter")
        wait_said(page, f"Reminder for Reply pending not saved. {ONE_FIELD}")
        expect(days).to_have_attribute("aria-invalid", "true")
        expect(days).to_be_focused()
        expect(days).to_have_value("0")
        described = (days.get_attribute("aria-describedby") or "").split()
        assert any(
            page.locator(f"[id='{i}']").inner_text()
            == "Enter a whole number of days from 1 to 365."
            for i in described
        )
        assert rows(app, "SELECT count(*) FROM reminder_rules WHERE status_key=?", (key,)) == [(0,)]
        replace_text(page, "10")
        page.keyboard.press("Enter")
        wait_said(page, "Reminder for Reply pending saved.")
        focus_inside(page, f"reminder-{key}")
        assert rows(
            app, "SELECT days, enabled, version FROM reminder_rules WHERE status_key=?", (key,)
        ) == [(10, 1, 1)]
        page.reload()
        page.wait_for_function("() => typeof window.htmx !== 'undefined'")
        expect(page.locator(f'{rule(key)} input[name="enabled"]')).to_be_checked()
        expect(page.locator(f'{rule(key)} input[name="days"]')).to_have_value("10")

        # --- a tab that is behind is told to reload and saves nothing -------------------------
        with new_page(browser, DESKTOP) as other:
            open_settings(other, app, statuses=10)  # the custom status exists by now
            tab_to(page, page.locator(f'{row(key)} input[name="label"]'))
            replace_text(page, "Reply awaited")
            page.keyboard.press("Enter")
            wait_said(page, "Renamed to Reply awaited.")
            stale = other.locator(f'{row(key)} input[name="label"]')
            stale.fill("Lost change")
            stale.press("Enter")
            prompt = other.locator(SECTION).get_by_role("alert")
            expect(prompt).to_be_visible()
            expect(prompt).to_contain_text("changed elsewhere")
            expect(prompt.get_by_role("link", name=re.compile("Reload"))).to_have_attribute(
                "href", "/settings/lists"
            )
            assert label_in_db(app, "Lost change") == []
            assert label_in_db(app, "Reply awaited")[0][0] == key

        # --- delete a custom status by keyboard (its rule goes with it) -------------------------
        delete = page.locator(f'{row(key)} form[action$="/delete"] button')
        tab_to(page, delete)
        page.keyboard.press("Enter")
        wait_said(page, "Deleted the status Reply awaited.")
        expect(page.locator(row(key))).to_have_count(0)
        expect(page.locator(ROWS)).to_have_count(9)
        expect(page.locator(STATUSES_HEADING)).to_be_focused()
        assert label_in_db(app, "Reply awaited") == []
        assert rows(app, "SELECT count(*) FROM reminder_rules WHERE status_key=?", (key,)) == [(0,)]
        assert rows(app, "SELECT count(*) FROM reminder_rules") == [(1,)]  # Applied's, untouched
        assert_no_serious_violations(page)

    # --- phone width, light and dark: no sideways scroll, targets, errors showing, axe ---------
    for scheme in ("light", "dark"):
        with new_page(browser, PHONE, color_scheme=scheme) as phone:
            open_settings(phone, app)
            wait_for_settled_styles(phone)
            assert_fits(phone)
            assert_no_serious_violations(phone)
            add = phone.locator(ADD_BUTTON)
            add.scroll_into_view_if_needed()
            assert_target_size(add, "Add status")
            first_up = phone.locator(f'{row("applied")} button[name="direction"][value="up"]')
            first_up.scroll_into_view_if_needed()
            assert_target_size(first_up, "Move up")
            assert_target_size(phone.locator(f'{rule("applied")} input[name="days"]'), "days")

            # Every error at once: the add form with nothing, and a rule with days out of range.
            add.click()
            expect(phone.locator(f"{ADD_FORM} [aria-invalid='true']")).to_have_count(2)
            days = phone.locator(f'{rule("applied")} input[name="days"]')
            days.fill("999")
            phone.locator(f'{rule("applied")} button[type="submit"]').click()
            expect(phone.locator(f'{rule("applied")} [aria-invalid="true"]')).to_have_count(1)
            wait_for_settled_styles(phone)
            assert_fits(phone)
            assert_no_serious_violations(phone)
            assert rows(app, "SELECT days FROM reminder_rules WHERE status_key='applied'") == [
                (14,)
            ]
