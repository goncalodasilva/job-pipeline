"""Desktop and phone page fixtures for end-to-end tests (M0.4, spec 3.9 and 4.4)."""

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

MINIMAL_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Viewport probe</title>
</head>
<body><main><h1>Viewport probe</h1></main></body>
</html>"""


def test_desktop_page_viewport(desktop_page: Page) -> None:
    desktop_page.set_content(MINIMAL_PAGE)

    assert desktop_page.evaluate("window.innerWidth") == 1280
    assert desktop_page.evaluate("window.innerHeight") == 800


def test_phone_page_viewport_and_touch(phone_page: Page) -> None:
    phone_page.set_content(MINIMAL_PAGE)

    assert phone_page.evaluate("window.innerWidth") == 390
    assert phone_page.evaluate("window.innerHeight") == 844
    assert phone_page.evaluate("navigator.maxTouchPoints > 0 || 'ontouchstart' in window")
