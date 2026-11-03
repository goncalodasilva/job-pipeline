"""M6.12: the whole M6 journey in a browser with the real CLI and the fake AI provider, by keyboard,
at desktop and phone (390x844) width, with an axe scan and a sideways-scroll check on the screens
named in the ``checked`` calls: Your details with the photo, the gap check, the angle, the
approved template with the first-time Generate form, the CV draft and the approved CV, the
letter draft and the approved letter, the exported files, the applied state and the board's
drawer with the documents sent. Nothing here uses a real provider, the network or real data;
every person, job and file is fictional.

The app starts with a completed setup (the fake provider saved) and a fictional application with a
current summary and confirmed facts, PT as the CV country (the e2e fixtures of the earlier M6
tests). Then, with Tab, Enter and typing only (the one exception is choosing the picture itself:
a browser's file dialog is operating-system UI, so the file input receives the file
programmatically after the keyboard has focused it):

1. Settings > Your details: a photo is uploaded (the name and email are written into the temporary
   database, as the earlier M6 tests do, to keep the journey short).
2. Step 1, gap check: the gaps are listed and, with must-haves missing, the page says plainly that
   the role may not be worth applying for (AC-M6.8).
3. Step 2: the angle is proposed, edited and used.
4. Step 3: the Portugal template is drafted once and approved (AC-M6.5), then the CV is generated
   and approved; every line is shown cited (AC-M6.1).
5. Step 4: the letter is drafted and approved.
6. Step 5: the files are exported and downloaded; the text of each equals the approved lines, the
   CV holds the photo and the letter does not (AC-M6.5, AC-M6.6).
7. Mark Applied: the versions are frozen (AC-M6.7), and the board's drawer shows the documents
   sent, with their files.

Acceptance criteria whose detail is checked by named integration tests (AC-M6.1 blocking on
flags, AC-M6.2, AC-M6.3, AC-M6.4, the freeze's later edits) are not repeated here; the per-step
e2e tests ``test_cv_step.py`` and ``test_cover_letter_step.py`` cover them in a browser.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from playwright.sync_api import Locator, Page, expect

from app.domain.settings import PERSONAL_DETAILS, PersonalDetails
from app.modules.storage import Database
from app.services.settings import SettingsService
from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_board import rows
from tests.e2e.test_cv_template_screens import expand_details, png_file, seeded_in_portugal
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, press_by_keyboard, tab_until, wait_said
from tests.e2e.test_shell import has_no_horizontal_scroll, new_page, open_app
from tests.e2e.test_tailoring_workspace import RESULT_TIMEOUT_MS, heading, region, stepper_link
from tests.export_pages import (
    APPLIED_ANNOUNCE,
    BUTTON_NAMES,
    EXPORT_ANNOUNCE,
    GENERATED_NAME,
    squash,
    text_of,
)
from tests.fakes.clock import FrozenClock
from tests.render_support import docx_media, pdf_image_count
from tests.running_app import RunningApp
from tests.waiting import wait_until

pytestmark = pytest.mark.e2e

NAME = "Zara Quillfeather-Marker"
EMAIL = "zara.marker@example.org"
PRACTICE = "Practice data (fake provider)"


def with_details(app: RunningApp) -> str:
    """The seeded Portugal application and a saved name and email for the exports."""
    aid = seeded_in_portugal(app)
    clock = FrozenClock(datetime(2026, 1, 5, 9, 0, tzinfo=UTC))
    with Database.open(app.data_dir / "app.db") as db:
        settings = SettingsService(db, clock)
        settings.save(
            PERSONAL_DETAILS,
            PersonalDetails(name=NAME, email=EMAIL),
            expected_version=settings.load(PERSONAL_DETAILS).version,
        )
    return aid


def fits(page: Page, label: str) -> None:
    ok, scroll_width, inner_width = has_no_horizontal_scroll(page)
    assert ok, f"{label}: scrollWidth {scroll_width} > {inner_width}"


def checked(page: Page, label: str) -> None:
    """The screen fits the width and has no serious accessibility violation."""
    wait_for_settled_styles(page)
    fits(page, label)
    assert_no_serious_violations(page)


def start_task_by_keyboard(page: Page, app: RunningApp, locator: Locator, task_type: str) -> None:
    """Press a start button by keyboard once it is shown and enabled, and wait (on a condition,
    no sleeps) until the app has recorded the task. Enter pressed while the step is still being
    swapped in is swallowed; the button is then pressed again, but only while no task of that type
    exists, so nothing is ever started twice."""
    count = "SELECT count(*) FROM tasks WHERE type=?"
    for _ in range(3):
        expect(locator).to_be_visible()
        expect(locator).to_be_enabled()
        press_by_keyboard(page, locator)
        try:
            wait_until(
                lambda: rows(app, count, (task_type,))[0][0] > 0,
                timeout=5,
                message=f"{task_type} recorded",
            )
            return
        except AssertionError:
            continue
    raise AssertionError(f"{task_type} was not started by keyboard after three presses")


def upload_photo(page: Page, app: RunningApp) -> None:
    open_app(page, app, "/settings/your-details")
    expand_details(page)
    photo = page.locator("#photo-form")
    picker = photo.locator('input[type="file"]')
    tab_until(page, picker)
    picker.set_input_files(png_file())
    press_by_keyboard(page, photo.get_by_role("button", name="Upload photo", exact=True))
    wait_said(page, "Photo saved.")
    expect(page.locator("#photo-preview")).to_be_visible()
    checked(page, "settings with the photo")


def gap_check(page: Page, app: RunningApp, aid: str) -> None:
    open_app(page, app, f"/applications/{aid}")
    press_by_keyboard(page, page.get_by_role("link", name="Tailoring", exact=True))
    expect(heading(page, 1, "Gap check")).to_be_visible()
    press_by_keyboard(page, region(page).get_by_role("button", name="Check gaps", exact=True))
    wait_said(page, "Gap check started.")
    expect(region(page).locator("[data-requirement-id]")).to_have_count(
        4, timeout=RESULT_TIMEOUT_MS
    )
    expect(region(page).locator("#gap-check-message")).to_contain_text(
        "It may not be worth applying for."
    )
    checked(page, "gap check")


def choose_angle(page: Page) -> None:
    press_by_keyboard(page, stepper_link(page, "Angle"))
    expect(heading(page, 2, "Angle")).to_be_visible()
    press_by_keyboard(page, region(page).get_by_role("button", name="Propose angles"))
    wait_said(page, "Angle proposals started.")
    proposals = region(page).locator("li[data-angle]")
    expect(proposals).to_have_count(2, timeout=RESULT_TIMEOUT_MS)
    first = proposals.first
    tab_until(page, first.get_by_label("Angle name", exact=True))
    page.keyboard.press("ControlOrMeta+A")
    page.keyboard.type("Plan with care")
    press_by_keyboard(page, first.get_by_role("button", name="Use this angle"))
    wait_said(page, "Angle saved.")
    expect(region(page).locator("#chosen-angle")).to_have_text("Your angle: Plan with care")
    checked(page, "angle")


def template_and_cv(page: Page, app: RunningApp) -> list[str]:
    """Draft and approve the country template once, then generate and approve the CV. Returns the
    lines of the CV as approved."""
    press_by_keyboard(page, stepper_link(page, "CV"))
    expect(heading(page, 3, "CV")).to_be_visible()
    panel = page.locator("#cv-template")
    expect(panel).to_have_attribute("data-state", "none")
    start_task_by_keyboard(
        page,
        app,
        panel.get_by_role("button", name="Draft a template for Portugal", exact=True),
        "draft_cv_template",
    )
    expect(panel).to_have_attribute("data-state", "draft", timeout=RESULT_TIMEOUT_MS)
    expect(panel).to_contain_text(PRACTICE)
    press_by_keyboard(page, panel.get_by_role("button", name="Approve template", exact=True))
    wait_said(page, "Template approved.")
    expect(panel).to_have_attribute("data-state", "approved")
    assert [r[0] for r in rows(app, "SELECT status FROM cv_templates WHERE country='PT'")] == [
        "approved"
    ]
    # Focus moved to the panel's heading, and the live region said it exactly once.
    expect(page.locator("#cv-template-heading")).to_be_focused()
    said = page.evaluate(
        "(text) => window.__said.filter((words) => words === text).length", "Template approved."
    )
    assert said == 1, f"'Template approved.' was announced {said} times"
    # With the template approved the same screen offers to make the CV: the "approve a template
    # first" note is gone and the button is there, without leaving or reloading the step. This
    # first-time Generate form is the screen that is scanned and width-checked.
    expect(page.locator("#cv-not-ready")).to_have_count(0)
    expect(page.locator("#cv-generate-start")).to_be_visible()
    checked(page, "template approved")
    start_task_by_keyboard(page, app, page.locator("#cv-generate-start"), "tailor_cv")
    expect(page.locator('#cv-step[data-state="draft"]')).to_be_visible(timeout=RESULT_TIMEOUT_MS)
    lines = page.locator("li[data-unit-id] [data-line-text]").all_inner_texts()
    assert lines, "the CV has lines"
    # Every line cites facts (AC-M6.1): none is flagged and approval is open.
    expect(page.locator("[data-flags]")).to_have_count(0)
    expect(page.locator("#cv-approve")).not_to_have_attribute("aria-disabled", "true")
    checked(page, "CV draft")
    press_by_keyboard(page, page.locator("#cv-approve"))
    expect(page.locator("#cv-approved")).to_be_visible()
    checked(page, "CV approved")
    return lines


def letter(page: Page, app: RunningApp) -> list[str]:
    press_by_keyboard(page, stepper_link(page, "Cover letter"))
    expect(heading(page, 4, "Cover letter")).to_be_visible()
    start_task_by_keyboard(page, app, page.locator("#letter-generate-start"), "draft_cover_letter")
    expect(page.locator('#letter-step[data-state="draft"]')).to_be_visible(
        timeout=RESULT_TIMEOUT_MS
    )
    paragraphs = page.locator("li[data-unit-id] [data-line-text]").all_inner_texts()
    assert paragraphs
    checked(page, "letter draft")
    press_by_keyboard(page, page.locator("#letter-approve"))
    expect(page.locator("#letter-approved")).to_be_visible()
    checked(page, "letter approved")
    return paragraphs


def export_and_download(
    page: Page, tmp_path: Path, cv_lines: list[str], paragraphs: list[str]
) -> dict[tuple[str, str], bytes]:
    press_by_keyboard(page, page.locator("#letter-next"))
    expect(heading(page, 5, "Submit")).to_be_visible()
    checks = page.locator("#submit-checklist li[data-check]")
    expect(checks).to_have_count(5)
    press_by_keyboard(page, page.locator("#submit-export"))
    wait_said(page, EXPORT_ANNOUNCE)
    expect(page.locator("#submit-files li[data-file]")).to_have_count(4)
    checked(page, "exported")
    saved: dict[tuple[str, str], bytes] = {}
    for (document, fmt), name in BUTTON_NAMES.items():
        with page.expect_download() as info:
            press_by_keyboard(page, page.get_by_role("button", name=name, exact=True))
        download = info.value
        assert GENERATED_NAME.fullmatch(download.suggested_filename), download.suggested_filename
        target = tmp_path / download.suggested_filename
        download.save_as(target)
        saved[(document, fmt)] = target.read_bytes()
    for (document, fmt), content in saved.items():  # AC-M6.6: exactly the approved content
        extracted = text_of(content, fmt)
        assert squash(NAME) in extracted
        position = 0
        for line in cv_lines if document == "cv" else paragraphs:
            found = extracted.find(squash(line), position)
            assert found >= 0, f"{line!r} is missing from the {document} {fmt} file"
            position = found + len(squash(line))
    return saved


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone-390x844"])
def test_m6_journey_gap_angle_template_cv_letter_export_applied_drawer(
    running_app: RunningApp, browser, tmp_path: Path, viewport: dict[str, int]
) -> None:
    app = running_app
    aid = with_details(app)
    with new_page(browser, viewport) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        upload_photo(page, app)
        gap_check(page, app, aid)
        choose_angle(page)
        cv_lines = template_and_cv(page, app)
        paragraphs = letter(page, app)
        saved = export_and_download(page, tmp_path, cv_lines, paragraphs)

        # The photo is in the CV (the template has the slot and one was uploaded), never in the
        # letter (AC-M6.5).
        assert pdf_image_count(saved[("cv", "pdf")]) >= 1
        assert docx_media(saved[("cv", "docx")])
        assert pdf_image_count(saved[("cover_letter", "pdf")]) == 0
        assert docx_media(saved[("cover_letter", "docx")]) == []

        # Mark Applied by keyboard: announced, and the versions are frozen (AC-M6.7).
        press_by_keyboard(page, page.locator("#submit-mark-applied"))
        wait_said(page, APPLIED_ANNOUNCE)
        expect(page.locator("#submit-applied")).to_be_visible()
        checked(page, "applied")
        frozen = rows(
            app,
            "SELECT s.cv_version_id, s.letter_version_id, a.applied_at "
            "FROM application_submissions s JOIN applications a ON a.id=s.application_id "
            "WHERE s.application_id=?",
            (aid,),
        )
        assert len(frozen) == 1 and frozen[0][0] and frozen[0][1] and frozen[0][2]
        for version_id in frozen[0][:2]:
            assert rows(app, "SELECT status FROM document_versions WHERE id=?", (version_id,)) == [
                ("sent",)
            ]

        # The drawer shows what was sent, with its files, reachable by keyboard.
        open_app(page, app, "/board")
        link = page.locator(f'[data-application-id="{aid}"] a[hx-get$="/detail"]')
        press_by_keyboard(page, link)
        drawer = page.locator("#application-drawer")
        expect(drawer).to_be_visible()
        sent = page.locator("#documents-sent")
        expect(sent).to_contain_text("Version 1")
        downloads = sent.locator("[data-file-download]")
        expect(downloads).to_have_count(4)
        with page.expect_download() as downloaded:
            press_by_keyboard(page, downloads.first)
        assert downloaded.value.failure() is None
        expect(downloads.first).to_be_focused()
        checked(page, "drawer with documents sent")
