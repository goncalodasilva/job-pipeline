"""Shared helpers for the Settings area end-to-end tests (M9.2 part A; ADR-131 item 7; TD-308).

``AREA_TAB_BUDGET`` replaces the single ``MAX_TABS = 120`` ceiling that Settings had as one long
page (``tests/e2e/test_setup_provider.py`` keeps ``MAX_TABS`` for the callers that are not
Settings). A budget is the most Tab presses it takes to walk a whole area page from the top of the
page, shell included (the skip link, the top bar, the sidebar on a desktop, the side list of
areas and the area's own controls), measured by ``count_tab_stops``. Each cap is what the area is
expected to hold, with room to grow, so a control added to an area later fails here and the area
is split or its form put behind a disclosure, rather than the ceiling being raised for
everything. Measured on the old single page (desktop): the shell took 13 stops, Providers 8,
Usage budgets 34, Board 54, Keyboard shortcuts 2, collapsed Sessions and Your details 1 each;
the side list adds one stop per area (9 now).

Raising a cap needs a reason in the commit or ADR (the rule behind TD-308). Every value in the
tests is fictional; nothing here sleeps: the walk waits on conditions only.
"""

from dataclasses import dataclass, field
from typing import Final

from playwright.sync_api import Locator, Page, expect

from tests.e2e.test_setup_provider_test import is_focused
from tests.running_app import RunningApp
from tests.settings_areas import AREAS, BY_SLUG, SETTINGS_NAV_LABEL

AREA_TAB_BUDGET: Final[dict[str, int]] = {
    # Providers (8), Testing tools and the guide link. Part C's tier list (a select per task and
    # a Save) sits in a collapsed <details> (like Sessions and Your details, ADR-125 item 1): one
    # stop while collapsed, so the cap stays 70 and is not raised.
    "providers": 70,
    "budgets": 80,  # the panel and four service forms plus background tasks: 34 on the old page
    "preferences": 50,  # a read-only summary and the guide link (removal rules arrive in M9.4)
    # Board statuses and reminders (54 on the old page) plus the lists editor (M9.2 part D). The
    # editor's groups (industry, seniority, employment type, work mode, skills) are collapsed
    # <details>, one Tab stop each, like Sessions and the tier list, so the cap stays 110 and is
    # not raised (tests/e2e/test_settings_lists.py pins that the groups are collapsed).
    "lists": 110,
    "sessions": 45,  # one collapsed disclosure (ADR-125)
    "your-details": 50,  # one collapsed disclosure and the template list
    "appearance": 40,  # theme and density
    "data": 45,  # export, a blocked import, delete all data (M9.1)
    "keyboard": 40,  # one switch
}

# What shows that the area has finished loading (some parts arrive by a protected fragment).
AREA_READY: Final[dict[str, tuple[str, ...]]] = {
    "providers": ("#providers", "#task-tiers"),
    "budgets": ("#usage-budgets", "#usage-panel"),
    "preferences": ("#preferences-settings",),
    "lists": ("#board-settings", "#lists-editor"),
    "sessions": ("#sessions-settings",),
    "your-details": ("#details-summary", "#templates-settings"),
    "appearance": ("#appearance-settings",),
    "data": ("#data-folder",),
    "keyboard": ("#shortcuts-settings",),
}

NAV = f'nav[aria-label="{SETTINGS_NAV_LABEL}"]'
MIN_STOPS: Final = 5  # a page with a skip link, a top bar and its side list has at least this

_STEP_JS = """(navSelector) => {
  const seen = (window.__tabSeen ??= new Set());
  const element = document.activeElement;
  if (!element || element === document.body || element === document.documentElement) {
    return { state: 'out', inNav: false };
  }
  if (seen.has(element)) return { state: 'repeat', inNav: false };
  seen.add(element);
  return { state: 'new', inNav: element.closest(navSelector) !== null };
}"""

# A cue other than colour that tells the current link from the others (WCAG 1.4.1).
_NON_COLOUR_CUE = """(selector) => {
  const links = Array.from(document.querySelectorAll(selector + ' a'));
  const current = links.find((a) => a.getAttribute('aria-current') === 'page');
  const other = links.find((a) => a !== current);
  if (!current || !other) return false;
  const cue = (a) => {
    const s = getComputedStyle(a);
    const before = getComputedStyle(a, '::before').content;
    const after = getComputedStyle(a, '::after').content;
    return [
      s.fontWeight, s.textDecorationLine, s.borderLeftWidth, s.borderBottomWidth,
      s.borderInlineStartWidth, s.fontStyle, before, after,
      a.querySelector('.visually-hidden') ? a.querySelector('.visually-hidden').textContent : '',
    ].join('|');
  };
  return cue(current) !== cue(other);
}"""


@dataclass
class TabWalk:
    """The Tab stops of a page, from its top: how many, and how many were in the side list."""

    stops: int = 0
    in_nav: int = 0
    limit_hit: bool = False
    trail: list[str] = field(default_factory=list)


def wait_until_loaded(page: Page, slug: str) -> None:
    """Wait for htmx and for every part of the area to be on the page (conditions, no sleeps)."""
    page.wait_for_function("() => typeof window.htmx !== 'undefined'")
    for selector in AREA_READY[slug]:
        expect(page.locator(selector).first).to_be_attached()
    page.wait_for_function("() => !document.querySelector('.htmx-request')")


def count_tab_stops(page: Page, limit: int) -> TabWalk:
    """Press Tab from the top of the page until focus leaves the page or comes round again, and
    count the elements reached (at most ``limit`` + 1 presses: ``limit_hit`` says it went on)."""
    page.evaluate("() => { window.__tabSeen = new Set(); document.activeElement?.blur(); }")
    walk = TabWalk()
    for _ in range(limit + 1):
        page.keyboard.press("Tab")
        step = page.evaluate(_STEP_JS, NAV)
        if step["state"] != "new":
            return walk
        walk.stops += 1
        walk.in_nav += 1 if step["inNav"] else 0
    walk.limit_hit = True
    return walk


def assert_area_within_budget(page: Page, slug: str, viewport_name: str) -> TabWalk:
    budget = AREA_TAB_BUDGET[slug]
    walk = count_tab_stops(page, budget)
    assert not walk.limit_hit and walk.stops <= budget, (
        f"{slug} ({viewport_name}): more than {budget} Tab stops (TD-308); "
        f"move controls to another area or behind a disclosure"
    )
    assert walk.stops >= MIN_STOPS, f"{slug} ({viewport_name}): only {walk.stops} Tab stops"
    return walk


def nav_link(page: Page, label: str) -> Locator:
    return page.locator(NAV).get_by_role("link", name=label, exact=True)


def current_has_non_colour_cue(page: Page) -> bool:
    cue: bool = page.evaluate(_NON_COLOUR_CUE, NAV)
    return cue


ALL_LABELS: Final = tuple(area.label for area in AREAS)


def tab_to_link(page: Page, link: Locator, limit: int) -> None:
    """Press Tab until ``link`` has focus, unless it has it already (keyboard only)."""
    expect(link).to_have_count(1)
    for _ in range(limit + 1):
        if is_focused(link):
            return
        page.keyboard.press("Tab")
    raise AssertionError(f"{link} never received focus within {limit} Tab presses")


def go_to_area_by_keyboard(page: Page, app: RunningApp, here: str, target: str) -> None:
    """From the area ``here``, Tab to ``target``'s link in the side list and press Enter; waits
    for the new page and its parts (slugs of ``tests/settings_areas.py``)."""
    tab_to_link(page, nav_link(page, BY_SLUG[target].label), AREA_TAB_BUDGET[here])
    page.keyboard.press("Enter")
    expect(page).to_have_url(f"{app.base_url}{BY_SLUG[target].path}")
    wait_until_loaded(page, target)
