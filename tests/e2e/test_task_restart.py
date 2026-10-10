"""A task the app was killed under shows as Interrupted with Retry after a restart (M2.21;
AC-M2.2; ADR-076 items 10 and 11), in a real browser.

The real app runs in a subprocess on temporary folders with a completed setup and the fake
provider. A slow practice task (5 steps of 3 s, M2.17) is started from Settings > Testing
tools, the process is SIGKILLed while it runs and started again on the same folders
(``start_app(..., restart=True)``; its port, and so its address, is new). A new browser
context then opens the tasks panel by keyboard: the task is Interrupted, with the reason and a
Retry button and no Cancel. Retry by keyboard puts it back in the queue and the runner runs it
again (a second attempt, Running); Cancel by keyboard then ends it so the test doesn't wait
the whole 15 s. The panel in this state is also checked at 390x844 (no horizontal scroll, the
panel inside the viewport) and with axe, at both sizes.

Nothing sleeps: every wait is a condition with a deadline. Every value is fictional.
"""

import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_setup_provider import tab_to
from tests.e2e.test_setup_provider_test import tab_to_locator
from tests.e2e.test_tasks_panel import (
    NONE_RUNNING,
    ONE_RUNNING,
    STEP_MS,
    TOGGLE,
    assert_fits,
    assert_focus_not_lost,
    assert_no_links,
    count,
    items,
    popover,
    settings_page,
    start_task,
    status_of,
    toggle,
    wait_server_shows,
)
from tests.integration.test_task_routes import MSG_INTERRUPTED
from tests.running_app import AppDidNotStart, RunningApp, start_app

pytestmark = pytest.mark.e2e

DATABASE = "app.db"
SETTINGS = "/settings"


def started(tmp_path: Path, *, restart: bool) -> RunningApp:
    try:
        return start_app(
            tmp_path,
            restart=restart,
            output_name="after-kill" if restart else "before-kill",
        )
    except AppDidNotStart as error:
        pytest.fail(f"the app did not start: {error}", pytrace=False)


def stored_tasks(app: RunningApp) -> list[tuple[str, int]]:
    """Each task's status and attempt, read through a read-only connection."""
    uri = f"{(app.data_dir / DATABASE).as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
        rows = conn.execute("SELECT status, attempt FROM tasks ORDER BY rowid").fetchall()
    return [(str(status), int(attempt)) for status, attempt in rows]


def open_panel_by_keyboard(page: Page) -> None:
    """From the top of the page, Tab to the Tasks button and press Enter (no clicks)."""
    tab_to(page, TOGGLE)
    page.keyboard.press("Enter")
    expect(toggle(page)).to_have_attribute("aria-expanded", "true")
    expect(popover(page)).to_be_visible()


def assert_interrupted_item(page: Page) -> None:
    expect(items(page)).to_have_count(1)
    item = items(page).first
    expect(status_of(item)).to_have_text("Interrupted")
    expect(item.locator('[id$="-reason"]')).to_have_text(MSG_INTERRUPTED)
    expect(item.get_by_role("button", name="Retry")).to_be_visible()
    expect(item.get_by_role("button", name="Cancel")).to_have_count(0)
    expect(item.locator("progress")).to_have_count(0)
    expect(count(page)).to_have_text(NONE_RUNNING)
    assert_no_links(item)


def test_interrupted_task_shows_retry_after_restart(browser: Browser, tmp_path: Path) -> None:
    first = started(tmp_path, restart=False)
    try:
        with settings_page(browser, first, DESKTOP) as page:
            start_task(page, duration="slow")
            wait_server_shows(page, "Running")  # the task is mid-flight when the app dies
        assert stored_tasks(first) == [("running", 1)]
        killed = first.kill()
    finally:
        first.stop()
    assert killed.killed, "the first app was killed, not stopped"
    assert stored_tasks(first) == [("running", 1)], "the crash left the row running"

    second = started(tmp_path, restart=True)
    try:
        assert stored_tasks(second) == [("interrupted", 1)], "recovered when the app opened"

        # The phone: the panel with an interrupted task fits and has no serious axe findings.
        with settings_page(browser, second, PHONE) as phone:
            open_panel_by_keyboard(phone)
            assert_interrupted_item(phone)
            assert_fits(phone, "interrupted task at 390x844")
            assert_no_serious_violations(phone)

        with settings_page(browser, second, DESKTOP) as page:
            # Keyboard only, from the top of the page: open the panel and read the task.
            open_panel_by_keyboard(page)
            assert_interrupted_item(page)
            assert_no_serious_violations(page)
            item = items(page).first
            retry = item.get_by_role("button", name="Retry")

            # Retry by keyboard puts it back in the queue; the runner runs it again.
            tab_to_locator(page, retry)
            page.keyboard.press("Enter")
            expect(status_of(item)).to_have_text(re.compile(r"^(Queued|Running)$"))
            expect(item.locator('[id$="-reason"]')).to_have_count(0)
            expect(status_of(item)).to_have_text("Running", timeout=STEP_MS)
            expect(count(page)).to_have_text(ONE_RUNNING)
            expect(item.locator("progress")).to_be_visible()
            assert stored_tasks(second) == [("running", 2)], "the same task, its second attempt"
            assert_focus_not_lost(page)

            # End it by keyboard too, so the test needn't wait for the whole run.
            cancel = item.get_by_role("button", name="Cancel")
            tab_to_locator(page, cancel)
            page.keyboard.press("Enter")
            expect(status_of(item)).to_have_text("Cancelled", timeout=STEP_MS)
            expect(count(page)).to_have_text(NONE_RUNNING)
            assert stored_tasks(second) == [("cancelled", 2)]
            expect(page.locator("#app-status")).to_be_hidden()
    finally:
        stopped = second.stop()
    assert not stopped.killed, second.output_tail()
    assert stopped.returncode == 0, second.output_tail()
