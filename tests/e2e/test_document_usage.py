"""M6.11 keyboard navigation and frozen history in the phone drawer."""

import pytest
from playwright.sync_api import expect

from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import PHONE
from tests.e2e.test_facts_page import press_by_keyboard
from tests.e2e.test_shell import has_no_horizontal_scroll, new_page
from tests.e2e.test_submit_step import approve_cv_and_letter, go_to_submit, with_name

pytestmark = pytest.mark.e2e


def test_used_in_link_opens_application_by_keyboard(running_app, desktop_page):
    page = desktop_page
    aid = with_name(running_app)
    approve_cv_and_letter(page, running_app, aid)
    page.goto(running_app.base_url + "/facts?tab=library")
    link = page.locator(f'.fact-used-in a[href="/applications/{aid}"]').first
    expect(link).to_be_visible()
    press_by_keyboard(page, link)
    expect(page).to_have_url(running_app.base_url + f"/applications/{aid}")
    expect(page.locator("#application-title")).to_be_visible()
    assert_no_serious_violations(page)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_drawer_documents_sent_phone_390x844_and_axe(running_app, browser, scheme):
    aid = with_name(running_app)
    with new_page(browser, PHONE, color_scheme=scheme) as page:
        approve_cv_and_letter(page, running_app, aid)
        go_to_submit(page)
        press_by_keyboard(page, page.locator("#submit-export"))
        expect(page.locator("#submit-files button")).to_have_count(4)
        press_by_keyboard(page, page.locator("#submit-mark-applied"))
        expect(page.locator("#submit-applied")).to_be_visible()
        page.goto(running_app.base_url + "/board")
        button = page.locator(f'[data-application-id="{aid}"] a[hx-get$="/detail"]')
        press_by_keyboard(page, button)
        expect(page.locator("#application-drawer")).to_be_visible()
        expect(page.locator("#documents-sent")).to_contain_text("Version 1")
        downloads = page.locator("#documents-sent [data-file-download]")
        expect(downloads).to_have_count(4)
        with page.expect_download() as downloaded:
            press_by_keyboard(page, downloads.first)
        assert downloaded.value.failure() is None
        expect(downloads.first).to_be_focused()
        assert has_no_horizontal_scroll(page)[0]
        assert_no_serious_violations(page)
