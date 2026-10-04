"""Browser fixtures for end-to-end tests (spec 4.1).

Uses Playwright's managed Chromium (`uv run playwright install chromium`). Set
JOBPIPELINE_E2E_CHROMIUM to a Chromium executable to use that instead, for example
where only a different, pre-installed build is available (ADR-035).

The fixtures use Playwright's sync API, so e2e tests must be plain (not async) functions.

``running_app`` starts the real app in a subprocess with temporary data and config folders
(``tests/running_app.py``). Teardown stops it gracefully and fails the test if the app had to
be killed or did not exit with code 0.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, sync_playwright

from tests.running_app import AppDidNotStart, RunningApp, start_app

CHROMIUM_ENV = "JOBPIPELINE_E2E_CHROMIUM"
DESKTOP = {"width": 1280, "height": 800}
PHONE = {"width": 390, "height": 844}


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    executable = os.environ.get(CHROMIUM_ENV) or None
    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch(executable_path=executable)
        yield chromium
        chromium.close()


@pytest.fixture
def running_app(tmp_path: Path) -> Iterator[RunningApp]:
    try:
        app = start_app(tmp_path)
    except AppDidNotStart as error:
        pytest.fail(f"the app did not start: {error}", pytrace=False)
    try:
        yield app
    finally:
        result = app.stop()
    if result.killed:
        pytest.fail(
            f"the app did not stop within the grace period and was killed\n{app.output_tail()}",
            pytrace=False,
        )
    if result.returncode != 0:
        pytest.fail(
            f"the app exited with code {result.returncode} after a graceful stop "
            f"(expected 0)\n{app.output_tail()}",
            pytrace=False,
        )


@pytest.fixture
def desktop_page(browser: Browser) -> Iterator[Page]:
    context = browser.new_context(viewport=DESKTOP)
    yield context.new_page()
    context.close()


@pytest.fixture
def phone_page(browser: Browser) -> Iterator[Page]:
    context = browser.new_context(viewport=PHONE, is_mobile=True, has_touch=True)
    yield context.new_page()
    context.close()
