"""M9.6 part A: every page of the app, over the shipped demo, by axe and by keyboard (AC-M9.5).

One ``jobpipeline --demo`` (the real command line, seeded with the demo dataset, under the test's
temporary home) serves the whole module. For each screen in ``tests/page_inventory.py``:

- ``test_ac_m9_5_axe_all_pages[<page>-<light|dark>-<desktop|phone>]``: no serious or critical axe
  violation and nothing sideways, in both themes, at 1280x800 and 390x844;
- ``test_ac_m9_5_keyboard_reaches_primary_action[<page>]``: from the top of the page, Tab alone
  reaches the screen's primary action, which shows where the focus is.

Fictional data only; no network; no fixed waits.
"""

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_shell import has_no_horizontal_scroll, new_page, open_app
from tests.page_inventory import OVERLAY_CASES, PAGE_CASES, OverlayCase, PageCase
from tests.running_app import RunningApp, start_app

pytestmark = pytest.mark.e2e

MAX_TABS = 150
VIEWPORTS = {"desktop": DESKTOP, "phone": PHONE}
FOCUS_SHOWN = """(element) => {
  const style = getComputedStyle(element);
  return style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) > 0
    || style.boxShadow !== 'none';
}"""


@pytest.fixture(scope="module")
def demo_app(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RunningApp]:
    app = start_app(
        tmp_path_factory.mktemp("m9-pages"), demo=True, setup="fresh", allow_fakes=False
    )
    try:
        yield app
    finally:
        app.stop()


def locate(page: Page, spec: tuple[str, str]) -> Locator:
    kind, value = spec
    if kind == "css":
        return page.locator(value).first
    return page.get_by_role(kind, name=value, exact=True).first  # type: ignore[arg-type]


# A screen reached through ``via``, once per running app: (app address, case) -> its path.
ARRIVALS: dict[tuple[str, str], str] = {}


def open_case(page: Page, app: RunningApp, case: PageCase) -> None:
    key = (app.base_url, case.name)
    if case.via is not None and key in ARRIVALS:
        open_app(page, app, ARRIVALS[key])
    else:
        open_app(page, app, case.url)
        if case.via is not None:
            locate(page, case.via).click()
            assert case.arrives is not None
            expect(page).to_have_url(re.compile(case.arrives))
            page.wait_for_load_state("load")
            page.wait_for_function("() => !!window.htmx")
            ARRIVALS[key] = page.url[len(app.base_url) :]
    expect(page.locator("h1").first).to_be_visible()
    if case.ready is not None:
        expect(page.locator(case.ready).first).to_be_visible(timeout=15000)


def wait_for_content(page: Page, case: PageCase) -> None:
    """The screen's content has arrived: its primary action shows (many screens load it from a
    protected fragment after the page) and no htmx request is still pending."""
    expect(locate(page, case.primary)).to_be_visible(timeout=15000)
    page.wait_for_function("() => !document.querySelector('.htmx-request')")


def case_ids() -> list[str]:
    return [case.name for case in PAGE_CASES]


@pytest.mark.parametrize("viewport", list(VIEWPORTS))
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("case", PAGE_CASES, ids=case_ids())
def test_ac_m9_5_axe_all_pages(
    demo_app: RunningApp, browser: Browser, case: PageCase, theme: str, viewport: str
) -> None:
    with new_page(browser, VIEWPORTS[viewport], color_scheme=theme) as page:
        open_case(page, demo_app, case)
        wait_for_content(page, case)
        wait_for_settled_styles(page)
        assert_no_serious_violations(page)
        fits, width, inner = has_no_horizontal_scroll(page)
        assert fits, f"{case.name}: {width}px wide in a {inner}px window"


@pytest.mark.parametrize("case", PAGE_CASES, ids=case_ids())
def test_ac_m9_5_keyboard_reaches_primary_action(
    demo_app: RunningApp, browser: Browser, case: PageCase
) -> None:
    with new_page(browser, DESKTOP) as page:
        open_case(page, demo_app, case)
        target = locate(page, case.primary)
        expect(target).to_be_visible()
        page.locator("body").focus()
        for _ in range(MAX_TABS):
            if target.evaluate("(element) => element === document.activeElement"):
                break
            page.keyboard.press("Tab")
        else:
            raise AssertionError(f"{case.name}: Tab never reached {case.primary}")
        assert target.evaluate(FOCUS_SHOWN), f"{case.name}: focus is not visible on the action"


def test_audit_runs_on_the_seeded_demo(demo_app: RunningApp, browser: Browser) -> None:
    with new_page(browser, DESKTOP) as page:
        open_app(page, demo_app, "/companies")
        expect(page.locator("main")).to_contain_text("Brightwater Fictional Greenhouses")
    assert not Path(demo_app.keyring_file).exists()


def test_progress_lines_stay_hidden_until_their_request_runs(
    demo_app: RunningApp, browser: Browser
) -> None:
    """An idle progress line such as "Exporting..." is out of the accessibility tree until its
    request runs (the M9.1 review asked the audit to check it). htmx's own indicator styles hide
    it (``visibility: hidden``) until htmx marks its form ``htmx-request``; this guards them."""
    with new_page(browser, DESKTOP) as page:
        open_app(page, demo_app, "/settings/data")
        form = page.locator("#data-export-form")
        expect(form).to_be_visible(timeout=15000)
        line = form.locator(".htmx-indicator")
        expect(line).to_be_hidden()
        form.evaluate("(form) => form.classList.add('htmx-request')")
        expect(line).to_be_visible()


@pytest.mark.parametrize("viewport", list(VIEWPORTS))
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("case", OVERLAY_CASES, ids=[case.name for case in OVERLAY_CASES])
def test_ac_m9_5_axe_overlays(
    demo_app: RunningApp, browser: Browser, case: OverlayCase, theme: str, viewport: str
) -> None:
    """Every dialog, drawer, menu and panel a page opens: no serious or critical axe violation and
    nothing sideways, in both themes and at both widths. Nothing in them is pressed."""
    with new_page(browser, VIEWPORTS[viewport], color_scheme=theme) as page:
        open_app(page, demo_app, case.url)
        expect(page.locator("h1").first).to_be_visible()
        opener = page.locator(case.opener).first
        expect(opener).to_be_visible(timeout=15000)
        opener.click()
        if case.typed is not None:
            field, text = case.typed
            page.locator(field).fill(text)
        expect(page.locator(case.ready).first).to_be_visible(timeout=15000)
        if case.has is not None:
            expect(page.locator(case.has).first).to_be_visible()
        page.wait_for_function("() => !document.querySelector('.htmx-request')")
        wait_for_settled_styles(page)
        assert_no_serious_violations(page)
        fits, width, inner = has_no_horizontal_scroll(page)
        assert fits, f"{case.name}: {width}px wide in a {inner}px window"
