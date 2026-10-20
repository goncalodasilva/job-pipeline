"""Exercise M4.3's POST-returned fallback form before M4.7 adds its inbox entry point."""

import json

import pytest
from playwright.sync_api import Browser, expect

from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_facts_page import press_by_keyboard, tab_until
from tests.e2e.test_shell import has_no_horizontal_scroll, open_app
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_intake_fallback_keyboard_phone_and_axe(
    running_app: RunningApp, browser: Browser, viewport, scheme
):
    with browser.new_context(viewport=viewport, color_scheme=scheme) as context:
        page = context.new_page()
        open_app(page, running_app, "/inbox")
        headers = json.loads(page.locator("body").get_attribute("hx-headers"))
        headers["Origin"] = running_app.origin
        # Obtain the full fallback page through its actual POST. M4.7 adds the Add-job entry.
        fallback = context.request.post(
            running_app.base_url + "/inbox/intake",
            form={"url": "http://127.0.0.1/ad"},
            headers=headers,
        )
        assert fallback.status == 422
        page.route(
            "**/intake-preview",
            lambda route: route.fulfill(status=200, content_type="text/html", body=fallback.text()),
        )
        page.goto(running_app.base_url + "/intake-preview")
        expect(page.locator("#job-url")).to_have_value("http://127.0.0.1/ad")
        for control in (
            page.locator("#job-url"),
            page.get_by_role("button", name="Add job", exact=True),
        ):
            box = control.bounding_box()
            assert box is not None and box["width"] >= 24 and box["height"] >= 24
        assert_no_serious_violations(page)
        assert has_no_horizontal_scroll(page)[0]
        tab_until(page, page.locator("#job-text"))
        page.keyboard.type("Fictional garden assistant. Plant imaginary flowers.")
        press_by_keyboard(page, page.get_by_role("button", name="Add job", exact=True))
        expect(page.locator("#job-intake")).to_contain_text("Job saved.")
        expect(page.locator("#job-intake-heading")).to_be_focused()
        expect(page.locator("#announcer")).to_have_text("Job saved.")
        for control in (
            page.locator("#job-url"),
            page.get_by_role("button", name="Add job", exact=True),
        ):
            box = control.bounding_box()
            assert box is not None and box["width"] >= 24 and box["height"] >= 24
        assert_no_serious_violations(page)
        assert has_no_horizontal_scroll(page)[0]


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE])
def test_intake_not_duplicate_by_keyboard(running_app: RunningApp, browser: Browser, viewport):
    with browser.new_context(viewport=viewport) as context:
        page = context.new_page()
        open_app(page, running_app, "/inbox")
        headers = json.loads(page.locator("body").get_attribute("hx-headers"))
        headers["Origin"] = running_app.origin
        url = running_app.base_url + "/inbox/intake"
        text = "Fictional gardener growing imaginary flowers"
        assert context.request.post(url, form={"text": text}, headers=headers).status == 200
        merged = context.request.post(url, form={"text": text}, headers=headers)
        assert merged.status == 200
        page.route(
            "**/intake-preview",
            lambda route: route.fulfill(status=200, content_type="text/html", body=merged.text()),
        )
        page.goto(running_app.base_url + "/intake-preview")
        split = page.get_by_role("button", name="Not a duplicate", exact=True)
        box = split.bounding_box()
        assert box is not None and box["width"] >= 24 and box["height"] >= 24
        press_by_keyboard(page, split)
        expect(page.locator("#job-intake")).to_contain_text("Job restored as its own card.")
        expect(page.locator("#job-intake-heading")).to_be_focused()
        expect(page.locator("#announcer")).to_have_text("Job restored as its own card.")
        assert_no_serious_violations(page)
