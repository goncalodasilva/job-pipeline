"""M6.5 the workspace's Tailoring tab in a real browser: the gap check and the angle by keyboard
with announcements, and every step at phone and desktop size, light and dark, with axe.

The app starts with a completed setup (the fake provider saved), so the gap check and the angle
proposals are the fake provider's practice answers: no requirement is shown by a fact and two
practice angles that cite nothing (both flagged). A fictional application with a current summary
(four requirements, two of them must-haves) and three confirmed facts is written into the app's
temporary database before the page is opened. See ``tests/tailoring_pages.py`` for the assumed
routes and markup; the browser-facing contract is:

- The application page has links "Timeline and contacts" and "Tailoring" in
  ``nav[aria-label="Application sections"]``. ``#tailoring`` holds the stepper
  (``nav[aria-label="Tailoring steps"]``, one link per step named for its title, with the state
  in words), the heading ``h4`` "Step N: <title>" (focus goes there after an answer with a
  message) and the step.
- Step 1: button "Check gaps" ("Check again" once a result exists) says "Gap check started." in
  the live region; ``[data-requirement-id]`` items; ``#gap-check-message``; the practice notice;
  each gap has a "Add a fact" summary opening a form with a field labelled "Fact" and the button
  "Save fact" ("Fact added.").
- Step 2: button "Propose angles" says "Angle proposals started."; ``li[data-angle]`` items with
  fields labelled "Angle name" and "Description" and the button "Use this angle" ("Angle
  saved."); ``#chosen-angle`` reads "Your angle: <name>".
- Opening a step from the stepper swaps ``#tailoring`` without a reload.

Keyboard path: Tab and Enter only (no clicks) from the application page. No fixed waits.
"""

import re
from datetime import UTC, datetime

import pytest
from playwright.sync_api import Browser, Page, expect

from app.modules.storage import Database
from app.services.facts import FactService
from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_board import desk, rows
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, press_by_keyboard, tab_until, wait_said
from tests.e2e.test_shell import assert_target_size, has_no_horizontal_scroll, new_page, open_app
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory
from tests.running_app import RunningApp
from tests.tailoring_pages import page_url, seed_facts, seed_summary
from tests.waiting import wait_until

pytestmark = pytest.mark.e2e

JOB_ID = "tailoring-e2e-job"
PRACTICE = "Practice data (fake provider)"
NEW_FACT = "Led a fictional Terraform migration for 2 teams."
RESULT_TIMEOUT_MS = 20_000


def seeded(app: RunningApp) -> str:
    """The application id of a fictional application with a summary and confirmed facts."""
    with desk(app) as seed:
        application = seed.application(
            JOB_ID, "Route Planner", "Harbor Kite Fictional", "preparing"
        )
        seed_summary(seed.db, JOB_ID)
    with Database.open(app.data_dir / "app.db") as db:
        clock = FrozenClock(datetime(2026, 1, 5, 9, 0, tzinfo=UTC))
        seed_facts(FactService(db, clock, SequentialIdFactory("seedfact")))
    return str(application.id)


def region(page: Page):
    return page.locator("#tailoring")


def heading(page: Page, step: int, title: str):
    return region(page).get_by_role("heading", name=f"Step {step}: {title}")


def stepper_link(page: Page, title: str):
    nav = region(page).get_by_role("navigation", name="Tailoring steps")
    return nav.get_by_role("link", name=re.compile(rf"\b{re.escape(title)}\b"))


def test_workspace_gap_check_and_angle_by_keyboard_announced(
    running_app: RunningApp, browser: Browser
):
    aid = seeded(running_app)
    with new_page(browser, DESKTOP) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        open_app(page, running_app, f"/applications/{aid}")
        # The tab is a link; Timeline and contacts is still the default.
        expect(page.locator(".application-timeline")).to_be_visible()
        press_by_keyboard(page, page.get_by_role("link", name="Tailoring", exact=True))
        expect(heading(page, 1, "Gap check")).to_be_visible()
        expect(region(page).get_by_role("navigation", name="Tailoring steps")).to_contain_text(
            "Not started"
        )

        # Step 1: start by keyboard, hear it, find the focus on the step heading.
        press_by_keyboard(page, region(page).get_by_role("button", name="Check gaps", exact=True))
        wait_said(page, "Gap check started.")
        expect(heading(page, 1, "Gap check")).to_be_focused()
        gaps = region(page).locator("[data-requirement-id]")
        expect(gaps).to_have_count(4, timeout=RESULT_TIMEOUT_MS)
        expect(region(page)).to_contain_text(PRACTICE)
        expect(region(page).locator("#gap-check-message")).to_contain_text(
            "It may not be worth applying for."
        )
        expect(region(page).get_by_role("navigation", name="Tailoring steps")).to_contain_text(
            "Done"
        )

        # Add a fact at a gap, by keyboard: it is confirmed at once and announced.
        gap = region(page).locator('[data-requirement-id="req-2"]')
        press_by_keyboard(page, gap.get_by_text("Add a fact", exact=True))
        field = gap.get_by_label("Fact", exact=True)
        tab_until(page, field)
        page.keyboard.type(NEW_FACT)
        press_by_keyboard(page, gap.get_by_role("button", name="Save fact", exact=True))
        wait_said(page, "Fact added.")
        expect(heading(page, 1, "Gap check")).to_be_focused()
        found = rows(
            running_app,
            "SELECT f.status FROM facts f JOIN fact_versions v ON v.id=f.current_version_id "
            "WHERE v.text=?",
            (NEW_FACT,),
        )
        assert [row[0] for row in found] == ["confirmed"]

        # Check again, by keyboard, and see the stored history grow.
        press_by_keyboard(page, region(page).get_by_role("button", name="Check again", exact=True))
        wait_said(page, "Gap check started.", times=2)
        wait_until(
            lambda: len(rows(running_app, "SELECT id FROM gap_checks")) == 2,
            timeout=20,
            message="the second gap check was stored",
        )

        # Step 2 from the stepper, by keyboard; the page is not reloaded.
        page.evaluate("() => { window.__same_document = true; }")
        press_by_keyboard(page, stepper_link(page, "Angle"))
        expect(heading(page, 2, "Angle")).to_be_visible()
        assert page.evaluate("() => window.__same_document === true")
        press_by_keyboard(page, region(page).get_by_role("button", name="Propose angles"))
        wait_said(page, "Angle proposals started.")
        proposals = region(page).locator("li[data-angle]")
        expect(proposals).to_have_count(2, timeout=RESULT_TIMEOUT_MS)
        expect(region(page)).to_contain_text(PRACTICE)
        expect(proposals.first).to_contain_text("This isn't in your fact bank.")

        # Edit the first proposal by keyboard and use it.
        first = proposals.first
        tab_until(page, first.get_by_label("Angle name", exact=True))
        page.keyboard.press("ControlOrMeta+A")
        page.keyboard.type("Edited practice angle")
        press_by_keyboard(page, first.get_by_role("button", name="Use this angle"))
        wait_said(page, "Angle saved.")
        expect(heading(page, 2, "Angle")).to_be_focused()
        expect(region(page).locator("#chosen-angle")).to_have_text(
            "Your angle: Edited practice angle"
        )
        saved = rows(running_app, "SELECT name, origin FROM angles")
        assert [tuple(row) for row in saved] == [("Edited practice angle", "user")]
        expect(region(page).get_by_role("navigation", name="Tailoring steps")).to_contain_text(
            "Done"
        )
        assert_no_serious_violations(page)


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone-390x844"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_workspace_steps_phone_390x844_and_axe(
    running_app: RunningApp, browser: Browser, viewport, scheme
):
    aid = seeded(running_app)
    with new_page(browser, viewport, color_scheme=scheme) as page:
        # Every step opens from its address; the placeholders say they come later.
        for step, title in enumerate(("Gap check", "Angle", "CV", "Cover letter", "Submit"), 1):
            open_app(page, running_app, page_url(aid, step))
            expect(heading(page, step, title)).to_be_visible()
            assert has_no_horizontal_scroll(page)[0], f"step {step} scrolls sideways"
            for link in (
                region(page)
                .get_by_role("navigation", name="Tailoring steps")
                .get_by_role("link")
                .all()
            ):
                assert_target_size(link, f"stepper link on step {step}")
            if step >= 4:
                expect(region(page)).to_contain_text(re.compile("later step", re.IGNORECASE))
            assert_no_serious_violations(page)

        # Step 1 with a result, and with a gap's "Add a fact" open.
        open_app(page, running_app, page_url(aid, 1))
        region(page).get_by_role("button", name="Check gaps", exact=True).click()
        expect(region(page).locator("[data-requirement-id]")).to_have_count(
            4, timeout=RESULT_TIMEOUT_MS
        )
        expect(region(page)).to_contain_text(PRACTICE)
        wait_for_settled_styles(page)
        assert has_no_horizontal_scroll(page)[0]
        assert_no_serious_violations(page)
        region(page).locator('[data-requirement-id="req-2"]').get_by_text(
            "Add a fact", exact=True
        ).click()
        expect(
            region(page).locator('[data-requirement-id="req-2"]').get_by_label("Fact", exact=True)
        ).to_be_visible()
        assert has_no_horizontal_scroll(page)[0]
        assert_no_serious_violations(page)

        # Step 2 with proposals.
        open_app(page, running_app, page_url(aid, 2))
        region(page).get_by_role("button", name="Propose angles").click()
        expect(region(page).locator("li[data-angle]")).to_have_count(2, timeout=RESULT_TIMEOUT_MS)
        wait_for_settled_styles(page)
        assert has_no_horizontal_scroll(page)[0]
        assert_target_size(
            region(page).get_by_role("button", name="Use this angle").first, "Use this angle"
        )
        assert_no_serious_violations(page)
