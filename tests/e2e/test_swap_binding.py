"""A form may be pressed as soon as it appears after an htmx swap (TD-320, ADR-138).

htmx binds swapped content when it settles. With a settle delay, a click or Enter right after a
swap submitted the form natively, without the launch token, and the server refused it (403).
The app's htmx configuration settles at once (``defaultSettleDelay: 0``), so swapped content is
bound in the same task as the swap and nothing can be pressed before that. These tests swap a
form in through a real htmx request and press it on the very next task.
"""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.test_shell import open_app
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e

SWAPPED_FORM = (
    '<form hx-post="/binding-probe" action="/binding-probe" method="post">'
    '<input name="kept" value="swapped"><button name="pressed" value="yes">Save</button></form>'
)

PRESS_RIGHT_AFTER_SWAP = """(press) => {
  const target = document.createElement('div');
  document.querySelector('main').append(target);
  document.body.addEventListener('htmx:afterSwap', function once(event) {
    if (event.target !== target) return;
    document.body.removeEventListener('htmx:afterSwap', once);
    window.setTimeout(() => {
      const form = target.querySelector('form');
      if (press === 'click') form.querySelector('button').click();
      else form.requestSubmit();
    }, 0);
  });
  htmx.ajax('GET', '/swap-probe', {target: target, swap: 'innerHTML'});
}"""


@pytest.mark.parametrize("press", ["click", "enter"])
def test_form_pressed_right_after_a_swap_is_sent_by_htmx_with_the_token(
    desktop_page: Page, running_app: RunningApp, press: str
) -> None:
    page = desktop_page
    open_app(page, running_app)
    page.route(
        "**/swap-probe",
        lambda route: route.fulfill(status=200, content_type="text/html", body=SWAPPED_FORM),
    )
    page.route("**/binding-probe", lambda route: route.fulfill(status=200, body="Saved probe"))
    with page.expect_request("**/binding-probe") as sent:
        page.evaluate(PRESS_RIGHT_AFTER_SWAP, press)
    request = sent.value
    assert request.headers.get("x-jp-token"), "sent natively, without the launch token"
    assert request.headers.get("hx-request") == "true"
    assert request.post_data is not None and "kept=swapped" in request.post_data
    if press == "click":
        assert "pressed=yes" in request.post_data
    expect(page.locator("main")).to_contain_text("Saved probe")


TWO_MARKS = (
    '<h2 id="first-mark" tabindex="-1" data-autofocus>Saved</h2>'
    '<section><h3 id="nested-mark" tabindex="-1" data-autofocus>Nested part</h3></section>'
)
SWAP_INTO = """([id, path]) => {
  let target = document.getElementById(id);
  if (!target) {
    target = document.createElement('div');
    target.id = id;
    document.querySelector('main').append(target);
  }
  htmx.ajax('GET', path, {target: target, swap: 'innerHTML'});
}"""


def test_a_later_swap_never_takes_focus_through_an_old_mark(
    desktop_page: Page, running_app: RunningApp
) -> None:
    """An answer with a nested fragment's own focus mark: focus goes to the answer's first
    mark, and the other mark is cleared, so the next unrelated swap leaves focus alone."""
    page = desktop_page
    open_app(page, running_app)
    page.route("**/two-marks", lambda route: route.fulfill(status=200, body=TWO_MARKS))
    page.route("**/unrelated", lambda route: route.fulfill(status=200, body="<p>Other</p>"))
    page.evaluate(SWAP_INTO, ["answer-region", "/two-marks"])
    expect(page.locator("#first-mark")).to_be_focused()
    expect(page.locator("[data-autofocus]")).to_have_count(0)
    page.evaluate(SWAP_INTO, ["other-region", "/unrelated"])
    expect(page.locator("#other-region")).to_contain_text("Other")
    expect(page.locator("#first-mark")).to_be_focused()
