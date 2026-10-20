"""M4.7 inbox triage, keyboard/focus, phone, announcements and disabled shortcuts."""

import re

import pytest
from playwright.sync_api import expect

from app.modules.storage import Database
from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, press_by_keyboard, tab_until
from tests.e2e.test_shell import assert_target_size, has_no_horizontal_scroll, open_app
from tests.job_fixtures import generate_jobs, seed_jobs
from tests.waiting import wait_until

pytestmark = pytest.mark.e2e


def seed(app):
    with Database.open(app.data_dir / "app.db") as db:
        seed_jobs(db, generate_jobs(3))


def state(app, id):
    with Database.open(app.data_dir / "app.db") as db:
        return db.connection.execute("SELECT triage_state FROM jobs WHERE id=?", (id,)).fetchone()[
            0
        ]


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_ac_m4_7_triage_by_keyboard_announces_each_action(running_app, browser, viewport, scheme):
    seed(running_app)
    with browser.new_context(viewport=viewport, color_scheme=scheme) as context:
        page = context.new_page()
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        open_app(page, running_app, "/inbox")
        card = page.locator("#inbox-card")
        expect(card).to_contain_text("fixture0003")
        wait_until(
            lambda: state(running_app, "job-0003") == "seen", timeout=10, message="shown card seen"
        )
        assert state(running_app, "job-0002") == "new"
        tab_until(page, page.locator("#inbox-card-heading"))
        page.keyboard.press("j")
        expect(card).to_contain_text("fixture0002")
        expect(page.locator("#inbox-card-heading")).to_be_focused()
        page.keyboard.press("k")
        expect(card).to_contain_text("fixture0003")
        page.keyboard.press("a")
        expect(card).to_contain_text("fixture0002")
        expect(page.locator("#inbox-undo")).to_contain_text("Marked to apply.")
        assert state(running_app, "job-0003") == "to_apply"
        press_by_keyboard(
            page, page.locator("#inbox-undo").get_by_role("button", name="Undo", exact=True)
        )
        expect(card).to_contain_text("fixture0003")
        page.keyboard.press("r")
        dialog = page.get_by_role("dialog", name="Remove job", exact=True)
        expect(dialog).to_be_visible()
        press_by_keyboard(page, dialog.get_by_role("button", name="Salary", exact=True))
        expect(card).to_contain_text("fixture0002")
        expect(page.locator("#inbox-undo")).to_contain_text("Removed. Undo available.")
        assert state(running_app, "job-0003") == "removed"
        press_by_keyboard(
            page, page.locator("#inbox-undo").get_by_role("button", name="Undo", exact=True)
        )
        expect(card).to_contain_text("fixture0003")
        page.keyboard.press("Enter")
        details = page.get_by_role("dialog", name="Job details", exact=True)
        expect(details).to_be_visible()
        expect(details.get_by_role("button", name="Close", exact=True)).to_be_focused()
        page.keyboard.press("Escape")
        expect(details).not_to_be_visible()
        expect(page.locator("#inbox-card-heading")).to_be_focused()
        press_by_keyboard(page, card.locator("summary", has_text="Card menu"))
        press_by_keyboard(page, card.get_by_role("button", name="Tomorrow", exact=True))
        expect(card).to_contain_text("fixture0002")
        expect(page.locator("#inbox-undo")).to_contain_text("Snoozed until")
        assert state(running_app, "job-0003") == "snoozed"
        assert_no_serious_violations(page)
        assert has_no_horizontal_scroll(page)[0]
        # Each action was announced even when a later card message replaced it.
        messages = page.evaluate("() => window.__said")
        assert any("Marked to apply." in m for m in messages)
        assert any("Removed." in m for m in messages)
        assert any("Snoozed until" in m for m in messages)
        assert any("Job 2 of 3" in m for m in messages)


def test_ac_m4_6_remove_undo_within_window(running_app, browser):
    seed(running_app)
    with browser.new_context(viewport=PHONE) as context:
        page = context.new_page()
        page.clock.install()
        open_app(page, running_app, "/inbox")
        wait_until(lambda: state(running_app, "job-0003") == "seen", timeout=10, message="seen")
        tab_until(page, page.locator("#inbox-card-heading"))
        page.keyboard.press("r")
        dialog = page.get_by_role("dialog", name="Remove job", exact=True)
        press_by_keyboard(page, dialog.get_by_role("button", name="Other", exact=True))
        expect(dialog.get_by_label("Reason (optional)")).to_be_focused()
        page.keyboard.type("A fictional reason")
        press_by_keyboard(page, dialog.get_by_role("button", name="Remove job", exact=True))
        expect(
            page.locator("#inbox-undo").get_by_role("button", name="Undo", exact=True)
        ).to_be_visible()
        page.clock.fast_forward(11000)
        expect(
            page.locator("#inbox-undo").get_by_role("button", name="Undo", exact=True)
        ).not_to_be_visible()
        press_by_keyboard(page, page.get_by_role("link", name="Removed", exact=True))
        expect(page.locator("#inbox-card")).to_contain_text("fixture0003")
        press_by_keyboard(page, page.get_by_role("button", name="Restore", exact=True))
        wait_until(lambda: state(running_app, "job-0003") == "seen", timeout=10, message="restored")
        assert_no_serious_violations(page)


def test_shortcuts_ignored_while_typing_and_off_when_disabled(running_app, browser):
    seed(running_app)
    with browser.new_context(viewport=PHONE) as context:
        page = context.new_page()
        open_app(page, running_app, "/inbox")
        press_by_keyboard(page, page.get_by_role("button", name="+ Add job", exact=True))
        wait_until(lambda: state(running_app, "job-0003") == "seen", timeout=10, message="seen")
        field = page.locator("#job-text")
        tab_until(page, field)
        page.keyboard.type("jkar?")
        expect(field).to_have_value("jkar?")
        # Nothing moved: the same card, still only seen, no dialog, no help opened by "?".
        assert state(running_app, "job-0003") == "seen"
        assert page.locator("#inbox-card").get_attribute("data-job-id") == "job-0003"
        assert page.evaluate("() => document.querySelectorAll('dialog[open]').length") == 0
        assert page.evaluate("() => document.getElementById('inbox-help').open") is False
        page.keyboard.press("Escape")
        open_app(page, running_app, "/settings")
        toggle = page.get_by_label("Use single-key shortcuts", exact=True)
        tab_until(page, toggle)
        page.keyboard.press("Space")
        press_by_keyboard(
            page, page.locator("#shortcuts-settings").get_by_role("button", name="Save", exact=True)
        )
        expect(page.locator("#shortcuts-settings")).to_contain_text("Saved.")
        open_app(page, running_app, "/inbox")
        assert page.locator("#inbox-page").get_attribute("data-shortcuts") == "false"
        # The first visit marked job-0003 seen, so the queue now starts with a new job.
        shown = page.locator("#inbox-card").get_attribute("data-job-id")
        tab_until(page, page.locator("#inbox-card-heading"))
        page.keyboard.press("j")
        page.keyboard.press("a")
        page.keyboard.press("r")
        expect(page.get_by_role("dialog", name="Remove job", exact=True)).not_to_be_visible()
        assert page.locator("#inbox-card").get_attribute("data-job-id") == shown
        assert {state(running_app, id) for id in ("job-0001", "job-0002", "job-0003")} <= {
            "new",
            "seen",
        }
        assert_no_serious_violations(page)


def test_ac_m4_2_add_job_by_url_then_paste_fallback(running_app, browser):
    with browser.new_context(viewport=PHONE) as context:
        page = context.new_page()
        open_app(page, running_app, "/inbox")
        press_by_keyboard(page, page.get_by_role("button", name="+ Add job", exact=True))
        tab_until(page, page.locator("#job-url"))
        page.keyboard.type("http://127.0.0.1/ad")
        press_by_keyboard(page, page.get_by_role("button", name="Add job", exact=True))
        expect(page.locator("#job-intake-error")).to_contain_text("Paste the job text instead")
        expect(page.locator("#job-text")).to_be_focused()
        page.keyboard.type(
            "Fictional canal gardener tending imaginary flowers, watering plants and "
            "planning seasonal planting with a small team."
        )
        press_by_keyboard(page, page.get_by_role("button", name="Add job", exact=True))
        expect(page.locator("#job-intake")).to_contain_text("Job saved.")
        page.keyboard.press("Escape")
        expect(page.locator("#inbox-queue").locator("a[data-job-id]")).to_have_count(1)
        expect(page.locator("#inbox-card")).to_be_visible()
        assert_no_serious_violations(page)
        assert has_no_horizontal_scroll(page)[0]


# Every request the page makes, recorded in the page (the sync API only delivers events while
# a call is running, and these tests wait with sleeps).
RECORD_REQUESTS = """
window.__requests = [];
// Recorded when sent, not when opened: htmx opens a request it may still cancel.
const open = XMLHttpRequest.prototype.open;
const sendXhr = XMLHttpRequest.prototype.send;
XMLHttpRequest.prototype.open = function (method, url) {
  this.__request = [method.toUpperCase(), String(url)];
  return open.apply(this, arguments);
};
XMLHttpRequest.prototype.send = function () {
  window.__requests.push(this.__request);
  return sendXhr.apply(this, arguments);
};
const send = window.fetch;
window.fetch = function (input, init) {
  const method = (init && init.method) || (input && input.method) || "GET";
  window.__requests.push([method.toUpperCase(), String(input && input.url || input)]);
  return send.apply(this, arguments);
};
"""


def requests(page, method, fragment):
    found = page.evaluate("() => window.__requests")
    return [url for verb, url in found if verb == method and fragment in url]


def test_ac_m4_6_card_marked_seen_when_shown(running_app, browser):
    seed(running_app)
    with browser.new_context(viewport=DESKTOP) as context:
        page = context.new_page()
        page.add_init_script(RECORD_REQUESTS)
        open_app(page, running_app, "/inbox")
        expect(page.locator("#inbox-card")).to_contain_text("fixture0003")
        wait_until(lambda: state(running_app, "job-0003") == "seen", timeout=10, message="shown")
        # The next card is fetched ahead of time by a GET, which marks nothing.
        wait_until(
            lambda: requests(page, "GET", "/inbox/jobs/job-0002/card"),
            timeout=10,
            message="next card prefetched",
        )
        assert state(running_app, "job-0002") == "new"
        assert requests(page, "POST", "/inbox/jobs/job-0003/seen") != []
        assert requests(page, "POST", "/inbox/jobs/job-0002/seen") == []
        assert not [r for r in requests(page, "GET", "/seen")]
        tab_until(page, page.locator("#inbox-card-heading"))
        page.keyboard.press("j")
        expect(page.locator("#inbox-card")).to_contain_text("fixture0002")
        wait_until(lambda: state(running_app, "job-0002") == "seen", timeout=10, message="shown")
        page.keyboard.press("k")
        expect(page.locator("#inbox-card")).to_contain_text("fixture0003")
        # A card that is already seen is not marked again, and job-0001 was never shown.
        assert len(requests(page, "POST", "/inbox/jobs/job-0003/seen")) == 1
        assert state(running_app, "job-0001") == "new"


def test_inbox_phone_390x844_no_sideways_scroll(running_app, browser):
    seed(running_app)
    with Database.open(running_app.data_dir / "app.db") as db, db.transaction() as conn:
        conn.execute(
            "UPDATE jobs SET title=? WHERE id='job-0003'", ("Fictional" + "Gardener" * 40,)
        )
    with browser.new_context(viewport=PHONE) as context:
        page = context.new_page()
        open_app(page, running_app, "/inbox")
        card = page.locator("#inbox-card")
        expect(card).to_be_visible()
        assert has_no_horizontal_scroll(page)[0]
        # One column: the queue and the card share a left edge instead of sitting side by side.
        queue_box = page.locator("#inbox-queue").bounding_box()
        card_box = card.bounding_box()
        assert queue_box is not None and card_box is not None
        assert abs(queue_box["x"] - card_box["x"]) < 1
        assert queue_box["y"] != card_box["y"]
        for name in ("Apply", "Remove"):
            assert_target_size(card.get_by_role("button", name=re.compile(rf"^{name}")), name)
        assert_target_size(card.locator("summary", has_text="Card menu"), "Card menu")
        assert_no_serious_violations(page)
        press_by_keyboard(page, page.get_by_role("button", name="+ Add job", exact=True))
        assert has_no_horizontal_scroll(page)[0]
        assert_target_size(page.get_by_role("button", name="Add job", exact=True), "Add job")
        assert_no_serious_violations(page)
        page.keyboard.press("Escape")
        press_by_keyboard(page, card.locator("summary", has_text="Card menu"))
        assert has_no_horizontal_scroll(page)[0]
        assert_no_serious_violations(page)


def test_refused_removal_reopens_with_its_error_and_stale_card_asks_for_reload(
    running_app, browser
):
    seed(running_app)
    with browser.new_context(viewport=PHONE) as context:
        page = context.new_page()
        open_app(page, running_app, "/inbox")
        wait_until(lambda: state(running_app, "job-0003") == "seen", timeout=10, message="seen")
        tab_until(page, page.locator("#inbox-card-heading"))
        page.keyboard.press("r")
        dialog = page.get_by_role("dialog", name="Remove job", exact=True)
        press_by_keyboard(page, dialog.get_by_role("button", name="Other", exact=True))
        # Only a forged form can send more than the box allows; the server still answers 422.
        page.evaluate("() => document.getElementById('remove-text').removeAttribute('maxlength')")
        page.keyboard.type("x" * 501)
        press_by_keyboard(page, dialog.get_by_role("button", name="Remove job", exact=True))
        expect(dialog).to_be_visible()
        expect(dialog).to_contain_text("500 characters")
        expect(dialog.get_by_label("Reason (optional)")).to_have_attribute("aria-invalid", "true")
        expect(dialog.get_by_label("Reason (optional)")).to_be_focused()
        assert state(running_app, "job-0003") == "seen"
        page.keyboard.press("Escape")
        expect(dialog).not_to_be_visible()
        expect(page.locator("#inbox-remove")).to_be_focused()
        # The job changed elsewhere: the next move says so instead of overwriting it.
        with Database.open(running_app.data_dir / "app.db") as db, db.transaction() as conn:
            conn.execute("UPDATE jobs SET version=version+1 WHERE id='job-0003'")
        page.keyboard.press("r")
        press_by_keyboard(page, dialog.get_by_role("button", name="Salary", exact=True))
        expect(dialog).not_to_be_visible()
        notice = page.locator("#inbox-notice")
        expect(notice).to_contain_text("Reload the page")
        expect(notice.locator("#inbox-reload")).to_be_focused()
        assert state(running_app, "job-0003") == "seen"
        assert_no_serious_violations(page)


def row(app, sql, *params):
    with Database.open(app.data_dir / "app.db") as db:
        return tuple(db.connection.execute(sql, params).fetchone())


def seed_one(app):
    with Database.open(app.data_dir / "app.db") as db:
        seed_jobs(db, generate_jobs(1))


def shortcuts_off(app):
    with Database.open(app.data_dir / "app.db") as db, db.transaction() as conn:
        conn.execute(
            "INSERT INTO settings (key,value,created_at,updated_at,version) "
            "VALUES ('shortcuts','{\"enabled\":false}','2026-10-20','2026-10-20',1)"
        )


def test_other_then_a_chip_and_enter_in_the_reason_box_post_one_reason(running_app, browser):
    seed(running_app)
    sql = "SELECT removal_reason,removal_text FROM jobs WHERE id=?"
    with browser.new_context(viewport=PHONE) as context:
        page = context.new_page()
        open_app(page, running_app, "/inbox")
        wait_until(lambda: state(running_app, "job-0003") == "seen", timeout=10, message="seen")
        tab_until(page, page.locator("#inbox-card-heading"))
        dialog = page.get_by_role("dialog", name="Remove job", exact=True)
        page.keyboard.press("r")
        other = dialog.get_by_role("button", name="Other", exact=True)
        press_by_keyboard(page, other)
        expect(dialog.get_by_label("Reason (optional)")).to_be_focused()
        expect(other).to_have_attribute("aria-expanded", "true")
        page.keyboard.type("Not sent with a chip")
        press_by_keyboard(page, dialog.get_by_role("button", name="Salary", exact=True))
        expect(page.locator("#inbox-card")).to_contain_text("fixture0002")
        assert state(running_app, "job-0003") == "removed"
        assert row(running_app, sql, "job-0003") == ("salary", None)
        # Enter in the reason box removes with "Other" and the words, never "Too junior".
        page.keyboard.press("r")
        press_by_keyboard(page, dialog.get_by_role("button", name="Other", exact=True))
        page.keyboard.type("A fictional reason")
        page.keyboard.press("Enter")
        expect(page.locator("#inbox-card")).to_contain_text("fixture0001")
        assert row(running_app, sql, "job-0002") == ("other", "A fictional reason")
        assert_no_serious_violations(page)


def test_enter_in_the_snooze_date_snoozes_to_that_date(running_app, browser):
    seed(running_app)
    with browser.new_context(viewport=PHONE) as context:
        page = context.new_page()
        open_app(page, running_app, "/inbox")
        wait_until(lambda: state(running_app, "job-0003") == "seen", timeout=10, message="seen")
        press_by_keyboard(page, page.locator("summary", has_text="Card menu"))
        date = page.get_by_label("Or pick a date")
        date.fill("2099-01-05")
        date.press("Enter")
        expect(page.locator("#inbox-undo")).to_contain_text("Snoozed until Mon 5 Jan.")
        assert row(running_app, "SELECT snooze_until FROM jobs WHERE id='job-0003'") == (
            "2099-01-05",
        )


def test_last_card_gone_moves_focus_to_the_empty_state_and_z_undoes(running_app, browser):
    seed_one(running_app)
    with browser.new_context(viewport=PHONE) as context:
        page = context.new_page()
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        open_app(page, running_app, "/inbox")
        wait_until(lambda: state(running_app, "job-0001") == "seen", timeout=10, message="seen")
        page.keyboard.press("a")
        heading = page.locator("#inbox-empty-heading")
        expect(heading).to_be_focused()
        expect(page.locator("#inbox-card")).to_have_count(0)
        expect(page.locator(".empty-state")).to_contain_text(
            "That was the last one. Add another by link or by pasting the ad."
        )
        assert_no_serious_violations(page)
        page.keyboard.press("z")
        expect(page.locator("#inbox-card")).to_contain_text("fixture0001")
        assert state(running_app, "job-0001") == "seen"
        assert any("Marked to apply." in said for said in page.evaluate("() => window.__said"))


def test_undo_waits_while_focused_and_has_a_shortcut(running_app, browser):
    seed(running_app)
    with browser.new_context(viewport=PHONE) as context:
        page = context.new_page()
        page.clock.install()
        open_app(page, running_app, "/inbox")
        wait_until(lambda: state(running_app, "job-0003") == "seen", timeout=10, message="seen")
        page.keyboard.press("a")
        undo = page.locator("#inbox-undo")
        button = undo.get_by_role("button", name="Undo", exact=True)
        expect(button).to_be_visible()
        expect(button).to_have_attribute("aria-keyshortcuts", "Z")
        button.focus()
        page.clock.fast_forward(30000)
        expect(button).to_be_visible()
        page.locator("#inbox-card-heading").focus()
        page.clock.fast_forward(11000)
        expect(button).not_to_be_visible()
        # Z undoes the last move without finding the button.
        page.keyboard.press("a")
        expect(button).to_be_visible()
        page.keyboard.press("z")
        wait_until(
            lambda: state(running_app, "job-0002") in ("new", "seen"), timeout=10, message="undone"
        )
        expect(page.locator("#inbox-card")).to_contain_text("fixture0002")
        expect(button).not_to_be_visible()


@pytest.mark.parametrize("on", [True, False])
def test_question_mark_opens_the_help_and_escape_closes_it(running_app, browser, on):
    seed(running_app)
    if not on:
        shortcuts_off(running_app)
    with browser.new_context(viewport=DESKTOP) as context:
        page = context.new_page()
        open_app(page, running_app, "/inbox")
        help = page.locator("#inbox-help")
        page.keyboard.press("?")
        expect(help).to_have_attribute("open", "")
        if on:
            for text in ("Next job", "Previous job", "Mark the job to apply", "Undo"):
                expect(help).to_contain_text(text)
        else:
            expect(help).to_contain_text("Single-key shortcuts are off")
            expect(help.get_by_role("link", name="Settings")).to_be_visible()
        assert_no_serious_violations(page)
        page.keyboard.press("Escape")
        expect(help).not_to_have_attribute("open", "")
        # The letters and their hints exist only while shortcuts are on.
        keys = page.locator("#inbox-card .key")
        assert (keys.count() > 0) is on
        page.keyboard.press("j")
        assert page.locator("#inbox-card").get_attribute("data-job-id") == (
            "job-0002" if on else "job-0003"
        )


def test_processing_refresh_keeps_an_open_menu_and_typed_date(running_app, browser):
    seed(running_app)
    with Database.open(running_app.data_dir / "app.db") as db, db.transaction() as conn:
        conn.execute(
            "INSERT INTO tasks (id,type,status,dedupe_key,created_at,updated_at) "
            "VALUES ('task-fictional-1','summarize_job','running','summarize_job:job-0003',"
            "'2026-10-20','2026-10-20')"
        )
    with browser.new_context(viewport=PHONE) as context:
        page = context.new_page()
        page.add_init_script(RECORD_REQUESTS)
        page.clock.install()
        open_app(page, running_app, "/inbox")
        expect(page.locator("#inbox-card")).to_contain_text("Processing")
        wait_until(lambda: state(running_app, "job-0003") == "seen", timeout=10, message="seen")
        menu = page.locator("summary", has_text="Card menu")
        press_by_keyboard(page, menu)
        date = page.get_by_label("Or pick a date")
        date.fill("2099-01-05")
        asked = len(requests(page, "GET", "/inbox/jobs/job-0003/card"))
        page.clock.fast_forward(10000)
        expect(page.locator("details.card-menu")).to_have_attribute("open", "")
        expect(date).to_have_value("2099-01-05")
        assert len(requests(page, "GET", "/inbox/jobs/job-0003/card")) == asked
        # With nothing open the card does ask again, and keeps focus where it was.
        date.fill("")
        press_by_keyboard(page, menu)
        expect(page.locator("details.card-menu")).not_to_have_attribute("open", "")
        page.locator("#inbox-card-heading").focus()
        page.clock.fast_forward(10000)
        wait_until(
            lambda: len(requests(page, "GET", "/inbox/jobs/job-0003/card")) > asked,
            timeout=10,
            message="refresh asked again",
        )
        expect(page.locator("#inbox-card-heading")).to_be_focused()


def test_a_quick_action_waits_for_the_seen_answer_instead_of_failing(running_app, browser):
    seed(running_app)
    held = []
    with browser.new_context(viewport=PHONE) as context:
        page = context.new_page()
        page.route("**/inbox/jobs/job-0003/seen", lambda route: held.append(route))
        open_app(page, running_app, "/inbox")
        expect(page.locator("#inbox-card")).to_contain_text("fixture0003")
        page.wait_for_timeout(500)
        assert len(held) == 1 and state(running_app, "job-0003") == "new"
        page.keyboard.press("a")
        page.wait_for_timeout(300)
        assert state(running_app, "job-0003") == "new"  # waiting, not refused
        held[0].continue_()
        wait_until(
            lambda: state(running_app, "job-0003") == "to_apply", timeout=10, message="applied"
        )
        expect(page.locator("#inbox-undo")).to_contain_text("Marked to apply.")
        expect(page.locator("#inbox-notice")).to_be_empty()


def test_prefetch_only_fetches_inbox_cards_of_this_site(running_app, browser):
    seed(running_app)
    with browser.new_context(viewport=PHONE) as context:
        page = context.new_page()
        page.add_init_script(RECORD_REQUESTS)
        open_app(page, running_app, "/inbox")
        wait_until(lambda: state(running_app, "job-0003") == "seen", timeout=10, message="seen")
        page.evaluate(
            "() => document.querySelector(\"#inbox-queue a[data-job-id='job-0002']\")"
            ".setAttribute('hx-get', 'https://other.example.test/inbox/jobs/job-0002/card')"
        )
        page.evaluate("() => document.body.dispatchEvent(new CustomEvent('htmx:afterSettle'))")
        page.wait_for_timeout(300)
        assert requests(page, "GET", "other.example.test") == []
