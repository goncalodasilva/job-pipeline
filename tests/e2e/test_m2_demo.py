"""The M2 demo end to end in a real browser (M2.26): run, cancel and recover tasks and see the
budget warnings, on an app started with ``--demo`` and nothing else.

The real app runs as a subprocess with ``--demo`` the way a user starts it: WITHOUT
``JOBPIPELINE_ALLOW_FAKE_PROVIDERS`` and WITHOUT ``JOBPIPELINE_DATA_DIR`` (ADR-077 update of
the M2.20 checkpoint; user decision of 2026-10-11: the demo offers the fake provider by
itself only on the folder the app chose for it). It never touches a real folder:
``tests/running_app.py`` points the home folders, the config folder and the keyring file
into the test's temporary folder, so the demo folder the app chooses lies under the pretend
home. The test checks that the database is in that demo folder and nowhere else, that no
settings file was written, and that the app was pointed at the test's file keyring (which
stays empty: the fake keeps no key). The demo folder starts with a finished setup guide and
the fake provider saved, as after the demo's own guide (``test_demo_guide.py`` walks that
step).

One user, one flow:

1. Settings > Testing tools is there. A practice task started by keyboard runs to the end
   and is announced; a finished task needs no attention.
2. A slow one is cancelled from the panel by keyboard; a cancelled task needs none either.
3. A failing one: the closed Tasks button says "1 needs attention" (kept across a reload)
   until the panel is opened by keyboard; the task shows why it failed and Retry runs it
   again (it fails again, in front of the user, so nothing new needs attention).
4. The app is killed under a slow task and started again on the same folders (a new port,
   so a new origin, in a new browser context): nothing is remembered as looked at there, so
   the closed button says "2 need attention" (the failed task of step 3 and the interrupted
   one); the panel shows the latter as Interrupted with Retry; Retry runs it and Cancel
   ends it.
5. A budget for the Fake provider is typed and saved by keyboard in Settings > Usage
   budgets, with its first warning one request ahead of what was used. The next start opens
   the warning dialog with the reset date; Continue (by keyboard) starts the task, whose five
   requests reach the blocking point: the Start button is then ``aria-disabled`` and described
   by "Monthly limit reached for Fake provider. Resets on <date>.".

The last state is also checked at 390x844 (no horizontal scroll) and with axe. No fixed
waits: every wait is a condition with a deadline. Every value is fictional.

Known limit (as in ``test_budget_warning_dialog.py``): the app counts on the real clock and
the budget resets on day 1, so a run that crosses local midnight into the first day of a
month between saving the budget and the last start can fail. Run it again.
"""

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect

from app.config import BOOTSTRAP_FILE, DEMO_APP_NAME, ENV_DATA_DIR
from app.domain.clock import SystemClock
from app.domain.errors import NetworkUnavailable, format_day
from app.modules.storage import Database
from app.services.usage import UsageService
from app.wiring import ALLOW_FAKES_VARIABLE, local_timezone
from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_budget_warning_dialog import (
    CONTINUE,
    DESCRIPTION,
    FAKE,
    MSG_STARTED,
    SERVICE_NAME,
    STATUS,
    TOOLS,
    dialog,
    dialog_button,
    expect_dialog,
    start_button,
    start_by_keyboard,
    tab_in_dialog_to,
)
from tests.e2e.test_settings_usage_budgets import SAVED, fill_by_keyboard
from tests.e2e.test_settings_usage_budgets import STATUS as BUDGET_STATUS
from tests.e2e.test_setup_provider_test import tab_to_locator
from tests.e2e.test_shell import has_no_horizontal_scroll
from tests.e2e.test_tasks_needs_attention import (
    ONE,
    TWO,
    attention,
    close_panel,
    expect_attention,
    open_panel_by_keyboard,
    seen_store,
)
from tests.e2e.test_tasks_panel import (
    NONE_RUNNING,
    ONE_RUNNING,
    SAID_FINISHED,
    STEP_MS,
    count,
    item_with_status,
    reopen,
    settings_page,
    start_task,
    wait_announced,
    wait_server_shows,
)
from tests.fakes.ids import SequentialIdFactory
from tests.integration.test_task_routes import MSG_INTERRUPTED
from tests.running_app import (
    DEMO_FLAG,
    KEYRING_BACKEND_VARIABLE,
    TEST_KEYRING_BACKEND,
    AppDidNotStart,
    RunningApp,
    start_app,
)
from tests.waiting import wait_until

pytestmark = pytest.mark.e2e

DATABASE = "app.db"
HEADING = "Testing tools"
DEADLINE_S = 20.0
PRACTICE_STEPS = 5  # a practice task makes five requests (M2.17)
DEMO_LOG_LINE = "Demo run: the fake AI provider is offered"
ALLOW_LOG_LINE = f"{ALLOW_FAKES_VARIABLE} is set"


def start_demo(tmp_path: Path, *, restart: bool) -> RunningApp:
    """The app with ``--demo`` alone: no allow variable and no data folder named, so it
    runs on its own demo folder under the temporary home."""
    try:
        return start_app(
            tmp_path,
            demo=True,
            allow_fakes=False,
            restart=restart,
            output_name="demo-after-kill" if restart else "demo",
        )
    except AppDidNotStart as error:
        pytest.fail(f"the demo did not start: {error}", pytrace=False)


def stored_tasks(app: RunningApp) -> list[tuple[str, int, str | None]]:
    """Each task's status, attempt and stored error, through a read-only connection."""
    uri = f"{(app.data_dir / DATABASE).as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
        rows = conn.execute("SELECT status, attempt, error FROM tasks ORDER BY rowid").fetchall()
    return [(str(status), int(attempt), error) for status, attempt, error in rows]


def usage_state(app: RunningApp) -> tuple[int, date | None]:
    """What the app counts for the Fake provider this cycle and, with a budget saved, the
    date the cycle resets, read with the app's own ``UsageService`` (the computer's zone)."""
    db = Database.open(app.data_dir / DATABASE)
    try:
        usage = UsageService(db, SystemClock(), SequentialIdFactory("e2e-demo"), local_timezone())
        status = usage.status(FAKE)
        return int(status.local_count), getattr(status.cycle, "reset_date", None)
    finally:
        db.close()


def expect_two_need_attention(page: Page) -> None:
    """After the restart the closed button counts two tasks: the interrupted one, and the
    failed one of step 3 (its second attempt) again.

    "Looked at" is remembered by the browser, per origin (localStorage). The restarted app
    listens on another port, which is another origin, and this page is in a new browser
    context besides: nothing is remembered here, so every failed or interrupted task the
    server still lists counts. It is a real effect of restarting on a new port; with the
    default fixed port a user's browser would still remember the failed task."""
    assert seen_store(page) == [], "a new origin and a new context: nothing was looked at"
    expect(attention(page)).to_have_text(TWO, timeout=STEP_MS)
    expect(attention(page)).to_be_visible()


def assert_demo_started_like_a_user_would(app: RunningApp) -> None:
    command = app.process.args
    assert isinstance(command, list) and DEMO_FLAG in command
    assert not any(str(part).startswith("--data-dir") for part in command)
    assert ENV_DATA_DIR not in app.env, "no data folder was named: the app chose the demo's"
    assert ALLOW_FAKES_VARIABLE not in app.env, "the demo offers the fake by itself"


def assert_only_the_demo_folder(app: RunningApp, tmp_path: Path) -> None:
    """The demo's database is in the demo folder under the pretend home, and nowhere else;
    no settings file was written; the keychain was the test's file, which holds nothing."""
    home = (tmp_path / "home").resolve()
    folder = app.data_dir.resolve()
    assert folder.is_relative_to(home), "the demo folder is under the pretend home"
    assert folder.name == DEMO_APP_NAME or DEMO_APP_NAME in folder.parts, folder.parts
    databases = sorted(path.resolve() for path in tmp_path.rglob(DATABASE))
    assert databases == [folder / DATABASE], [str(path) for path in databases]
    assert not (tmp_path / "data").exists(), "no folder beside the demo's was created"
    settings_files = [str(path) for path in tmp_path.rglob(BOOTSTRAP_FILE)]
    assert settings_files == [], f"the demo writes no settings file: {settings_files}"
    # Never the real keychain: the app was pointed at the test's file-backed one, in the
    # temporary folder, and the fake provider kept nothing in it.
    assert app.env[KEYRING_BACKEND_VARIABLE] == TEST_KEYRING_BACKEND
    assert app.keyring_file.resolve().is_relative_to(tmp_path.resolve())
    assert not app.keyring_file.exists() or app.keyring_file.read_text(encoding="utf-8") in (
        "",
        "{}",
    )


def assert_demo_logged_why_the_fake_is_offered(app: RunningApp) -> None:
    logs = "".join(path.read_text(encoding="utf-8") for path in app.log_files())
    assert DEMO_LOG_LINE in logs, "the fake is offered because this is the demo"
    assert ALLOW_LOG_LINE not in logs, "and not because of the allow variable"


def run_cancel_and_fail(page: Page, app: RunningApp) -> None:
    """Steps 1 to 3 of the flow."""
    # 1. The demo offers the practice task; started by keyboard, it runs to the end.
    expect(page.get_by_role("heading", name=HEADING, level=2)).to_be_visible()
    start_by_keyboard(page)
    expect(page.locator(STATUS)).to_have_text(MSG_STARTED)
    wait_announced(page, SAID_FINISHED)
    expect(count(page)).to_have_text(NONE_RUNNING)
    assert stored_tasks(app) == [("succeeded", 1, None)]
    expect_attention(page, None)

    # 2. A slow one, cancelled from the panel by keyboard.
    start_task(page, duration="slow")
    expect(count(page)).to_have_text(ONE_RUNNING, timeout=STEP_MS)
    reopen(page, app)
    open_panel_by_keyboard(page)
    running = item_with_status(page, "Running")
    expect(running).to_have_count(1, timeout=STEP_MS)
    tab_to_locator(page, running.get_by_role("button", name="Cancel"))
    page.keyboard.press("Enter")
    expect(item_with_status(page, "Cancelled")).to_have_count(1, timeout=STEP_MS)
    close_panel(page)
    expect(count(page)).to_have_text(NONE_RUNNING)
    expect_attention(page, None)

    # 3. A failing one: the closed button says so until the panel is opened.
    start_task(page, outcome="network")
    wait_server_shows(page, "Failed")
    expect_attention(page, ONE)
    expect(count(page)).to_have_text(NONE_RUNNING)
    reopen(page, app)
    expect_attention(page, ONE)
    open_panel_by_keyboard(page)
    expect_attention(page, None)
    failed = item_with_status(page, "Failed")
    expect(failed).to_have_count(1)
    expect(failed.locator('[id$="-reason"]')).to_have_text(NetworkUnavailable().user_message)
    tab_to_locator(page, failed.get_by_role("button", name="Retry"))
    page.keyboard.press("Enter")
    wait_until(
        lambda: ("failed", 2, NetworkUnavailable.kind) in stored_tasks(app),
        timeout=DEADLINE_S,
        message="the retried task failed again (its second attempt)",
    )
    expect(item_with_status(page, "Failed")).to_have_count(1, timeout=STEP_MS)
    close_panel(page)
    expect_attention(page, None)


def recover_the_interrupted_task(page: Page, app: RunningApp) -> None:
    """Step 4, after the restart."""
    expect_two_need_attention(page)
    expect(count(page)).to_have_text(NONE_RUNNING)
    open_panel_by_keyboard(page)
    expect_attention(page, None)
    interrupted = item_with_status(page, "Interrupted")
    expect(interrupted).to_have_count(1)
    expect(interrupted.locator('[id$="-reason"]')).to_have_text(MSG_INTERRUPTED)
    expect(interrupted.get_by_role("button", name="Cancel")).to_have_count(0)

    tab_to_locator(page, interrupted.get_by_role("button", name="Retry"))
    page.keyboard.press("Enter")
    running = item_with_status(page, "Running")
    expect(running).to_have_count(1, timeout=STEP_MS)
    expect(count(page)).to_have_text(ONE_RUNNING)
    assert ("running", 2, None) in stored_tasks(app), "the same task, its second attempt"
    tab_to_locator(page, running.get_by_role("button", name="Cancel"))
    page.keyboard.press("Enter")
    expect(item_with_status(page, "Cancelled")).to_have_count(2, timeout=STEP_MS)
    close_panel(page)
    expect(count(page)).to_have_text(NONE_RUNNING)
    expect_attention(page, None)


def warn_then_block(page: Page, app: RunningApp) -> str:
    """Step 5; returns the blocked message."""
    used, _no_cycle = usage_state(app)
    assert used > 0, "the tasks so far were counted on the Fake provider"
    first_warning, block_at, allowance = used + 1, used + PRACTICE_STEPS, used + 10
    reopen(page, app)
    fill_by_keyboard(
        page,
        {
            "allowance": str(allowance),
            "first_warning": str(first_warning),
            "second_warning": str(used + 2),
            "block_at": str(block_at),
            "reset_day": "1",
        },
    )
    page.keyboard.press("Enter")
    expect(page.locator("#announcer")).to_contain_text(SAVED)
    expect(page.locator(BUDGET_STATUS)).to_have_text(f"Budget set: {allowance} requests per month.")
    _count, resets_on = usage_state(app)
    assert resets_on is not None, "the saved budget has a cycle"

    before = len(stored_tasks(app))
    start_by_keyboard(page)
    expect_dialog(page, first_warning, allowance, resets_on=resets_on)
    assert len(stored_tasks(app)) == before, "showing the dialog starts nothing"
    tab_in_dialog_to(page, dialog_button(page, CONTINUE))
    page.keyboard.press("Enter")
    expect(page.locator(STATUS)).to_have_text(MSG_STARTED)
    expect(dialog(page)).to_be_hidden()
    wait_until(
        lambda: stored_tasks(app)[before:] == [("succeeded", 1, None)],
        timeout=DEADLINE_S,
        message="the task started with Continue ran to the end",
    )
    assert usage_state(app)[0] == block_at, "its requests reached the blocking point"

    message = f"Monthly limit reached for {SERVICE_NAME}. Resets on {format_day(resets_on)}."
    reopen(page, app)
    assert_blocked(page, message)
    return message


def assert_blocked(page: Page, message: str) -> None:
    start = start_button(page)
    expect(start).to_have_attribute("aria-disabled", "true")
    described = start.evaluate(DESCRIPTION)
    assert any(message in part["text"] and part["shown"] for part in described), described
    expect(page.locator(TOOLS).get_by_text(message)).to_be_visible()
    expect(page.locator(f'{TOOLS} a[href$="#usage-budgets"]')).to_be_visible()


def test_m2_demo_run_cancel_recover_and_budget_warnings(browser: Browser, tmp_path: Path) -> None:
    first = start_demo(tmp_path, restart=False)
    try:
        assert_demo_started_like_a_user_would(first)
        assert_only_the_demo_folder(first, tmp_path)
        with settings_page(browser, first, DESKTOP) as page:
            run_cancel_and_fail(page, first)
            # 4. The app dies under a slow task.
            start_task(page, duration="slow")
            wait_server_shows(page, "Running")
        assert stored_tasks(first)[-1] == ("running", 1, None)
        killed = first.kill()
    finally:
        first.stop()
    assert killed.killed, "the demo was killed, not stopped"

    second = start_demo(tmp_path, restart=True)
    try:
        assert stored_tasks(second)[-1] == ("interrupted", 1, None), "recovered at start-up"
        with settings_page(browser, second, DESKTOP) as page:
            recover_the_interrupted_task(page, second)
            message = warn_then_block(page, second)
            assert_no_serious_violations(page)

        assert PHONE == {"width": 390, "height": 844}
        with settings_page(browser, second, PHONE) as phone:
            assert_blocked(phone, message)
            fits, scroll_width, inner_width = has_no_horizontal_scroll(phone)
            assert fits, f"scrollWidth {scroll_width} > {inner_width}"
            assert_no_serious_violations(phone)
        assert_demo_started_like_a_user_would(second)
        assert_only_the_demo_folder(second, tmp_path)
        assert first.data_dir == second.data_dir, "the restart ran on the same demo folder"
        stored = stored_tasks(second)
        assert {error for _status, _attempt, error in stored} <= {None, NetworkUnavailable.kind}, (
            "a stored error is a kind, never text"
        )
    finally:
        stopped = second.stop()
    assert not stopped.killed, second.output_tail()
    assert stopped.returncode == 0, second.output_tail()
    assert_demo_logged_why_the_fake_is_offered(second)
