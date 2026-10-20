"""M4.10 companies in a browser: the page from the drawer by keyboard, notes, the sortable list.

Assumed markup (see ``tests/company_pages.py`` and ``tests/integration/test_companies.py``):
the list is ``#companies`` holding ``#companies-table`` (each header cell holds a sort link and
carries ``aria-sort``); the page is ``#company-detail`` with ``<h1>``, the edit form in
``#company-edit`` (heading ``#company-edit-heading``, button "Save company", fields labelled
Name, Website, Industry, Size, Locations and Notes with ids ``company-edit-<field>``) and the
jobs in ``#company-roles``.
"""

import re

import pytest
from playwright.sync_api import expect

from app.modules.storage import Database
from tests.company_pages import seed_many
from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, press_by_keyboard, tab_until, wait_said
from tests.e2e.test_inbox import seed
from tests.e2e.test_job_detail import open_drawer
from tests.e2e.test_shell import assert_target_size, has_no_horizontal_scroll, open_app
from tests.job_fixtures import generate_jobs, seed_jobs

pytestmark = pytest.mark.e2e


def rows(app, sql, params=()):
    with Database.open(app.data_dir / "app.db") as db:
        return db.connection.execute(sql, params).fetchall()


def sort_link(page, column):
    return page.locator("#companies-table th").filter(has_text=column).get_by_role("link")


def first_row(page):
    return page.locator("#companies-table tbody tr").first


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE])
def test_company_page_from_job_drawer_by_keyboard(running_app, browser, viewport):
    seed(running_app)  # three jobs at "Fictional Orchard 1" to "Fictional Orchard 3"
    with browser.new_context(viewport=viewport) as context:
        page = context.new_page()
        panel = open_drawer(page, running_app)
        link = panel.get_by_role("link", name="Fictional Orchard 3", exact=True)
        expect(link).to_have_attribute("href", "/companies/company-0003")
        press_by_keyboard(page, link)
        expect(page).to_have_url(running_app.base_url + "/companies/company-0003")
        expect(page.get_by_role("heading", level=1, name="Fictional Orchard 3")).to_be_visible()
        expect(page.locator('nav a[href="/companies"][aria-current="page"]')).to_have_count(1)
        roles = page.locator("#company-roles")
        job = roles.get_by_role("link", name=re.compile("fixture0003"))
        expect(job).to_have_attribute("href", "/jobs/job-0003")
        expect(roles).to_contain_text("Seen")
        assert_no_serious_violations(page)
        assert has_no_horizontal_scroll(page)[0]
        # And on to the job's own page, still by keyboard.
        press_by_keyboard(page, job)
        expect(page).to_have_url(running_app.base_url + "/jobs/job-0003")
        expect(page.locator("#job-detail")).to_be_visible()
        # The card links to the company too.
        open_app(page, running_app, "/inbox?selected=job-0003")
        card_link = page.locator("#inbox-card").get_by_role("link", name="Fictional Orchard 3")
        expect(card_link).to_have_attribute("href", "/companies/company-0003")
        press_by_keyboard(page, card_link)
        expect(page).to_have_url(running_app.base_url + "/companies/company-0003")


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE])
def test_company_notes_saved(running_app, browser, viewport):
    seed(running_app)
    note = "<b>Ask</b> about the fictional greenhouse team & the fictional rota."
    with browser.new_context(viewport=viewport) as context:
        page = context.new_page()
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        open_app(page, running_app, "/companies/company-0001")
        notes = page.get_by_label("Notes", exact=True)
        tab_until(page, notes)
        page.keyboard.type(note)
        press_by_keyboard(page, page.get_by_role("button", name="Save company", exact=True))
        edit = page.locator("#company-edit")
        expect(edit).to_contain_text("Saved.")
        expect(page.locator("#company-edit-heading")).to_be_focused()
        wait_said(page, "Saved.")
        assert rows(running_app, "SELECT notes,version FROM companies WHERE id='company-0001'") == [
            (note, 2)
        ]
        assert_no_serious_violations(page)
        page.reload()
        expect(page.get_by_label("Notes", exact=True)).to_have_value(note)
        expect(page.locator("#company-detail b")).to_have_count(0)  # shown as text, not markup
        assert has_no_horizontal_scroll(page)[0]


def test_company_edit_error_and_industry_by_keyboard(running_app, browser):
    seed(running_app)
    with browser.new_context(viewport=PHONE) as context:
        page = context.new_page()
        open_app(page, running_app, "/companies/company-0001")
        name = page.get_by_label("Name", exact=True)
        tab_until(page, name)
        page.keyboard.press("ControlOrMeta+A")
        page.keyboard.type("fictional  ORCHARD 2")  # the name another company already uses
        press_by_keyboard(page, page.get_by_role("button", name="Save company", exact=True))
        expect(name).to_be_focused()
        expect(name).to_have_attribute("aria-invalid", "true")
        error = page.locator("#company-edit-name-error")
        expect(error).to_be_visible()
        expect(name).to_have_attribute("aria-describedby", "company-edit-name-error")
        assert_no_serious_violations(page)
        assert rows(running_app, "SELECT name FROM companies WHERE id='company-0001'") == [
            ("Fictional Orchard 1",)
        ]
        # Fix the name (the field has focus) and choose the industry from the controlled list.
        page.keyboard.press("ControlOrMeta+A")
        page.keyboard.type("Fictional Orchard One")
        industry = page.get_by_label("Industry", exact=True)
        tab_until(page, industry)
        industry.select_option(value="agriculture")
        press_by_keyboard(page, page.get_by_role("button", name="Save company", exact=True))
        expect(page.locator("#company-edit")).to_contain_text("Saved.")
        assert rows(running_app, "SELECT name,industry FROM companies WHERE id='company-0001'") == [
            ("Fictional Orchard One", "agriculture")
        ]


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_companies_list_phone_and_axe(running_app, browser, viewport, scheme):
    with Database.open(running_app.data_dir / "app.db") as db:
        seed_jobs(db, generate_jobs(25))  # 20 companies; the first five have two jobs
    with browser.new_context(viewport=viewport, color_scheme=scheme) as context:
        page = context.new_page()
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        open_app(page, running_app, "/companies")
        table = page.locator("#companies-table")
        expect(table).to_be_visible()
        expect(page.locator("#companies-table tbody tr")).to_have_count(20)
        assert_no_serious_violations(page)
        ok, scroll, inner = has_no_horizontal_scroll(page)
        assert ok, f"sideways scroll: page {scroll}px wide in a {inner}px window"
        for column in ("Name", "Open roles", "To apply", "Last activity"):
            assert_target_size(sort_link(page, column), column)

        # Sort by open roles with the keyboard: most first, then fewest first.
        link = sort_link(page, "Open roles")
        press_by_keyboard(page, link)
        expect(page).to_have_url(re.compile(r"sort=open_roles"))
        expect(first_row(page)).to_contain_text("Fictional Orchard 1")  # two jobs, name first
        expect(page.locator('#companies-table th[aria-sort="descending"]')).to_have_count(1)
        expect(sort_link(page, "Open roles")).to_be_focused()
        wait_said(page, "Sorted by Open roles, most first.")
        page.keyboard.press("Enter")
        expect(page.locator('#companies-table th[aria-sort="ascending"]')).to_have_count(1)
        expect(first_row(page)).to_contain_text("Fictional Orchard 10")  # one job, name first
        expect(sort_link(page, "Open roles")).to_be_focused()
        wait_said(page, "Sorted by Open roles, fewest first.")
        assert_no_serious_violations(page)
        assert has_no_horizontal_scroll(page)[0]

        # The sort survives a reload, and name order is not by code point.
        page.reload()
        expect(page.locator('#companies-table th[aria-sort="ascending"]')).to_have_count(1)
        press_by_keyboard(page, sort_link(page, "Name"))
        expect(page.locator('#companies-table th[aria-sort="ascending"]')).to_have_count(1)
        expect(first_row(page)).to_contain_text("Fictional Orchard 1")

        # A company opens from the list by keyboard.
        press_by_keyboard(page, page.get_by_role("link", name="Fictional Orchard 2", exact=True))
        expect(page).to_have_url(running_app.base_url + "/companies/company-0002")
        expect(page.get_by_role("heading", level=1, name="Fictional Orchard 2")).to_be_visible()
        assert_no_serious_violations(page)
        assert has_no_horizontal_scroll(page)[0]


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE])
def test_company_save_updates_the_website_link_and_summary(running_app, browser, viewport):
    seed(running_app)
    url = "https://example.test/careers"
    with browser.new_context(viewport=viewport) as context:
        page = context.new_page()
        open_app(page, running_app, "/companies/company-0001")
        header = page.locator("#company-header")
        expect(header.get_by_role("link")).to_have_count(0)
        website = page.get_by_label("Website", exact=True)
        tab_until(page, website)
        page.keyboard.type(url)
        size = page.get_by_label("Size", exact=True)
        tab_until(page, size)
        page.keyboard.type("11-50")
        press_by_keyboard(page, page.get_by_role("button", name="Save company", exact=True))
        expect(page.locator("#company-edit")).to_contain_text("Saved.")
        expect(header.get_by_role("link", name=url)).to_have_attribute("href", url)
        expect(page.locator("#company-summary")).to_have_text("Size: 11-50")
        assert has_no_horizontal_scroll(page)[0]
        # Clearing the website removes the link without a reload.
        page.get_by_label("Website", exact=True).fill("")
        press_by_keyboard(page, page.get_by_role("button", name="Save company", exact=True))
        expect(page.locator("#company-edit")).to_contain_text("Saved.")
        expect(header.get_by_role("link")).to_have_count(0)
        assert_no_serious_violations(page)


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE])
def test_companies_paging_by_keyboard_announces_and_keeps_focus_on_the_table(
    running_app, browser, viewport
):
    with Database.open(running_app.data_dir / "app.db") as db:
        seed_many(db, 60)
    with browser.new_context(viewport=viewport) as context:
        page = context.new_page()
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        open_app(page, running_app, "/companies")
        expect(page.locator("#companies-table tbody tr")).to_have_count(50)
        press_by_keyboard(page, page.get_by_role("link", name="Next page", exact=True))
        expect(page).to_have_url(re.compile(r"/companies\?sort=name&dir=asc&page=2$"))
        expect(page.locator("#companies-table tbody tr")).to_have_count(10)
        expect(first_row(page)).to_contain_text("Fictional Company 51")
        expect(page.locator("#companies .table-scroll")).to_be_focused()
        wait_said(page, "Page 2 of 2.")
        assert has_no_horizontal_scroll(page)[0]
        assert_no_serious_violations(page)
        press_by_keyboard(page, page.get_by_role("link", name="Previous page", exact=True))
        expect(page).to_have_url(re.compile(r"/companies\?sort=name&dir=asc$"))
        expect(page.locator("#companies-table tbody tr")).to_have_count(50)
        expect(page.locator("#companies .table-scroll")).to_be_focused()
        wait_said(page, "Page 1 of 2.")
        # Back returns to the page before; a reload keeps the page.
        page.go_back()
        expect(page).to_have_url(re.compile(r"page=2$"))
        expect(first_row(page)).to_contain_text("Fictional Company 51")
        page.reload()
        expect(first_row(page)).to_contain_text("Fictional Company 51")
