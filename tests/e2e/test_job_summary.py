"""M4.4 summary budget dialogs, refresh and persistent completion announcements."""

import json
import re

import pytest
from playwright.sync_api import Browser, Route, expect

from app.domain.budgets import Budget
from app.modules.storage import Database
from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_budget_warning_dialog import seed
from tests.e2e.test_facts_page import press_by_keyboard, tab_until
from tests.e2e.test_shell import has_no_horizontal_scroll, open_app
from tests.running_app import RunningApp
from tests.waiting import wait_until

pytestmark = pytest.mark.e2e
BUDGET = Budget(allowance=4, first_warning=1, second_warning=2, block_at=3, reset_day=1)
TEXT = "Fictional gardener growing imaginary flowers with a friendly team."


def preview(page, context, app):
    open_app(page, app, "/inbox")
    headers = json.loads(page.locator("body").get_attribute("hx-headers"))
    headers["Origin"] = app.origin
    fallback = context.request.post(
        app.base_url + "/inbox/intake", form={"url": "http://127.0.0.1/ad"}, headers=headers
    )
    assert fallback.status == 422
    page.route(
        "**/intake-preview",
        lambda route: route.fulfill(status=200, content_type="text/html", body=fallback.text()),
    )
    page.goto(app.base_url + "/intake-preview")
    page.evaluate("""() => { window.summaryMessages=[]; window.summaryTimings=[];
      new MutationObserver(() => {
      const text=document.querySelector('#announcer').textContent;
      if(text) { window.summaryMessages.push(text);
        window.summaryTimings.push({text,at:performance.now()}); }
    }).observe(document.querySelector('#announcer'),
       {childList:true,subtree:true,characterData:true}); }""")
    tab_until(page, page.locator("#job-text"))
    page.keyboard.type(TEXT)
    press_by_keyboard(page, page.get_by_role("button", name="Add job", exact=True))


def summary_tasks(app):
    with Database.open(app.data_dir / "app.db") as db:
        return db.connection.execute(
            "SELECT status FROM tasks WHERE type='summarize_job'"
        ).fetchall()


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_summary_warning_cancel_continue_keyboard_and_announcement(
    running_app: RunningApp, browser: Browser, viewport, scheme
):
    seed(running_app, BUDGET)
    with browser.new_context(viewport=viewport, color_scheme=scheme) as context:
        page = context.new_page()
        preview(page, context, running_app)
        dialog = page.get_by_role("dialog")
        expect(dialog).to_be_visible()
        expect(dialog.get_by_role("button", name="Not now", exact=True)).to_be_focused()
        assert summary_tasks(running_app) == []
        assert_no_serious_violations(page)
        page.keyboard.press("Escape")
        start = page.get_by_role("button", name="Summarize", exact=True)
        expect(start).to_be_focused()
        assert summary_tasks(running_app) == []
        press_by_keyboard(page, start)
        expect(dialog).to_be_visible()
        press_by_keyboard(page, dialog.get_by_role("button", name="Continue", exact=True))
        expect(page.locator('[id^="job-summary-"]').first).to_contain_text(
            "Practice data (fake provider)"
        )
        expect(page.locator("#announcer")).to_have_text("Job summary ready.")
        assert summary_tasks(running_app) == [("succeeded",)]
        for _ in range(2):
            # A response event precedes its DOM swap. Wait for listeners on the new
            # fragment before dispatching the next unrelated refresh.
            page.wait_for_function("""() => !document.querySelector(
                '#job-intake .htmx-request, #job-intake .htmx-swapping, #job-intake .htmx-settling'
            )""")
            with page.expect_response(lambda r: "/summary" in r.url and r.request.method == "GET"):
                page.evaluate(
                    "() => document.body.dispatchEvent("
                    "new CustomEvent('tasks:resync',{bubbles:true}))"
                )
        assert (
            page.evaluate("() => window.summaryMessages.filter(x=>x==='Job summary ready.').length")
            == 1
        )
        assert_no_serious_violations(page)
        assert has_no_horizontal_scroll(page)[0]


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE])
def test_summary_block_reason_survives_task_refresh(
    running_app: RunningApp, browser: Browser, viewport
):
    seed(running_app, BUDGET, fast=3)
    with browser.new_context(viewport=viewport) as context:
        page = context.new_page()
        preview(page, context, running_app)
        button = page.get_by_role("button", name="Summarize", exact=True)
        expect(button).to_have_attribute("aria-disabled", "true")
        expect(page.locator('[id^="job-summary-reason-"]')).to_contain_text("Monthly limit reached")
        with page.expect_response(lambda r: "/summary" in r.url and r.request.method == "GET"):
            page.evaluate(
                "() => document.body.dispatchEvent(new CustomEvent('tasks:resync',{bubbles:true}))"
            )
        expect(button).to_have_attribute("aria-disabled", "true")
        expect(page.locator('[id^="job-summary-reason-"]')).to_contain_text("Monthly limit reached")
        expect(page.get_by_role("link", name="Open Usage budgets", exact=True)).to_be_visible()
        assert summary_tasks(running_app) == []
        assert_no_serious_violations(page)


@pytest.mark.parametrize("start_from", ["intake", "summary"])
def test_fast_summary_before_start_response_announced_only_once(
    running_app: RunningApp, browser: Browser, start_from
):
    if start_from == "summary":
        seed(running_app, BUDGET)
    with browser.new_context(viewport=PHONE) as context:
        page = context.new_page()
        page.clock.install()
        observed = []

        def hold_start(route: Route):
            response = route.fetch()
            match = re.search(r'data-task-announcement-key="([^"]+)"', response.text())
            if match:
                key = match.group(1)
                observed.append(key)
                headers = json.loads(page.locator("body").get_attribute("hx-headers"))

                def finished():
                    indicator = page.request.get(
                        running_app.base_url + "/tasks/indicator", headers=headers
                    )
                    detail = json.loads(indicator.headers.get("hx-trigger", "{}")).get(
                        "tasks:finished", {}
                    )
                    return detail if any(t["key"] == key for t in detail.get("tasks", [])) else None

                detail = wait_until(finished, timeout=10, message="held summary finished")
                page.clock.fast_forward(2000)
                page.evaluate(
                    """detail => document.body.dispatchEvent(
                    new CustomEvent('tasks:finished', {detail}))""",
                    detail,
                )
            route.fulfill(response=response)

        if start_from == "intake":
            page.route("**/inbox/intake", hold_start)
        preview(page, context, running_app)
        if start_from == "summary":
            dialog = page.get_by_role("dialog")
            expect(dialog).to_be_visible()
            page.route("**/inbox/jobs/*/summarize", hold_start)
            press_by_keyboard(page, dialog.get_by_role("button", name="Continue", exact=True))
        expect(page.locator("#announcer")).to_have_text("Job summary ready.")
        assert len(observed) == 1
        messages = page.evaluate("window.summaryMessages")
        assert not any("Summarize job finished." in message for message in messages)
        assert messages.count("Job summary ready.") == 1
        timings = page.evaluate("window.summaryTimings")
        acknowledgment = "Job saved." if start_from == "intake" else "Summary started."
        acknowledged = next(item["at"] for item in timings if item["text"] == acknowledgment)
        ready = next(item["at"] for item in timings if item["text"] == "Job summary ready.")
        assert ready - acknowledged >= 1000
