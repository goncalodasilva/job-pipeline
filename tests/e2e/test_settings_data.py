"""M9.1 part D1 Settings > Data and privacy in a real browser (AC-M9.3; ADR-131 items 2-4;
spec 3.9): export, download and restore into an empty workspace by keyboard, the delete-all-data
dialog by keyboard, and the area at 390x844 with an axe scan in light and dark.

The assumed markup and routes are in ``tests/data_pages.py`` and the behaviour in
``tests/integration/test_settings_data_routes.py``. Two real apps run in the first test: the
``running_app`` one holds a fictional fact and files and is exported; a second one, started on a
finished setup with no content (no saved provider), is the restore target (a restore needs a
workspace with no content). Setup step 1's own "Restore from an export" is a later part. The
files the browser downloads and uploads stay in ``tmp_path``. No fixed waits; every value is
fictional.
"""

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Locator, Page, Request, expect

from app import __version__
from app.config import write_bootstrap
from app.modules.storage import Database
from app.onboarding.registry import m1_registry
from app.services.facts import FactInput, FactService
from tests.data_pages import EXPORT_NAME, FACT_TEXT, FILES, MARKER, OTHER_ENTRIES, plant
from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_board import rows
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS
from tests.e2e.test_settings_board import tab_to
from tests.e2e.test_setup_provider_test import is_focused
from tests.e2e.test_shell import assert_target_size, has_no_horizontal_scroll, new_page, open_app
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory
from tests.integration.test_setup_redirect import START
from tests.running_app import RunningApp, StopResult, start_app
from tests.setup_harness import seed_workspace

pytestmark = pytest.mark.e2e

PAGE = "/settings/data"
LINK = "Data and privacy"
EXPORT_BUTTON = "#data-export-form button"
DOWNLOAD = "#data-export button[data-file-download]"
IMPORT_FILE = "#data-import-file"
IMPORT_BUTTON = "#data-import-submit"
DELETE_OPEN = "#data-delete-open"
DELETE_SUBMIT = "#data-delete-submit"
MAX_DIALOG_TABS = 12
FOCUS_IN_DIALOG = """() => {
  const dialog = document.querySelector('[role="dialog"]');
  const active = document.activeElement;
  return dialog !== null && active !== null && active !== document.body
    && dialog.contains(active);
}"""
FOCUS_IN = """(selector) => {
  const box = document.querySelector(selector);
  return !!box && box.contains(document.activeElement);
}"""
GAP_ABOVE = """(selector) => {
  const box = document.querySelector(selector);
  return box ? parseFloat(getComputedStyle(box).marginTop) : -1;
}"""
DIALOG_TITLE_IN_VIEW = """() => {
  const dialog = document.querySelector('[role="dialog"]');
  const id = dialog.getAttribute('aria-labelledby');
  const title = document.getElementById(id).getBoundingClientRect();
  return {
    scrollTop: dialog.scrollTop,
    titleTop: title.top,
    titleBottom: title.bottom,
    innerHeight: window.innerHeight,
  };
}"""
DIALOG_BOX = """() => {
  const box = document.querySelector('[role="dialog"]').getBoundingClientRect();
  return { left: box.left, right: box.right, innerWidth: window.innerWidth };
}"""
FOCUSED_NAME = """() => {
  const e = document.activeElement;
  return e ? (e.getAttribute('name') || e.id || (e.textContent || '').trim()) : '';
}"""
SAID_MATCHING = (
    """(pattern) => (window.__said || []).some((text) => new RegExp(pattern, 'i').test(text))"""
)


# --- the apps ---------------------------------------------------------------------------------


def seed(app: RunningApp) -> None:
    """A fictional fact and the app's files, written into the running app's folder."""
    with Database.open(app.data_dir / "app.db") as db:
        facts = FactService(db, FrozenClock(START), SequentialIdFactory("dataf"))
        facts.create(FactInput(text=FACT_TEXT))
    plant(app.data_dir, {**FILES, **OTHER_ENTRIES})


def _stop_clean(app: RunningApp) -> None:
    result: StopResult = app.stop()
    assert not result.killed, f"the app had to be killed\n{app.output_tail()}"
    assert result.returncode == 0, f"the app exited with {result.returncode}\n{app.output_tail()}"


@contextmanager
def launched(tmp_path: Path, *, empty: bool, name: str = "app") -> Iterator[RunningApp]:
    """The real app on a finished setup under ``tmp_path/name``: with the fake provider saved
    (content), or without (an empty workspace, where a restore is allowed)."""
    root = tmp_path / name
    root.mkdir()
    if empty:
        (root / "config").mkdir()
        write_bootstrap(root / "config", data_dir=root / "data", version=__version__)
        seed_workspace(root / "data", done=[step.id for step in m1_registry().steps])
        app = start_app(root, restart=True, output_name=name)
    else:
        app = start_app(root, output_name=name)
    try:
        yield app
    finally:
        _stop_clean(app)


# --- helpers -------------------------------------------------------------------------------


def open_data_by_keyboard(page: Page, app: RunningApp) -> None:
    """Settings, then the "Data and privacy" link by Tab and Enter."""
    page.add_init_script(RECORD_ANNOUNCEMENTS)
    open_app(page, app, "/settings")
    link = page.locator("main").get_by_role("link", name=LINK, exact=True)
    tab_to(page, link)
    page.keyboard.press("Enter")
    expect(page).to_have_url(f"{app.base_url}{PAGE}")
    page.wait_for_function("() => typeof window.htmx !== 'undefined'")
    expect(page.get_by_role("heading", name=LINK, level=1)).to_be_visible()


def shown_folder(page: Page, app: RunningApp) -> None:
    """The folder's path is loaded into the page (not part of it) and shows the real folder."""
    folder = page.locator("#data-folder")
    expect(folder).to_be_visible()
    text = folder.inner_text()
    assert str(app.data_dir) in text or str(app.data_dir.resolve()) in text, text


def is_blocked(locator: Locator) -> bool:
    return locator.get_attribute("aria-disabled") == "true" or locator.is_disabled()


def said(page: Page, pattern: str) -> None:
    page.wait_for_function(SAID_MATCHING, arg=pattern)


def dialog(page: Page) -> Locator:
    return page.get_by_role("dialog")


def record_posts(page: Page) -> list[Request]:
    posts: list[Request] = []
    page.on("request", lambda r: posts.append(r) if r.method == "POST" else None)
    return posts


def tab_in_dialog_to(page: Page, target: Locator) -> None:
    for _ in range(MAX_DIALOG_TABS):
        if is_focused(target):
            return
        page.keyboard.press("Tab")
        assert page.evaluate(FOCUS_IN_DIALOG), "Tab left the dialog"
    raise AssertionError("the control never received focus inside the dialog")


def assert_focus_trapped(page: Page, key: str) -> set[str]:
    reached: set[str] = set()
    for _ in range(MAX_DIALOG_TABS):
        page.keyboard.press(key)
        assert page.evaluate(FOCUS_IN_DIALOG), f"{key} moved focus out of the dialog"
        reached.add(str(page.evaluate(FOCUSED_NAME)))
    return reached


def assert_fits(page: Page) -> None:
    fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
    assert fits, f"scrollWidth {scroll_width} > {inner_width}"


def no_secrets_in_output(app: RunningApp) -> None:
    """The fact's text is in the file and the database, never in what the app prints or logs."""
    outputs = [app.stdout_text(), app.stderr_text()]
    outputs.extend(path.read_text(encoding="utf-8", errors="replace") for path in app.log_files())
    assert not [o for o in outputs if MARKER in o], "the exported text reached a log or console"


# --- export, then restore --------------------------------------------------------------------


def test_export_then_restore_into_empty_workspace_in_settings_by_keyboard(
    running_app: RunningApp, browser: Browser, tmp_path: Path
) -> None:
    seed(running_app)
    saved = tmp_path / "downloaded-export.json"
    with new_page(browser, DESKTOP) as page:
        open_data_by_keyboard(page, running_app)
        shown_folder(page, running_app)

        # A way back to Settings comes first.
        back = page.get_by_role("link", name="Back to Settings", exact=True)
        expect(back).to_have_attribute("href", "/settings")

        # Import is blocked here (the workspace has content) and says why, once; there is no
        # file to choose. Export is open.
        blocked = page.locator(IMPORT_BUTTON)
        assert is_blocked(blocked)
        expect(page.locator("#data-import-note")).to_have_text(
            "Import only works in a workspace with no content. This one already has some."
        )
        expect(page.locator(IMPORT_FILE)).to_have_count(0)
        expect(page.get_by_label("Include provider usage snapshots")).not_to_be_checked()

        tab_to(page, page.locator(EXPORT_BUTTON))
        page.keyboard.press("Enter")
        done = page.locator("#data-export-done")
        expect(done).to_contain_text(".json")
        name = EXPORT_NAME.search(done.inner_text())
        assert name is not None, done.inner_text()
        expect(done).to_contain_text("Download a copy and keep it somewhere private.")
        # Only the file name is emphasised, not the whole sentence.
        weight = "(e) => Number(getComputedStyle(e).fontWeight)"
        assert page.locator("#data-export-done").evaluate(weight) < 600
        assert page.locator("#data-export-done strong").evaluate(weight) >= 600
        expect(page.locator("#data-export-done strong")).to_have_text(name.group(0))
        said(page, "export")
        assert page.evaluate(FOCUS_IN, "#data-export"), "focus stays in the Export block"

        tab_to(page, page.locator(DOWNLOAD))
        with page.expect_download() as info:
            page.keyboard.press("Enter")
        download = info.value
        assert EXPORT_NAME.fullmatch(download.suggested_filename), download.suggested_filename
        assert download.suggested_filename == name.group(0)
        download.save_as(saved)
        expect(page.locator(DOWNLOAD)).to_be_focused()

    payload = json.loads(saved.read_bytes())
    assert payload["format"] == 1
    assert MARKER in saved.read_text(encoding="utf-8")
    assert {item["name"] for item in payload["files"]} == set(FILES)
    assert (running_app.data_dir / "exports" / name.group(0)).read_bytes() == saved.read_bytes()
    no_secrets_in_output(running_app)

    with (
        launched(tmp_path, empty=True, name="second") as second,
        new_page(browser, DESKTOP) as page,
    ):
        open_data_by_keyboard(page, second)
        assert not is_blocked(page.locator(IMPORT_BUTTON))
        expect(page.locator("#data-import-note")).to_have_count(0)

        tab_to(page, page.locator(IMPORT_FILE))
        page.set_input_files(IMPORT_FILE, str(saved))
        tab_to(page, page.locator(IMPORT_BUTTON))
        page.keyboard.press("Enter")

        expect(page.locator("#data-import-done")).to_contain_text("Restored")
        said(page, "restored")
        assert page.evaluate(FOCUS_IN, "#data-import"), "focus stays in the Import block"
        start_again = page.locator('#data-import a[href="/"]')
        tab_to(page, start_again)
        page.keyboard.press("Enter")
        expect(page).to_have_url(f"{second.base_url}/")

        # The fact is back, and so are the files, byte for byte.
        page.goto(f"{second.base_url}/facts")
        expect(page.get_by_text(FACT_TEXT).first).to_be_visible()
        for relative, content in FILES.items():
            assert (second.data_dir / relative).read_bytes() == content, relative
        assert not [p for p in second.data_dir.iterdir() if p.name.startswith(".restore-")]
        assert rows(second, "SELECT COUNT(*) FROM facts")[0][0] >= 1
        # Now it has content, so a second restore is blocked.
        open_app(page, second, PAGE)
        assert is_blocked(page.locator(IMPORT_BUTTON))
        no_secrets_in_output(second)


# --- delete all data ------------------------------------------------------------------------------


def test_delete_all_data_dialog_by_keyboard(running_app: RunningApp, browser: Browser) -> None:
    seed(running_app)
    assert (running_app.config_dir / "config.toml").is_file()
    with new_page(browser, DESKTOP) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        open_app(page, running_app, PAGE)
        posts = record_posts(page)
        opener = page.locator(DELETE_OPEN)

        # Open by keyboard: the safe choice has focus; Tab and Shift+Tab stay inside.
        tab_to(page, opener)
        page.keyboard.press("Enter")
        shown = dialog(page)
        expect(shown).to_be_visible()
        expect(shown).to_have_count(1)
        expect(shown).to_have_attribute("aria-modal", "true")
        page.wait_for_function(FOCUS_IN_DIALOG)
        expect(shown.locator("[data-modal-close]").first).to_be_focused()
        words = (shown.inner_text() + " " + page.locator("#data-delete").inner_text()).lower()
        assert "export" in words, "the dialog or the section suggests exporting first"
        assert "environment" in words
        forward = assert_focus_trapped(page, "Tab")
        assert "confirm" in forward and len(forward) >= 2, forward
        backward = assert_focus_trapped(page, "Shift+Tab")
        assert "confirm" in backward and len(backward) >= 2, backward

        # Esc closes it, sends nothing and gives focus back to the button that opened it.
        page.keyboard.press("Escape")
        expect(shown).to_be_hidden()
        expect(opener).to_be_focused()
        assert not posts, "closing the dialog sent nothing"

        # Again: a wrong word keeps Delete blocked, and Enter in the field erases nothing.
        page.keyboard.press("Enter")
        expect(shown).to_be_visible()
        page.wait_for_function(FOCUS_IN_DIALOG)
        field = shown.locator('input[name="confirm"]')
        confirm = shown.locator(DELETE_SUBMIT)
        tab_in_dialog_to(page, field)
        page.keyboard.type("delet")
        assert is_blocked(confirm)
        page.keyboard.press("Enter")
        # Nothing is erased: either nothing was sent, or the server refused the word (422).
        expect(shown).to_be_visible()
        for request in posts:
            response = request.response()
            assert response is not None and response.status == 422, request.url
        assert (running_app.data_dir / "app.db").is_file()
        assert page.url.endswith(PAGE)

        # The word frees it; Delete by keyboard erases everything and lands on first run.
        tab_in_dialog_to(page, field)
        page.keyboard.press("ControlOrMeta+a")
        page.keyboard.type("delete")
        expect(field).to_have_value("delete")
        expect(confirm).to_be_enabled()
        assert confirm.get_attribute("aria-disabled") != "true"
        tab_in_dialog_to(page, confirm)
        page.keyboard.press("Enter")
        expect(page).to_have_url(re.compile(r"/setup/welcome$"), timeout=20_000)
        expect(page.get_by_role("heading", level=1)).to_be_visible()
        # The start of setup says what happened, on screen and to a screen reader, for this
        # visit only.
        notice = page.locator("#erased-notice")
        expect(notice).to_be_visible()
        expect(notice).to_have_text("All data was deleted. You're at the start of setup again.")
        said(page, "All data was deleted")
        page.reload()
        expect(page.locator("#erased-notice")).to_be_hidden()

    assert not (running_app.config_dir / "config.toml").exists(), "config.toml is gone"
    for relative in {**FILES, **OTHER_ENTRIES}:
        assert not (running_app.data_dir / relative).exists(), relative
    assert rows(running_app, "SELECT COUNT(*) FROM facts")[0][0] == 0
    leftovers = " ".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in running_app.data_dir.rglob("*")
        if path.is_file() and path.suffix in {".json", ".log", ".txt"}
    )
    assert MARKER not in leftovers


# --- phone and accessibility ---------------------------------------------------------------------


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("empty", [False, True], ids=["with-content", "empty-workspace"])
def test_data_area_phone_390x844_and_axe(
    tmp_path: Path, browser: Browser, theme: str, empty: bool
) -> None:
    with (
        launched(tmp_path, empty=empty) as app,
        new_page(browser, PHONE, color_scheme=theme) as page,
    ):
        if not empty:
            seed(app)
        open_app(page, app, PAGE)
        shown_folder(page, app)  # the long path wraps inside the screen
        folder_box = page.locator("#data-folder").bounding_box()
        assert folder_box is not None and folder_box["x"] + folder_box["width"] <= PHONE["width"]
        wait_for_settled_styles(page)
        assert_fits(page)
        assert_no_serious_violations(page)

        # Targets of at least 24 by 24 px, and a blocked Import still reachable and explained.
        targets = {
            "Back to Settings": page.get_by_role("link", name="Back to Settings", exact=True),
            "Export": page.locator(EXPORT_BUTTON),
            "snapshots label": page.locator("#data-export-form label").first,
            "Import": page.locator(IMPORT_BUTTON),
            "Delete all data": page.locator(DELETE_OPEN),
        }
        if empty:
            targets["file input"] = page.locator(IMPORT_FILE)
        else:
            expect(page.locator(IMPORT_FILE)).to_have_count(0)  # blocked: nothing to choose
        for label, locator in targets.items():
            assert_target_size(locator, label)
        assert is_blocked(page.locator(IMPORT_BUTTON)) is (not empty)
        if not empty:
            assert page.evaluate(GAP_ABOVE, "#data-import-note") >= 8, "paragraphs do not touch"

        # An export on the phone layout: its result and the Download button fit and are clean.
        page.locator(EXPORT_BUTTON).click()
        expect(page.locator("#data-export-done")).to_contain_text(".json")
        expect(page.locator(DOWNLOAD)).to_be_visible()  # hidden until app.js has revealed it
        assert_target_size(page.locator(DOWNLOAD), "Download")
        # Paragraphs do not touch: the fresh result is set apart from the form above it, and the
        # Download button from the result.
        assert page.evaluate(GAP_ABOVE, "#data-export-done") >= 8
        assert page.evaluate(GAP_ABOVE, "#data-export-done + .form-actions") >= 8
        wait_for_settled_styles(page)
        assert_fits(page)
        assert_no_serious_violations(page)

        # The dialog fits the screen, and is clean.
        page.locator(DELETE_OPEN).click()
        shown = dialog(page)
        expect(shown).to_be_visible()
        box = page.evaluate(DIALOG_BOX)
        assert box["left"] >= 0 and box["right"] <= box["innerWidth"], box
        # It opens at the top: its title is on screen, not scrolled away by the focus move.
        view = page.evaluate(DIALOG_TITLE_IN_VIEW)
        assert view["scrollTop"] == 0, view
        assert view["titleTop"] >= 0 and view["titleBottom"] <= view["innerHeight"], view
        expect(shown.locator("[data-modal-close]").first).to_be_focused()
        for label, locator in {
            "word field": shown.locator('input[name="confirm"]'),
            "Delete": shown.locator(DELETE_SUBMIT),
            "close": shown.locator("[data-modal-close]").first,
        }.items():
            assert_target_size(locator, label)
        wait_for_settled_styles(page)
        assert_fits(page)
        assert_no_serious_violations(page)
        page.keyboard.press("Escape")
        expect(shown).to_be_hidden()
        expect(page.locator(DELETE_OPEN)).to_be_focused()
