"""M6.6 Your details, the photo and the country CV template in a real browser: contact details, a
photo upload and a template draft, review and approval by keyboard with announcements; then the
same screens at 390x844 and desktop size, light and dark, with axe and no sideways scroll.

The app starts with a completed setup (the fake provider saved), so the template draft is the fake
provider's practice template. A fictional application (the one of ``test_tailoring_workspace``) is
written into the app's temporary database. Selectors and messages follow ``tests/template_pages.py``
(unit ``#cv-template`` on step 3 of the Tailoring tab, Settings > CV templates), and
``tests/details_pages.py`` (``#details-settings`` loaded lazily into Settings, ``#details-form``,
``#photo-form``, ``#photo-preview``).

Keyboard path: Tab, Space and Enter only. The one exception is choosing the picture itself: a
browser's file dialog is operating-system UI, so the file input receives the file programmatically
after the keyboard has focused it, and the upload button is pressed by keyboard. No fixed waits.
"""

import json
import re
import stat
from datetime import UTC, datetime

import pytest
from playwright.sync_api import Browser, Page, expect

from app.domain.settings import JOB_PREFERENCES, JobPreferences
from app.modules.storage import Database
from app.services.settings import SettingsService
from tests.details_pages import GOOD
from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_board import rows
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, press_by_keyboard, tab_until, wait_said
from tests.e2e.test_shell import assert_target_size, has_no_horizontal_scroll, new_page, open_app
from tests.e2e.test_tailoring_workspace import RESULT_TIMEOUT_MS, seeded, stepper_link
from tests.fakes.clock import FrozenClock
from tests.running_app import RunningApp
from tests.tailoring_pages import page_url
from tests.template_pages import real_png
from tests.waiting import wait_until

pytestmark = pytest.mark.e2e

PRACTICE = "Practice data (fake provider)"
UNIT = "#cv-template"
HEADING = "CV template: Portugal"
SAVED = "Template saved. Approve it to use it."
FIELDS = (
    ("Name", GOOD["name"]),
    ("Email", GOOD["email"]),
    ("Phone", GOOD["phone"]),
    ("Location", GOOD["location"]),
    ("Link 1", GOOD["link_1"]),
    ("Link 2", GOOD["link_2"]),
)


def seeded_in_portugal(app: RunningApp) -> str:
    """The tailoring application of ``test_tailoring_workspace``, in an app whose saved job
    preferences name PT as the CV country (the e2e setup saves none)."""
    aid = seeded(app)
    clock = FrozenClock(datetime(2026, 1, 5, 9, 0, tzinfo=UTC))
    preferences = JobPreferences(target_roles=("Route Planner",), cv_country="PT")
    with Database.open(app.data_dir / "app.db") as db:
        settings = SettingsService(db, clock)
        settings.save(
            JOB_PREFERENCES, preferences, expected_version=settings.load(JOB_PREFERENCES).version
        )
    return aid


def png_file() -> dict[str, object]:
    return {"name": "me.png", "mimeType": "image/png", "buffer": real_png(100, 100)}


def template_row(app: RunningApp) -> tuple[str, str | None, dict[str, object]]:
    found = rows(app, "SELECT status, approved_at, spec FROM cv_templates WHERE country='PT'")
    assert len(found) == 1, found
    status, approved_at, spec = found[0]
    template: dict[str, object] = json.loads(spec)["template"]  # beside the notes
    return status, approved_at, template


def has_status(app: RunningApp, status: str) -> bool:
    found = rows(app, "SELECT status FROM cv_templates WHERE country='PT'")
    return [row[0] for row in found] == [status]


def stored_details(app: RunningApp) -> dict[str, object]:
    found = rows(app, "SELECT value FROM settings WHERE key='personal_details'")
    assert len(found) == 1, found
    value: dict[str, object] = json.loads(found[0][0])
    return value


def photo_files(app: RunningApp) -> list[str]:
    folder = app.data_dir / "assets"
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def unit(page: Page):
    return page.locator(UNIT)


def fits(page: Page, label: str) -> None:
    ok, scroll_width, inner_width = has_no_horizontal_scroll(page)
    assert ok, f"{label}: scrollWidth {scroll_width} > {inner_width}"


def settled_axe(page: Page) -> None:
    wait_for_settled_styles(page)
    assert_no_serious_violations(page)


def details_open(page: Page):
    return page.locator("#details-settings details")


def expand_details(page: Page, key: str = "Enter") -> None:
    """Open the collapsed 'Your details' disclosure by keyboard: focus its summary with Tab, then
    press ``key`` (Enter or Space). The section arrives lazily, so wait for the summary first."""
    summary = page.locator("#details-summary")
    expect(summary).to_be_visible(timeout=RESULT_TIMEOUT_MS)
    expect(details_open(page)).not_to_have_attribute("open", re.compile(".*"))
    tab_until(page, summary)
    page.keyboard.press(key)
    expect(details_open(page)).to_have_attribute("open", re.compile(".*"))


def test_details_photo_and_template_review_by_keyboard(
    running_app: RunningApp, browser: Browser
) -> None:
    app = running_app
    aid = seeded_in_portugal(app)
    with new_page(browser, DESKTOP) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)

        # Your details: the lazy section arrives, every field is typed and saved by keyboard.
        open_app(page, app, "/settings")
        section = page.locator("#details-settings")
        expect(section.get_by_role("heading", name="Your details", level=2)).to_be_visible()
        form = page.locator("#details-form")
        expand_details(page)  # collapsed by default; opened by keyboard before filling
        expect(form).to_be_visible(timeout=RESULT_TIMEOUT_MS)
        for label, text in FIELDS:
            tab_until(page, form.get_by_label(label, exact=True))
            page.keyboard.type(text)
        press_by_keyboard(page, form.get_by_role("button", name="Save details", exact=True))
        wait_said(page, "Details saved.")
        expect(page.locator("#details-heading")).to_be_focused()  # app.js drops data-autofocus
        saved = stored_details(app)
        assert saved["name"] == GOOD["name"] and saved["email"] == GOOD["email"]
        assert saved["links"] == [GOOD["link_1"], GOOD["link_2"]]

        # The photo: focus the file input by keyboard, give it a small PNG, upload by keyboard.
        photo = page.locator("#photo-form")
        picker = photo.locator('input[type="file"]')
        tab_until(page, picker)
        picker.set_input_files(png_file())
        press_by_keyboard(page, photo.get_by_role("button", name="Upload photo", exact=True))
        wait_said(page, "Photo saved.")
        preview = page.locator("#photo-preview")
        expect(preview).to_be_visible()
        assert str(preview.get_attribute("src")).startswith("data:image/png;base64,")
        assert (preview.get_attribute("alt") or "").strip()
        (name,) = photo_files(app)
        assert re.fullmatch(r"photo-[A-Za-z0-9_-]+\.png", name), name
        assert stat.S_IMODE((app.data_dir / "assets" / name).stat().st_mode) == 0o600

        # The application's CV step: no template yet, so it offers the draft.
        open_app(page, app, f"/applications/{aid}")
        press_by_keyboard(page, page.get_by_role("link", name="Tailoring", exact=True))
        press_by_keyboard(page, stepper_link(page, "CV"))
        panel = unit(page)
        heading = panel.get_by_role("heading", name=HEADING)
        expect(heading).to_be_visible()
        expect(panel).to_have_attribute("data-state", "none")

        # Draft with a note, by keyboard: announced, focus on the unit's heading, then it arrives.
        tab_until(page, panel.get_by_label("Note (optional)", exact=True))
        page.keyboard.type("Keep it plain.")
        press_by_keyboard(
            page, panel.get_by_role("button", name="Draft a template for Portugal", exact=True)
        )
        wait_said(page, "Template draft started.")
        expect(heading).to_be_focused()
        expect(panel).to_have_attribute("data-state", "draft", timeout=RESULT_TIMEOUT_MS)
        wait_said(page, "Template draft ready.")
        expect(panel).to_contain_text(PRACTICE)
        assert not has_status(app, "approved")

        # Review: change a heading by keyboard and approve; the unit says so and focus returns.
        field = panel.get_by_label("Heading for section 1", exact=True)
        tab_until(page, field)
        page.keyboard.press("ControlOrMeta+A")
        page.keyboard.type("Profile")
        press_by_keyboard(page, panel.get_by_role("button", name="Approve template", exact=True))
        wait_said(page, "Template approved.")
        expect(panel).to_have_attribute("data-state", "approved")
        expect(heading).to_be_focused()
        status, approved_at, template = template_row(app)
        assert status == "approved" and approved_at is not None
        sections = template["sections"]
        assert isinstance(sections, list) and sections[0]["heading"] == "Profile"

        # Editing an approved template sends it back for approval, by keyboard (Space toggles).
        # The draft pinned the slot to the person's "optional photo" choice, so it starts ticked.
        slot = panel.get_by_label("Photo slot", exact=True)
        expect(slot).to_be_checked()
        tab_until(page, slot)
        page.keyboard.press("Space")
        expect(slot).not_to_be_checked()
        press_by_keyboard(page, panel.get_by_role("button", name="Save changes", exact=True))
        wait_said(page, SAVED)
        expect(panel).to_have_attribute("data-state", "draft")
        status, approved_at, template = template_row(app)
        assert (status, approved_at, template["photo_slot"]) == ("draft", None, False)
        assert_no_serious_violations(page)


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone-390x844"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_details_and_templates_phone_390x844_and_axe(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int], scheme: str
) -> None:
    app = running_app
    aid = seeded_in_portugal(app)
    with new_page(browser, viewport, color_scheme=scheme) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)

        # Settings, nothing saved yet: details form, empty photo block, empty template list.
        open_app(page, app, "/settings")
        form = page.locator("#details-form")
        summary = page.locator("#details-summary")
        expect(summary).to_be_visible(timeout=RESULT_TIMEOUT_MS)
        expect(form).to_be_hidden()  # collapsed on first load
        assert_target_size(summary, "Your details summary")
        fits(page, "settings, details collapsed")
        settled_axe(page)  # the collapsed state is scanned too
        expand_details(page, "Space")  # opened by keyboard
        expect(form).to_be_visible()
        expect(page.locator("#photo-empty")).to_be_visible()
        expect(page.locator("#template-empty")).to_be_visible()
        fits(page, "settings, empty")
        assert_target_size(form.get_by_role("button", name="Save details"), "Save details")
        settled_axe(page)

        # Details and photo saved with the pointer; the preview is inline and fits the screen.
        for label, text in FIELDS:
            form.get_by_label(label, exact=True).fill(text)
        form.get_by_role("button", name="Save details", exact=True).click()
        wait_said(page, "Details saved.")
        form.get_by_label("Location", exact=True).fill("Typed, not saved yet")
        page.locator("#photo-form input[type=file]").set_input_files(png_file())
        page.locator("#photo-form").get_by_role("button", name="Upload photo").click()
        wait_said(page, "Photo saved.")
        expect(page.locator("#photo-preview")).to_be_visible()
        # The photo swap leaves the details form alone: unsaved typing stays.
        expect(form.get_by_label("Location", exact=True)).to_have_value("Typed, not saved yet")
        expect(page.locator("#details-contents")).to_contain_text("Photo: Saved.")
        fits(page, "settings, details and photo")
        assert_target_size(
            page.locator("#photo-form").get_by_role("button", name="Replace photo"), "Replace photo"
        )
        assert_target_size(
            page.locator("#photo-remove-form").get_by_role("button", name="Remove photo"),
            "Remove photo",
        )
        settled_axe(page)

        # A refused detail keeps its error beside the field and the layout intact.
        form.get_by_label("Link 1", exact=True).fill("ftp://example.org/not-http")
        form.get_by_role("button", name="Save details", exact=True).click()
        wait_said(page, "Details not saved. 1 field needs a change.")
        expect(form.get_by_label("Link 1", exact=True)).to_have_attribute("aria-invalid", "true")
        fits(page, "settings, refused link")
        settled_axe(page)

        # The CV step: the draft offer, then the review form (twelve slots) and the approved unit.
        open_app(page, app, page_url(aid, 3))
        panel = unit(page)
        expect(panel.get_by_role("heading", name=HEADING)).to_be_visible()
        start = panel.get_by_role("button", name="Draft a template for Portugal", exact=True)
        assert_target_size(start, "Draft a template for Portugal")
        fits(page, "step 3, no template")
        settled_axe(page)
        start.click()
        wait_said(page, "Template draft started.")
        expect(panel).to_have_attribute("data-state", "draft", timeout=RESULT_TIMEOUT_MS)
        expect(panel).to_contain_text(PRACTICE)
        fits(page, "step 3, review form")
        assert_target_size(panel.get_by_role("button", name="Approve template"), "Approve template")
        assert_target_size(panel.get_by_role("button", name="Save changes"), "Save changes")
        settled_axe(page)
        panel.get_by_role("button", name="Approve template", exact=True).click()
        wait_said(page, "Template approved.")
        expect(panel).to_have_attribute("data-state", "approved")
        fits(page, "step 3, approved")
        settled_axe(page)

        # Settings > CV templates lists it; Review opens the editor in place; an edit shows in
        # the list without a reload.
        open_app(page, app, "/settings")
        item = page.locator("#template-PT")
        expect(item).to_have_attribute("data-status", "approved", timeout=RESULT_TIMEOUT_MS)
        expect(item).to_contain_text("Portugal")
        review = item.get_by_role("button", name="Review Portugal template")
        assert_target_size(review, "Review Portugal template")
        review.click()
        editor = page.locator("#template-editor")
        expect(editor.get_by_role("heading", name=HEADING)).to_be_visible()
        fits(page, "settings, template editor")
        settled_axe(page)
        editor.get_by_label("Photo slot", exact=True).check()
        editor.get_by_role("button", name="Save changes", exact=True).click()
        wait_said(page, SAVED)
        expect(item).to_have_attribute("data-status", "draft")
        wait_until(
            lambda: has_status(app, "draft"), timeout=20, message="the edit reset the approval"
        )
        fits(page, "settings, edited template")
        settled_axe(page)
