"""M5.6 in a browser: contacts on the company page, linking and interactions in the drawer.

Assumed markup is the one in ``tests/contact_pages.py``. Every step uses the keyboard (Tab and
Enter); the phone check is 390x844. Everything is fictional.
"""

import pytest
from playwright.sync_api import expect

from tests.contact_pages import insert_contact, insert_interaction, link_contact
from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_board import card, desk, open_board, rows, wait_said_containing
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, press_by_keyboard, tab_until, wait_said
from tests.e2e.test_shell import has_no_horizontal_scroll, new_page, open_app

pytestmark = pytest.mark.e2e
COMPANY = "Fictional Orchard"


def company_id(app):
    return rows(app, "SELECT id FROM companies WHERE name=?", (COMPANY,))[0][0]


def made(app):
    with desk(app) as seed:
        return seed.application("contact-job", "Seed Buyer", COMPANY, "applied")


def open_drawer(page, app, application_id):
    open_board(page, app)
    press_by_keyboard(page, card(page, application_id).get_by_role("link", name="Open application"))
    dialog = page.get_by_role("dialog", name="Application details")
    expect(dialog).to_be_visible()
    return dialog


def type_into(page, locator, text):
    tab_until(page, locator)
    page.keyboard.type(text)


def test_add_contact_on_company_page_and_link_in_drawer_by_keyboard(running_app, browser):
    a = made(running_app)
    cid = company_id(running_app)
    with new_page(browser, DESKTOP) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        open_app(page, running_app, f"/companies/{cid}")
        box = page.locator("#company-contacts")
        expect(box.get_by_role("heading", name="Contacts", exact=True)).to_be_visible()
        expect(box).to_contain_text("No contacts yet")
        for label, text in (
            ("Contact name", "Sam Fictional"),
            ("Role", "Hiring manager"),
            ("Email", "sam@example.test"),
            ("Phone", "+00 555 0100"),
            ("How you know them", "Met at a fictional job fair"),
            ("Contact notes", "Prefers a short email."),
        ):
            type_into(page, box.get_by_label(label, exact=True), text)
        press_by_keyboard(page, box.get_by_role("button", name="Add contact", exact=True))
        expect(box).to_contain_text("Contact saved.")
        expect(box.locator("#company-contacts-heading")).to_be_focused()
        wait_said(page, "Contact saved.")
        item = box.locator("li[data-contact-id]")
        expect(item).to_have_count(1)
        expect(item).to_contain_text("Sam Fictional")
        expect(item.locator('a[href="mailto:sam@example.test"]')).to_have_count(1)
        [(contact, name, email, version)] = rows(
            running_app, "SELECT id, name, email, version FROM contacts"
        )
        assert (name, email, version) == ("Sam Fictional", "sam@example.test", 1)
        assert_no_serious_violations(page)

        # In the drawer: link the company's contact, then unlink it, then add one inline.
        dialog = open_drawer(page, running_app, a.id)
        section = dialog.locator("#application-contacts")
        expect(section.get_by_role("heading", name="Contacts", exact=True)).to_be_visible()
        picker = section.get_by_label("Contact to link", exact=True)
        tab_until(page, picker)
        picker.select_option(value=contact)
        press_by_keyboard(page, section.get_by_role("button", name="Link contact", exact=True))
        wait_said_containing(page, "Contact linked.")
        expect(section.locator("#application-contacts-heading")).to_be_focused()
        expect(section.locator("li[data-contact-id]")).to_have_count(1)
        expect(section.locator("li[data-contact-id]")).to_contain_text("Sam Fictional")
        assert rows(running_app, "SELECT application_id, contact_id FROM application_contacts") == [
            (a.id, contact)
        ]

        press_by_keyboard(page, section.get_by_role("button", name="Unlink Sam Fictional"))
        wait_said_containing(page, "Contact unlinked.")
        expect(section.locator("li[data-contact-id]")).to_have_count(0)
        assert rows(running_app, "SELECT * FROM application_contacts") == []
        assert rows(running_app, "SELECT count(*) FROM contacts") == [(1,)]  # never deleted

        press_by_keyboard(page, section.locator("summary").filter(has_text="Add a contact"))
        type_into(page, section.get_by_label("Contact name", exact=True), "Alex Fictional")
        type_into(page, section.get_by_label("Email", exact=True), "alex@example.test")
        press_by_keyboard(
            page, section.get_by_role("button", name="Add and link contact", exact=True)
        )
        wait_said_containing(page, "Contact added and linked.")
        expect(section.locator("li[data-contact-id]")).to_contain_text("Alex Fictional")
        added = rows(running_app, "SELECT id, company_id FROM contacts WHERE name='Alex Fictional'")
        assert added and added[0][1] == cid
        assert rows(running_app, "SELECT contact_id FROM application_contacts") == [(added[0][0],)]
        assert_no_serious_violations(page)


def test_log_interaction_from_drawer_by_keyboard(running_app, browser):
    a = made(running_app)
    cid = company_id(running_app)
    with desk(running_app) as seed:
        insert_contact(seed.db, "contact-sam", cid, "Sam Fictional", role="Hiring manager")
        link_contact(seed.db, a.id, "contact-sam")
    with new_page(browser, DESKTOP) as page:
        dialog = open_drawer(page, running_app, a.id)
        form = dialog.locator("#interaction-form")
        expect(dialog.get_by_role("heading", name="Log an interaction", exact=True)).to_be_visible()
        press_by_keyboard(page, dialog.locator("#application-interactions summary"))
        expect(form).to_be_visible()
        tab_until(page, form.get_by_label("Interaction date", exact=True))
        form.get_by_label("Interaction date", exact=True).fill("2026-10-18")
        channel = form.get_by_label("Channel", exact=True)
        tab_until(page, channel)
        channel.select_option(value="call")
        type_into(page, form.get_by_label("Interaction note", exact=True), "Talked about the team.")
        contact = form.get_by_label("Contact (optional)", exact=True)
        tab_until(page, contact)
        contact.select_option(value="contact-sam")
        press_by_keyboard(page, form.get_by_role("button", name="Log interaction", exact=True))
        wait_said_containing(page, "Interaction logged.")
        expect(dialog.locator(".save-message")).to_have_text("Interaction logged.")
        expect(dialog.locator("#application-title")).to_be_focused()
        entry = dialog.locator('li[data-timeline-kind="interaction"]')
        expect(entry).to_have_count(1)
        expect(entry).to_contain_text("Talked about the team.")
        expect(entry).to_contain_text("2026-10-18")
        expect(entry).to_contain_text("Sam Fictional")
        [row] = rows(
            running_app,
            "SELECT contact_id, application_id, channel, note FROM interactions",
        )
        assert row == ("contact-sam", a.id, "call", "Talked about the team.")
        assert_no_serious_violations(page)

        # The company page shows it as the contact's last contact.
        open_app(page, running_app, f"/companies/{cid}")
        expect(page.locator("#company-contacts li[data-contact-id]")).to_contain_text(
            "Last contact: 2026-10-18"
        )


def test_company_contacts_phone_390x844_and_axe(running_app, browser):
    a = made(running_app)
    cid = company_id(running_app)
    with desk(running_app) as seed:
        insert_contact(
            seed.db,
            "contact-sam",
            cid,
            "Sam Fictional",
            role="Hiring manager at a fictional orchard with a very long name for wrapping",
            email="sam.with.a.very.long.address@a-fictional-orchard-company.example.test",
            phone="+00 555 0100",
            relationship="Met at a fictional job fair",
            notes="Prefers a short email, and a fairly long note to check the layout wraps.",
        )
        insert_contact(seed.db, "contact-pat", cid, "Pat Fictional", email="not-an-email")
        link_contact(seed.db, a.id, "contact-sam")
        insert_interaction(
            seed.db,
            "inter-1",
            contact_id="contact-sam",
            application_id=a.id,
            day="2026-10-12",
            note="A fictional call.",
        )
    with new_page(browser, PHONE) as page:
        open_app(page, running_app, f"/companies/{cid}")
        box = page.locator("#company-contacts")
        expect(box.get_by_role("heading", name="Contacts", exact=True)).to_be_visible()
        expect(
            page.locator("#company-history").get_by_role("heading", name="History")
        ).to_be_visible()
        expect(page.locator("#company-history")).to_contain_text("Applied:")
        expect(box.locator("li[data-contact-id]")).to_have_count(2)
        expect(box).to_contain_text("Last contact: 2026-10-12")
        assert has_no_horizontal_scroll(page)[0]
        assert_no_serious_violations(page)
        add = box.get_by_role("button", name="Add contact", exact=True)
        add.scroll_into_view_if_needed()
        box_size = add.bounding_box()
        assert box_size is not None and box_size["height"] >= 24 and box_size["width"] >= 24
        # An edit form opens by keyboard and is still accessible and inside the viewport.
        edit = box.locator("summary").filter(has_text="Edit Sam Fictional")
        press_by_keyboard(page, edit)
        form = box.locator("form[action$='/contacts/contact-sam']")
        expect(form.get_by_label("Email", exact=True)).to_be_visible()
        assert has_no_horizontal_scroll(page)[0]
        assert_no_serious_violations(page)

        # The application page has the same contacts and interaction forms at phone width.
        open_app(page, running_app, f"/applications/{a.id}")
        section = page.locator("#application-contacts")
        expect(section).to_contain_text("Sam Fictional")
        press_by_keyboard(page, page.locator("#application-interactions summary"))
        expect(page.locator("#interaction-form")).to_be_visible()
        expect(page.locator('li[data-timeline-kind="interaction"]')).to_contain_text(
            "A fictional call."
        )
        assert has_no_horizontal_scroll(page)[0]
        assert_no_serious_violations(page)


def test_refused_contact_on_company_page_keeps_typing_and_ties_the_error(running_app, browser):
    made(running_app)
    cid = company_id(running_app)
    with new_page(browser, DESKTOP) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        open_app(page, running_app, f"/companies/{cid}")
        box = page.locator("#company-contacts")
        type_into(page, box.get_by_label("Contact name", exact=True), "Sam Fictional")
        type_into(page, box.get_by_label("Email", exact=True), "not-an-email")
        type_into(page, box.get_by_label("Contact notes", exact=True), "Typed notes stay.")
        press_by_keyboard(page, box.get_by_role("button", name="Add contact", exact=True))
        email = box.get_by_label("Email", exact=True)
        expect(email).to_have_attribute("aria-invalid", "true")
        expect(email).to_be_focused()
        error_id = email.get_attribute("aria-describedby")
        assert error_id
        expect(page.locator(f"#{error_id}")).to_contain_text("Enter an address like")
        expect(box.get_by_label("Contact name", exact=True)).to_have_value("Sam Fictional")
        expect(box.get_by_label("Contact notes", exact=True)).to_have_value("Typed notes stay.")
        assert rows(running_app, "SELECT count(*) FROM contacts") == [(0,)]
        assert_no_serious_violations(page)
        # Correcting the address saves it.
        email.fill("sam@example.test")
        press_by_keyboard(page, box.get_by_role("button", name="Add contact", exact=True))
        expect(box).to_contain_text("Contact saved.")
        assert rows(running_app, "SELECT email FROM contacts") == [("sam@example.test",)]


def test_stale_contact_edit_shows_the_reload_prompt_and_keeps_typing(running_app, browser):
    made(running_app)
    cid = company_id(running_app)
    with desk(running_app) as seed:
        insert_contact(seed.db, "contact-sam", cid, "Sam Fictional", role="Hiring manager")
    with new_page(browser, DESKTOP) as page:
        open_app(page, running_app, f"/companies/{cid}")
        box = page.locator("#company-contacts")
        press_by_keyboard(page, box.locator("summary").filter(has_text="Edit Sam Fictional"))
        form = box.locator("form[action$='/contacts/contact-sam']")
        with desk(running_app) as seed, seed.db.transaction() as conn:  # changed elsewhere
            conn.execute("UPDATE contacts SET role='Elsewhere', version=version+1")
        role = form.get_by_label("Role", exact=True)
        tab_until(page, role)
        role.fill("My change")
        press_by_keyboard(page, form.get_by_role("button", name="Save contact", exact=True))
        prompt = box.locator("[role='alert']")
        expect(prompt).to_contain_text("This contact changed")
        expect(prompt).to_be_focused()
        expect(prompt.get_by_role("link", name="Reload the page")).to_be_visible()
        expect(role).to_have_value("My change")
        assert rows(running_app, "SELECT role FROM contacts") == [("Elsewhere",)]
        assert_no_serious_violations(page)


def test_refused_link_in_the_drawer_ties_the_error_to_the_field(running_app, browser):
    a = made(running_app)
    cid = company_id(running_app)
    with desk(running_app) as seed:
        insert_contact(seed.db, "contact-sam", cid, "Sam Fictional")
    with new_page(browser, DESKTOP) as page:
        dialog = open_drawer(page, running_app, a.id)
        section = dialog.locator("#application-contacts")
        press_by_keyboard(page, section.get_by_role("button", name="Link contact", exact=True))
        picker = section.get_by_label("Contact to link", exact=True)
        expect(picker).to_have_attribute("aria-invalid", "true")
        expect(picker).to_be_focused()
        error_id = picker.get_attribute("aria-describedby")
        assert error_id
        expect(page.locator(f"#{error_id}")).to_contain_text("Choose one of")
        assert rows(running_app, "SELECT * FROM application_contacts") == []
        assert_no_serious_violations(page)


def test_application_drawer_with_contacts_phone_390x844_and_axe(running_app, browser):
    a = made(running_app)
    cid = company_id(running_app)
    with desk(running_app) as seed:
        insert_contact(
            seed.db,
            "contact-sam",
            cid,
            "Sam Fictional with a rather long name to check that wrapping works",
            role="Hiring manager at a fictional orchard with a very long name for wrapping",
        )
        insert_contact(seed.db, "contact-pat", cid, "Pat Fictional")
        link_contact(seed.db, a.id, "contact-sam")
    with new_page(browser, PHONE) as page:
        dialog = open_drawer(page, running_app, a.id)
        expect(dialog.locator("#application-contacts li[data-contact-id]")).to_have_count(1)
        assert has_no_horizontal_scroll(page)[0]
        assert_no_serious_violations(page)
        for summary in ("Add a contact", "Log an interaction"):
            press_by_keyboard(page, dialog.locator("summary").filter(has_text=summary))
        expect(dialog.locator("#interaction-form")).to_be_visible()
        expect(dialog.locator("#application-contact-form")).to_be_visible()
        assert has_no_horizontal_scroll(page)[0]
        assert_no_serious_violations(page)
