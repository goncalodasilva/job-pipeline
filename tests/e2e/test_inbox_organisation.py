"""M4.9 values to map, saved views and grouping, by keyboard, at desktop and phone width."""

import pytest
from playwright.sync_api import expect

from app.modules.storage import Database
from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_facts_page import press_by_keyboard, tab_until
from tests.e2e.test_shell import assert_target_size, has_no_horizontal_scroll, open_app
from tests.job_fixtures import generate_jobs, seed_jobs

pytestmark = pytest.mark.e2e


def seed(app, count=25):
    with Database.open(app.data_dir / "app.db") as db:
        seed_jobs(db, generate_jobs(count))


def queue_unknown(app, value="Fictional cultivation"):
    with Database.open(app.data_dir / "app.db") as db, db.transaction() as conn:
        conn.execute(
            "INSERT INTO field_value_queue (id,field,value,created_at,updated_at) "
            "VALUES ('unknown-1','industry',?,'2026-10-20','2026-10-20')",
            (value,),
        )
        conn.execute("UPDATE jobs SET industry=? WHERE id IN ('job-0001','job-0002')", (value,))


def rows(app, sql, params=()):
    with Database.open(app.data_dir / "app.db") as db:
        return db.connection.execute(sql, params).fetchall()


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE])
def test_ac_m4_4_map_unknown_value_by_keyboard(running_app, browser, viewport):
    seed(running_app)
    queue_unknown(running_app)
    with browser.new_context(viewport=viewport) as context:
        page = context.new_page()
        open_app(page, running_app, "/inbox")
        press_by_keyboard(page, page.get_by_role("link", name="1 new value to map"))
        heading = page.get_by_role("heading", name="Values to map", exact=True)
        expect(heading).to_be_visible()
        section = page.locator(".value-choice")
        expect(section).to_contain_text("Fictional cultivation")
        expect(section).to_contain_text("Industry · 2 jobs")
        assert_no_serious_violations(page)
        assert has_no_horizontal_scroll(page)[0]
        select = page.get_by_label("Map Fictional cultivation to")
        tab_until(page, select)
        select.select_option(label="agriculture")
        press_by_keyboard(page, page.get_by_role("button", name="Map value", exact=True))
        expect(page.get_by_role("status")).to_contain_text("Mapped.")
        expect(page.get_by_text("No new values to map.")).to_be_visible()
        assert {
            r[0]
            for r in rows(
                running_app, "SELECT industry FROM jobs WHERE id IN ('job-0001','job-0002')"
            )
        } == {"agriculture"}
        aliases = rows(
            running_app,
            "SELECT aliases FROM controlled_values WHERE field='industry' AND value='agriculture'",
        )[0][0]
        assert "Fictional cultivation" in aliases
        assert rows(running_app, "SELECT status FROM field_value_queue")[0][0] == "mapped"
        assert_no_serious_violations(page)


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE])
def test_save_view_and_open_it_by_keyboard(running_app, browser, viewport):
    seed(running_app)
    with browser.new_context(viewport=viewport) as context:
        page = context.new_page()
        open_app(page, running_app, "/inbox?work_mode=remote&group_by=company")
        saved = page.locator("#saved-views")
        press_by_keyboard(page, saved.locator("summary", has_text="Saved views"))
        name = page.get_by_label("Name this view")
        tab_until(page, name)
        name.fill("Remote by company")
        press_by_keyboard(page, page.get_by_role("button", name="Save view", exact=True))
        expect(saved).to_contain_text("View saved.")
        link = saved.get_by_role("link", name="Remote by company")
        expect(link).to_be_visible()
        assert_no_serious_violations(page)
        assert has_no_horizontal_scroll(page)[0]
        for summary in page.locator("summary:visible").all():
            assert_target_size(summary, "summary")
        open_app(page, running_app, "/inbox")
        press_by_keyboard(page, saved.locator("summary", has_text="Saved views"))
        press_by_keyboard(page, saved.get_by_role("link", name="Remote by company"))
        page.wait_for_url("**work_mode=remote**")
        expect(page.locator(".queue-group").first).to_be_visible()
        # The saved address is a page: a reload keeps the filters and the grouping.
        page.reload()
        expect(page.locator(".queue-group").first).to_be_visible()
        assert page.url.split("?")[0].endswith("/inbox")
        press_by_keyboard(page, page.locator(".inbox-filters-panel summary"))
        expect(page.get_by_label("Work mode")).to_have_value("remote")
        # Rename, then delete and undo, each with the version that was shown.
        press_by_keyboard(page, saved.locator("summary", has_text="Saved views"))
        press_by_keyboard(page, saved.locator("summary", has_text="Manage Remote by company"))
        rename = page.get_by_label("Name for Remote by company")
        tab_until(page, rename)
        rename.fill("Remote jobs")
        press_by_keyboard(page, page.get_by_role("button", name="Rename view", exact=True))
        expect(saved).to_contain_text("View renamed.")
        assert rows(running_app, "SELECT name FROM saved_views")[0][0] == "Remote jobs"
        press_by_keyboard(page, saved.locator("summary", has_text="Manage Remote jobs"))
        press_by_keyboard(page, page.get_by_role("button", name="Delete view", exact=True))
        expect(saved).to_contain_text("View deleted.")
        assert not rows(running_app, "SELECT 1 FROM saved_views")
        press_by_keyboard(page, page.get_by_role("button", name="Undo delete of Remote jobs"))
        expect(saved.get_by_role("link", name="Remote jobs")).to_be_visible()
        assert rows(running_app, "SELECT name FROM saved_views")[0][0] == "Remote jobs"


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE])
def test_group_by_company_shows_counts(running_app, browser, viewport):
    seed(running_app)
    with browser.new_context(viewport=viewport) as context:
        page = context.new_page()
        open_app(page, running_app, "/inbox")
        press_by_keyboard(page, page.locator(".inbox-filters-panel summary"))
        group = page.get_by_label("Group by")
        tab_until(page, group)
        group.select_option("company")
        press_by_keyboard(page, page.get_by_role("button", name="Apply filters", exact=True))
        groups = page.locator(".queue-group")
        expect(groups).to_have_count(20)
        expect(groups.first.locator("h3")).to_contain_text("(2)")
        total = sum(
            int(h.split("(")[-1].rstrip(")")) for h in groups.locator("h3").all_inner_texts()
        )
        assert total == 25
        # The address is the page, not the fragment: a reload and Back still show the groups.
        assert page.url.split("?")[0].endswith("/inbox") and "group_by=company" in page.url
        page.reload()
        expect(page.locator(".queue-group")).to_have_count(20)
        assert_no_serious_violations(page)
        assert has_no_horizontal_scroll(page)[0]
        # Filters are applied, so the panel is open after the reload.
        assert_target_size(page.get_by_role("button", name="Apply filters", exact=True), "Apply")
        assert_target_size(page.get_by_role("link", name="Clear filters", exact=True), "Clear")
