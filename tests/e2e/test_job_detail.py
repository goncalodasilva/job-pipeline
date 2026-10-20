"""The M4.8 drawer, exact fact wording, local saves and duplicate separation."""

import json

import pytest
from playwright.sync_api import expect

from app.modules.storage import Database
from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_fact_refine_panel import seed_fact
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, press_by_keyboard, tab_until
from tests.e2e.test_inbox import seed, state
from tests.e2e.test_shell import assert_target_size, has_no_horizontal_scroll, open_app
from tests.waiting import wait_until

pytestmark = pytest.mark.e2e


def open_drawer(page, app):
    open_app(page, app, "/inbox?selected=job-0003")
    wait_until(lambda: state(app, "job-0003") == "seen", timeout=10, message="seen")
    tab_until(page, page.locator("#inbox-card-heading"))
    page.keyboard.press("Enter")
    panel = page.get_by_role("dialog", name="Job details", exact=True)
    expect(panel).to_be_visible()
    return panel


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_drawer_focus_trap_and_esc_returns_focus(running_app, browser, viewport, scheme):
    seed(running_app)
    with browser.new_context(viewport=viewport, color_scheme=scheme) as context:
        page = context.new_page()
        panel = open_drawer(page, running_app)
        close = panel.get_by_role("button", name="Close", exact=True)
        expect(close).to_be_focused()
        for _ in range(85):
            page.keyboard.press("Tab")
            assert panel.evaluate("el => el.contains(document.activeElement)")
        for _ in range(3):
            page.keyboard.press("Shift+Tab")
            assert panel.evaluate("el => el.contains(document.activeElement)")
        assert_no_serious_violations(page)
        assert has_no_horizontal_scroll(page)[0]
        assert panel.evaluate("el => el.scrollWidth <= el.clientWidth")
        for button in panel.get_by_role("button").all():
            assert_target_size(button, button.inner_text())
        page.keyboard.press("Escape")
        expect(panel).not_to_be_visible()
        expect(page.locator("#inbox-card-heading")).to_be_focused()


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE])
def test_snapshot_shown_with_capture_date_and_notes_saved(running_app, browser, viewport):
    seed(running_app)
    with browser.new_context(viewport=viewport) as context:
        page = context.new_page()
        panel = open_drawer(page, running_app)
        expect(panel.locator("time")).to_have_text("2026-10-20")
        expect(panel.locator(".posting-text")).to_contain_text("Maintain irrigation systems.")
        tab_until(page, panel.get_by_label("Notes", exact=True))
        page.keyboard.type("Ask about the fictional greenhouse team.")
        press_by_keyboard(page, panel.get_by_role("button", name="Save fields and notes"))
        expect(panel.locator("#job-edit-heading")).to_be_focused()
        expect(panel.locator("#job-edit")).to_contain_text("Saved.")
        page.keyboard.press("Escape")
        panel = open_drawer(page, running_app)
        expect(panel.get_by_label("Notes", exact=True)).to_have_value(
            "Ask about the fictional greenhouse team."
        )


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE])
def test_ac_m4_9_edit_field_then_resummarize_keeps_it(running_app, browser, viewport):
    seed(running_app)
    with browser.new_context(viewport=viewport) as context:
        page = context.new_page()
        panel = open_drawer(page, running_app)
        title = panel.get_by_label("Title", exact=True)
        tab_until(page, title)
        page.keyboard.press("ControlOrMeta+A")
        page.keyboard.type("Fictional principal gardener")
        press_by_keyboard(page, panel.get_by_role("button", name="Save fields and notes"))
        expect(panel.locator("#job-edit")).to_contain_text("Corrected by you")
        press_by_keyboard(page, panel.get_by_role("button", name="Summarize", exact=True))
        expect(panel.get_by_role("button", name="Summarize again", exact=True)).to_be_visible()
        expect(panel).to_contain_text("Practice data (fake provider)")
        page.keyboard.press("Escape")
        panel = open_drawer(page, running_app)
        expect(panel.get_by_label("Title", exact=False).first).to_have_value(
            "Fictional principal gardener"
        )


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE])
def test_ac_m4_3_fit_explained_links_to_facts(running_app, browser, viewport):
    seed(running_app)
    with browser.new_context(viewport=viewport) as context:
        page = context.new_page()
        wording = "Maintained fictional greenhouse irrigation systems."
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        fact_path = seed_fact(page, running_app, wording)
        with Database.open(running_app.data_dir / "app.db") as db, db.transaction() as conn:
            number, version = conn.execute(
                "SELECT f.number,v.version FROM facts f "
                "JOIN fact_versions v ON v.id=f.current_version_id"
            ).fetchone()
            ref = f"F{number}@{version}"
            conn.execute(
                "INSERT INTO job_summaries (id,job_id,summary,fields_json,provider,model,"
                "prompt_id,prompt_version,created_at) VALUES ('detail-summary','job-0003',"
                "'A fictional role.','{}','fake','fake-fast','summarize_job','1','2026-10-20')"
            )
            conn.execute(
                "INSERT INTO job_requirements (id,summary_id,position,kind,text) "
                "VALUES ('req-match','detail-summary',1,'key','Maintain irrigation')"
            )
            conn.execute(
                "INSERT INTO job_requirements (id,summary_id,position,kind,text) "
                "VALUES ('req-gap','detail-summary',2,'key','Train imaginary butterflies')"
            )
            payload = {
                "requirements": [{"requirement_id": "req-match", "met": True, "fact_ids": [ref]}]
            }
            conn.execute(
                "INSERT INTO job_assessments (id,job_id,summary_id,result_json,fit_band,"
                "deal_breaker,fact_refs_json,provider,model,prompt_id,prompt_version,created_at) "
                "VALUES ('detail-assessment','job-0003','detail-summary',?,'possible',0,?,"
                "'fake','fake-fast','assess_fit','1','2026-10-20')",
                (json.dumps(payload), json.dumps([ref])),
            )
        panel = open_drawer(page, running_app)
        link = panel.get_by_role("link", name=wording, exact=True)
        expect(link).to_have_attribute("href", fact_path)
        expect(panel.locator(".job-matches")).to_contain_text("Missing")
        expect(panel).to_contain_text("Practice data")
        press_by_keyboard(page, link)
        expect(page).to_have_url(running_app.base_url + fact_path)


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE])
def test_ac_m4_5_not_a_duplicate_from_sources(running_app, browser, viewport):
    seed(running_app)
    with Database.open(running_app.data_dir / "app.db") as db, db.transaction() as conn:
        conn.execute("UPDATE jobs SET merged_into_job_id='job-0003' WHERE id='job-0002'")
        conn.execute(
            "INSERT INTO job_sources (id,job_id,kind,found_at,created_at) "
            "VALUES ('detail-source','job-0002','pasted','2026-10-20','2026-10-20')"
        )
    with browser.new_context(viewport=viewport) as context:
        page = context.new_page()
        panel = open_drawer(page, running_app)
        press_by_keyboard(page, panel.get_by_role("button", name="Not a duplicate", exact=True))
        expect(panel.locator("#job-sources")).to_contain_text("Job restored as its own card.")
        expect(panel.locator("#job-sources-heading")).to_be_focused()
        page.keyboard.press("Escape")
        expect(page.locator("#inbox-queue")).to_contain_text("fixture0002")


def test_drawer_triage_keeps_typed_notes_and_save_version(running_app, browser):
    seed(running_app)
    with browser.new_context(viewport=PHONE) as context:
        page = context.new_page()
        panel = open_drawer(page, running_app)
        panel.get_by_label("Notes", exact=True).fill("A fictional note kept during triage.")
        press_by_keyboard(page, panel.get_by_role("button", name="Apply", exact=True))
        expect(panel.locator("#job-detail-header")).to_contain_text("To apply")
        press_by_keyboard(page, panel.get_by_role("button", name="Save fields and notes"))
        expect(panel.locator("#job-edit")).to_contain_text("Saved.")
        expect(panel.get_by_label("Notes", exact=True)).to_have_value(
            "A fictional note kept during triage."
        )
        with page.expect_response(lambda response: "/inbox/queue?" in response.url):
            page.keyboard.press("Escape")
        expect(page.locator("#inbox-card")).to_contain_text("fixture0002")
        expect(page.locator("#inbox-card-heading")).to_be_focused()


def test_budget_warning_above_drawer_returns_focus_and_keeps_fields(running_app, browser):
    from app.domain.budgets import Budget
    from tests.e2e.test_budget_warning_dialog import seed as seed_budget

    seed(running_app)
    seed_budget(
        running_app, Budget(allowance=4, first_warning=1, second_warning=2, block_at=3, reset_day=1)
    )
    with browser.new_context(viewport=PHONE) as context:
        page = context.new_page()
        panel = open_drawer(page, running_app)
        panel.get_by_label("Notes", exact=True).fill("Fictional unsaved note.")
        button = panel.get_by_role("button", name="Summarize", exact=True)
        press_by_keyboard(page, button)
        warning = panel.get_by_role("dialog", name="First warning for Fake provider", exact=True)
        expect(warning).to_be_visible()
        expect(warning.get_by_role("button", name="Not now", exact=True)).to_be_focused()
        page.keyboard.press("Escape")
        expect(button).to_be_focused()
        expect(panel.get_by_label("Notes", exact=True)).to_have_value("Fictional unsaved note.")
        expect(panel).to_be_visible()
        assert_no_serious_violations(page)
        press_by_keyboard(page, button)
        expect(warning).to_be_visible()
        press_by_keyboard(page, warning.get_by_role("button", name="Continue", exact=True))
        expect(panel.get_by_role("button", name="Summarize again", exact=True)).to_be_visible()
        expect(panel.get_by_label("Notes", exact=True)).to_have_value("Fictional unsaved note.")


def test_unsaved_drawer_fields_restored_on_escape_and_close(running_app, browser):
    seed(running_app)
    with browser.new_context(viewport=PHONE) as context:
        page = context.new_page()
        panel = open_drawer(page, running_app)
        panel.get_by_label("Notes", exact=True).fill("Unsaved fictional note.")
        panel.get_by_label("Title", exact=True).fill("Unsaved fictional title")
        version = panel.locator("#job-edit input[name='version']").input_value()
        page.keyboard.press("Escape")
        press_by_keyboard(page, page.locator("#inbox-card-heading"))
        expect(panel.get_by_label("Notes", exact=True)).to_have_value("Unsaved fictional note.")
        expect(panel.get_by_label("Title", exact=True)).to_have_value("Unsaved fictional title")
        expect(panel.locator("#job-edit-draft")).to_be_visible()
        assert panel.locator("#job-edit input[name='version']").input_value() == version
        press_by_keyboard(page, panel.get_by_role("button", name="Close", exact=True))
        press_by_keyboard(page, page.locator("#inbox-card-heading"))
        expect(panel.get_by_label("Notes", exact=True)).to_have_value("Unsaved fictional note.")
        press_by_keyboard(page, panel.get_by_role("button", name="Save fields and notes"))
        expect(panel.locator("#job-edit")).to_contain_text("Saved.")
        expect(panel.locator("#job-edit-draft")).not_to_be_visible()


def test_delayed_header_refresh_preserves_focus(running_app, browser):
    seed(running_app)
    with browser.new_context(viewport=PHONE) as context:
        page = context.new_page()
        panel = open_drawer(page, running_app)
        held = []
        page.route("**/inbox/jobs/job-0003/header", lambda route: held.append(route))
        panel.get_by_label("Notes", exact=True).focus()
        page.evaluate("document.body.dispatchEvent(new CustomEvent('tasks:updated'))")
        wait_until(lambda: page.evaluate("true") and held, timeout=5, message="held header refresh")
        apply = panel.get_by_role("button", name="Apply", exact=True)
        apply.focus()
        with page.expect_response(lambda response: response.url.endswith("/header")):
            held.pop().continue_()
        expect(apply).to_be_focused()


def test_drawer_removal_reason_and_snooze_choices(running_app, browser):
    seed(running_app)
    with browser.new_context(viewport=PHONE) as context:
        page = context.new_page()
        panel = open_drawer(page, running_app)
        press_by_keyboard(page, panel.get_by_role("button", name="Remove", exact=True))
        remove = panel.get_by_role("dialog", name="Remove job", exact=True)
        expect(remove).to_be_visible()
        press_by_keyboard(page, remove.get_by_role("button", name="Other", exact=True))
        expect(remove.get_by_label("Reason (optional)")).to_be_focused()
        page.keyboard.type("Fictional reason")
        page.keyboard.press("Enter")
        expect(panel.locator("#job-detail-header")).to_contain_text("Removed")
        with Database.open(running_app.data_dir / "app.db") as db:
            assert db.connection.execute(
                "SELECT removal_reason,removal_text FROM jobs WHERE id='job-0003'"
            ).fetchone() == ("other", "Fictional reason")
        press_by_keyboard(page, panel.get_by_role("button", name="Restore", exact=True))
        press_by_keyboard(page, panel.locator("#job-detail-header summary"))
        for label in ("Tomorrow", "Next week", "In a month", "Snooze until that date"):
            expect(panel.get_by_role("button", name=label, exact=True)).to_be_visible()
        date = panel.get_by_label("Or pick a date")
        date.fill("2099-01-05")
        date.press("Enter")
        expect(panel.locator("#job-detail-header")).to_contain_text("Snoozed")
        assert state(running_app, "job-0003") == "snoozed"
        assert_no_serious_violations(page)


def test_new_job_page_marks_seen_by_post_before_snooze(running_app, browser):
    seed(running_app)
    with browser.new_context(viewport=PHONE) as context:
        page = context.new_page()
        open_app(page, running_app, "/jobs/job-0003")
        assert state(running_app, "job-0003") == "new"
        press_by_keyboard(page, page.locator("#job-detail-header summary"))
        press_by_keyboard(page, page.get_by_role("button", name="Mark seen", exact=True))
        expect(page.locator("#job-detail-header")).to_contain_text("Seen")
        press_by_keyboard(page, page.locator("#job-detail-header summary"))
        press_by_keyboard(page, page.get_by_role("button", name="Next week", exact=True))
        expect(page.locator("#job-detail-header")).to_contain_text("Snoozed")


def test_refused_edit_survives_closing_and_external_change_stays_stale(running_app, browser):
    seed(running_app)
    with browser.new_context(viewport=PHONE) as context:
        page = context.new_page()
        panel = open_drawer(page, running_app)
        panel.get_by_label("Title", exact=True).fill("")
        panel.get_by_label("Notes", exact=True).fill("Fictional unsaved refused note.")
        press_by_keyboard(page, panel.get_by_role("button", name="Save fields and notes"))
        expect(panel.get_by_label("Title", exact=True)).to_have_attribute("aria-invalid", "true")
        page.keyboard.press("Escape")
        with Database.open(running_app.data_dir / "app.db") as db, db.transaction() as conn:
            conn.execute(
                "UPDATE jobs SET title='Concurrent fictional title',version=version+1 "
                "WHERE id='job-0003'"
            )
        press_by_keyboard(page, page.locator("#inbox-card-heading"))
        expect(panel.get_by_label("Notes", exact=True)).to_have_value(
            "Fictional unsaved refused note."
        )
        panel.get_by_label("Title", exact=True).fill("My proposed fictional title")
        press_by_keyboard(page, panel.get_by_role("button", name="Save fields and notes"))
        expect(panel.locator("#job-edit")).to_contain_text("Reload the page")
        with Database.open(running_app.data_dir / "app.db") as db:
            assert (
                db.connection.execute("SELECT title FROM jobs WHERE id='job-0003'").fetchone()[0]
                == "Concurrent fictional title"
            )


def test_reverting_restored_draft_clears_it(running_app, browser):
    seed(running_app)
    with browser.new_context(viewport=PHONE) as context:
        page = context.new_page()
        panel = open_drawer(page, running_app)
        panel.get_by_label("Notes", exact=True).fill("Fictional reverted draft.")
        page.keyboard.press("Escape")
        press_by_keyboard(page, page.locator("#inbox-card-heading"))
        expect(panel.get_by_label("Notes", exact=True)).to_have_value("Fictional reverted draft.")
        panel.get_by_label("Notes", exact=True).fill("")
        page.keyboard.press("Escape")
        press_by_keyboard(page, page.locator("#inbox-card-heading"))
        expect(panel.get_by_label("Notes", exact=True)).to_have_value("")
        expect(panel.locator("#job-edit-draft")).not_to_be_visible()


def test_success_after_drawer_closed_clears_draft(running_app, browser):
    seed(running_app)
    with browser.new_context(viewport=PHONE) as context:
        page = context.new_page()
        panel = open_drawer(page, running_app)
        held = []

        def hold(route):
            held.append((route, route.fetch()))

        page.route("**/inbox/jobs/job-0003/edit", hold)
        panel.get_by_label("Notes", exact=True).fill("Fictional saved late note.")
        press_by_keyboard(page, panel.get_by_role("button", name="Save fields and notes"))
        wait_until(lambda: page.evaluate("true") and held, timeout=5, message="held edit response")
        page.keyboard.press("Escape")
        expect(panel).not_to_be_visible()
        route, response = held.pop()
        with page.expect_response(lambda response: response.url.endswith("/edit")):
            route.fulfill(response=response)
        press_by_keyboard(page, page.locator("#inbox-card-heading"))
        expect(panel.get_by_label("Notes", exact=True)).to_have_value("Fictional saved late note.")
        expect(panel.locator("#job-edit-draft")).not_to_be_visible()
        page.unroute("**/inbox/jobs/job-0003/edit")
        panel.get_by_label("Notes", exact=True).fill("Fictional second save.")
        press_by_keyboard(page, panel.get_by_role("button", name="Save fields and notes"))
        expect(panel.locator("#job-edit")).to_contain_text("Saved.")
