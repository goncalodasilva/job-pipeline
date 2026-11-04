"""M9.2 part D: the lists editor on ``/settings/lists`` in a real browser (AC-M9.2; ADR-131 items
6-7; ADR-125 item 1; spec 3.9; TD-308).

By keyboard only the user opens a group, adds an industry value (a duplicate is refused in place),
replaces another value's spellings, deletes an unused value, is told a value in use cannot be
deleted (announced, count named, nothing deleted), edits a skill's aliases (an alias that means
another skill is refused), and finds work mode and the removal reasons read only with a link to
"Values to map". At 390x844 and desktop size nothing scrolls sideways, every target is at least
24 by 24 px, and axe finds nothing serious in light and in dark with every group open. The groups
are collapsed ``<details>`` (one Tab stop each), which is how the Lists tab cap stays 110. The
markup is described in ``tests/lists_pages.py``. The app starts with a finished setup; nothing
sleeps. Every value is fictional.
"""

import json
import re

import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from app.modules.storage import Database, skills_repo
from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.settings_areas import (
    AREA_TAB_BUDGET,
    assert_area_within_budget,
    wait_until_loaded,
)
from tests.e2e.test_board import rows
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, wait_said
from tests.e2e.test_settings_appearance import SMALL_TARGETS
from tests.e2e.test_settings_board import tab_to
from tests.e2e.test_shell import assert_target_size, has_no_horizontal_scroll, new_page, open_app
from tests.job_fixtures import generate_jobs, seed_jobs
from tests.lists_pages import (
    GROUPS,
    add_refused,
    added_message,
    aliases_message,
    aliases_refused,
    deleted_message,
    seed_id,
)
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e

SECTION = "#lists-editor"
VIEWPORTS = {"desktop": DESKTOP, "phone": PHONE}
STAMP = "2026-11-04T09:00:00+00:00"
FITS = """(selector) => {
  const box = document.querySelector(selector);
  return box.scrollWidth <= box.clientWidth;
}"""
IS_OPEN = "(element) => element.open"


def seed(app: RunningApp) -> None:
    """Three jobs hold industry agriculture (so it is in use), and two skills exist."""
    with Database.open(app.data_dir / "app.db") as db:
        seed_jobs(db, generate_jobs(3))
        with db.transaction() as conn:
            conn.execute("UPDATE jobs SET industry='agriculture'")
            conn.execute("UPDATE jobs SET user_corrected='[\"industry\"]' WHERE id='job-0001'")
            for skill_id, name, aliases in (
                ("skill-py", "Python", ["py"]),
                ("skill-rs", "Rust", ["rs"]),
            ):
                skills_repo.insert_skill(
                    conn,
                    skill_id=skill_id,
                    name=name,
                    normalized_name=name.lower(),
                    aliases=json.dumps(aliases),
                    now=STAMP,
                )


def value_id(app: RunningApp, field: str, value: str) -> str:
    found = rows(app, "SELECT id FROM controlled_values WHERE field=? AND value=?", (field, value))
    assert len(found) == 1, (field, value)
    return str(found[0][0])


def aliases_in_db(app: RunningApp, field: str, value: str) -> list[str]:
    found = rows(
        app, "SELECT aliases FROM controlled_values WHERE field=? AND value=?", (field, value)
    )
    return list(json.loads(found[0][0])) if found else []


def skill_aliases(app: RunningApp, skill_id: str) -> list[str]:
    return list(json.loads(rows(app, "SELECT aliases FROM skills WHERE id=?", (skill_id,))[0][0]))


def group(page: Page, key: str) -> Locator:
    return page.locator(f"#list-{key}")


def open_group(page: Page, key: str) -> None:
    """Tab to the group's summary and press Enter (keyboard only); it is open afterwards."""
    details = group(page, key)
    if details.evaluate(IS_OPEN):
        return
    tab_to(page, page.locator(f"#list-{key}-summary"))
    page.keyboard.press("Enter")
    expect(details).to_have_attribute("open", "")


def open_editor(page: Page, app: RunningApp) -> None:
    page.add_init_script(RECORD_ANNOUNCEMENTS)
    open_app(page, app, "/settings/lists")
    wait_until_loaded(page, "lists")
    expect(
        page.locator(SECTION).get_by_role("heading", name="Lists", level=2, exact=True)
    ).to_be_visible()


def wait_said_containing(page: Page, text: str) -> None:
    page.wait_for_function(
        "(text) => (window.__said || []).some((said) => said.includes(text))", arg=text
    )


def assert_fits(page: Page, label: str) -> None:
    fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
    assert fits, f"{label}: scrollWidth {scroll_width} > {inner_width}"
    assert page.evaluate(FITS, SECTION), f"{label}: the editor fits its own width"


def assert_targets(page: Page, label: str) -> None:
    small: list[str] = page.evaluate(SMALL_TARGETS)
    assert small == [], f"{label}: targets under 24 px:\n" + "\n".join(small)


def type_lines(page: Page, field: Locator, lines: list[str]) -> None:
    """Replace a textarea's text: select all, then type the lines with Enter between them."""
    tab_to(page, field)
    page.keyboard.press("ControlOrMeta+a")
    for number, line in enumerate(lines):
        if number:
            page.keyboard.press("Enter")
        page.keyboard.type(line)


def scan_with_everything_open(page: Page, label: str) -> None:
    for key in GROUPS:
        open_group(page, key)
    wait_for_settled_styles(page)
    assert_fits(page, label)
    assert_no_serious_violations(page)
    assert_targets(page, label)


# --- the groups are collapsed, so the Lists tab cap holds ----------------------------------------


@pytest.mark.parametrize("viewport_name", list(VIEWPORTS))
def test_lists_editor_groups_are_collapsed_one_tab_stop_each(
    running_app: RunningApp, browser: Browser, viewport_name: str
) -> None:
    """The cap for Lists stays 110 (it is not raised for the editor): the five groups are
    collapsed ``<details>``, so the whole area is still walked within it, and no control of the
    editor is reachable until its group is opened."""
    assert AREA_TAB_BUDGET["lists"] <= 110
    seed(running_app)
    with new_page(browser, VIEWPORTS[viewport_name]) as page:
        open_editor(page, running_app)
        for key in GROUPS:
            expect(group(page, key)).not_to_have_attribute("open", "")
            expect(page.locator(f"#list-{key}-summary")).to_be_visible()
        expect(page.locator(f"{SECTION} textarea:visible")).to_have_count(0)
        expect(page.locator(f"{SECTION} details button:visible")).to_have_count(0)
        assert_area_within_budget(page, "lists", viewport_name)


# --- the keyboard path ---------------------------------------------------------------------------


@pytest.mark.parametrize("viewport_name", list(VIEWPORTS))
def test_lists_editor_by_keyboard_phone_axe(
    running_app: RunningApp, browser: Browser, viewport_name: str
) -> None:
    app = running_app
    seed(app)
    viewport = VIEWPORTS[viewport_name]
    with new_page(browser, viewport) as page:
        open_editor(page, app)
        section = page.locator(SECTION)

        # --- add an industry value; a duplicate is refused in place ---------------------------
        open_group(page, "industry")
        field = page.locator("#value-add-industry")
        tab_to(page, field)
        page.keyboard.type("Aerospace")
        page.keyboard.press("Enter")
        wait_said(page, added_message("industry", "aerospace"))
        new_id = value_id(app, "industry", "aerospace")
        expect(page.locator(f"#value-{new_id}")).to_be_focused()
        expect(section.locator("#lists-editor-done")).to_have_text(
            added_message("industry", "aerospace")
        )
        expect(group(page, "industry")).to_have_attribute("open", "")

        tab_to(page, field)
        page.keyboard.type("Energy")
        page.keyboard.press("Enter")
        expect(field).to_have_attribute("aria-invalid", "true")
        expect(field).to_be_focused()
        wait_said(page, add_refused(1))
        assert rows(app, "SELECT count(*) FROM controlled_values WHERE value='energy'") == [(1,)]

        # --- replace the other spellings --------------------------------------------------------
        box = page.locator(f"#value-{new_id}-aliases")
        type_lines(page, box, ["Aviation tech", "space"])
        save = section.get_by_role("button", name="Save aliases for aerospace", exact=True)
        tab_to(page, save)
        page.keyboard.press("Enter")
        wait_said(page, aliases_message("aerospace"))
        expect(page.locator(f"#value-{new_id}")).to_be_focused()
        assert aliases_in_db(app, "industry", "aerospace") == ["aviation tech", "space"]

        # --- an alias that is another value's is refused in place -------------------------------
        type_lines(page, box, ["utilities"])
        tab_to(page, save)
        page.keyboard.press("Enter")
        expect(box).to_have_attribute("aria-invalid", "true")
        expect(box).to_be_focused()
        wait_said(page, aliases_refused(1))
        assert aliases_in_db(app, "industry", "aerospace") == ["aviation tech", "space"]

        # --- delete the unused value ------------------------------------------------------------
        remove = section.get_by_role("button", name="Delete aerospace", exact=True)
        tab_to(page, remove)
        page.keyboard.press("Enter")
        wait_said(page, deleted_message("industry", "aerospace"))
        expect(page.locator("#list-industry-summary")).to_be_focused()
        assert rows(app, "SELECT 1 FROM controlled_values WHERE value='aerospace'") == []

        # --- a value in use is refused, announced, and nothing is deleted -----------------------
        farm = seed_id("industry", "agriculture")
        in_use = section.get_by_role("button", name="Delete agriculture", exact=True)
        tab_to(page, in_use)
        page.keyboard.press("Enter")
        wait_said_containing(page, "3 jobs")
        message = page.locator(f"#value-{farm}-delete-error")
        expect(message).to_contain_text("3 jobs")
        expect(message).to_be_focused()
        expect(in_use).to_have_attribute("aria-describedby", f"value-{farm}-delete-error")
        assert rows(app, "SELECT 1 FROM controlled_values WHERE value='agriculture'") == [(1,)]
        assert rows(app, "SELECT count(*) FROM jobs WHERE industry='agriculture'") == [(3,)]

        # --- skill aliases ------------------------------------------------------------------------
        open_group(page, "skills")
        python = page.locator("#skill-skill-py-aliases")
        type_lines(page, python, ["py", "CPython"])
        save_skill = section.get_by_role("button", name="Save aliases for Python", exact=True)
        tab_to(page, save_skill)
        page.keyboard.press("Enter")
        wait_said(page, aliases_message("Python"))
        expect(page.locator("#skill-skill-py")).to_be_focused()
        assert skill_aliases(app, "skill-py") == ["py", "CPython"]
        type_lines(page, python, ["rust"])
        tab_to(page, save_skill)
        page.keyboard.press("Enter")
        expect(python).to_have_attribute("aria-invalid", "true")
        expect(python).to_be_focused()
        wait_said(page, aliases_refused(1))
        assert skill_aliases(app, "skill-py") == ["py", "CPython"]

        # --- work mode is read only; the removal reasons are fixed; the link goes on -----------
        open_group(page, "work_mode")
        expect(group(page, "work_mode").locator("li")).to_have_count(3)
        expect(
            group(page, "work_mode").locator("textarea, button, input, select, a")
        ).to_have_count(0)
        expect(section.locator("#removal-reasons-list li")).to_have_count(8)
        expect(
            section.locator("#removal-reasons").locator("button, input, textarea, select")
        ).to_have_count(0)
        expect(section.locator("#removal-reason-location")).to_contain_text("work mode")

        # --- the whole editor, every group open: sideways scroll, axe, target size --------------
        scan_with_everything_open(page, f"lists ({viewport_name})")
        for control in (
            page.locator("#value-add-seniority"),
            section.get_by_role("button", name="Add seniority value", exact=True),
            section.get_by_role("button", name="Save aliases for Rust", exact=True),
            page.locator("#list-seniority-summary"),
        ):
            assert_target_size(control, "lists editor control")

        link = section.get_by_role("link", name="Values to map", exact=True)
        tab_to(page, link)
        page.keyboard.press("Enter")
        expect(page).to_have_url(f"{app.base_url}/inbox/values")
        expect(page.get_by_role("heading", name="Values to map", level=1)).to_be_visible()


@pytest.mark.parametrize("viewport_name", list(VIEWPORTS))
def test_lists_editor_passes_axe_in_dark_with_every_group_open(
    running_app: RunningApp, browser: Browser, viewport_name: str
) -> None:
    seed(running_app)
    with new_page(browser, VIEWPORTS[viewport_name], color_scheme="dark") as page:
        open_editor(page, running_app)
        scan_with_everything_open(page, f"lists dark ({viewport_name})")


@pytest.mark.parametrize("viewport_name", list(VIEWPORTS))
def test_lists_editor_refused_and_stale_answers_pass_axe(
    running_app: RunningApp, browser: Browser, viewport_name: str
) -> None:
    """A refused value (422, in place) and a stale save (409 reload prompt) keep the page usable:
    focus on the refused field or the prompt, the reload link works, nothing scrolls sideways."""
    app = running_app
    seed(app)
    with new_page(browser, VIEWPORTS[viewport_name]) as page:
        open_editor(page, app)
        open_group(page, "seniority")
        field = page.locator("#value-add-seniority")
        tab_to(page, field)
        page.keyboard.type("Senior")
        page.keyboard.press("Enter")
        expect(field).to_have_attribute("aria-invalid", "true")
        expect(field).to_be_focused()
        assert_fits(page, "refused")
        assert_no_serious_violations(page)

        # Another tab saves the value this page still shows at version 1: this save is stale.
        with Database.open(app.data_dir / "app.db") as db, db.transaction() as conn:
            conn.execute(
                "UPDATE controlled_values SET aliases='[\"sr.\"]', version=2 WHERE id=?",
                (seed_id("seniority", "senior"),),
            )
        box = page.locator(f"#value-{seed_id('seniority', 'senior')}-aliases")
        type_lines(page, box, ["snr"])
        save = page.get_by_role("button", name="Save aliases for senior", exact=True)
        tab_to(page, save)
        page.keyboard.press("Enter")
        prompt = page.locator("#lists-editor-conflict")
        expect(prompt).to_be_visible()
        expect(prompt).to_be_focused()
        assert aliases_in_db(app, "seniority", "senior") == ["sr."]
        assert_fits(page, "stale")
        assert_no_serious_violations(page)
        reload_link = prompt.get_by_role("link", name="Reload", exact=True)
        tab_to(page, reload_link)
        page.keyboard.press("Enter")
        expect(page).to_have_url(f"{app.base_url}/settings/lists")
        wait_until_loaded(page, "lists")
        open_group(page, "seniority")
        expect(page.locator(f"#value-{seed_id('seniority', 'senior')}-aliases")).to_have_value(
            re.compile(r"^sr\.\s*$")
        )
