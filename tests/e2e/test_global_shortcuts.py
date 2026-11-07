"""M9.5 part B: global keyboard shortcuts in a real browser (design "Cross-cutting UX", its
shortcut table; ADR-098 item 7 and its M4.7 item 8; ADR-138).

G then T, I, B or F goes to Today, Inbox, Board or Fact bank; N opens Quick add; ? lists every
shortcut on every app page; Esc closes the dialog and focus goes back. G and N follow the
Settings switch; ? stays and then says the single keys are off; Ctrl/Cmd+K always works (the
palette's own tests). The visible "Quick add" and "Keyboard shortcuts" buttons open the same
dialogs, by keyboard too, at 390x844 and with no serious axe violation. Fictional data; no
network; no fixed waits.
"""

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import PHONE
from tests.e2e.test_facts_page import tab_until
from tests.e2e.test_shell import assert_target_size, has_no_horizontal_scroll, new_page, open_app
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e

QUICK_ADD = "Quick add"
SHORTCUTS = "Keyboard shortcuts"
APP_PAGES = ["/", "/inbox", "/board", "/facts", "/companies", "/settings", "/sessions/new"]
EVERY_SHORTCUT = (
    "Ctrl+K",
    "Cmd+K",
    "G then T",
    "G then I",
    "G then B",
    "G then F",
    "Quick add",
    "Next job",
    "Previous job",
    "Mark the job to apply",
    "Esc",
)


def dialog(page: Page, name: str) -> Locator:
    return page.get_by_role("dialog", name=name, exact=True)


def switch_single_keys_off(page: Page, app: RunningApp) -> None:
    open_app(page, app, "/settings/keyboard")
    page.get_by_role("checkbox", name="Use single-key shortcuts").uncheck()
    section = page.locator("#shortcuts-settings")
    section.get_by_role("button", name="Save").click()
    expect(section).to_contain_text("Saved")


def test_global_go_shortcuts_g_then_t_i_b_f(desktop_page: Page, running_app: RunningApp) -> None:
    page = desktop_page
    open_app(page, running_app)
    for key, path in (("i", "/inbox"), ("b", "/board"), ("f", "/facts"), ("t", "/")):
        page.keyboard.press("g")
        page.keyboard.press(key)
        expect(page).to_have_url(running_app.base_url + path)
        page.wait_for_function("() => !!window.htmx")

    # G then a key that names no page goes nowhere, and a later T alone is not "go".
    page.keyboard.press("g")
    page.keyboard.press("x")
    page.keyboard.press("i")
    assert page.url == running_app.base_url + "/"

    # N opens Quick add with its three choices; Esc closes it.
    page.keyboard.press("n")
    quick_add = dialog(page, QUICK_ADD)
    expect(quick_add).to_be_visible()
    expect(quick_add.get_by_role("link")).to_have_count(3)
    page.keyboard.press("Escape")
    expect(quick_add).not_to_be_visible()


def test_keys_are_ignored_while_typing(desktop_page: Page, running_app: RunningApp) -> None:
    page = desktop_page
    open_app(page, running_app, "/inbox")
    page.get_by_role("button", name="+ Add job", exact=True).click()
    field = page.locator("#job-text")
    field.focus()
    page.keyboard.type("g i n ?")
    expect(field).to_have_value("g i n ?")
    assert page.url == running_app.base_url + "/inbox"
    expect(dialog(page, QUICK_ADD)).not_to_be_visible()
    expect(dialog(page, SHORTCUTS)).not_to_be_visible()


@pytest.mark.parametrize("path", APP_PAGES)
def test_question_mark_lists_all_shortcuts_on_every_page(
    desktop_page: Page, running_app: RunningApp, path: str
) -> None:
    page = desktop_page
    open_app(page, running_app, path)
    page.keyboard.press("?")
    help_dialog = dialog(page, SHORTCUTS)
    expect(help_dialog).to_be_visible()
    for text in EVERY_SHORTCUT:
        expect(help_dialog).to_contain_text(text)
    expect(help_dialog.get_by_role("button", name="Close")).to_be_focused()
    page.keyboard.press("Escape")
    expect(help_dialog).not_to_be_visible()


def test_single_key_shortcuts_off_stop_g_n_and_question_mark(
    desktop_page: Page, running_app: RunningApp
) -> None:
    """With the switch off, G, N and ? do nothing (WCAG 2.1.4; ADR-138 item 9), Ctrl/Cmd+K still
    opens the palette, and the Keyboard shortcuts button still opens the list, which says so."""
    page = desktop_page
    switch_single_keys_off(page, running_app)
    open_app(page, running_app)
    assert page.locator("body").get_attribute("data-single-keys") == "off"
    page.evaluate("() => { window.__stayed = true; }")
    for key in ("g", "f", "n", "?"):
        page.keyboard.press(key)
    expect(dialog(page, QUICK_ADD)).not_to_be_visible()
    expect(dialog(page, SHORTCUTS)).not_to_be_visible()

    # Ctrl/Cmd+K still works; once the palette's own answer is in, a navigation G then F had
    # started would have replaced the page, so the marker shows nothing moved.
    page.keyboard.press("ControlOrMeta+k")
    palette = page.get_by_role("dialog", name="Command palette")
    expect(palette).to_be_visible()
    expect(palette.locator("[data-palette-action]").first).to_be_visible()
    assert page.evaluate("() => window.__stayed === true && location.pathname === '/'")
    page.keyboard.press("Escape")
    expect(page.locator("#palette")).not_to_be_visible()

    opener = page.get_by_role("button", name=SHORTCUTS, exact=True)
    opener.focus()
    page.keyboard.press("Enter")
    help_dialog = dialog(page, SHORTCUTS)
    expect(help_dialog).to_contain_text("Single-key shortcuts are off")
    expect(help_dialog).to_contain_text("Ctrl+K")
    for single in ("G then T", "Next job", "Show this list"):
        expect(help_dialog).not_to_contain_text(single)
    expect(help_dialog.get_by_role("link", name="Settings")).to_be_visible()
    page.keyboard.press("Escape")
    expect(opener).to_be_focused()


def test_switch_saved_on_the_page_applies_at_once(
    desktop_page: Page, running_app: RunningApp
) -> None:
    page = desktop_page
    switch_single_keys_off(page, running_app)
    # Back on, on the same page: the keys and the list follow without a reload.
    page.get_by_role("checkbox", name="Use single-key shortcuts").check()
    section = page.locator("#shortcuts-settings")
    section.get_by_role("button", name="Save").click()
    expect(section).to_contain_text("Saved")
    expect(page.locator("body")).to_have_attribute("data-single-keys", "on")
    page.locator("#shortcuts-heading").focus()
    page.keyboard.press("?")
    expect(dialog(page, SHORTCUTS)).to_contain_text("G then T")
    expect(dialog(page, SHORTCUTS)).not_to_contain_text("Single-key shortcuts are off")
    page.keyboard.press("Escape")
    page.keyboard.press("g")
    page.keyboard.press("b")
    expect(page).to_have_url(running_app.base_url + "/board")


def test_quick_add_job_opens_the_inbox_add_panel(
    desktop_page: Page, running_app: RunningApp
) -> None:
    page = desktop_page
    open_app(page, running_app, "/board")
    page.keyboard.press("n")
    dialog(page, QUICK_ADD).get_by_role("link", name="Add a job").click()
    expect(page).to_have_url(running_app.base_url + "/inbox#add-job")
    expect(page.locator("#job-intake-heading")).to_be_focused()
    expect(page.locator("#job-text")).to_be_visible()

    # On the Inbox itself, again and again: the dialog closes and the panel opens, though the
    # address already names it.
    for _ in range(2):
        page.keyboard.press("Escape")  # closes the add panel
        expect(page.locator("#job-text")).not_to_be_visible()
        expect(page).to_have_url(running_app.base_url + "/inbox")
        page.locator("#inbox-add-toggle").focus()
        page.keyboard.press("n")
        dialog(page, QUICK_ADD).get_by_role("link", name="Add a job").click()
        expect(dialog(page, QUICK_ADD)).not_to_be_visible()
        expect(page.locator("#job-intake-heading")).to_be_focused()
        expect(page.locator("#job-text")).to_be_visible()


def test_palette_add_job_on_the_inbox_opens_the_panel_and_closes_the_palette(
    desktop_page: Page, running_app: RunningApp
) -> None:
    page = desktop_page
    open_app(page, running_app, "/inbox")
    for _ in range(2):
        page.keyboard.press("ControlOrMeta+k")
        palette = page.get_by_role("dialog", name="Command palette")
        expect(palette).to_be_visible()
        page.keyboard.type("add job")
        expect(palette.locator('[data-action="add-job"]')).to_have_attribute(
            "aria-selected", "true"
        )
        page.keyboard.press("Enter")
        expect(palette).not_to_be_visible()
        expect(page.locator("#job-intake-heading")).to_be_focused()
        expect(page.locator("#job-text")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("#job-text")).not_to_be_visible()


def test_ctrl_k_takes_over_from_the_list_and_quick_add(
    desktop_page: Page, running_app: RunningApp
) -> None:
    page = desktop_page
    open_app(page, running_app, "/board")
    for key in ("?", "n"):
        page.keyboard.press(key)
        expect(page.locator("dialog[data-global-dialog][open]")).to_have_count(1)
        page.keyboard.press("ControlOrMeta+k")
        expect(page.get_by_role("dialog", name="Command palette")).to_be_visible()
        expect(page.locator("dialog[data-global-dialog][open]")).to_have_count(0)
        expect(page.locator("#palette-input")).to_be_focused()
        page.keyboard.press("Escape")
        expect(page.locator("#palette")).not_to_be_visible()
        page.wait_for_function("() => document.activeElement.id !== 'palette-input'")


def test_the_setup_guide_has_no_global_keys(desktop_page: Page, running_app: RunningApp) -> None:
    page = desktop_page
    open_app(page, running_app, "/setup")
    path = page.evaluate("() => location.pathname")
    page.evaluate("() => { window.__stayed = true; }")
    for key in ("g", "i", "n", "?"):
        page.keyboard.press(key)
    page.keyboard.press("ControlOrMeta+k")  # no palette in the guide either
    page.wait_for_timeout(300)  # a navigation G then I had started would have committed
    assert page.evaluate("() => window.__stayed === true && location.pathname") == path
    expect(page.locator("dialog[open]")).to_have_count(0)


@pytest.mark.parametrize("size", [(360, 640), (390, 844)])
def test_global_tools_come_after_the_content_at_phone_width(
    running_app: RunningApp, browser, size: tuple[int, int]
) -> None:
    """The row is drawn after the main content, as it comes after it in the Tab order."""
    with new_page(browser, {"width": size[0], "height": size[1]}) as page:
        open_app(page, running_app, "/board")
        main = page.locator("main").bounding_box()
        tools = page.locator("footer.global-tools").bounding_box()
        assert main is not None and tools is not None
        assert tools["y"] >= main["y"] + main["height"] - 1, (main, tools)
        assert page.evaluate(
            "() => document.querySelector('main').compareDocumentPosition("
            "document.querySelector('footer.global-tools')) & Node.DOCUMENT_POSITION_FOLLOWING"
        )


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_quick_add_and_shortcut_list_by_keyboard_phone_390x844_and_axe(
    running_app: RunningApp, browser, scheme: str
) -> None:
    with new_page(browser, PHONE, color_scheme=scheme) as page:
        open_app(page, running_app, "/board")
        for name in (QUICK_ADD, SHORTCUTS):
            opener = page.get_by_role("button", name=name, exact=True)
            expect(opener).to_be_visible()
            assert_target_size(opener, f"the {name} button")
            tab_until(page, opener)
            page.keyboard.press("Enter")
            shown = dialog(page, name)
            expect(shown).to_be_visible()
            wait_for_settled_styles(page)
            assert_no_serious_violations(page)
            assert has_no_horizontal_scroll(page)[0]
            for index in range(shown.locator(".palette-item").count()):
                assert_target_size(shown.locator(".palette-item").nth(index), f"{name} choice")
            assert_target_size(shown.get_by_role("button", name="Close"), f"{name} Close")
            # Tab stays inside the dialog.
            for _ in range(8):
                page.keyboard.press("Tab")
                assert page.evaluate(
                    "(id) => document.getElementById(id).contains(document.activeElement)",
                    "quick-add" if name == QUICK_ADD else "shortcut-help",
                )
            page.keyboard.press("Escape")
            expect(shown).not_to_be_visible()
            expect(opener).to_be_focused()
