"""M9.1 part D2 setup step 1 "Restore from an export" in a real browser (AC-M9.3; ADR-131 items
3-4; spec 3.9). The assumed route and markup are in ``tests/integration/test_setup_restore.py``
(``section#setup-restore``, ``form#restore-form``, ``input#restore-file``,
``button#restore-submit``, ``p#restore-error``) and the export side in ``tests/data_pages.py``.

The first test exports from a seeded first app through the real Data and privacy screen by
keyboard, then starts a brand-new app at step 1 (a true first run: nothing exists), types a folder,
chooses the downloaded file and restores by keyboard. The second checks the step at 390x844 in light
and dark with an axe scan, and a refused upload. The files the browser downloads and uploads stay in
``tmp_path``. No fixed waits; every value is fictional.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from playwright.sync_api import Browser, expect

from app.config import load_bootstrap
from tests.data_pages import EXPORT_NAME, FACT_TEXT, FILES, MARKER
from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_settings_board import tab_to
from tests.e2e.test_settings_data import (
    DOWNLOAD,
    EXPORT_BUTTON,
    assert_fits,
    no_secrets_in_output,
    open_data_by_keyboard,
    seed,
)
from tests.e2e.test_shell import assert_target_size, new_page, open_app
from tests.running_app import RunningApp, StopResult, start_app

pytestmark = pytest.mark.e2e

WELCOME = "/setup/welcome"
RESTORE_PATH = "/setup/welcome/restore"
FOLDER = "#folder"
RESTORE_FILE = "#restore-file"
RESTORE_BUTTON = "#restore-submit"
RESTORE_ERROR = "#restore-error"


@contextmanager
def first_run(tmp_path: Path, name: str) -> Iterator[RunningApp]:
    """A real app before step 1: nothing exists until the step is used."""
    root = tmp_path / name
    root.mkdir()
    app = start_app(root, setup="first_run", output_name=name)
    try:
        yield app
    finally:
        result: StopResult = app.stop()
        assert not result.killed, f"the app had to be killed\n{app.output_tail()}"
        assert result.returncode == 0, f"the app exited with {result.returncode}"


def test_export_then_restore_at_setup_step_1_by_keyboard(
    running_app: RunningApp, browser: Browser, tmp_path: Path
) -> None:
    seed(running_app)
    saved = tmp_path / "downloaded-export.json"

    # 1. Export from the first app through its real screen, by keyboard.
    with new_page(browser, DESKTOP) as page:
        open_data_by_keyboard(page, running_app)
        tab_to(page, page.locator(EXPORT_BUTTON))
        page.keyboard.press("Enter")
        done = page.locator("#data-export-done")
        expect(done).to_contain_text(".json")
        assert EXPORT_NAME.search(done.inner_text()) is not None
        tab_to(page, page.locator(DOWNLOAD))
        with page.expect_download() as info:
            page.keyboard.press("Enter")
        info.value.save_as(saved)
    assert MARKER in saved.read_text(encoding="utf-8")
    assert json.loads(saved.read_bytes())["format"] == 1

    # 2. A brand-new app at step 1.
    with first_run(tmp_path, "second") as second, new_page(browser, DESKTOP) as page:
        chosen = tmp_path / "second" / "my-restored-data"
        open_app(page, second, "/")
        expect(page).to_have_url(f"{second.base_url}{WELCOME}")
        expect(page.locator('[aria-current="step"]')).to_have_text("Welcome and data folder")
        assert not second.data_dir.exists(), "a first run creates nothing before step 1"
        expect(page.locator("#restore-heading")).to_have_text("Restore from an export")

        # Keyboard only: the Data folder field, then the restore control.
        tab_to(page, page.locator(FOLDER))
        page.keyboard.press("ControlOrMeta+a")
        page.keyboard.type(str(chosen))
        expect(page.locator(FOLDER)).to_have_value(str(chosen))
        tab_to(page, page.locator(RESTORE_FILE))
        page.set_input_files(RESTORE_FILE, str(saved))
        tab_to(page, page.locator(RESTORE_BUTTON))
        with page.expect_response(lambda r: r.url.endswith(RESTORE_PATH)) as sent:
            page.keyboard.press("Enter")

        answer = sent.value
        assert answer.status == 200, answer.status
        assert answer.headers.get("hx-redirect") == "/"
        said = json.loads(answer.headers["hx-trigger"])["announce"]
        assert "Restored" in said, said
        expect(page).to_have_url(f"{second.base_url}/")
        expect(page.locator('[aria-current="step"]')).to_have_count(0)  # past the guide
        expect(page.get_by_role("heading", level=1)).to_be_visible()

        # The fact is back, and so are the files, byte for byte, in the folder that was typed.
        page.goto(f"{second.base_url}/facts")
        expect(page.get_by_text(FACT_TEXT).first).to_be_visible()
        for relative, content in FILES.items():
            assert (chosen / relative).read_bytes() == content, relative
        assert not [p for p in chosen.iterdir() if p.name.startswith(".restore-")]
        assert not second.data_dir.exists(), "only the typed folder was created"
        bootstrap = load_bootstrap(second.config_dir)
        assert bootstrap is not None and bootstrap.data_dir == chosen
        no_secrets_in_output(second)
    no_secrets_in_output(running_app)


@pytest.mark.parametrize("running_app", ["first_run"], indirect=True)
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_setup_restore_step_phone_390x844_and_axe(
    running_app: RunningApp, browser: Browser, tmp_path: Path, theme: str
) -> None:
    refused = tmp_path / "not-an-export.json"
    refused.write_text('{"note": "fictional, and cut off', encoding="utf-8")
    with new_page(browser, PHONE, color_scheme=theme) as page:
        open_app(page, running_app, WELCOME)
        expect(page.locator("#restore-heading")).to_be_visible()
        for label, locator in {
            "restore file": page.locator(RESTORE_FILE),
            "restore button": page.locator(RESTORE_BUTTON),
            "folder": page.locator(FOLDER),
        }.items():
            assert_target_size(locator, label)
        page.locator(RESTORE_FILE).scroll_into_view_if_needed()
        wait_for_settled_styles(page)
        assert_fits(page)
        assert_no_serious_violations(page)

        # A refused upload: the fixed message is on the file field, nothing leaves the screen.
        page.set_input_files(RESTORE_FILE, str(refused))
        page.locator(RESTORE_BUTTON).click()
        field = page.locator(RESTORE_FILE)
        expect(field).to_have_attribute("aria-invalid", "true")
        error = page.locator(RESTORE_ERROR)
        expect(error).to_be_visible()
        expect(error).to_have_attribute("role", "alert")
        assert "restore-error" in (field.get_attribute("aria-describedby") or "").split()
        assert "fictional, and cut off" not in error.inner_text(), "nothing of the file is shown"
        assert "not-an-export" not in error.inner_text()
        expect(page).to_have_url(f"{running_app.base_url}{WELCOME}")
        expect(field).to_be_focused()
        expect(page.locator(f"{FOLDER}[readonly]")).to_have_count(1)  # the opened folder is fixed
        expect(page.locator("#folder-fixed")).to_have_text(
            "This folder is open now. Choose the export file again, or use it as it is "
            "and continue."
        )
        assert "Nothing was restored" in error.inner_text()
        expect(page.locator(RESTORE_BUTTON)).to_be_enabled()
        wait_for_settled_styles(page)
        assert_fits(page)
        assert_no_serious_violations(page)

    # The refused file left the opened workspace empty, with no upload behind.
    assert (running_app.data_dir / "app.db").is_file()
    assert not [p for p in running_app.data_dir.iterdir() if p.name.startswith(".restore-")]
    assert not (running_app.data_dir / "documents").exists()


@pytest.mark.parametrize("running_app", ["first_run"], indirect=True)
def test_a_stale_setup_tab_keeps_its_guide_when_restore_is_no_longer_offered(
    running_app: RunningApp, browser: Browser, tmp_path: Path
) -> None:
    """Step 1 is finished in another tab; the stale tab then tries to restore. The answer is a 409
    that htmx swaps into the guide, so it must be the guide of the step the app is on now (the
    guide does not vanish), and the restore form is not offered again."""
    export = tmp_path / "an-export.json"
    export.write_text('{"note": "fictional"}', encoding="utf-8")
    with new_page(browser, DESKTOP) as stale, new_page(browser, DESKTOP) as other:
        open_app(stale, running_app, WELCOME)
        open_app(other, running_app, WELCOME)
        expect(stale.locator(RESTORE_BUTTON)).to_be_visible()

        other.locator(FOLDER).fill(str(tmp_path / "chosen"))
        other.get_by_role("button", name="Use this folder and continue").click()
        expect(other.locator('[aria-current="step"]')).not_to_contain_text("Welcome")

        stale.set_input_files(RESTORE_FILE, str(export))
        with stale.expect_response(lambda r: r.url.endswith(RESTORE_PATH)) as sent:
            stale.locator(RESTORE_BUTTON).click()

        assert sent.value.status == 409
        expect(stale.locator("#setup-guide")).to_be_visible()
        expect(stale.get_by_role("heading", level=1)).to_have_text(
            "Let's set up Job Search Pipeline"
        )
        expect(stale.locator('[aria-current="step"]')).to_have_count(1)
        expect(stale.locator('[aria-current="step"]')).not_to_contain_text("Welcome")
        expect(stale.locator("#restore-form")).to_have_count(0)
