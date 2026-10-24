"""A card merged just after rendering recovers from its automatic requests."""

import pytest
from playwright.sync_api import expect

from app.modules.storage import Database
from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_board import desk
from tests.e2e.test_facts_page import press_by_keyboard
from tests.e2e.test_shell import has_no_horizontal_scroll, new_page, open_app

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])
@pytest.mark.parametrize("automatic", ["seen", "card"])
def test_merged_card_automatic_request_refreshes_queue_without_failure_banner(
    running_app, browser, viewport, automatic
):
    with desk(running_app) as seed:
        seed.job("snapshot-root", "Fictional Gardener", "Fictional Grove")
        seed.job("snapshot-duplicate", "Fictional Planner", "Fictional Grove")
    merged = False
    with new_page(browser, viewport) as page:
        missing = []
        page.on(
            "response",
            lambda response: (
                missing.append(response.status)
                if response.url.endswith(f"/inbox/jobs/snapshot-duplicate/{automatic}")
                else None
            ),
        )

        def deliver_stale_card(route):
            nonlocal merged
            response = route.fetch()
            body = response.text()
            assert response.status == 200
            assert 'data-job-id="snapshot-duplicate"' in body
            # The page was valid when rendered. A summary commits its merge before the
            # browser receives it, so the automatic seen/card request gets a real 404.
            with Database.open(running_app.data_dir / "app.db") as db, db.transaction() as conn:
                conn.execute(
                    "UPDATE jobs SET merged_into_job_id='snapshot-root',version=version+1 "
                    "WHERE id='snapshot-duplicate'"
                )
            merged = True
            if automatic == "card":
                # Model the processing card's own task-driven refresh, rather than a
                # user-selected queue link. It must retain the server's strict 404.
                body = body.replace(
                    'data-job-id="snapshot-duplicate" data-state="new"',
                    'data-job-id="snapshot-duplicate" data-state="seen" '
                    'hx-get="/inbox/jobs/snapshot-duplicate/card" hx-trigger="load" '
                    'hx-swap="outerHTML"',
                )
                # Seen is covered separately; this variant asks only for the card.
                body = body.replace('id="inbox-seen"', 'id="disabled-seen" hx-disable')
            route.fulfill(response=response, body=body)

        page.route("**/inbox?selected=snapshot-duplicate", deliver_stale_card)
        open_app(page, running_app, "/inbox?selected=snapshot-duplicate")
        assert merged
        expect(page.locator("#inbox-card")).to_have_attribute("data-job-id", "snapshot-root")
        expect(page.locator("#inbox-queue a[data-job-id]")).to_have_count(1)
        assert 404 in missing
        expect(page.locator("#app-status")).to_be_hidden()
        press_by_keyboard(page, page.locator("#inbox-card-heading"))
        expect(page.get_by_role("dialog", name="Job details", exact=True)).to_be_visible()
        page.keyboard.press("Escape")
        assert has_no_horizontal_scroll(page)[0]
        assert_no_serious_violations(page)


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])
def test_missing_user_requested_job_still_shows_failure(running_app, browser, viewport):
    with desk(running_app) as seed:
        seed.job("snapshot-root", "Fictional Gardener", "Fictional Grove")
    with new_page(browser, viewport) as page:
        open_app(page, running_app, "/inbox")
        page.route(
            "**/inbox/jobs/snapshot-root/detail",
            lambda route: route.fulfill(status=404, body="This job wasn't found."),
        )
        press_by_keyboard(page, page.locator("#inbox-card-heading"))
        expect(page.locator("#app-status")).to_be_visible()
        expect(page.locator("#app-status")).to_contain_text("that action didn't go through")
        assert_no_serious_violations(page)


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])
@pytest.mark.parametrize("editor", ["menu", "dialog"])
@pytest.mark.parametrize("phase", ["acknowledgement", "queue-response"])
def test_stale_card_recovery_preserves_editor_and_resumes_after_leaving(
    running_app, browser, viewport, editor, phase
):
    with desk(running_app) as seed:
        seed.job("snapshot-root", "Fictional Gardener", "Fictional Grove")
        seed.job("snapshot-duplicate", "Fictional Planner", "Fictional Grove")
    with new_page(browser, viewport) as page:
        held_seen, held_queue = [], []
        page.route("**/inbox/jobs/snapshot-duplicate/seen", lambda route: held_seen.append(route))
        if phase == "queue-response":
            page.route("**/inbox/queue?**", lambda route: held_queue.append(route))
        open_app(page, running_app, "/inbox?selected=snapshot-duplicate")
        expect(page.locator("#inbox-seen")).to_have_class("htmx-request")
        assert held_seen
        with Database.open(running_app.data_dir / "app.db") as db, db.transaction() as conn:
            conn.execute(
                "UPDATE jobs SET merged_into_job_id='snapshot-root',version=version+1 "
                "WHERE id='snapshot-duplicate'"
            )
        if phase == "queue-response":
            held_seen[0].continue_()
            expect(page.locator("#inbox")).to_have_class("inbox htmx-request")
            assert held_queue
        if editor == "menu":
            press_by_keyboard(page, page.locator("#inbox-menu"))
            page.locator("#snooze-date").fill("2030-01-02")
        else:
            press_by_keyboard(page, page.locator("#inbox-remove"))
            expect(page.get_by_role("dialog", name="Remove job", exact=True)).to_be_visible()
        if phase == "acknowledgement":
            held_seen[0].continue_()
        else:
            page.unroute("**/inbox/queue?**")
            held_queue[0].continue_()
        expect(page.locator("#inbox-stale-card")).to_be_visible()
        expect(page.locator("#inbox-card")).to_have_attribute("data-job-id", "snapshot-duplicate")
        expect(page.locator("#app-status")).to_be_hidden()
        if editor == "menu":
            expect(page.locator("#snooze-date")).to_have_value("2030-01-02")
            expect(page.locator("#inbox-menu").locator("..")).to_have_attribute("open", "")
            # Reload explicitly abandons the unsaved date; no automatic refresh may do it.
            press_by_keyboard(page, page.get_by_role("button", name="Reload inbox", exact=True))
        else:
            expect(page.get_by_role("dialog", name="Remove job", exact=True)).to_be_visible()
            page.keyboard.press("Escape")
        expect(page.locator("#inbox-card")).to_have_attribute("data-job-id", "snapshot-root")
        expect(page.locator("#inbox-card-heading")).to_be_focused()
        expect(page.locator("#app-status")).to_be_hidden()
        assert_no_serious_violations(page)


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])
def test_late_queue_recovery_does_not_replace_a_new_card_selection(running_app, browser, viewport):
    with desk(running_app) as seed:
        seed.job("snapshot-third", "Fictional Mechanic", "Fictional Grove")
        seed.job("snapshot-root", "Fictional Gardener", "Fictional Grove")
        seed.job("snapshot-duplicate", "Fictional Planner", "Fictional Grove")
    with new_page(browser, viewport) as page:
        held_seen, held_queue = [], []
        page.route("**/inbox/jobs/snapshot-duplicate/seen", lambda route: held_seen.append(route))
        page.route("**/inbox/queue?**", lambda route: held_queue.append(route))
        open_app(page, running_app, "/inbox?selected=snapshot-duplicate")
        expect(page.locator("#inbox-seen")).to_have_class("htmx-request")
        with Database.open(running_app.data_dir / "app.db") as db, db.transaction() as conn:
            conn.execute(
                "UPDATE jobs SET merged_into_job_id='snapshot-root',version=version+1 "
                "WHERE id='snapshot-duplicate'"
            )
        held_seen[0].continue_()
        expect(page.locator("#inbox")).to_have_class("inbox htmx-request")
        assert held_queue
        page.unroute("**/inbox/queue?**")
        press_by_keyboard(page, page.locator('#inbox-queue a[data-job-id="snapshot-third"]'))
        expect(page.locator("#inbox-card")).to_have_attribute("data-job-id", "snapshot-third")
        page.evaluate("""() => {
          window.__queueRecoveryDone = false;
          document.body.addEventListener('htmx:afterRequest', event => {
            if ((event.detail.xhr.responseURL || '').includes('/inbox/queue?')) {
              window.__queueRecoveryDone = true;
            }
          });
        }""")
        held_queue[0].continue_()
        page.wait_for_function("window.__queueRecoveryDone === true")
        expect(page.locator("#inbox-card")).to_have_attribute("data-job-id", "snapshot-third")
        expect(page.locator("#inbox-card-heading")).to_be_focused()
        expect(page.locator("#inbox-stale-card")).to_be_hidden()
        expect(page.locator("#app-status")).to_be_hidden()
        assert_no_serious_violations(page)
