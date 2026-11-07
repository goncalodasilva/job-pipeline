"""M9.6: one journey across M9 through the real command line with the fake provider
(AC-M9.1, AC-M9.3; ADR-131): remove jobs by keyboard with a reason, learn from them (accept the
suggestion), export the workspace, restore it into a fresh empty workspace, and find the rule,
the removals and the flag again. On a desktop and on a phone. Fictional data; no network.
"""

import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect

from app.modules.storage import Database
from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_facts_page import press_by_keyboard, tab_until
from tests.e2e.test_inbox_organisation import rows, seed
from tests.e2e.test_settings_board import tab_to
from tests.e2e.test_settings_data import (
    DOWNLOAD,
    EXPORT_BUTTON,
    IMPORT_BUTTON,
    IMPORT_FILE,
    launched,
    open_data_by_keyboard,
)
from tests.e2e.test_shell import has_no_horizontal_scroll, new_page, open_app
from tests.running_app import RunningApp
from tests.waiting import wait_until

pytestmark = pytest.mark.e2e

RULE = "Work mode: Onsite"
SQL_RULES = "SELECT field, mode, active FROM removal_rules"
SQL_REMOVED = "SELECT id, removal_reason FROM jobs WHERE triage_state='removed' ORDER BY id"


def state(app: RunningApp, job_id: str) -> str:
    found = rows(app, "SELECT triage_state FROM jobs WHERE id=?", (job_id,))
    return str(found[0][0])


def shown(page: Page) -> str:
    return str(page.locator("#inbox-card").get_attribute("data-job-id"))


def remove_for_location(page: Page, app: RunningApp) -> None:
    job = shown(page)
    wait_until(lambda: state(app, job) != "new", timeout=10, message=f"{job} seen")
    tab_until(page, page.locator("#inbox-card-heading"))
    page.keyboard.press("r")
    dialog = page.get_by_role("dialog", name="Remove job", exact=True)
    press_by_keyboard(page, dialog.get_by_role("button", name="Location", exact=True))
    wait_until(lambda: state(app, job) == "removed", timeout=10, message=f"{job} removed")
    expect(page.locator("#inbox-card")).not_to_have_attribute("data-job-id", job)


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])
def test_m9_journey_remove_learn_export_restore(
    running_app: RunningApp, browser: Browser, tmp_path: Path, viewport: dict[str, int]
) -> None:
    seed(running_app, count=5)
    with Database.open(running_app.data_dir / "app.db") as db, db.transaction() as conn:
        conn.execute("UPDATE jobs SET work_mode='on_site' WHERE id!='job-0005'")
        conn.execute("UPDATE jobs SET work_mode='remote' WHERE id='job-0005'")
    saved = tmp_path / "journey-export.json"

    with new_page(browser, viewport) as page:
        # Remove three onsite jobs for "Location", by keyboard: job-0003, -0002 and -0001.
        open_app(page, running_app, "/inbox?selected=job-0003")
        expect(page.locator("#inbox-card")).to_have_attribute("data-job-id", "job-0003")
        for _ in range(3):
            remove_for_location(page, running_app)
        assert rows(running_app, SQL_REMOVED) == [
            ("job-0001", "location"),
            ("job-0002", "location"),
            ("job-0003", "location"),
        ]

        # The app learns: a suggestion to flag onsite roles, accepted by keyboard.
        open_app(page, running_app, "/inbox?selected=job-0004")
        press_by_keyboard(page, page.get_by_role("button", name="Flag them", exact=True))
        expect(page.get_by_text(f"Matches your rule: {RULE}")).to_be_visible()
        assert rows(running_app, SQL_RULES) == [("work_mode", "flag", 1)]
        assert_no_serious_violations(page)
        assert has_no_horizontal_scroll(page)[0]

        # Export from Settings > Data and privacy, and download the file.
        open_data_by_keyboard(page, running_app)
        tab_to(page, page.locator(EXPORT_BUTTON))
        page.keyboard.press("Enter")
        expect(page.locator("#data-export-done")).to_contain_text(".json")
        tab_to(page, page.locator(DOWNLOAD))
        with page.expect_download() as info:
            page.keyboard.press("Enter")
        info.value.save_as(saved)
    payload = json.loads(saved.read_bytes())
    assert payload["format"] == 1 and payload["tables"]["removal_rules"]["rows"]

    # Restore into a fresh, empty workspace: the rule, the removals and the flag are back.
    with (
        launched(tmp_path, empty=True, name="restored") as second,
        new_page(browser, viewport) as page,
    ):
        open_data_by_keyboard(page, second)
        tab_to(page, page.locator(IMPORT_FILE))
        page.set_input_files(IMPORT_FILE, str(saved))
        tab_to(page, page.locator(IMPORT_BUTTON))
        page.keyboard.press("Enter")
        expect(page.locator("#data-import-done")).to_contain_text("Restored")

        assert rows(second, SQL_RULES) == [("work_mode", "flag", 1)]
        assert rows(second, SQL_REMOVED) == rows(running_app, SQL_REMOVED)
        open_app(page, second, "/settings/preferences")
        expect(page.locator("#removal-rules")).to_contain_text(RULE)
        open_app(page, second, "/inbox?selected=job-0004")
        expect(page.get_by_text(f"Matches your rule: {RULE}")).to_be_visible()
        removed = page.get_by_role("link", name=re.compile(r"^Removed"))
        press_by_keyboard(page, removed.first)
        expect(page.locator("#inbox")).to_have_attribute("data-view", "removed")
        expect(page.locator("#inbox-queue-heading")).to_have_text("Removed (3)")
        card = page.locator("#inbox-card")
        expect(card).to_have_attribute("data-job-id", re.compile(r"^job-000[123]$"))
        expect(card).to_have_attribute("data-state", "removed")
        expect(card.locator(".card-state")).to_have_text("Removed")
        assert_no_serious_violations(page)
        assert has_no_horizontal_scroll(page)[0]
