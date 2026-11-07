"""A form may be activated as soon as it appears, before htmx settles (TD-320)."""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.test_shell import open_app
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e


def test_swapped_form_submit_before_settle_keeps_token(
    desktop_page: Page, running_app: RunningApp
) -> None:
    page = desktop_page
    open_app(page, running_app)
    page.route("**/binding-probe", lambda route: route.fulfill(status=200, body="Saved probe"))
    with page.expect_request("**/binding-probe") as sent:
        page.evaluate("""() => {
          const target = document.createElement('div');
          document.querySelector('main').append(target);
          const form = '<form hx-post="/binding-probe" action="/binding-probe" method="post">'
            + '<button>Save</button></form>';
          htmx.swap(target, form, {swapStyle: 'innerHTML', settleDelay: 1000});
          target.querySelector('button').click();
        }""")
    assert sent.value.headers.get("x-jp-token")
    expect(page.locator("main")).to_contain_text("Saved probe")
