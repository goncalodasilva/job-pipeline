"""Browser fixtures for end-to-end tests (spec 4.1).

Uses Playwright's managed Chromium (`uv run playwright install chromium`). Set
JOBPIPELINE_E2E_CHROMIUM to a Chromium executable to use that instead, for example
where only a different, pre-installed build is available (ADR-035).

The fixtures use Playwright's sync API, so e2e tests must be plain (not async) functions.
"""

import os
from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser, Page, sync_playwright

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
def desktop_page(browser: Browser) -> Iterator[Page]:
    context = browser.new_context(viewport=DESKTOP)
    yield context.new_page()
    context.close()


@pytest.fixture
def phone_page(browser: Browser) -> Iterator[Page]:
    context = browser.new_context(viewport=PHONE, is_mobile=True, has_touch=True)
    yield context.new_page()
    context.close()
