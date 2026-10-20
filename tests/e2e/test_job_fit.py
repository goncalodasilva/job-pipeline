"""M4.5 fit budget confirmation, fake provenance and blocked status at phone width."""

import pytest
from playwright.sync_api import Browser, expect

from app.domain.budgets import Budget
from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_budget_warning_dialog import seed
from tests.e2e.test_fact_refine_panel import seed_fact
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, press_by_keyboard, tab_until
from tests.e2e.test_job_summary import preview
from tests.e2e.test_shell import has_no_horizontal_scroll
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e
BUDGET = Budget(allowance=5, first_warning=2, second_warning=3, block_at=4, reset_day=1)


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_fit_warning_cancel_then_continue_keyboard_fake_label_and_refresh(
    running_app: RunningApp, browser: Browser, viewport, scheme
):
    seed(running_app, BUDGET)
    with browser.new_context(viewport=viewport, color_scheme=scheme) as context:
        page = context.new_page()
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        seed_fact(page, running_app, "Worked with Python and Terraform on fictional deployments.")
        preview(page, context, running_app)
        fit = page.locator('[id^="job-fit-"]').first
        button = fit.get_by_role("button", name="Assess fit", exact=True)
        expect(button).to_be_visible()
        press_by_keyboard(page, button)
        dialog = page.get_by_role("dialog")
        expect(dialog).to_be_visible()
        expect(dialog.get_by_role("button", name="Not now", exact=True)).to_be_focused()
        assert_no_serious_violations(page)
        page.keyboard.press("Escape")
        expect(button).to_be_focused()
        press_by_keyboard(page, button)
        expect(dialog).to_be_visible()
        press_by_keyboard(page, dialog.get_by_role("button", name="Continue", exact=True))
        expect(fit).to_contain_text("Fit: Stretch")
        expect(fit).to_contain_text("Practice data (fake provider)")
        expect(fit).to_contain_text("weren't read")
        expect(fit.get_by_role("button", name="Assess fit again", exact=True)).to_be_visible()
        expect(fit.get_by_role("heading", name="How you match")).to_be_focused()
        tab_until(page, page.locator("#job-text"))
        with page.expect_response(
            lambda response: "/fit" in response.url and response.request.method == "GET"
        ):
            page.evaluate(
                "() => document.body.dispatchEvent(new CustomEvent('tasks:resync',{bubbles:true}))"
            )
        expect(page.locator("#job-text")).to_be_focused()
        assert_no_serious_violations(page)
        assert has_no_horizontal_scroll(page)[0]
