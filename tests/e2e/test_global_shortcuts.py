"""Global keys share visible actions and honor the single-key setting (M9.5)."""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.test_shell import open_app
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e


def test_global_go_shortcuts_g_then_t_i_b_f(desktop_page: Page, running_app: RunningApp) -> None:
    page = desktop_page
    open_app(page, running_app)
    for key, path in (("i", "/inbox"), ("b", "/board"), ("f", "/facts"), ("t", "/")):
        page.keyboard.press("g")
        page.keyboard.press(key)
        expect(page).to_have_url(running_app.base_url + path)
        page.wait_for_function("() => !!window.htmx")
    page.keyboard.press("n")
    expect(page.get_by_role("dialog", name="Quick add", exact=True)).to_be_visible()
    expect(
        page.get_by_role("dialog", name="Quick add", exact=True).get_by_role("link")
    ).to_have_count(3)
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog", name="Quick add", exact=True)).not_to_be_visible()


@pytest.mark.parametrize(
    "path", ["/", "/inbox", "/board", "/facts", "/companies", "/settings", "/sessions/new"]
)
def test_question_mark_lists_all_shortcuts_on_every_page(
    desktop_page: Page, running_app: RunningApp, path: str
) -> None:
    page = desktop_page
    open_app(page, running_app, path)
    page.keyboard.press("?")
    help_dialog = page.get_by_role("dialog", name="Keyboard shortcuts", exact=True)
    expect(help_dialog).to_be_visible()
    for text in (
        "Ctrl/Cmd+K",
        "G then T",
        "G then I",
        "G then B",
        "G then F",
        "Quick add",
        "Next job",
        "Escape",
    ):
        expect(help_dialog).to_contain_text(text)
    page.keyboard.press("Escape")
    expect(help_dialog).not_to_be_visible()


def test_single_key_shortcuts_off_keeps_ctrl_k(desktop_page: Page, running_app: RunningApp) -> None:
    page = desktop_page
    open_app(page, running_app, "/settings/keyboard")
    checkbox = page.get_by_role("checkbox", name="Use single-key shortcuts")
    checkbox.uncheck()
    page.locator("#shortcuts-settings").get_by_role("button", name="Save").click()
    expect(page.locator("#shortcuts-settings")).to_contain_text("Saved")
    open_app(page, running_app)
    page.keyboard.press("g")
    page.keyboard.press("f")
    page.keyboard.press("n")
    assert page.url == running_app.base_url + "/"
    expect(page.get_by_role("dialog", name="Quick add", exact=True)).not_to_be_visible()
    page.keyboard.press("ControlOrMeta+k")
    expect(page.get_by_role("dialog", name="Command palette")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#palette")).not_to_be_visible()
    page.wait_for_function("() => document.activeElement.id !== 'palette-input'")
    page.keyboard.press("?")
    expect(page.get_by_role("dialog", name="Keyboard shortcuts", exact=True)).to_contain_text(
        "Single-key shortcuts are off"
    )
