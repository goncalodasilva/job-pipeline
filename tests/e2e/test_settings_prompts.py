"""M9.3 part B: Settings > Prompts in a real browser (AC-M9.2; ADR-055; spec 3.9; TD-308).

By keyboard only the user opens Prompts from the side list, opens a task, waits for the editor to
load, edits the system text, saves (announced, the status line takes focus), reloads and finds the
edit, adds an unsafe template (refused, announced, the refused field takes focus, nothing written),
resets (announced, back to the default) and finds the list without a badge. At 390x844 and desktop
size, in light and dark, the list and a task page (loading and loaded) do not scroll sideways, keep
targets at least 24 by 24 px, keep the text areas usable and pass axe. The cap for the area is
``AREA_TAB_BUDGET["prompts"]`` = 50 (``tests/e2e/settings_areas.py``). The markup is described in
``tests/prompt_pages.py``. The app starts with a finished setup; nothing sleeps. Every value is
fictional.
"""

import re
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Locator, Page, Route, expect

from app.domain.tiers import DEFAULT_TIERS
from app.modules.ai.prompts import PromptLibrary
from app.wiring import PROMPTS_DIR
from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.settings_areas import (
    AREA_TAB_BUDGET,
    assert_area_within_budget,
    go_to_area_by_keyboard,
    nav_link,
    wait_until_loaded,
)
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, wait_said
from tests.e2e.test_settings_appearance import SMALL_TARGETS
from tests.e2e.test_settings_board import tab_to
from tests.e2e.test_shell import assert_target_size, has_no_horizontal_scroll, new_page, open_app
from tests.prompt_pages import (
    NOT_SAVED,
    RESET,
    SAVED,
    default_updated,
    edited_on,
    label_of,
    task_path,
    using_default,
)
from tests.running_app import RunningApp

pytestmark = pytest.mark.e2e

TASK = "summarize_job"
VIEWPORTS = {"desktop": DESKTOP, "phone": PHONE}
EDIT_LINE = "Keep every answer under five lines, said a fictional reviewer."
UNSAFE_LINE = "{% include 'kestrel_marker_0042.md' %}"
LOADING = re.compile(r"loading", re.IGNORECASE)
EDITOR_ROUTE = "**/settings/prompts/*/editor"


def default_version(task: str) -> int:
    found = re.search(r"(?m)^version: (\d+)$", (PROMPTS_DIR / task / "prompt.md").read_text())
    assert found is not None
    return int(found.group(1))


def overrides(app: RunningApp) -> Path:
    return app.data_dir / "prompts" / "overrides"


def library(app: RunningApp) -> PromptLibrary:
    return PromptLibrary(PROMPTS_DIR, overrides(app))


def seed_edit(app: RunningApp, task: str, *, base: int | None = None) -> None:
    """An override made against the default (the file the editor writes), optionally rewritten
    as if made against default version ``base``."""
    text = library(app).default_text(task)
    library(app).save_override(
        task, f"{text.system}\n{EDIT_LINE}", text.user, expected_content_version="none"
    )
    if base is not None:
        path = overrides(app) / task / "prompt.md"
        data = path.read_bytes()
        path.write_bytes(re.sub(rb"(?m)^version: \d+$", f"version: {base}".encode(), data, count=1))


def seed_states(app: RunningApp) -> str:
    """Edited (summarize_job), Default updated (the first task above version 1) and Can't load
    (extract_cv). Returns the task with the update."""
    seed_edit(app, TASK)
    updated = next(task for task in DEFAULT_TIERS if default_version(task) >= 2)
    seed_edit(app, updated, base=default_version(updated) - 1)
    folder = overrides(app) / "extract_cv"
    folder.mkdir(parents=True)
    (folder / "prompt.md").write_bytes(b"this is not a prompt file")
    return updated


def wait_task_loaded(page: Page) -> None:
    """The protected editor has replaced the placeholder (conditions only)."""
    page.wait_for_function("() => typeof window.htmx !== 'undefined'")
    expect(page.locator("#prompt-form")).to_be_attached()
    expect(page.locator("#prompt-editor[hx-get]")).to_have_count(0)
    page.wait_for_function("() => !document.querySelector('.htmx-request')")


def open_task(page: Page, app: RunningApp, task: str) -> None:
    open_app(page, app, task_path(task))
    wait_task_loaded(page)


def assert_fits(page: Page, label: str) -> None:
    fits, scroll_width, inner_width = has_no_horizontal_scroll(page)
    assert fits, f"{label}: scrollWidth {scroll_width} > {inner_width}"


def assert_targets(page: Page, label: str) -> None:
    small: list[str] = page.evaluate(SMALL_TARGETS)
    assert small == [], f"{label}: targets under 24 px:\n" + "\n".join(small)


def caret_to_end(field: Locator) -> None:
    """Put the caret at the end of the focused text area. (The first draft pressed
    ControlOrMeta+End, which moves the caret to the end on Windows and Linux but not on macOS,
    where it left the caret in the middle of the text.) The text is then typed with real keys."""
    field.evaluate("(el) => el.setSelectionRange(el.value.length, el.value.length)")


def editor_took_no_focus(page: Page) -> bool:
    """A plain load of the editor puts focus nowhere in it and leaves no focus mark behind. (The
    first draft expected the page heading to be focused here; nothing focuses a page on load,
    app.js drops the marks of the loaded page, and the spec asks for deliberate focus after a
    swap, which for this load is none.)"""
    inside: bool = page.evaluate(
        """() => {
          const editor = document.getElementById('prompt-editor');
          return !!editor && editor.contains(document.activeElement);
        }"""
    )
    marked: int = page.locator("[data-autofocus]").count()
    return not inside and marked == 0


def refused_field_is_focused(page: Page) -> bool:
    focused: bool = page.evaluate(
        """() => {
          const field = document.activeElement;
          if (!field || field.tagName !== 'TEXTAREA') return false;
          if (field.getAttribute('aria-invalid') !== 'true') return false;
          const ids = (field.getAttribute('aria-describedby') || '').split(/\\s+/);
          return ids.some((id) => {
            const note = document.getElementById(id);
            return note && note.getAttribute('role') === 'alert' && note.textContent.trim() !== '';
          });
        }"""
    )
    return focused


# --- the keyboard path ---------------------------------------------------------------------------


def test_prompt_edit_save_and_reset_by_keyboard(running_app: RunningApp, browser: Browser) -> None:
    app = running_app
    file = overrides(app) / TASK / "prompt.md"
    base = default_version(TASK)
    with new_page(browser, DESKTOP) as page:
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        open_app(page, app, "/settings")
        wait_until_loaded(page, "providers")

        # --- open Prompts from the side list, then the task, with the keyboard ---------------
        go_to_area_by_keyboard(page, app, "providers", "prompts")
        expect(page.locator("#prompt-list li")).to_have_count(len(DEFAULT_TIERS))
        tab_to(page, page.locator(f"#prompt-link-{TASK}"))
        page.keyboard.press("Enter")
        expect(page).to_have_url(f"{app.base_url}{task_path(TASK)}")
        heading = page.get_by_role("heading", level=1, name=label_of(TASK), exact=True)
        expect(heading).to_be_visible()
        wait_task_loaded(page)
        assert editor_took_no_focus(page), "loading the editor takes no focus"
        expect(page.locator("#prompt-status")).to_have_text(using_default(base))
        expect(page.locator("#prompt-reset")).to_have_count(0)
        assert not file.exists()

        # --- edit the system text and save ----------------------------------------------------
        system = page.locator("#prompt-system")
        tab_to(page, system)
        caret_to_end(system)
        page.keyboard.type(f"\n{EDIT_LINE}")
        tab_to(page, page.locator("#prompt-save"))
        page.keyboard.press("Enter")
        wait_said(page, SAVED)
        expect(page.locator("#prompt-status")).to_be_focused()
        expect(page.locator("#prompt-status")).to_have_text(edited_on(base))
        assert EDIT_LINE in file.read_text()
        assert (file.stat().st_mode & 0o777) == 0o600
        saved_bytes = file.read_bytes()

        # --- reload: the edit is still there ---------------------------------------------------
        page.reload()
        wait_task_loaded(page)
        expect(page.locator("#prompt-system")).to_have_value(re.compile(re.escape(EDIT_LINE)))
        expect(page.locator("#prompt-status")).to_have_text(edited_on(base))

        # --- an unsafe template is refused in place --------------------------------------------
        tab_to(page, page.locator("#prompt-user"))
        caret_to_end(page.locator("#prompt-user"))
        page.keyboard.type(f"\n{UNSAFE_LINE}")
        tab_to(page, page.locator("#prompt-save"))
        page.keyboard.press("Enter")
        page.wait_for_function(
            "(text) => (window.__said || []).some((said) => said.startsWith(text))",
            arg=NOT_SAVED,
        )
        page.wait_for_function("() => document.activeElement.tagName === 'TEXTAREA'")
        assert refused_field_is_focused(page), "the refused field is focused and tied to its alert"
        expect(page.locator("#prompt-user")).to_have_value(re.compile(re.escape("kestrel_marker")))
        assert file.read_bytes() == saved_bytes, "a refused template writes nothing"
        assert_no_serious_violations(page)

        # --- reset: back to the default --------------------------------------------------------
        tab_to(page, page.locator("#prompt-reset"))
        page.keyboard.press("Enter")
        wait_said(page, RESET)
        expect(page.locator("#prompt-status")).to_be_focused()
        expect(page.locator("#prompt-status")).to_have_text(using_default(base))
        expect(page.locator("#prompt-reset")).to_have_count(0)
        expect(page.locator("#prompt-system")).not_to_have_value(re.compile(re.escape(EDIT_LINE)))
        assert not file.exists() and not file.parent.exists()

        # --- the list has no badge for it any more --------------------------------------------
        tab_to(page, nav_link(page, "Prompts"))
        page.keyboard.press("Enter")
        expect(page).to_have_url(f"{app.base_url}/settings/prompts")
        wait_until_loaded(page, "prompts")
        expect(page.locator(f"#prompt-{TASK}")).to_be_visible()
        expect(page.locator(f"#prompt-{TASK} [data-badge]")).to_have_count(0)


# --- phone, tab budget and accessibility -------------------------------------------------------


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("viewport_name", list(VIEWPORTS))
def test_prompts_area_phone_390x844_and_axe(
    running_app: RunningApp, browser: Browser, viewport_name: str, theme: str
) -> None:
    """The list (with an Edited, a Default updated and a Can't load row) and a task page, while
    the editor loads and once loaded, at desktop size and 390x844 in light and dark: no sideways
    scroll, targets of at least 24 by 24 px, text areas as wide as the screen allows, the Tab walk
    within the cap, no serious or critical axe finding."""
    app = running_app
    assert PHONE == {"width": 390, "height": 844}
    updated = seed_states(app)
    viewport = VIEWPORTS[viewport_name]
    label = f"{viewport_name} {theme}"
    with new_page(browser, viewport, color_scheme=theme) as page:
        # --- the list -----------------------------------------------------------------------
        open_app(page, app, "/settings/prompts")
        wait_until_loaded(page, "prompts")
        wait_for_settled_styles(page)
        assert_fits(page, f"list {label}")
        assert_targets(page, f"list {label}")
        links = page.locator("#prompt-list a")
        expect(links).to_have_count(len(DEFAULT_TIERS))
        for index in range(len(DEFAULT_TIERS)):
            assert_target_size(links.nth(index), f"list link {index}")
        expect(page.locator(f"#prompt-{TASK} [data-badge]")).to_have_text(["Edited"])
        expect(page.locator(f"#prompt-{updated} [data-badge]")).to_have_text(
            ["Edited", "Default updated"]
        )
        expect(
            page.locator("#prompt-extract_cv [data-badge]").filter(has_text="Can't load")
        ).to_be_visible()
        walk = assert_area_within_budget(page, "prompts", f"list {viewport_name}")
        assert walk.stops <= AREA_TAB_BUDGET["prompts"]
        assert_no_serious_violations(page)

        # --- a task page while its editor loads ----------------------------------------------
        held: list[Route] = []
        page.route(EDITOR_ROUTE, lambda route: held.append(route))
        open_app(page, app, task_path(updated))
        page.wait_for_function("() => document.querySelector('#prompt-editor.htmx-request')")
        expect(page.locator("#prompt-editor")).to_contain_text(LOADING)
        expect(page.locator("#prompt-form")).to_have_count(0)
        assert editor_took_no_focus(page), "the loading placeholder holds no focus"
        wait_for_settled_styles(page)
        assert_fits(page, f"task loading {label}")
        assert_targets(page, f"task loading {label}")
        assert_no_serious_violations(page)
        assert len(held) == 1, "one request for the editor"
        # Release the held request first: unrouting cancels a handler that is still waiting, and
        # its route can no longer be continued ("Route is already handled").
        held[0].continue_()
        page.unroute(EDITOR_ROUTE)

        # --- loaded: the status, the compare view and text areas that can be used ------------
        wait_task_loaded(page)
        expect(page.locator("#prompt-status")).to_have_text(
            default_updated(default_version(updated) - 1, default_version(updated))
        )
        expect(page.locator("#prompt-compare")).to_be_visible()
        assert editor_took_no_focus(page), "a plain load of the editor takes no focus"
        wait_for_settled_styles(page)
        assert_fits(page, f"task loaded {label}")
        assert_targets(page, f"task loaded {label}")
        for control in (
            page.locator("#prompt-save"),
            page.locator("#prompt-reset"),
            page.locator("#prompt-system"),
            page.locator("#prompt-user"),
        ):
            assert_target_size(control, "prompt control")
        for identifier in ("prompt-system", "prompt-user"):
            box = page.locator(f"#{identifier}").bounding_box()
            assert box is not None
            assert box["x"] >= 0 and box["x"] + box["width"] <= viewport["width"], (identifier, box)
            # As wide as the screen allows, up to the 40rem (640 px) measure of a settings section:
            # at 1280 px the app's sidebar and the Settings side list leave about 760 px, so the
            # first draft's flat 70% of the viewport (896 px) could never hold on a desktop.
            wanted = min(0.7 * viewport["width"], 600)
            assert box["width"] >= wanted, f"{identifier} is too narrow: {box}"
            assert box["height"] >= 96, f"{identifier} is too short to edit in: {box}"
        # The Tab walk starts from the top of the page, so it comes before anything is clicked
        # (a click sets the point Tab goes on from, and the walk would only see what follows).
        assert_area_within_budget(page, "prompts", f"task {viewport_name}")
        field = page.locator("#prompt-user")
        field.click()
        caret_to_end(field)
        page.keyboard.type(" typed")
        expect(field).to_have_value(re.compile(r" typed$"))
        assert_fits(page, f"task typed {label}")
        assert_no_serious_violations(page)
