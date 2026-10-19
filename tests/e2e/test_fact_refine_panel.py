"""Refine with AI on the fact editor, in a real browser (M3.9; AC-M3.5 UI; spec 3.1 item 5 as
updated on 2026-10-19; ADR-086, ADR-097 and its update).

The app starts with a completed setup and the fake provider saved, so a refine turn is
answered from the fake's fictional scripts (``app/modules/ai/providers/fake_scripts.py``):
a fact with no keyword gets a question and a pure rephrase; a fact whose wording holds
``fake-refine-invented`` gets inflated numbers and an invented employer, which the honesty
check flags; an answer holding ``fake-refine-uses-answer`` gets a suggestion that uses the
number the answer gives. Facts are seeded through the editor page with the mouse; the keyboard
tests go on with Tab, typing, Space and Enter only. The panel's contract (ids, names, copy)
is in ``tests/refine_panel.py``. Everything is fictional and nothing leaves loopback.
"""

import re

import pytest
from playwright.sync_api import Browser, Locator, Page, Request, expect

from tests.cv_import_harness import message
from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_facts_page import (
    LONG,
    expect_focus_inside,
    facts_page,
    named_button,
    press_by_keyboard,
    tab_until,
    wait_said,
)
from tests.e2e.test_settings_usage_budgets import replace_text
from tests.e2e.test_shell import assert_target_size, has_no_horizontal_scroll, open_app
from tests.facts_page import (
    EDITOR_ID,
    NEW_URL,
    PRIVACY_FIELD,
    SAID_ADDED,
    SAID_SAVED,
    SAVE,
    version_id_attr,
)
from tests.refine_harness import TARGET
from tests.refine_panel import (
    ACCEPT,
    ACKNOWLEDGED_FIELD,
    ANSWER_1200,
    ANSWER_FIELD,
    CANCEL_ID,
    DIFF_ADDED,
    DIFF_REMOVED,
    EDIT_ID,
    EDIT_MYSELF,
    EDITOR_WORDING_ID,
    FLAGGED_ID,
    INVENTED,
    INVENTED_FACT,
    INVENTED_NAME,
    INVENTED_NUMBERS,
    NOT_IN_BANK,
    OK_SUGGESTION,
    PANEL_ID,
    PRACTICE_BADGE,
    QUESTION,
    QUESTIONS_ID,
    REFINE_BUTTON,
    REFINED_SOURCE,
    SAID_ACCEPTED,
    SAID_ANSWERED,
    SAID_READY,
    SAID_STARTED,
    SEND_ANSWER,
    SLOW_FACT,
    STATUS_ID,
    SUGGESTION_ID,
    TRUE_ADD,
    USES_ANSWER,
    WAS_FIELD,
    WORDING_FIELD,
)
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e

EDIT_PAGE = re.compile(r"/facts/[^/?#]+/edit$")
NEVER_USE_FACT = "Handled the confidential Brightwater Trust audit."
# Each id that describes a control: its words and whether it is in sight.
DESCRIPTION = """(control) => {
  const ids = (control.getAttribute('aria-describedby') || '').split(/\\s+/).filter(Boolean);
  return ids.map((id) => {
    const element = document.getElementById(id);
    if (element === null) return { text: '', shown: false };
    const box = element.getBoundingClientRect();
    return {
      text: element.textContent.replace(/\\s+/g, ' ').trim(),
      shown: box.width > 0 && box.height > 0
        && getComputedStyle(element).visibility === 'visible',
    };
  });
}"""
# A round trip after a key press: a request the press made would have been sent before it.
ROUND_TRIP = "() => fetch('/health').then((answer) => answer.status)"


# --- helpers -----------------------------------------------------------------------------------


def panel(page: Page) -> Locator:
    return page.locator(f"#{PANEL_ID}")


def seed_fact(page: Page, app: RunningApp, text: str, *, privacy: str = "normal") -> str:
    """Seed (mouse): add a fact on the editor page and reload its own page. Returns its path."""
    open_app(page, app, NEW_URL)
    form = page.locator(f"form#{EDITOR_ID}")
    form.locator('textarea[name="text"]').fill(text)
    if privacy != "normal":
        form.locator(f'input[name="{PRIVACY_FIELD}"][value="{privacy}"]').check()
    named_button(form, SAVE).click()
    wait_said(page, SAID_ADDED)
    expect(page).to_have_url(EDIT_PAGE)
    path = page.url.removeprefix(app.base_url)
    open_app(page, app, path)
    expect(panel(page)).to_be_visible()
    return path


def record_posts(page: Page) -> list[Request]:
    posts: list[Request] = []
    page.on("request", lambda request: posts.append(request) if request.method == "POST" else None)
    return posts


def refine_posts(posts: list[Request], action: str = "") -> list[str]:
    ending = "/refine" + (f"/{action}" if action else "")
    return [request.url for request in posts if request.url.endswith(ending)]


def wording(page: Page) -> Locator:
    """The suggested wording in the Accept form, which the user may edit."""
    return panel(page).locator(f'textarea[name="{WORDING_FIELD}"]')


def claim_boxes(page: Page) -> Locator:
    return panel(page).locator(f'input[type="checkbox"][name="{ACKNOWLEDGED_FIELD}"]')


def accept_button(page: Page) -> Locator:
    return named_button(panel(page), ACCEPT)


def expect_accept_blocked(page: Page) -> None:
    accept = accept_button(page)
    expect(accept).to_have_attribute("aria-disabled", "true")
    assert accept.get_attribute("disabled") is None, "a blocked Accept stays focusable"


def expect_accept_open(page: Page) -> None:
    expect(accept_button(page)).not_to_have_attribute("aria-disabled", "true")


def wait_answered(page: Page, words: str) -> None:
    """The panel shows a suggestion holding ``words`` (the turn ran in the background)."""
    expect(panel(page).locator(f"#{SUGGESTION_ID}")).to_contain_text(words, timeout=LONG)


def runs(page: Page, tag: str) -> list[str]:
    found: list[str] = (
        panel(page)
        .locator(f"#{SUGGESTION_ID} {tag}")
        .evaluate_all("(runs) => runs.map((run) => run.textContent.replace(/\\s+/g, ' ').trim())")
    )
    return found


def said_list(page: Page) -> list[str]:
    found: list[str] = page.evaluate("() => window.__said || []")
    return found


def assert_fits(page: Page, state: str) -> None:
    fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
    assert fits, f"{state}: scrollWidth {scroll_width} > {inner_width}"


def press_blocked_by_keyboard(page: Page, control: Locator, posts: list[Request]) -> None:
    """Enter and Space on a blocked control, and a forced click: nothing is sent."""
    tab_until(page, control)
    page.keyboard.press("Enter")
    page.keyboard.press("Space")
    expect(control).to_be_focused()
    control.click(force=True)
    assert page.evaluate(ROUND_TRIP) == 200


# --- AC-M3.5 -----------------------------------------------------------------------------------


def test_ac_m3_5_flagged_suggestion_cannot_be_accepted_until_acknowledged(
    running_app: RunningApp, browser: Browser
) -> None:
    """A fake answer with invented claims: each is shown under "This isn't in your fact
    bank." with its own "This is true; add it" checkbox. Accept is aria-disabled and sends
    nothing until every claim is ticked (or edited out of the wording); unticking one blocks
    it again. Until Accept, the fact is untouched. Then Accept saves version 2."""
    with facts_page(browser, DESKTOP) as page:
        seed_fact(page, running_app, INVENTED_FACT)
        posts = record_posts(page)

        press_by_keyboard(page, named_button(panel(page), REFINE_BUTTON))
        flagged = panel(page).locator(f"#{FLAGGED_ID}")
        expect(flagged).to_be_visible(timeout=LONG)
        expect(flagged.get_by_text(NOT_IN_BANK, exact=True)).to_be_visible()
        expect(panel(page).get_by_text(PRACTICE_BADGE, exact=True)).to_be_visible()

        boxes = claim_boxes(page)
        claims = [boxes.nth(i).get_attribute("value") or "" for i in range(boxes.count())]
        assert len(claims) >= 3, claims
        assert {claim.replace(" ", "") for claim in claims} >= INVENTED_NUMBERS, claims
        assert any(INVENTED_NAME in claim for claim in claims), claims
        expect(
            panel(page).get_by_role("checkbox", name=re.compile(re.escape(TRUE_ADD)))
        ).to_have_count(len(claims))
        for claim in claims:
            expect(flagged).to_contain_text(claim)
        expect(wording(page)).to_have_value(INVENTED)

        # Blocked: reachable, says why, and sends nothing by Enter, Space or a click.
        expect_accept_blocked(page)
        described = accept_button(page).evaluate(DESCRIPTION)
        assert any(part["text"] and part["shown"] for part in described), described
        press_blocked_by_keyboard(page, accept_button(page), posts)
        assert refine_posts(posts, "accept") == [], "a blocked Accept sends nothing"
        expect(page.locator(f"#{EDITOR_WORDING_ID}")).to_have_value(INVENTED_FACT)
        expect(page.locator(f"#{version_id_attr(2)}")).to_have_count(0)

        # Edited out: the wording without the claims opens Accept; putting them back blocks it.
        tab_until(page, wording(page))
        replace_text(page, TARGET)
        expect_accept_open(page)
        tab_until(page, wording(page))
        replace_text(page, INVENTED)
        expect_accept_blocked(page)

        # Ticked one by one: blocked until the last; unticking one blocks it again.
        for index in range(len(claims)):
            box = boxes.nth(index)
            tab_until(page, box)
            page.keyboard.press("Space")
            expect(box).to_be_checked()
            if index < len(claims) - 1:
                expect_accept_blocked(page)
        expect_accept_open(page)
        first = boxes.nth(0)
        tab_until(page, first)
        page.keyboard.press("Space")
        expect(first).not_to_be_checked()
        expect_accept_blocked(page)
        page.keyboard.press("Space")
        expect(first).to_be_checked()
        expect_accept_open(page)
        assert refine_posts(posts, "accept") == []

        press_by_keyboard(page, accept_button(page))

        wait_said(page, SAID_ACCEPTED)
        assert len(refine_posts(posts, "accept")) == 1
        expect(page.locator(f"#{EDITOR_WORDING_ID}")).to_have_value(INVENTED)
        version = page.locator(f"#{version_id_attr(2)}")
        expect(version).to_contain_text(REFINED_SOURCE)
        expect(page.get_by_text("Something went wrong")).to_have_count(0)


# --- the conversation by keyboard --------------------------------------------------------------


def test_refine_conversation_by_keyboard_saves_version(
    running_app: RunningApp, browser: Browser
) -> None:
    """Keyboard only: Refine with AI, read the question and the diff, "Edit it myself" takes
    focus to the editor's wording, answer the question, read the new suggestion (it uses the
    number given) and Accept. The fact has a second version from Refine with AI and the editor
    shows the new wording."""
    with facts_page(browser, DESKTOP) as page:
        seed_fact(page, running_app, TARGET)

        press_by_keyboard(page, named_button(panel(page), REFINE_BUTTON))
        expect(panel(page).locator(f"#{QUESTIONS_ID}")).to_contain_text(QUESTION, timeout=LONG)
        wait_answered(page, "Reduced")
        expect_focus_inside(page, panel(page))
        removed, added = runs(page, "del"), runs(page, "ins")
        assert any(r.startswith(DIFF_REMOVED) and "Cut" in r for r in removed), removed
        assert any(a.startswith(DIFF_ADDED) and "Reduced" in a for a in added), added
        expect(panel(page).locator(f"#{FLAGGED_ID}")).to_have_count(0)
        expect_accept_open(page)

        press_by_keyboard(page, panel(page).get_by_text(EDIT_MYSELF, exact=True))
        expect(page.locator(f"#{EDITOR_WORDING_ID}")).to_be_focused()

        answer = panel(page).locator(f'textarea[name="{ANSWER_FIELD}"]')
        tab_until(page, answer)
        page.keyboard.type(ANSWER_1200)
        press_by_keyboard(page, named_button(panel(page), SEND_ANSWER))
        wait_said(page, SAID_ANSWERED)

        wait_answered(page, "1200")
        assert any("1200" in a and a.startswith(DIFF_ADDED) for a in runs(page, "ins"))
        expect(wording(page)).to_have_value(USES_ANSWER)
        expect_accept_open(page)

        press_by_keyboard(page, accept_button(page))

        wait_said(page, SAID_ACCEPTED)
        expect_focus_inside(page, panel(page))
        expect(page.locator(f"#{EDITOR_WORDING_ID}")).to_have_value(USES_ANSWER)
        expect(page.locator(f"#{version_id_attr(2)}")).to_contain_text(REFINED_SOURCE)
        expect(page.get_by_text("Something went wrong")).to_have_count(0)


# --- never use ---------------------------------------------------------------------------------


def test_refine_blocked_for_never_use_fact(running_app: RunningApp, browser: Browser) -> None:
    """A "never use" fact: "Refine with AI" is reachable by keyboard, aria-disabled and
    described by its visible reason; Enter, Space and a click send nothing and no
    conversation opens. Axe finds nothing serious."""
    with facts_page(browser, DESKTOP) as page:
        seed_fact(page, running_app, NEVER_USE_FACT, privacy="never_use")
        posts = record_posts(page)
        start = named_button(panel(page), REFINE_BUTTON)

        expect(start).to_have_attribute("aria-disabled", "true")
        assert start.get_attribute("disabled") is None, "a blocked action stays focusable"
        reason = message("RefineNeverUse")
        described = start.evaluate(DESCRIPTION)
        assert any(reason in part["text"] and part["shown"] for part in described), described

        press_blocked_by_keyboard(page, start, posts)

        assert refine_posts(posts) == [], "nothing is sent for a never-use fact"
        expect(panel(page).locator(f"#{QUESTIONS_ID}")).to_have_count(0)
        expect(panel(page).locator(f"#{SUGGESTION_ID}")).to_have_count(0)
        assert SAID_STARTED not in said_list(page)
        assert_no_serious_violations(page)


# --- phone and axe -----------------------------------------------------------------------------


def test_refine_panel_phone_and_axe(running_app: RunningApp, browser: Browser) -> None:
    """At 390x844, the panel with questions, an answer box, a diff and flagged claims: nothing
    scrolls sideways, its buttons and claim checkboxes' labels are at least 24 by 24 px, and
    axe finds nothing serious, in light and in dark."""
    assert PHONE == {"width": 390, "height": 844}
    with facts_page(browser, PHONE) as page:
        seed_fact(page, running_app, INVENTED_FACT)
        named_button(panel(page), REFINE_BUTTON).click()
        expect(panel(page).locator(f"#{FLAGGED_ID}")).to_be_visible(timeout=LONG)
        expect(panel(page).locator(f'textarea[name="{ANSWER_FIELD}"]')).to_be_visible()
        expect(panel(page).locator(f"#{SUGGESTION_ID} ins").first).to_be_visible()

        assert_fits(page, "flagged suggestion")
        controls = panel(page).locator("button:visible")
        assert controls.count() >= 2
        for index in range(controls.count()):
            control = controls.nth(index)
            assert_target_size(control, control.inner_text().strip()[:30])
        boxes = claim_boxes(page)
        for index in range(boxes.count()):
            box_id = boxes.nth(index).get_attribute("id")
            assert box_id, "each checkbox has an id for its label"
            assert_target_size(page.locator(f'label[for="{box_id}"]'), f"claim {index + 1}")
        box = panel(page).bounding_box()
        assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= PHONE["width"]

        assert_no_serious_violations(page)
        page.emulate_media(color_scheme="dark")
        wait_for_settled_styles(page)
        assert_no_serious_violations(page)
        assert_fits(page, "flagged suggestion, dark")


# --- the live region ---------------------------------------------------------------------------


def test_screen_reader_told_when_suggestion_ready(
    running_app: RunningApp, browser: Browser
) -> None:
    """The turn runs in the background: the live region says the AI is reading the fact, then
    that the suggestion is ready once it is shown (also when the turn ended before the
    start's answer arrived: that answer says both, in that order); and again after an
    answer's turn."""
    with facts_page(browser, DESKTOP) as page:
        seed_fact(page, running_app, TARGET)

        press_by_keyboard(page, named_button(panel(page), REFINE_BUTTON))

        wait_said(page, SAID_READY)
        expect(panel(page).locator(f"#{SUGGESTION_ID}")).to_be_visible()
        said = said_list(page)
        assert SAID_STARTED in said, said
        assert said.index(SAID_STARTED) < len(said) - 1 - said[::-1].index(SAID_READY), said

        ready = said_list(page).count(SAID_READY)
        answer = panel(page).locator(f'textarea[name="{ANSWER_FIELD}"]')
        tab_until(page, answer)
        page.keyboard.type(ANSWER_1200)
        press_by_keyboard(page, named_button(panel(page), SEND_ANSWER))

        wait_said(page, SAID_ANSWERED)
        wait_said(page, SAID_READY, times=ready + 1)
        expect(panel(page).locator(f"#{SUGGESTION_ID}")).to_contain_text("1200", timeout=LONG)


# --- review round: focus while waiting, the editor after an Accept -----------------------------


def test_waiting_refresh_keeps_focus_on_cancel_then_the_status_line(
    running_app: RunningApp, browser: Browser
) -> None:
    """A slow turn (the fake takes SLOW_REFINE_SECONDS): with focus on Cancel, the panel's
    refetch while nothing changed answers 204 and Cancel keeps focus. When the turn ends,
    focus was in the panel, so it goes to the status line, which says the suggestion is
    ready."""
    with facts_page(browser, DESKTOP) as page:
        seed_fact(page, running_app, SLOW_FACT)
        press_by_keyboard(page, named_button(panel(page), REFINE_BUTTON))
        cancel = panel(page).locator(f"#{CANCEL_ID}")
        tab_until(page, cancel)

        # The next refetch (the poll, or a task event) comes while the turn still runs.
        with page.expect_response(
            lambda answer: f"/refine?{WAS_FIELD}=waiting" in answer.url, timeout=LONG
        ) as polled:
            pass
        assert polled.value.status == 204, polled.value.status
        expect(cancel).to_be_focused()

        wait_said(page, SAID_READY)
        expect(page.locator(f"#{STATUS_ID}")).to_be_focused()
        expect(page.locator(f"#{STATUS_ID}")).to_have_text(SAID_READY)


def test_editor_saves_after_an_accept_without_a_reload(
    running_app: RunningApp, browser: Browser
) -> None:
    """After an Accept the editor's wording, version and history are the new ones, so a save
    from the editor right away is not stale: it adds a third version."""
    with facts_page(browser, DESKTOP) as page:
        seed_fact(page, running_app, TARGET)
        named_button(panel(page), REFINE_BUTTON).click()
        wait_answered(page, "Reduced")
        accept_button(page).click()
        wait_said(page, SAID_ACCEPTED)
        editor_wording = page.locator(f"#{EDITOR_WORDING_ID}")
        expect(editor_wording).to_have_value(OK_SUGGESTION)

        editor_wording.fill(f"{OK_SUGGESTION[:-1]} in 2023.")
        named_button(page.locator(f"form#{EDITOR_ID}"), SAVE).click()

        wait_said(page, SAID_SAVED)
        expect(page.locator(f"#{version_id_attr(3)}")).to_have_count(1)
        expect(page.get_by_role("alert")).to_have_count(0)


def test_focus_moved_out_of_the_panel_is_never_taken_back(
    running_app: RunningApp, browser: Browser
) -> None:
    """With focus on Cancel, a refetch that changes nothing (204) swaps nothing. "Edit it
    myself" then takes focus to the editor; neither a save of the editor (a swap elsewhere,
    inside the poll's 3 s) nor the turn ending moves it back to the panel, and what is typed
    stays."""
    with facts_page(browser, DESKTOP) as page:
        seed_fact(page, running_app, SLOW_FACT)
        press_by_keyboard(page, named_button(panel(page), REFINE_BUTTON))
        tab_until(page, panel(page).locator(f"#{CANCEL_ID}"))
        with page.expect_response(
            lambda answer: f"/refine?{WAS_FIELD}=waiting" in answer.url, timeout=LONG
        ) as polled:
            pass
        assert polled.value.status == 204, polled.value.status

        press_by_keyboard(page, panel(page).locator(f"#{EDIT_ID}"))
        editor_wording = page.locator(f"#{EDITOR_WORDING_ID}")
        expect(editor_wording).to_be_focused()
        page.keyboard.type(" Typed")
        named_button(page.locator(f"form#{EDITOR_ID}"), SAVE).click()
        wait_said(page, SAID_SAVED)
        expect(page.locator(f"#{STATUS_ID}")).not_to_be_focused()

        editor_wording = page.locator(f"#{EDITOR_WORDING_ID}")
        editor_wording.focus()
        page.keyboard.type(" again")
        wait_said(page, SAID_READY)
        expect(editor_wording).to_be_focused()
        # What was typed before and after the save is all there (wherever the caret was).
        expect(editor_wording).to_have_value(
            re.compile(r"(?=[\s\S]*Typed)(?=[\s\S]*again)(?=[\s\S]*warehouses)")
        )
