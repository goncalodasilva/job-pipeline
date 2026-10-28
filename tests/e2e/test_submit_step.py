"""M6.10 the Submit step (step 5) in a real browser with the real CLI, fictional facts, the
keyboard, a phone viewport and axe. Markup hooks and routes are in the docstring of
``tests/export_pages.py``.

The app starts with a completed setup (the fake provider saved), so the CV and the letter are the
fake provider's practice answers. A fictional application with a current summary, confirmed facts,
PT as the CV country and an approved PT template is written into the app's temporary database
first, with a saved name (the export needs one). The real PDF and Word renderers write the files
into the app's temporary data folder; the test downloads them with the keyboard and reads them back
(pypdf, python-docx). Keyboard path: Tab, Enter and typing only. No fixed waits.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect

from app.domain.settings import PERSONAL_DETAILS, PersonalDetails
from app.modules.storage import Database
from app.services.settings import SettingsService
from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_board import rows
from tests.e2e.test_cover_letter_step import open_letter
from tests.e2e.test_cv_step import open_cv, seeded_cv
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, press_by_keyboard, wait_said
from tests.e2e.test_shell import (
    assert_target_size,
    has_no_horizontal_scroll,
    new_page,
    open_app,
)
from tests.e2e.test_tailoring_workspace import heading
from tests.export_pages import (
    APPLIED_ANNOUNCE,
    BUTTON_NAMES,
    EXPORT_ANNOUNCE,
    GENERATED_NAME,
    MARK_APPLIED,
    squash,
    text_of,
)
from tests.fakes.clock import FrozenClock
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e

NAME = "Zara Quillfeather-Marker"
EMAIL = "zara.marker@example.org"


def with_name(app: RunningApp) -> str:
    """The seeded Portugal application, and a saved name and email for the exports."""
    aid = seeded_cv(app)
    clock = FrozenClock(datetime(2026, 1, 5, 9, 0, tzinfo=UTC))
    with Database.open(app.data_dir / "app.db") as db:
        settings = SettingsService(db, clock)
        settings.save(
            PERSONAL_DETAILS,
            PersonalDetails(name=NAME, email=EMAIL),
            expected_version=settings.load(PERSONAL_DETAILS).version,
        )
    return aid


def approve_cv_and_letter(page: Page, app: RunningApp, aid: str) -> tuple[list[str], list[str]]:
    """Draft and approve the CV (step 3) and the letter (step 4) with the keyboard. Returns the
    texts of the CV's lines and of the letter's paragraphs as approved."""
    open_cv(page, app, aid)
    cv_lines = page.locator("li[data-unit-id] [data-line-text]").all_inner_texts()
    press_by_keyboard(page, page.locator("#cv-approve"))
    expect(page.locator("#cv-approved")).to_be_visible()
    open_letter(page, app, aid)
    paragraphs = page.locator("li[data-unit-id] [data-line-text]").all_inner_texts()
    press_by_keyboard(page, page.locator("#letter-approve"))
    expect(page.locator("#letter-approved")).to_be_visible()
    assert cv_lines and paragraphs
    return cv_lines, paragraphs


def go_to_submit(page: Page) -> None:
    press_by_keyboard(page, page.locator("#letter-next"))
    expect(heading(page, 5, "Submit")).to_be_visible()


def test_submit_step_export_download_both_files_then_mark_applied(
    running_app: RunningApp, browser: Browser, tmp_path: Path
):
    aid = with_name(running_app)
    with new_page(browser, DESKTOP) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        cv_lines, paragraphs = approve_cv_and_letter(page, running_app, aid)
        go_to_submit(page)

        # The step is real: a checklist, the export button and the mark-Applied button.
        expect(page.locator("#tailoring")).not_to_contain_text("later step", ignore_case=True)
        checks = page.locator("#submit-checklist li[data-check]")
        expect(checks).to_have_count(5)
        expect(page.locator('#submit-checklist li[data-check="cv"]')).to_have_attribute(
            "data-done", "true"
        )
        expect(page.locator('#submit-checklist li[data-check="files"]')).to_have_attribute(
            "data-done", "false"
        )
        expect(page.locator("#submit-mark-applied")).to_have_text(MARK_APPLIED)

        # Export by keyboard: announced, the heading takes focus, four files are listed.
        press_by_keyboard(page, page.locator("#submit-export"))
        wait_said(page, EXPORT_ANNOUNCE)
        expect(page.locator("#submit-heading")).to_be_focused()
        expect(page.locator("#submit-files li[data-file]")).to_have_count(4)
        expect(page.locator('#submit-checklist li[data-check="files"]')).to_have_attribute(
            "data-done", "true"
        )
        expect(page.locator('#submit-checklist li[data-check="pages"]')).to_have_attribute(
            "data-done", "true"
        )

        # Download each file by keyboard, then read it back the way a user's reader would.
        saved: dict[tuple[str, str], bytes] = {}
        for (document, fmt), name in BUTTON_NAMES.items():
            button = page.get_by_role("button", name=name, exact=True)
            with page.expect_download() as info:
                press_by_keyboard(page, button)
            download = info.value
            assert GENERATED_NAME.fullmatch(download.suggested_filename), (
                download.suggested_filename
            )
            assert download.suggested_filename.endswith("." + fmt)
            target = tmp_path / download.suggested_filename
            download.save_as(target)
            saved[(document, fmt)] = target.read_bytes()
        assert len(saved) == 4
        for (document, fmt), content in saved.items():
            extracted = text_of(content, fmt)
            assert squash(NAME) in extracted
            wanted = cv_lines if document == "cv" else paragraphs
            position = 0
            for line in wanted:  # every approved line, in the order approved
                found = extracted.find(squash(line), position)
                assert found >= 0, f"{line!r} is missing from the {document} {fmt} file"
                position = found + len(squash(line))
        assert saved[("cv", "pdf")].startswith(b"%PDF-")
        assert saved[("cv", "docx")].startswith(b"PK")

        # Mark Applied by keyboard: the existing status move, announced, and the freeze happened.
        press_by_keyboard(page, page.locator("#submit-mark-applied"))
        wait_said(page, APPLIED_ANNOUNCE)
        expect(page.locator("#submit-applied")).to_be_visible()
        expect(page.locator("#submit-mark-applied")).to_have_count(0)
        expect(page.locator("#submit-heading")).to_be_focused()
        expect(page.locator("#submit-files li[data-file]")).to_have_count(4)
        assert_no_serious_violations(page)

    (status, applied_at) = rows(
        running_app, "SELECT status_key, applied_at FROM applications WHERE id=?", (aid,)
    )[0]
    assert status == "applied" and applied_at
    (cv_id, letter_id) = rows(
        running_app,
        "SELECT cv_version_id, letter_version_id FROM application_submissions "
        "WHERE application_id=?",
        (aid,),
    )[0]
    assert cv_id and letter_id
    for version_id in (cv_id, letter_id):
        assert rows(
            running_app, "SELECT status FROM document_versions WHERE id=?", (version_id,)
        ) == [("sent",)]
    exported = rows(
        running_app,
        "SELECT e.stored_name FROM document_exports e JOIN document_versions v "
        "ON v.id=e.document_version_id WHERE v.id IN (?, ?)",
        (cv_id, letter_id),
    )
    assert len(exported) == 4
    for (name,) in exported:  # the files are in the data folder, under the application
        assert (running_app.data_dir / "documents" / aid / name).is_file()


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone-390x844"])
def test_submit_step_phone_390x844_and_axe(
    running_app: RunningApp, browser: Browser, viewport, scheme
):
    aid = with_name(running_app)
    with new_page(browser, viewport, color_scheme=scheme) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        # Step 5 before anything is approved: the checklist says what is missing.
        open_app(page, running_app, f"/applications/{aid}?tab=tailoring&step=5")
        expect(heading(page, 5, "Submit")).to_be_visible()
        expect(page.locator("#submit-checklist li[data-check]")).to_have_count(5)
        expect(page.locator("#submit-export")).to_have_attribute("aria-disabled", "true")
        wait_for_settled_styles(page)
        assert has_no_horizontal_scroll(page)[0]
        assert_target_size(page.locator("#submit-mark-applied"), "mark Applied")
        assert_no_serious_violations(page)

        # Ready to export, exported, and applied: each state at this size and colour scheme.
        approve_cv_and_letter(page, running_app, aid)
        go_to_submit(page)
        expect(page.locator("#submit-export")).not_to_have_attribute("aria-disabled", "true")
        wait_for_settled_styles(page)
        assert has_no_horizontal_scroll(page)[0]
        assert_no_serious_violations(page)

        press_by_keyboard(page, page.locator("#submit-export"))
        expect(page.locator("#submit-files li[data-file]")).to_have_count(4)
        wait_for_settled_styles(page)
        assert has_no_horizontal_scroll(page)[0]
        for name in BUTTON_NAMES.values():
            assert_target_size(page.get_by_role("button", name=name, exact=True), name)
        assert_target_size(page.locator("#submit-export"), "export")
        assert_target_size(page.locator("#submit-mark-applied"), "mark Applied")
        assert_no_serious_violations(page)

        press_by_keyboard(page, page.locator("#submit-mark-applied"))
        expect(page.locator("#submit-applied")).to_be_visible()
        wait_for_settled_styles(page)
        assert has_no_horizontal_scroll(page)[0]
        assert_no_serious_violations(page)
