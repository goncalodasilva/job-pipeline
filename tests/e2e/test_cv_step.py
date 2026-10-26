"""M6.8 CV review with the real CLI, fictional facts, keyboard, phone and axe."""

import pytest
from playwright.sync_api import Browser, Page, expect

from app.modules.storage import Database
from tests.document_pages import template_json
from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_cv_template_screens import seeded_in_portugal
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, press_by_keyboard, tab_until, wait_said
from tests.e2e.test_shell import has_no_horizontal_scroll, new_page, open_app
from tests.running_app import RunningApp
from tests.tailoring_pages import CURRENT_STAMP
from tests.waiting import wait_until

pytestmark = pytest.mark.e2e
NEW_CLAIM = "Led 9 people and saved $2m."
TYPED = "My unsaved careful wording."


def seeded_cv(app: RunningApp) -> str:
    aid = seeded_in_portugal(app)
    with Database.open(app.data_dir / "app.db") as db:
        db.connection.execute(
            "INSERT INTO cv_templates (id,country,status,spec,approved_at,provider,model,"
            "prompt_id,prompt_version,created_at,updated_at) "
            "VALUES ('cv-e2e-template','PT','approved',?,?,'fake','fake-thorough',"
            "'draft_cv_template','1',?,?)",
            (template_json(), CURRENT_STAMP, CURRENT_STAMP, CURRENT_STAMP),
        )
    return aid


def open_cv(page: Page, app: RunningApp, aid: str, note: str = "") -> None:
    page.add_init_script(RECORD_ANNOUNCEMENTS)
    open_app(page, app, f"/applications/{aid}?tab=tailoring&step=3")
    if note:
        page.locator("#cv-note").fill(note)
    press_by_keyboard(page, page.locator("#cv-generate-start"))
    expect(page.locator('#cv-step[data-state="draft"]')).to_be_visible(timeout=20_000)


def edit_first(page: Page, text: str) -> None:
    line = page.locator("li[data-unit-id]").first
    press_by_keyboard(page, line.locator("details[data-line-edit] > summary"))
    box = line.get_by_label("Edit this line", exact=True)
    tab_until(page, box)
    page.keyboard.press("ControlOrMeta+A")
    page.keyboard.type(text)


def test_ac_m6_1_flagged_line_blocks_approval_until_resolved(running_app, browser: Browser):
    aid = seeded_cv(running_app)
    with new_page(browser, DESKTOP) as page:
        open_cv(page, running_app, aid, "fake-tailor-invented")
        expect(page.locator("#cv-approve")).to_have_attribute("aria-disabled", "true")
        expect(page.locator("[data-flags]")).to_contain_text("Check this number")
        press_by_keyboard(page, page.get_by_role("button", name="This is true — save it as a fact"))
        wait_said(page, "Saved as a fact. The line now cites it.")
        expect(page.locator("#cv-approve")).not_to_have_attribute("aria-disabled")
        press_by_keyboard(page, page.locator("#cv-approve"))
        expect(page.locator("#cv-approved")).to_be_visible()
        expect(page.locator("#cv-heading")).to_be_focused()
        assert_no_serious_violations(page)


def test_ac_m6_4_typing_new_claim_shows_save_as_fact_by_keyboard(running_app, browser):
    aid = seeded_cv(running_app)
    with new_page(browser, DESKTOP) as page:
        open_cv(page, running_app, aid)
        edit_first(page, NEW_CLAIM)
        press_by_keyboard(page, page.locator("[data-line-form=edit]").first.get_by_role("button"))
        dialog = page.locator("#cv-save-fact")
        expect(dialog).to_be_visible()
        expect(dialog.get_by_role("button", name="Not now")).to_be_focused()
        page.keyboard.press("Escape")
        expect(dialog).to_have_count(0)
        expect(page.locator("#cv-approve")).to_have_attribute("aria-disabled", "true")
        press_by_keyboard(page, page.get_by_role("button", name="This is true — save it as a fact"))
        wait_said(page, "Saved as a fact. The line now cites it.")
        expect(page.locator("#cv-approve")).not_to_have_attribute("aria-disabled")


def test_diff_line_facts_reachable_by_keyboard(running_app, browser):
    aid = seeded_cv(running_app)
    with new_page(browser, DESKTOP) as page:
        open_cv(page, running_app, aid)
        disclosure = page.locator("details[data-line-facts]").first
        page.locator("li[data-unit-id]").first.hover()
        expect(disclosure.get_by_role("link").first).to_be_visible()
        page.mouse.move(0, 0)
        press_by_keyboard(page, disclosure.locator("summary"))
        link = disclosure.get_by_role("link").first
        tab_until(page, link)
        expect(link).to_be_focused()
        assert str(link.get_attribute("href")).startswith("/facts/")


def test_regenerate_while_editing_keeps_typing(running_app, browser):
    aid = seeded_cv(running_app)
    with new_page(browser, DESKTOP) as page:
        open_cv(page, running_app, aid)
        edit_first(page, TYPED)
        box = page.locator("[data-line-form=edit] textarea").first
        handle = box.element_handle()
        page.evaluate("document.body.dispatchEvent(new CustomEvent('tasks:updated'))")
        expect(page.locator("#cv-stale-note")).to_be_visible()
        expect(box).to_have_value(TYPED)
        assert handle and handle.evaluate("el => el.isConnected")
        # Leaving and revisiting a step keeps typing in memory, with its original version.
        page.locator("#tailoring-step-2").click()
        expect(page.locator('#tailoring[data-step="2"]')).to_be_visible()
        page.locator("#tailoring-step-3").click()
        expect(page.locator("[data-line-form=edit] textarea").first).to_have_value(TYPED)
        expect(page.locator("#cv-draft-note")).to_be_visible()


def test_ac_m6_3_regenerate_with_note_keeps_edit_and_replace_everything_confirms(
    running_app, browser
):
    aid = seeded_cv(running_app)
    with new_page(browser, DESKTOP) as page:
        open_cv(page, running_app, aid)
        original = page.locator("[data-line-text]").first.inner_text()
        edit_first(page, original + " ")
        press_by_keyboard(page, page.locator("[data-line-form=edit]").first.get_by_role("button"))
        wait_said(page, "Line saved.")
        page.locator("#cv-note").fill("Keep it short.")
        press_by_keyboard(page, page.locator("#cv-generate-start"))
        expect(page.locator('#cv-step[data-version-number="3"]')).to_be_visible(timeout=20_000)
        expect(page.locator('[data-origin="user"]')).to_have_count(1)
        page.locator("#cv-replace-all").check()
        press_by_keyboard(page, page.locator("#cv-generate-start"))
        expect(page.locator("#cv-confirm-replace")).to_be_focused()
        expect(page.locator("#cv-confirm-replace")).to_have_attribute("aria-invalid", "true")
        page.keyboard.press("Space")
        press_by_keyboard(page, page.locator("#cv-generate-start"))
        expect(page.locator('#cv-step[data-version-number="4"]')).to_be_visible(timeout=20_000)
        expect(page.locator('[data-origin="user"]')).to_have_count(0)


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone-390x844"])
def test_cv_step_phone_390x844_and_axe(running_app, browser, viewport, scheme):
    aid = seeded_cv(running_app)
    with new_page(browser, viewport, color_scheme=scheme) as page:
        open_cv(page, running_app, aid, "fake-tailor-invented")
        edit_first(page, NEW_CLAIM)
        wait_for_settled_styles(page)
        assert has_no_horizontal_scroll(page)[0]
        assert_no_serious_violations(page)


def test_cv_delayed_refresh_cannot_replace_an_editor_opened_in_flight(running_app, browser):
    aid = seeded_cv(running_app)
    with new_page(browser, DESKTOP) as page:
        open_cv(page, running_app, aid)
        held = []
        path = f"**/applications/{aid}/tailoring/3"

        def hold(route):
            held.append((route, route.fetch()))

        page.route(path, hold)
        page.locator("h1").evaluate("el => { el.tabIndex = -1; el.focus(); }")
        page.evaluate("document.body.dispatchEvent(new CustomEvent('tasks:updated'))")
        page.wait_for_function(
            "() => document.querySelector('#tailoring').classList.contains('htmx-request')"
        )
        # Wait on the intercepted real response, without a fixed sleep.
        wait_until(lambda: page.evaluate("true") and held, timeout=5, message="held CV refresh")
        edit_first(page, TYPED)
        handle = page.locator("[data-line-form=edit] textarea").first.element_handle()
        route, response = held.pop()
        route.fulfill(response=response)
        expect(page.locator("#cv-stale-note")).to_be_visible()
        assert handle and handle.evaluate("el => el.isConnected")
        expect(page.locator("[data-line-form=edit] textarea").first).to_have_value(TYPED)


def test_cv_409_keeps_typing_and_its_original_version(running_app, browser):
    aid = seeded_cv(running_app)
    with new_page(browser, DESKTOP) as page:
        open_cv(page, running_app, aid)
        old = page.locator("#cv-step").get_attribute("data-version-id")
        edit_first(page, TYPED)
        # A separate tab writes first, through the real protected form.
        other = page.context.new_page()
        open_app(other, running_app, f"/applications/{aid}?tab=tailoring&step=3")
        other.locator("li[data-unit-id]").last.locator("[data-line-form=remove] button").click()
        expect(other.locator("#cv-step")).not_to_have_attribute("data-version-id", old)
        other.close()
        press_by_keyboard(page, page.locator("[data-line-form=edit]").first.get_by_role("button"))
        expect(page.locator("#cv-conflict")).to_be_focused()
        expect(page.locator("#cv-kept-text")).to_have_value(TYPED)
        expect(page.locator("[data-line-form=edit] textarea").first).to_have_value(TYPED)
        expect(page.locator("[data-line-form=edit] input[name=version]").first).to_have_value(old)


def test_cv_typing_during_successful_save_is_recovered(running_app, browser):
    aid = seeded_cv(running_app)
    with new_page(browser, DESKTOP) as page:
        open_cv(page, running_app, aid)
        original = page.locator("[data-line-text]").first.inner_text()
        edit_first(page, original + " ")
        held = []

        def hold(route):
            held.append((route, route.fetch()))

        page.route("**/tailoring/3/unit/*/edit", hold)
        button = page.locator("[data-line-form=edit]").first.get_by_role("button")
        button.click()
        wait_until(lambda: page.evaluate("true") and held, timeout=5, message="held CV save")
        box = page.locator("[data-line-form=edit] textarea").first
        box.fill(TYPED)
        route, response = held.pop()
        route.fulfill(response=response)
        expect(page.locator('#cv-step[data-version-number="2"]')).to_be_visible()
        recovered = page.locator("textarea[data-recovered-key]")
        expect(recovered).to_have_value(TYPED)
        expect(recovered).to_have_attribute("readonly", "")


def test_cv_pending_save_revert_to_original_text_is_kept(running_app, browser):
    aid = seeded_cv(running_app)
    with new_page(browser, DESKTOP) as page:
        open_cv(page, running_app, aid)
        original = page.locator("[data-line-form=edit] textarea").first.input_value()
        edit_first(page, original + " again.")
        held = []
        page.route("**/tailoring/3/unit/*/edit", lambda route: held.append((route, route.fetch())))
        page.locator("[data-line-form=edit]").first.get_by_role("button").click()
        wait_until(lambda: page.evaluate("true") and held, timeout=5, message="held saved CV")
        page.locator("[data-line-form=edit] textarea").first.fill(original)
        route, response = held.pop()
        route.fulfill(response=response)
        expect(page.locator('#cv-step[data-version-number="2"]')).to_be_visible()
        expect(page.locator("textarea[data-recovered-key]")).to_have_value(original)


def test_cv_unrelated_task_refresh_does_not_repeat_completion(running_app, browser):
    aid = seeded_cv(running_app)
    with new_page(browser, DESKTOP) as page:
        open_cv(page, running_app, aid)
        wait_said(page, "CV ready to review.")
        key = page.locator("#tailoring").get_attribute("data-cv-task-key")
        for _ in range(2):
            page.locator("h1").evaluate("el => { el.tabIndex = -1; el.focus(); }")
            old = page.locator("#tailoring").element_handle()
            with page.expect_response(
                lambda r: r.url.endswith(f"/applications/{aid}/tailoring/3")
            ) as response:
                page.evaluate("document.body.dispatchEvent(new CustomEvent('tasks:updated'))")
            assert response.value.request.headers.get("x-jp-cv-seen") == key
            assert "CV ready to review." not in response.value.headers.get("hx-trigger", "")
            page.wait_for_function(
                "old => !old.isConnected && "
                "!document.querySelector('#tailoring').classList.contains('htmx-settling')",
                arg=old,
            )


def test_cv_refused_save_keeps_reverted_typing(running_app, browser):
    aid = seeded_cv(running_app)
    with new_page(browser, DESKTOP) as page:
        open_cv(page, running_app, aid)
        original = page.locator("[data-line-form=edit] textarea").first.input_value()
        edit_first(page, "x" * 401)
        held = []
        page.route("**/tailoring/3/unit/*/edit", lambda route: held.append((route, route.fetch())))
        page.locator("[data-line-form=edit]").first.get_by_role("button").click()
        wait_until(lambda: page.evaluate("true") and held, timeout=5, message="held refused CV")
        page.locator("[data-line-form=edit] textarea").first.fill(original)
        route, response = held.pop()
        assert response.status == 422
        route.fulfill(response=response)
        expect(page.locator("#cv-field-error")).to_be_visible()
        expect(page.locator("[data-line-form=edit] textarea").first).to_have_value(original)


def test_cv_parent_refresh_preserves_a_pending_template_action(running_app, browser):
    aid = seeded_in_portugal(running_app)
    with new_page(browser, DESKTOP) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        open_app(page, running_app, f"/applications/{aid}?tab=tailoring&step=3")
        held = []
        page.route("**/settings/cv-templates/PT/draft*", lambda r: held.append((r, r.fetch())))
        page.locator("#template-draft-start").click()
        wait_until(lambda: page.evaluate("true") and held, timeout=5, message="held template start")
        page.locator("h1").evaluate("el => { el.tabIndex = -1; el.focus(); }")
        try:
            page.evaluate("document.body.dispatchEvent(new CustomEvent('tasks:updated'))")
            expect(page.locator("#cv-stale-note")).to_be_visible()
        finally:
            route, response = held.pop()
            route.fulfill(response=response)
        wait_said(page, "Template draft started.")
        expect(page.locator("#cv-template-heading")).to_be_focused()
