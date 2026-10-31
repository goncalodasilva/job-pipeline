"""M7.6 real CLI/fake sessions by keyboard, phone accessibility and delayed responses."""

import json
import re

import pytest
from playwright.sync_api import expect

from app.modules.storage import Database, capture_repo, sessions_repo
from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import PHONE
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, press_by_keyboard, wait_said
from tests.e2e.test_shell import has_no_horizontal_scroll, new_page, open_app
from tests.waiting import wait_until

pytestmark = pytest.mark.e2e


def seed_notes(app, count=4):
    with Database.open(app.data_dir / "app.db") as db, db.transaction() as conn:
        for n in range(count):
            capture_repo.insert_note(
                conn,
                note_id=f"session-screen-note-{n}",
                text=f"Fictional garden route experience {n}.",
                now="2026-10-30T10:00:00+00:00",
            )


def begin(page, app, *, length="5"):
    seed_notes(app)
    open_app(page, app, "/sessions/new")
    page.get_by_label("Length", exact=True).select_option(length)
    press_by_keyboard(page, page.get_by_role("button", name="Start session", exact=True))
    expect(page.locator("#session-answer")).to_be_visible(timeout=20000)


def press(page, name):
    press_by_keyboard(
        page, page.locator("#session-panel").get_by_role("button", name=name, exact=True)
    )


def test_full_session_answer_skip_not_sure_later_summary_by_keyboard_announced(
    running_app, desktop_page
):
    page = desktop_page
    page.add_init_script(RECORD_ANNOUNCEMENTS)
    begin(page, running_app)
    press_by_keyboard(page, page.get_by_label("Your answer", exact=True))
    page.keyboard.type("I planned 12 fictional garden routes.")
    original = page.locator("#session-answer").input_value()
    press(page, "Submit answer")
    expect(page.locator(".session-draft")).to_be_visible(timeout=20000)
    expect(page.locator("#session-answer")).to_have_value(original)
    wait_said(page, "Your answer was sent.")
    expect(page.locator("#session-status")).to_be_focused()
    press(page, "Next question")
    expect(page.locator("#session-panel-heading")).to_have_text("Question 2 of 5")
    press(page, "Skip")
    wait_said(page, "Question skipped.")
    press(page, "Next question")
    expect(page.locator("#session-panel-heading")).to_have_text("Question 3 of 5")
    press(page, "I don't remember")
    wait_said(page, "Not sure recorded.")
    press(page, "Next question")
    expect(page.locator("#session-panel-heading")).to_have_text("Question 4 of 5")
    press_by_keyboard(page, page.locator("summary").filter(has_text="Ask me later"))
    press(page, "Save for later")
    wait_said(page, "Ask me later saved.")
    press(page, "End session")
    expect(page.get_by_role("heading", name="Session summary")).to_be_visible()
    expect(page.locator(".session-draft")).to_have_count(1)
    expect(page.get_by_role("link", name="Leave in Drafts to review")).to_be_visible()
    press(page, "Confirm")
    wait_said(page, "Draft confirmed.")
    expect(page.locator(".session-draft")).to_contain_text("Confirmed")


def test_answer_typing_kept_while_structuring(running_app, desktop_page):
    page = desktop_page
    begin(page, running_app)
    held = []
    page.route("**/sessions/*/answers", lambda route: held.append((route, route.fetch())))
    page.route("**/sessions/*/panel?*", lambda route: route.fulfill(status=204))
    version = page.locator('#session-answer-form [name="version"]').input_value()
    session_version = page.locator('#session-answer-form [name="session_version"]').input_value()
    page.get_by_label("Your answer", exact=True).fill("First fictional route answer.")
    press(page, "Submit answer")
    expect(page.get_by_role("button", name="Submit answer", exact=True)).to_be_disabled()
    wait_until(lambda: page.evaluate("true") and held, timeout=5, message="held session response")
    page.locator("#session-answer").fill("Newer unsaved orchard wording.")
    route, response = held.pop()
    route.fulfill(response=response)
    page.unroute("**/sessions/*/panel?*")
    page.evaluate("() => document.body.dispatchEvent(new Event('tasks:updated'))")
    expect(page.locator(".session-draft")).to_be_visible(timeout=20000)
    expect(page.locator("#session-answer")).to_have_value("Newer unsaved orchard wording.")
    expect(page.locator('#session-answer-form [name="version"]')).to_have_value(version)
    expect(page.locator('#session-answer-form [name="session_version"]')).to_have_value(
        session_version
    )
    expect(page.locator("[data-session-typing-note]")).to_be_visible()
    assert page.evaluate(
        "() => !JSON.stringify({...localStorage, ...sessionStorage})"
        ".includes('Newer unsaved orchard wording')"
    )


def test_session_invalid_answer_focuses_its_field(running_app, desktop_page):
    page = desktop_page
    begin(page, running_app)
    press(page, "Submit answer")
    expect(page.get_by_label("Your answer", exact=True)).to_have_attribute("aria-invalid", "true")
    expect(page.get_by_label("Your answer", exact=True)).to_be_focused()


def test_refused_session_later_keeps_date_and_field_focus(running_app, desktop_page):
    page = desktop_page
    begin(page, running_app)
    page.locator("summary").filter(has_text="Ask me later").click()
    page.locator("#session-later-choice").select_option("date")
    page.locator("#session-later-date").fill("2020-01-01")
    press(page, "Save for later")
    expect(page.locator("#session-later-date")).to_have_attribute("aria-invalid", "true")
    expect(page.locator("#session-later-choice")).to_have_value("date")
    expect(page.locator("#session-later-date")).to_have_value("2020-01-01")
    expect(page.locator("#session-later-date")).to_be_focused()


def test_session_open_later_controls_block_polling(running_app, desktop_page):
    page = desktop_page
    begin(page, running_app)
    page.locator("summary").filter(has_text="Ask me later").click()
    page.locator("#session-later-choice").select_option("date")
    page.locator("#session-later-date").fill("2030-01-01")
    seen = []
    page.route(
        "**/sessions/*/panel?*",
        lambda route: (seen.append(route.request.url), route.fulfill(status=204)),
    )
    page.evaluate("() => document.body.dispatchEvent(new Event('tasks:updated'))")
    page.evaluate("() => fetch('/health').then(r => r.status)")
    assert not seen
    expect(page.locator("#session-later-date")).to_have_value("2030-01-01")


def test_session_pending_post_owns_completion_and_blocks_polling(running_app, desktop_page):
    page = desktop_page
    page.add_init_script(RECORD_ANNOUNCEMENTS)
    begin(page, running_app)
    held, polls = [], []
    page.route("**/sessions/*/answers", lambda route: held.append((route, route.fetch())))
    page.route(
        "**/sessions/*/panel?*",
        lambda route: (polls.append(route.request.url), route.fulfill(status=204)),
    )
    page.locator("#session-answer").fill("I planned fictional orchard routes.")
    press(page, "Submit answer")
    wait_until(lambda: page.evaluate("true") and held, timeout=5, message="held answer response")
    route, response = held.pop()
    key = re.search(r'data-task-announcement-key="([^"]+)"', response.text()).group(1)
    headers = json.loads(page.locator("body").get_attribute("hx-headers"))

    def finished():
        indicator = page.request.get(running_app.base_url + "/tasks/indicator", headers=headers)
        detail = json.loads(indicator.headers.get("hx-trigger", "{}")).get("tasks:finished", {})
        return any(task["key"] == key for task in detail.get("tasks", []))

    wait_until(finished, timeout=10, message="held answer task completed")
    page.evaluate("""() => document.body.addEventListener('tasks:finished', e => {
      window.__sessionFinishedKeys = e.detail.tasks.map(t => t.key);
    })""")
    page.evaluate("() => document.body.dispatchEvent(new Event('tasks:updated'))")
    page.wait_for_function("key => (window.__sessionFinishedKeys || []).includes(key)", arg=key)
    assert "Structure a session answer finished." not in page.evaluate("window.__said")
    assert not polls
    route.fulfill(response=response)
    page.unroute("**/sessions/*/panel?*")
    expect(page.locator("#session-status")).to_contain_text("Your answer was sent.")
    wait_said(page, "Your answer was sent.")
    assert "Structure a session answer finished." not in page.evaluate("window.__said")


@pytest.mark.parametrize("refused", [False, True])
def test_dirty_session_editor_cannot_rebase_on_automatic_refresh(
    running_app, desktop_page, refused
):
    page = desktop_page
    begin(page, running_app)
    page.locator("#session-answer").fill(" " if refused else "Local unsaved orchard text.")
    if refused:
        press(page, "Submit answer")
        expect(page.locator("#session-answer")).to_have_attribute("aria-invalid", "true")
    form = page.locator("#session-answer-form")
    sid = page.locator("#session-panel").get_attribute("data-session-id")
    aid = form.locator('[name="answer_id"]').input_value()
    av = form.locator('[name="version"]').input_value()
    sv = form.locator('[name="session_version"]').input_value()
    # Seed the retained pending state of a failed answer from another tab. The integration
    # stale-answer test exercises this state through the real service and malformed fake.
    with Database.open(running_app.data_dir / "app.db") as db, db.transaction() as conn:
        sessions_repo.update_answer(
            conn,
            aid,
            answer_text="Other tab retained text.",
            outcome="pending",
            now="2026-10-31T10:00:00+00:00",
            expected_version=int(av),
        )
        sessions_repo.touch_session(
            conn, sid, now="2026-10-31T10:00:00+00:00", expected_version=int(sv)
        )
    page.evaluate("""() => document.body.addEventListener('htmx:beforeRequest', e => {
      if (e.detail.requestConfig.verb === 'get' && e.detail.target.id === 'session-panel')
        window.__sessionDirtyBlocked = e.defaultPrevented;
    })""")
    page.evaluate("() => document.body.dispatchEvent(new Event('tasks:updated'))")
    page.wait_for_function("() => window.__sessionDirtyBlocked !== undefined")
    assert page.evaluate("window.__sessionDirtyBlocked") is True
    expect(form.locator('[name="version"]')).to_have_value(av)
    expect(form.locator('[name="session_version"]')).to_have_value(sv)
    page.locator("#session-answer").fill("Local unsaved orchard text.")
    press(page, "Submit answer")
    expect(page.locator("#session-reload")).to_be_visible()
    with Database.open(running_app.data_dir / "app.db") as db:
        assert (
            sessions_repo.get_answer(db.connection, aid).answer_text == "Other tab retained text."
        )


@pytest.mark.parametrize("control", ["later", "answer"])
def test_session_inflight_refresh_keeps_active_controls(running_app, desktop_page, control):
    page = desktop_page
    begin(page, running_app)
    held = []
    page.evaluate("""() => document.body.addEventListener('htmx:beforeRequest', e => {
      if (e.detail.requestConfig.verb === 'get' && e.detail.target.id === 'session-panel')
        e.detail.xhr.addEventListener('loadend', () => {
          window.__sessionPollDone = true;
        }, {once:true});
    })""")
    page.route(
        "**/sessions/*/panel?*",
        lambda route: held.append(
            (route, route.fetch(url=route.request.url.split("?")[0] + "?was=older-review-snapshot"))
        ),
    )
    page.evaluate("() => document.body.dispatchEvent(new Event('tasks:updated'))")
    wait_until(
        lambda: page.evaluate("true") and held, timeout=5, message="held old session refresh"
    )
    if control == "later":
        page.locator("summary").filter(has_text="Ask me later").click()
        page.locator("#session-later-choice").select_option("date")
        page.locator("#session-later-date").fill("2030-01-01")
    else:
        page.locator("#session-answer").fill("Local typing while refresh is in flight.")
        page.evaluate(
            "() => { window.__sessionEditor = document.getElementById('session-answer'); }"
        )
    route, response = held.pop(0)
    assert response.status == 200
    route.fulfill(response=response)
    page.wait_for_function("() => window.__sessionPollDone === true")
    if control == "later":
        expect(page.locator("#session-later-date")).to_be_visible()
        expect(page.locator("#session-later-date")).to_have_value("2030-01-01")
    else:
        # A rejected refresh leaves the actual editor node and its version snapshot intact.
        assert page.evaluate(
            "() => window.__sessionEditor === document.getElementById('session-answer')"
        )
        assert page.evaluate("() => document.activeElement.id") == "session-answer"
        expect(page.locator("#session-answer")).to_have_value(
            "Local typing while refresh is in flight."
        )
    for route, response in held:
        route.fulfill(response=response)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_session_phone_390x844_and_axe(running_app, browser, theme):
    with new_page(browser, PHONE) as page:
        page.emulate_media(color_scheme=theme)
        seed_notes(running_app)
        open_app(page, running_app, "/sessions/new")
        wait_for_settled_styles(page)
        has_no_horizontal_scroll(page)
        assert_no_serious_violations(page)
        press_by_keyboard(page, page.get_by_role("button", name="Start session", exact=True))
        expect(page.locator("#session-answer")).to_be_visible(timeout=20000)
        wait_for_settled_styles(page)
        has_no_horizontal_scroll(page)
        assert_no_serious_violations(page)
        press(page, "End session")
        expect(page.get_by_role("heading", name="Session summary")).to_be_visible()
        assert_no_serious_violations(page)
