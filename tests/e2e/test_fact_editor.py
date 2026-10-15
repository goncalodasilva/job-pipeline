"""The fact editor, the history and the merge screen in a real browser (M3.6; AC-M3.2, AC-M3.3).

The app starts with a completed setup and the fake provider saved, so a pasted CV gives the
four fictional "Alex Example" drafts (``tests/e2e/test_facts_page.py``), all labelled Practice
data. Facts are seeded through the page itself; the keyboard tests use the mouse only to seed,
and from there every step is Tab, typing, the arrow keys on a radio group and Enter. Names,
ids and copy come from ``tests/facts_page.py``. Everything is fictional and nothing leaves
loopback.
"""

import re
from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_facts_page import (
    AXE_CASES,
    DEGREE,
    ENGLISH,
    LONG,
    REWORDED,
    ROUTES,
    draft,
    edit_control,
    expect_draft_count,
    expect_focus_inside,
    facts_page,
    import_drafts,
    library_fact,
    named_button,
    press_by_keyboard,
    tab_until,
    wait_said,
)
from tests.e2e.test_settings_usage_budgets import replace_text
from tests.e2e.test_setup_provider import SMALL_PHONE
from tests.e2e.test_shell import assert_target_size, has_no_horizontal_scroll, open_app
from tests.facts_page import (
    ADD_FACT,
    ADD_STORY,
    ARCHIVE,
    BADGE_NEVER_USE,
    COMPARE,
    CONFIRM,
    DIFF_ADDED,
    DIFF_END_ADDED,
    DIFF_END_REMOVED,
    DIFF_REMOVED,
    DISCARD,
    EDIT_DETAILS,
    EDIT_HEADINGS,
    EDITOR_DRAFT_ID,
    EDITOR_ID,
    EDITOR_STATE_STATUS_ID,
    EDITOR_STATUS_ID,
    END_BEFORE_START,
    HISTORY,
    HISTORY_ID,
    MERGE,
    MERGE_CANDIDATES_HEADING_ID,
    MERGE_CANDIDATES_ID,
    MERGE_CHOICE_FIELD,
    MERGE_COMPARE_ID,
    MERGE_DRAFT_ID,
    MERGE_FORM_ID,
    MERGE_HEADING,
    MERGE_RESULT_ID,
    MERGE_SEARCH_ID,
    MERGE_TARGET_ID,
    MERGE_WITH,
    NEW_HEADINGS,
    NEW_URL,
    NO_MATCH,
    PRACTICE_BADGE,
    PRIVACY_FIELD,
    PRIVACY_HELP,
    QUOTE_LABEL,
    RESTORE,
    RESTORE_DRAFT,
    RESULTS_ID,
    SAID_ADDED,
    SAID_ARCHIVED,
    SAID_CONFIRMED,
    SAID_DISCARDED,
    SAID_DRAFT_SAVED,
    SAID_EDITED,
    SAID_MERGE_UNDONE,
    SAID_MERGED,
    SAID_RESTORED,
    SAID_SAVED,
    SAID_STORY_ADDED,
    SAID_STORY_SAVED,
    SAID_UNARCHIVED,
    SAVE,
    SAVE_CONFIRM,
    SEARCH,
    TITLE_SUFFIX,
    UNDO_MERGE,
    VERSION_SOURCES,
    new_url,
    tab_url,
    version_heading,
    version_id_attr,
)
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e

SITUATION = "Invoices took nine days to process."
ACTION = "Mapped the steps and removed two approvals."
RESULT = "Processing fell to six days."
EMPLOYER = "Sample Logistics SA"
TITLE = "Junior Planner"
SETTLED = "Fluent English, used daily with suppliers."
EDIT_PAGE = re.compile(r"/facts/[^/?#]+/edit$")
HISTORY_PAGE = re.compile(rf"/facts/[^/?#]+/edit#{HISTORY_ID}$")
MERGE_PAGE = re.compile(r"/facts/[^/?#]+/merge")

# Whether the word that says what a change is can be heard: nothing between it and the page
# hides it from assistive technology.
HEARD = """(el) => {
  for (let node = el.firstElementChild || el; node && node !== el.parentElement;
       node = node.parentElement) {
    const style = getComputedStyle(node);
    if (style.display === "none" || style.visibility === "hidden") return false;
    if (node.getAttribute("aria-hidden") === "true") return false;
  }
  return true;
}"""
# Whether a change is told apart by more than its colour: its word is in sight, or the run
# is underlined or struck through.
SEEN_WITHOUT_COLOUR = """([el, word]) => {
  const line = getComputedStyle(el).textDecorationLine || "";
  return el.innerText.toLowerCase().includes(word) || line !== "none";
}"""


# --- helpers -----------------------------------------------------------------------------------


def editor(page: Page) -> Locator:
    return page.locator(f"form#{EDITOR_ID}")


def field(form: Locator, name: str) -> Locator:
    return form.locator(f'[name="{name}"]')


def heading(page: Page, text: str) -> Locator:
    return page.get_by_role("heading", level=1, name=text, exact=True)


def link(scope: Locator | Page, text: str) -> Locator:
    """A link whose words hold ``text`` (its accessible name may also say which fact)."""
    return scope.locator("a").filter(has_text=text)


def type_into(page: Page, control: Locator, words: str) -> None:
    tab_until(page, control)
    replace_text(page, words)


def choose_radio_by_keyboard(page: Page, form: Locator, name: str, value: str) -> None:
    """Tab to the group (its checked radio) and move with the arrow keys."""
    radios = form.locator(f'input[type="radio"][name="{name}"]')
    wanted = form.locator(f'input[type="radio"][name="{name}"][value="{value}"]')
    tab_until(page, form.locator(f'input[type="radio"][name="{name}"]:checked'))
    for _ in range(radios.count()):
        if wanted.is_checked():
            break
        page.keyboard.press("ArrowDown")
    expect(wanted).to_be_checked()
    expect(wanted).to_be_focused()


def expect_tied_message(control: Locator, message: str | None = None) -> None:
    """The control is marked invalid and described by a message that is in sight."""
    expect(control).to_have_attribute("aria-invalid", "true")
    shown: list[str] = control.evaluate(
        "(el) => (el.getAttribute('aria-describedby') || '').split(' ')"
        ".map((id) => document.getElementById(id))"
        ".filter((note) => note && note.offsetParent !== null)"
        ".map((note) => note.textContent.trim()).filter(Boolean)"
    )
    assert shown, "a visible message is tied to the field"
    if message is not None:
        assert message in shown, shown


def reword_and_confirm(page: Page, text: str, reworded: str) -> None:
    """Seed (mouse): a draft's inline form gives the fact a second version."""
    block = draft(page, text)
    edit_control(block).click()
    wording = block.locator('textarea[name="text"]')
    expect(wording).to_be_visible()
    wording.fill(reworded)
    named_button(block, SAVE_CONFIRM).click()
    wait_said(page, SAID_EDITED)


def boxes(page: Page) -> tuple[dict[str, float], dict[str, float]]:
    ours = page.locator(f"#{MERGE_DRAFT_ID}").bounding_box()
    theirs = page.locator(f"#{MERGE_TARGET_ID}").bounding_box()
    assert ours is not None and theirs is not None
    return ours, theirs


def assert_side_by_side(page: Page) -> None:
    first, second = sorted(boxes(page), key=lambda box: box["x"])
    assert first["x"] + first["width"] <= second["x"] + 1, "the two wordings sit side by side"
    assert (
        first["y"] < second["y"] + second["height"] and second["y"] < first["y"] + first["height"]
    )


def assert_stacked(page: Page) -> None:
    first, second = sorted(boxes(page), key=lambda box: box["y"])
    assert first["y"] + first["height"] <= second["y"] + 1, "on a phone one is under the other"


def candidate(page: Page, text: str) -> Locator:
    rows = page.locator(f"#{MERGE_CANDIDATES_ID} [id^='merge-candidate-']")
    return rows.filter(has_text=text)


# --- the editor --------------------------------------------------------------------------------


def test_add_story_by_keyboard(running_app: RunningApp, browser: Browser) -> None:
    """Keyboard only, from the Library: open "Add a story", get an empty one refused with the
    first part marked and focused, write the parts, a new role, skills, tags, dates and a
    privacy choice, and save. The story is confirmed and in the Library at once."""
    with facts_page(browser, DESKTOP) as page:
        open_app(page, running_app, tab_url("library"))
        expect(link(page.locator("main"), ADD_FACT)).to_have_count(1)
        press_by_keyboard(page, link(page.locator("main"), ADD_STORY))
        expect(heading(page, NEW_HEADINGS["story"])).to_be_visible()
        expect(page).to_have_url(re.compile(re.escape(new_url("story")) + "$"))
        page.wait_for_function("() => typeof window.htmx !== 'undefined'")
        page.evaluate("() => { window.__stayed = true; }")
        form = editor(page)
        expect(field(form, "text")).to_have_count(0)

        # Nothing written yet: refused in place, the first part marked and focused.
        press_by_keyboard(page, named_button(form, SAVE))
        situation = field(form, "situation")
        expect_tied_message(situation)
        expect(situation).to_be_focused()

        page.keyboard.type(SITUATION)
        typed = {
            "action": ACTION,
            "result": RESULT,
            "new_role_employer": EMPLOYER,
            "new_role_title": TITLE,
            "new_skills": "Process mapping, Excel",
            "tags": "finance",
            "start_date": "2023-04",
            "end_date": "2022",
        }
        for name, words in typed.items():
            type_into(page, field(form, name), words)
        choose_radio_by_keyboard(page, form, PRIVACY_FIELD, "never_use")
        expect(form.get_by_text(PRIVACY_HELP["never_use"], exact=True)).to_be_visible()

        # The end is before the start: refused on that field, and everything typed is kept.
        press_by_keyboard(page, named_button(form, SAVE))
        end_date = field(form, "end_date")
        expect_tied_message(end_date, END_BEFORE_START)
        expect(end_date).to_be_focused()
        expect(field(form, "situation")).to_have_value(SITUATION)
        expect(field(form, "new_role_title")).to_have_value(TITLE)
        expect(form.locator(f'input[name="{PRIVACY_FIELD}"][value="never_use"]')).to_be_checked()
        replace_text(page, "2023-09")

        press_by_keyboard(page, named_button(form, SAVE))

        wait_said(page, SAID_STORY_ADDED)
        status = page.locator(f"#{EDITOR_STATUS_ID}")
        expect(status).to_have_text(SAID_STORY_ADDED)
        # htmx applied the answer's <title>: it names the page this has become.
        expect(page).to_have_title(EDIT_HEADINGS["story"] + TITLE_SUFFIX)
        assert page.evaluate("() => window.__stayed === true"), "the save was a swap"
        expect(page).to_have_url(EDIT_PAGE)
        expect(heading(page, EDIT_HEADINGS["story"])).to_be_visible()
        assert page.evaluate(
            "() => !['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON'].includes("
            "document.activeElement.tagName) && document.activeElement !== document.body"
        ), "focus moved to the result, where no key press saves again"
        # The page is the story's own now: its fields hold what was saved and it has a history.
        expect(field(form, "action")).to_have_value(ACTION)
        expect(field(form, "new_role_employer")).to_have_value("")
        role = field(form, "role_id")
        chosen = role.evaluate("(el) => el.options[el.selectedIndex].textContent.trim()")
        assert TITLE in chosen and EMPLOYER in chosen, chosen
        for skill in ("Process mapping", "Excel"):
            expect(form.get_by_label(skill, exact=True)).to_be_checked()
        expect(page.locator(f"#{HISTORY_ID}")).to_contain_text(version_heading(1))
        expect(page.locator(f"#{HISTORY_ID}")).to_contain_text(VERSION_SOURCES["manual"])

        # A second save by keyboard changes the wording: one more version.
        type_into(page, field(form, "result"), "Processing fell to four days.")
        press_by_keyboard(page, named_button(form, SAVE))
        wait_said(page, SAID_STORY_SAVED)
        expect(page.locator(f"#{version_id_attr(2)}")).to_be_visible()

        # Confirmed at once: in the Library, with its role, skills and the privacy badge.
        open_app(page, running_app, tab_url("library"))
        story = library_fact(page, ACTION)
        for words in (SITUATION, "four days", TITLE, "Process mapping", BADGE_NEVER_USE):
            expect(story).to_contain_text(words)
        expect(story).not_to_contain_text("Draft")
        expect(page.get_by_text("Something went wrong")).to_have_count(0)


# --- the history -------------------------------------------------------------------------------


def test_history_diff_readable_by_keyboard_and_axe(
    running_app: RunningApp, browser: Browser
) -> None:
    """AC-M3.3: a fact the fake made, then reworded by the user. From the Library, by keyboard,
    its History link opens the versions, newest first; what was removed and added is said in
    words that assistive technology hears and is told apart without colour. Axe is clean in
    light and dark, and nothing scrolls sideways on a phone."""
    with facts_page(browser, DESKTOP) as page:
        import_drafts(page, running_app)
        reword_and_confirm(page, DEGREE, REWORDED)

        open_app(page, running_app, tab_url("library"))
        fact = library_fact(page, REWORDED)
        press_by_keyboard(page, link(fact, HISTORY))

        expect(page).to_have_url(HISTORY_PAGE)
        history = page.locator(f"#{HISTORY_ID}")
        expect(history.get_by_role("heading", level=2, name=HISTORY, exact=True)).to_be_visible()
        expect(history).to_be_in_viewport()
        second = page.locator(f"#{version_id_attr(2)}")
        first = page.locator(f"#{version_id_attr(1)}")
        expect(second.get_by_role("heading", name=version_heading(2), exact=True)).to_be_visible()
        expect(first.get_by_role("heading", name=version_heading(1), exact=True)).to_be_visible()
        above = second.bounding_box()
        below = first.bounding_box()
        assert above is not None and below is not None and above["y"] < below["y"], "newest first"

        expect(first).to_contain_text(DEGREE)
        expect(first.get_by_text(PRACTICE_BADGE, exact=True)).to_be_visible()
        expect(first).to_contain_text(VERSION_SOURCES["cv_import"])
        expect(second).to_contain_text(VERSION_SOURCES["manual"])
        expect(second.get_by_text(PRACTICE_BADGE, exact=True)).to_have_count(0)
        expect(first.locator("ins, del")).to_have_count(0)
        for entry in (first, second):
            expect(entry.locator("time[datetime]")).to_have_count(1)

        removed = second.locator("del")
        added = second.locator("ins")
        expect(removed.filter(has_text="2018")).to_have_count(1)
        for run, word, end in (
            (removed, DIFF_REMOVED, DIFF_END_REMOVED),
            (added, DIFF_ADDED, DIFF_END_ADDED),
        ):
            for index in range(run.count()):
                change = run.nth(index)
                said = (change.text_content() or "").strip().lower()
                assert said.startswith(word), f"a change starts with the word {word!r}: {said!r}"
                assert said.endswith(end), f"a change ends with {end!r}: {said!r}"
                assert said[len(word) : -len(end)].strip(": "), (
                    "never a change that is only spacing"
                )
                assert change.evaluate(HEARD), f"{word!r} is not hidden from assistive technology"
                assert change.evaluate(
                    "(el, word) => (" + SEEN_WITHOUT_COLOUR + ")([el, word])", word
                ), "the change is told apart by more than its colour"

        # The editor above the history belongs to the same fact, and Tab goes on from here.
        expect(field(editor(page), "text")).to_have_value(REWORDED)
        page.keyboard.press("Tab")
        assert page.evaluate("() => document.activeElement !== document.body")

        assert_no_serious_violations(page)
        page.emulate_media(color_scheme="dark")
        assert_no_serious_violations(page)
        page.set_viewport_size(PHONE)
        fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert fits, f"history on a phone: scrollWidth {scroll_width} > {inner_width}"
        expect(second).to_be_visible()


# --- AC-M3.2: merge ----------------------------------------------------------------------------


def test_ac_m3_2_merge_draft_into_fact_by_keyboard(
    running_app: RunningApp, browser: Browser
) -> None:
    """Keyboard only after the seed: "Merge with…" on a draft, search, pick a fact, compare
    the two wordings side by side, write the wording, merge, undo, then merge again taking
    the draft's wording. The fact's history holds every step."""
    with facts_page(browser, DESKTOP) as page:
        import_drafts(page, running_app)
        named_button(draft(page, ROUTES), CONFIRM).click()  # seeding: the fact to merge into
        wait_said(page, SAID_CONFIRMED)
        expect_draft_count(page, 3)

        press_by_keyboard(page, link(draft(page, ENGLISH), MERGE_WITH))
        expect(page).to_have_url(MERGE_PAGE)
        expect(heading(page, MERGE_HEADING)).to_be_visible()
        expect(page.locator(f"#{MERGE_DRAFT_ID}")).to_contain_text(ENGLISH)
        listing = page.locator(f"#{MERGE_CANDIDATES_ID}")
        expect(candidate(page, ROUTES)).to_have_count(1)
        expect(candidate(page, DEGREE)).to_have_count(1)  # another draft can be a target
        expect(candidate(page, ENGLISH)).to_have_count(0)  # never itself

        # Search: type and press Enter.
        search = page.locator(f"#{MERGE_SEARCH_ID}")
        tab_until(page, field(search, "q"))
        page.keyboard.type("xylophone")
        page.keyboard.press("Enter")
        expect(listing).to_contain_text(NO_MATCH)
        type_into(page, field(search, "q"), "delivery routes")
        page.keyboard.press("Enter")
        expect(candidate(page, ROUTES)).to_have_count(1)
        expect(candidate(page, DEGREE)).to_have_count(0)
        expect(named_button(search, SEARCH)).to_be_visible()

        # Pick it: both wordings side by side, each with what made it.
        press_by_keyboard(page, link(candidate(page, ROUTES), COMPARE))
        expect(page.locator(f"#{MERGE_COMPARE_ID}")).to_be_visible()
        ours = page.locator(f"#{MERGE_DRAFT_ID}")
        theirs = page.locator(f"#{MERGE_TARGET_ID}")
        expect(ours).to_contain_text(ENGLISH)
        expect(theirs).to_contain_text(ROUTES)
        expect(ours.get_by_text(PRACTICE_BADGE, exact=True)).to_be_visible()
        expect(theirs.get_by_text(PRACTICE_BADGE, exact=True)).to_be_visible()
        assert_side_by_side(page)
        page.wait_for_function("() => typeof window.htmx !== 'undefined'")
        page.evaluate("() => { window.__stayed = true; }")

        # Write the wording, and merge.
        form = page.locator(f"form#{MERGE_FORM_ID}")
        expect(form.locator(f'input[name="{MERGE_CHOICE_FIELD}"][value="target"]')).to_be_checked()
        choose_radio_by_keyboard(page, form, MERGE_CHOICE_FIELD, "edited")
        wording = field(form, "text")
        expect(wording).to_have_value(ROUTES)
        type_into(page, wording, "")
        press_by_keyboard(page, named_button(form, MERGE))
        expect_tied_message(wording)  # a blank wording is refused in place
        expect(wording).to_be_focused()
        expect(form.locator(f'input[name="{MERGE_CHOICE_FIELD}"][value="edited"]')).to_be_checked()
        page.keyboard.type(SETTLED)
        press_by_keyboard(page, named_button(form, MERGE))

        wait_said(page, SAID_MERGED)
        result = page.locator(f"#{MERGE_RESULT_ID}")
        expect(result).to_contain_text(SETTLED)
        expect_focus_inside(page, result)
        expect(form).to_have_count(0)
        assert page.evaluate("() => window.__stayed === true"), "the merge was a swap"

        # Undo, right away.
        press_by_keyboard(page, named_button(result, UNDO_MERGE))
        wait_said(page, SAID_MERGE_UNDONE)
        expect(named_button(page, UNDO_MERGE)).to_have_count(0)
        open_app(page, running_app, tab_url("drafts"))
        expect_draft_count(page, 3)
        expect(draft(page, ENGLISH)).to_be_visible()
        open_app(page, running_app, tab_url("library"))
        expect(library_fact(page, ROUTES)).not_to_contain_text(SETTLED)

        # Merge again, this time taking the draft's wording.
        open_app(page, running_app, tab_url("drafts"))
        press_by_keyboard(page, link(draft(page, ENGLISH), MERGE_WITH))
        press_by_keyboard(page, link(candidate(page, ROUTES), COMPARE))
        form = page.locator(f"form#{MERGE_FORM_ID}")
        choose_radio_by_keyboard(page, form, MERGE_CHOICE_FIELD, "draft")
        press_by_keyboard(page, named_button(form, MERGE))
        wait_said(page, SAID_MERGED)
        result = page.locator(f"#{MERGE_RESULT_ID}")
        expect(named_button(result, UNDO_MERGE)).to_be_visible()

        # Versioned: the fact's page shows the merge, its undo and the second merge.
        press_by_keyboard(page, result.locator("a[href$='/edit']"))
        expect(page).to_have_url(EDIT_PAGE)
        expect(field(editor(page), "text")).to_have_value(ENGLISH)
        history = page.locator(f"#{HISTORY_ID}")
        for number in (1, 2, 3, 4):
            expect(history.locator(f"#{version_id_attr(number)}")).to_be_visible()
        expect(history.locator(f"#{version_id_attr(4)}")).to_contain_text(VERSION_SOURCES["merge"])
        expect(history.locator(f"#{version_id_attr(3)}")).to_contain_text(
            VERSION_SOURCES["merge_undo"]
        )
        expect(history.locator(f"#{version_id_attr(2)}")).to_contain_text(SETTLED.split()[0])
        open_app(page, running_app, tab_url("drafts"))
        expect_draft_count(page, 2)
        expect(page.get_by_text("Something went wrong")).to_have_count(0)


# --- phone widths and axe ----------------------------------------------------------------------


def editor_states(page: Page, app: RunningApp) -> Iterator[str]:
    """Walk the M3.6 screens through their states, yielding at each one to be checked."""
    open_app(page, app, NEW_URL)
    expect(heading(page, NEW_HEADINGS["fact"])).to_be_visible()
    yield "new fact"
    open_app(page, app, new_url("story"))
    form = editor(page)
    named_button(form, SAVE).click()
    expect(field(form, "situation")).to_have_attribute("aria-invalid", "true")
    yield "refused story"
    field(form, "action").fill(ACTION)
    field(form, "new_skills").fill("Process mapping")
    named_button(form, SAVE).click()
    expect(page.locator(f"#{EDITOR_STATUS_ID}")).to_have_text(SAID_STORY_ADDED)
    expect(page.locator(f"#{version_id_attr(1)}")).to_be_visible()
    yield "saved story with its history"
    named_button(page.locator("main"), ARCHIVE).click()
    wait_said(page, SAID_ARCHIVED)
    expect(named_button(page.locator("main"), RESTORE)).to_be_visible()
    yield "archived, with restore"

    import_drafts(page, app)
    reword_and_confirm(page, DEGREE, REWORDED)
    open_app(page, app, tab_url("library"))
    link(library_fact(page, REWORDED), HISTORY).click()
    expect(page.locator(f"#{version_id_attr(2)} del")).to_be_visible()
    yield "edit page with a diff"

    open_app(page, app, tab_url("drafts"))
    expect(link(draft(page, ENGLISH), MERGE_WITH)).to_be_visible()
    yield "drafts with their links"
    link(draft(page, ENGLISH), MERGE_WITH).click()
    expect(candidate(page, REWORDED)).to_be_visible(timeout=LONG)
    yield "merge search"
    search = page.locator(f"#{MERGE_SEARCH_ID}")
    field(search, "q").fill("xylophone")
    named_button(search, SEARCH).click()
    expect(page.locator(f"#{MERGE_CANDIDATES_ID}")).to_contain_text(NO_MATCH)
    yield "merge search with no match"
    field(page.locator(f"#{MERGE_SEARCH_ID}"), "q").fill("")
    named_button(page.locator(f"#{MERGE_SEARCH_ID}"), SEARCH).click()
    link(candidate(page, REWORDED), COMPARE).click()
    expect(page.locator(f"#{MERGE_COMPARE_ID}")).to_be_visible()
    yield "merge comparison"
    merge_form = page.locator(f"form#{MERGE_FORM_ID}")
    merge_form.locator(f'input[name="{MERGE_CHOICE_FIELD}"][value="edited"]').check()
    field(merge_form, "text").fill("")
    named_button(merge_form, MERGE).click()
    expect(field(merge_form, "text")).to_have_attribute("aria-invalid", "true")
    yield "refused merge"
    field(merge_form, "text").fill(SETTLED)
    named_button(merge_form, MERGE).click()
    expect(named_button(page.locator(f"#{MERGE_RESULT_ID}"), UNDO_MERGE)).to_be_visible()
    yield "merge result with undo"
    open_app(page, app, tab_url("library") + "&status=discarded")
    expect(page.locator(f"#{RESULTS_ID}")).to_contain_text(ENGLISH)
    yield "discarded drafts"
    open_app(page, app, tab_url("library"))
    expect(link(library_fact(page, SETTLED), HISTORY)).to_be_visible()
    yield "library with its links"


STATES = 13


@pytest.mark.parametrize("viewport", [PHONE, SMALL_PHONE], ids=["390x844", "360x740"])
def test_fact_editor_390x844_no_horizontal_scroll(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int]
) -> None:
    """At 390x844 (and at 360 px, the narrowest supported width) no state of the editor, the
    history or the merge screen scrolls sideways, its controls are at least 24 by 24 px, and
    the two wordings of a comparison are stacked."""
    assert PHONE == {"width": 390, "height": 844}
    with facts_page(browser, viewport) as page:
        seen = 0
        for state in editor_states(page, running_app):
            fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
            assert fits, f"{state}: scrollWidth {scroll_width} > {inner_width}"
            controls = page.locator("main").locator("button:visible, summary:visible")
            for index in range(controls.count()):
                control = controls.nth(index)
                assert_target_size(control, f"{state}: {control.inner_text().strip()[:30]!r}")
            if state == "merge comparison":
                assert_stacked(page)
            seen += 1
        assert seen == STATES


@pytest.mark.parametrize(("viewport", "scheme"), AXE_CASES)
def test_axe_fact_editor(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int], scheme: str
) -> None:
    """No serious or critical axe findings in any state of the editor, the history or the
    merge screen, on desktop and phone, in light and dark."""
    with facts_page(browser, viewport, color_scheme=scheme) as page:
        seen = []
        for state in editor_states(page, running_app):
            try:
                assert_no_serious_violations(page)
            except AssertionError as error:
                raise AssertionError(f"{state}: {error}") from None
            seen.append(state)
        assert len(seen) == STATES


def test_phone_restore_a_discarded_draft(running_app: RunningApp, browser: Browser) -> None:
    """TD-195 at 390x844: a draft discarded earlier is found under the Library's Discarded
    filter after leaving the page, and "Restore draft" brings it back."""
    with facts_page(browser, PHONE) as page:
        import_drafts(page, running_app)
        named_button(draft(page, ENGLISH), DISCARD).click()
        wait_said(page, SAID_DISCARDED)
        open_app(page, running_app, tab_url("library") + "&status=discarded")
        gone = library_fact(page, ENGLISH)
        restore = named_button(gone, RESTORE_DRAFT)
        expect(restore).to_be_visible()
        assert_target_size(restore, RESTORE_DRAFT)
        restore.click()
        wait_said(page, SAID_RESTORED)
        open_app(page, running_app, tab_url("drafts"))
        expect_draft_count(page, 4)
        expect(draft(page, ENGLISH)).to_be_visible()
        fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert fits, f"scrollWidth {scroll_width} > {inner_width}"


# --- review fixes ------------------------------------------------------------------------------


def test_draft_in_editor_keeps_its_provenance_and_archive_needs_two_presses(
    running_app: RunningApp, browser: Browser
) -> None:
    """A draft opened in the full editor still says it is a draft, with the fake's label and
    the CV line, and its save line says it still needs confirming. On a confirmed fact,
    Archive moves focus to the line that says so: a second Enter doesn't restore it."""
    with facts_page(browser, DESKTOP) as page:
        import_drafts(page, running_app)
        press_by_keyboard(page, link(draft(page, ROUTES), EDIT_DETAILS))
        expect(page).to_have_url(EDIT_PAGE)
        expect(heading(page, EDIT_HEADINGS["fact"])).to_be_visible()
        provenance = page.locator(f"#{EDITOR_DRAFT_ID}")
        expect(provenance.get_by_text("Draft", exact=True)).to_be_visible()
        expect(provenance.get_by_text(PRACTICE_BADGE, exact=True)).to_be_visible()
        expect(provenance.locator("figcaption")).to_have_text(QUOTE_LABEL)
        expect(provenance.locator("blockquote")).to_be_visible()
        expect(named_button(page.locator("main"), ARCHIVE)).to_have_count(0)
        assert_no_serious_violations(page)

        form = editor(page)
        type_into(page, field(form, "tags"), "logistics")
        press_by_keyboard(page, named_button(form, SAVE))
        wait_said(page, SAID_DRAFT_SAVED)
        expect(page.locator(f"#{EDITOR_STATUS_ID}")).to_have_text(SAID_DRAFT_SAVED)
        expect(page.locator(f"#{EDITOR_DRAFT_ID}").locator("blockquote")).to_be_visible()
        page.set_viewport_size(PHONE)
        fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
        assert fits, f"draft in the editor on a phone: scrollWidth {scroll_width} > {inner_width}"
        page.set_viewport_size(DESKTOP)
        open_app(page, running_app, tab_url("drafts"))
        expect_draft_count(page, 4)  # still a draft

        # A confirmed fact: Archive, then Enter again where focus landed.
        open_app(page, running_app, NEW_URL)
        form = editor(page)
        type_into(page, field(form, "text"), SETTLED)
        press_by_keyboard(page, named_button(form, SAVE))
        wait_said(page, SAID_ADDED)
        expect(page).to_have_title(EDIT_HEADINGS["fact"] + TITLE_SUFFIX)
        press_by_keyboard(page, named_button(page.locator("main"), ARCHIVE))
        wait_said(page, SAID_ARCHIVED)
        status = page.locator(f"#{EDITOR_STATE_STATUS_ID}")
        expect(status).to_be_focused()
        expect(status).to_contain_text(SAID_ARCHIVED)
        page.keyboard.press("Enter")
        restore = named_button(page.locator("main"), RESTORE)
        expect(restore).to_be_visible()
        expect(named_button(page.locator("main"), ARCHIVE)).to_have_count(0)
        # Restore is the next stop, and takes its own press.
        page.keyboard.press("Tab")
        expect(restore).to_be_focused()
        page.keyboard.press("Enter")
        wait_said(page, SAID_UNARCHIVED)
        expect(named_button(page.locator("main"), ARCHIVE)).to_be_visible()
        expect(page.get_by_text("Something went wrong")).to_have_count(0)


def test_merge_search_swaps_moves_focus_and_announces(
    running_app: RunningApp, browser: Browser
) -> None:
    """The merge screen's search by keyboard: the page is not reloaded, the address follows
    the search, focus lands on the candidates' heading and what was found is announced."""
    with facts_page(browser, DESKTOP) as page:
        import_drafts(page, running_app)
        press_by_keyboard(page, link(draft(page, ENGLISH), MERGE_WITH))
        expect(heading(page, MERGE_HEADING)).to_be_visible()
        page.wait_for_function("() => typeof window.htmx !== 'undefined'")
        page.evaluate("() => { window.__stayed = true; }")
        found = page.locator(f"#{MERGE_CANDIDATES_HEADING_ID}")

        search = page.locator(f"#{MERGE_SEARCH_ID}")
        tab_until(page, field(search, "q"))
        page.keyboard.type("delivery routes")
        page.keyboard.press("Enter")
        wait_said(page, "1 fact found.")
        expect(found).to_be_focused()
        expect(candidate(page, ROUTES)).to_have_count(1)
        expect(candidate(page, DEGREE)).to_have_count(0)
        expect(page).to_have_url(re.compile(r"/merge\?q=delivery(\+|%20)routes$"))
        expect(field(page.locator(f"#{MERGE_SEARCH_ID}"), "q")).to_have_value("delivery routes")
        assert page.evaluate("() => window.__stayed === true"), "the search was a swap"
        # The next Tab stop is the first candidate's link.
        page.keyboard.press("Tab")
        expect(link(candidate(page, ROUTES), COMPARE)).to_be_focused()

        type_into(page, field(page.locator(f"#{MERGE_SEARCH_ID}"), "q"), "xylophone")
        page.keyboard.press("Enter")
        wait_said(page, NO_MATCH)
        expect(found).to_be_focused()
        expect(page.locator(f"#{MERGE_CANDIDATES_ID}")).to_contain_text(NO_MATCH)
        assert_no_serious_violations(page)
        # A reload of the address shows the same search, as a full page.
        page.reload()
        expect(field(page.locator(f"#{MERGE_SEARCH_ID}"), "q")).to_have_value("xylophone")
        expect(page.locator(f"#{MERGE_CANDIDATES_ID}")).to_contain_text(NO_MATCH)
        expect(page.get_by_text("Something went wrong")).to_have_count(0)


def test_history_changed_runs_do_not_read_as_one_word(
    running_app: RunningApp, browser: Browser
) -> None:
    """A number replaced by another: the removed and the added run are apart on the screen,
    and the text a reader copies or hears has a space between them."""
    with facts_page(browser, DESKTOP) as page:
        open_app(page, running_app, NEW_URL)
        form = editor(page)
        field(form, "text").fill("Planned 60 delivery routes a week.")
        named_button(form, SAVE).click()
        wait_said(page, SAID_ADDED)
        field(form, "text").fill("Planned 80 delivery routes a week.")
        named_button(form, SAVE).click()
        wait_said(page, SAID_SAVED)
        second = page.locator(f"#{version_id_attr(2)}")
        removed, added = second.locator("del"), second.locator("ins")
        expect(removed).to_have_count(1)
        expect(added).to_have_count(1)
        words = second.locator(".version-words")
        assert "6080" not in (words.inner_text() or "")
        said = " ".join((words.text_content() or "").split())
        assert "60 (end removed) added: 80 (end added)" in said, said
        left, right = removed.bounding_box(), added.bounding_box()
        assert left is not None and right is not None
        assert right["x"] - (left["x"] + left["width"]) >= 2, "a gap the eye can see"
        assert_no_serious_violations(page)
