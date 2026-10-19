"""M3.10: import to explicit acceptance, keyboard only, with the real CLI and fake AI."""

import json
import re

import pytest
from playwright.sync_api import Browser, Route, expect

from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_fact_refine_panel import panel, seed_fact, wording
from tests.e2e.test_facts_page import (
    CV_TEXT,
    ROUTES,
    draft,
    facts_page,
    library_fact,
    named_button,
    open_drafts,
    paste_field,
    press_by_keyboard,
    tab_link,
    tab_until,
    wait_said,
)
from tests.e2e.test_shell import has_no_horizontal_scroll
from tests.facts_page import CONFIRM, IMPORT_START_ID, SAID_CONFIRMED, SAID_SAVED, SAVE
from tests.refine_panel import ACCEPT, REFINE_BUTTON, SAID_ACCEPTED, SAID_READY, SLOW_FACT
from tests.running_app import RunningApp
from tests.waiting import wait_until

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.parametrize("running_app", ["complete"], indirect=True),
]


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])
def test_m3_journey_import_review_confirm_refine(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int]
) -> None:
    with facts_page(browser, viewport) as page:
        open_drafts(page, running_app)
        tab_until(page, paste_field(page))
        page.keyboard.insert_text(CV_TEXT)
        press_by_keyboard(page, page.locator(f"#{IMPORT_START_ID}"))
        imported = draft(page, ROUTES)
        expect(imported).to_contain_text("Planned 60 delivery routes")
        expect(imported).to_contain_text("Practice data (fake provider)")
        assert_no_serious_violations(page)
        press_by_keyboard(page, named_button(imported, CONFIRM))
        wait_said(page, SAID_CONFIRMED)
        press_by_keyboard(page, tab_link(page, "library"))
        confirmed = library_fact(page, ROUTES)
        press_by_keyboard(page, confirmed.get_by_role("link", name="Edit", exact=True))
        expect(page.locator("#editor-text")).to_have_value(ROUTES)
        press_by_keyboard(page, named_button(panel(page), REFINE_BUTTON))
        wait_said(page, SAID_READY)
        expect(panel(page)).to_contain_text(
            "Based on this fact, your answers and 0 other confirmed facts."
        )
        # The fake's fixed wording is reviewable and unapplied; edit it to an honest rephrase.
        expect(page.locator("#editor-text")).to_have_value(ROUTES)
        tab_until(page, wording(page))
        page.keyboard.press("ControlOrMeta+A")
        page.keyboard.insert_text("Planned 60 delivery routes weekly with a team of 3.")
        press_by_keyboard(page, named_button(panel(page), ACCEPT))
        wait_said(page, SAID_ACCEPTED)
        expect(page.locator("#editor-text")).to_have_value(
            "Planned 60 delivery routes weekly with a team of 3."
        )
        expect(page.locator("#fact-history")).to_contain_text("Refined with AI, accepted by you")
        assert has_no_horizontal_scroll(page)[0]
        assert_no_serious_violations(page)


@pytest.mark.parametrize("when", ["waiting", "answered"])
def test_refine_panel_owns_only_its_turn_announcement(
    running_app: RunningApp, browser: Browser, when: str
) -> None:
    with facts_page(browser, DESKTOP) as page:
        seed_fact(page, running_app, SLOW_FACT)
        press_by_keyboard(page, named_button(panel(page), REFINE_BUTTON))
        expect(panel(page)).to_have_attribute("data-task-announcement-key", re.compile(r".+"))
        if when == "answered":
            wait_said(page, SAID_READY)
        key = panel(page).get_attribute("data-task-announcement-key")
        assert key
        # Exercise either order of the independent indicator and panel answers. An unrelated
        # task still announces, and the owned key remains remembered if the panel is removed.
        page.evaluate(
            """(key) => {
              window.__said = [];
              document.body.dispatchEvent(new CustomEvent('tasks:finished', {detail: {tasks: [
                {key: key, message: 'Refine with AI finished.', age: 0},
                {key: 'fictional-unrelated:1', message: 'Unrelated task finished.', age: 0}
              ]}}));
            }""",
            key,
        )
        wait_said(page, "Unrelated task finished.")
        assert "Refine with AI finished." not in page.evaluate("window.__said")
        page.evaluate(
            """(key) => {
              document.getElementById('fact-refine').remove();
              document.body.dispatchEvent(new CustomEvent('tasks:finished', {detail: {tasks: [
                {key: key, message: 'Refine with AI finished.', age: 0},
                {key: 'fictional-unrelated:2', message: 'Another task finished.', age: 0}
              ]}}));
            }""",
            key,
        )
        wait_said(page, "Another task finished.")
        assert "Refine with AI finished." not in page.evaluate("window.__said")


def test_refine_completion_before_the_start_response_is_announced_only_by_the_panel(
    running_app: RunningApp, browser: Browser
) -> None:
    with facts_page(browser, DESKTOP) as page:
        seed_fact(page, running_app, "Cut invoice processing time by 35% across 4 warehouses.")
        headers = json.loads(page.locator("body").get_attribute("hx-headers") or "{}")
        observed: dict[str, object] = {}

        def hold_start(route: Route) -> None:
            response = route.fetch()
            match = re.search(r'data-task-announcement-key="([^"]+)"', response.text())
            assert match
            key = match.group(1)
            observed["key_before_swap"] = panel(page).get_attribute("data-task-announcement-key")

            def finished() -> object:
                response = page.request.get(
                    f"{running_app.base_url}/tasks/indicator", headers=headers
                )
                detail = json.loads(response.headers.get("hx-trigger", "{}")).get(
                    "tasks:finished", {}
                )
                return detail if any(t["key"] == key for t in detail.get("tasks", [])) else None

            detail = wait_until(finished, timeout=10, message="the held start's turn finished")
            page.evaluate(
                """detail => document.body.dispatchEvent(
                    new CustomEvent('tasks:finished', {detail})
                )""",
                detail,
            )
            route.fulfill(response=response)

        page.route("**/facts/*/refine", hold_start)
        press_by_keyboard(page, named_button(panel(page), REFINE_BUTTON))
        wait_said(page, SAID_READY)
        assert observed["key_before_swap"] is None
        assert not any(
            "Refine with AI finished." in text for text in page.evaluate("window.__said")
        )


def test_recent_refine_completion_announced_when_returning_to_the_editor(
    running_app: RunningApp, browser: Browser
) -> None:
    with facts_page(browser, DESKTOP) as page:
        path = seed_fact(page, running_app, SLOW_FACT)
        headers = json.loads(page.locator("body").get_attribute("hx-headers") or "{}")
        press_by_keyboard(page, named_button(panel(page), REFINE_BUTTON))
        expect(panel(page)).to_have_attribute("data-task-announcement-key", re.compile(r".+"))
        key = panel(page).get_attribute("data-task-announcement-key")
        page.goto("about:blank")

        def finished() -> bool:
            response = page.request.get(f"{running_app.base_url}/tasks/indicator", headers=headers)
            detail = json.loads(response.headers.get("hx-trigger", "{}")).get("tasks:finished", {})
            return any(t["key"] == key for t in detail.get("tasks", []))

        wait_until(finished, timeout=15, message="the refine finished while away")
        page.goto(f"{running_app.base_url}{path}")
        wait_said(page, "Refine with AI finished.")


def test_detached_refine_request_failure_releases_other_task_announcements(
    running_app: RunningApp, browser: Browser
) -> None:
    with facts_page(browser, DESKTOP) as page:
        seed_fact(page, running_app, SLOW_FACT)
        held: list[Route] = []

        def hold_start(route: Route) -> None:
            held.append(route)
            page.evaluate("window.__refineStartHeld = true")

        page.route("**/facts/*/refine", hold_start)
        press_by_keyboard(page, named_button(panel(page), REFINE_BUTTON))
        page.wait_for_function("window.__refineStartHeld === true")
        # This swaps the whole editor, detaching the request's original form.
        press_by_keyboard(page, named_button(page.locator("#fact-editor"), SAVE))
        wait_said(page, SAID_SAVED)
        assert len(held) == 1
        held[0].abort()
        page.evaluate(
            """() => document.body.dispatchEvent(new CustomEvent('tasks:finished', {
              detail: {tasks: [{key: 'fictional-after-detach:1',
                message: 'Unrelated task finished.', age: 0}]}
            }))"""
        )
        wait_said(page, "Unrelated task finished.")


@pytest.mark.parametrize("empty_reply", [False, True], ids=["no-reply", "empty-reply"])
def test_refine_request_settlement_preserves_the_indicator_baseline(
    running_app: RunningApp, browser: Browser, empty_reply: bool
) -> None:
    with facts_page(browser, DESKTOP) as page:
        # No indicator response has supplied a baseline yet; model a delayed first reply.
        page.route("**/tasks/indicator", lambda route: route.fulfill(status=204))
        seed_fact(page, running_app, "Cut invoice processing time by 35% across 4 warehouses.")
        if empty_reply:

            def hold_start(route: Route) -> None:
                response = route.fetch()
                page.evaluate(
                    """() => document.body.dispatchEvent(new CustomEvent('tasks:finished', {
                      detail: {tasks: []}
                    }))"""
                )
                route.fulfill(response=response)

            page.route("**/facts/*/refine", hold_start)
        press_by_keyboard(page, named_button(panel(page), REFINE_BUTTON))
        wait_said(page, SAID_READY)
        page.evaluate(
            """() => document.body.dispatchEvent(new CustomEvent('tasks:finished', {
              detail: {tasks: [
                {key: 'fictional-new-baseline:1', message: 'Fresh task finished.', age: 0},
                {key: 'fictional-old-baseline:1', message: 'Old task finished.', age: 1000}
              ]}
            }))"""
        )
        # Only an actual reply establishes the baseline, including an empty deferred reply.
        expected = (
            "Old task finished. Fresh task finished." if empty_reply else "Fresh task finished."
        )
        wait_said(page, expected)
