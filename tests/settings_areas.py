"""The Settings areas (M9.2 part A; ADR-131 item 7; TD-308; detailed design "Settings areas"),
shared by the integration and end-to-end tests. Every value is fictional. No app module is
imported, so a missing route or template fails the test that needs it, not the collection.

Settings is a set of area pages. Each one is a token-free, read-only page (``PAGE_TAG``) whose
controls are protected fragments and POSTs (spec 2.9 rules 2, 3, 12 and 13):

- providers: ``/settings``, "Providers and AI" (nav label): Providers, the collapsed per-task
  tier list ``#task-tiers`` (part C), Testing tools, the setup guide link.
- budgets: ``/settings/budgets``, "Usage budgets": ``#usage-budgets`` (panel, budget forms,
  background tasks).
- preferences: ``/settings/preferences``, "Job preferences": ``#preferences-settings``.
- lists: `/settings/lists`, "Lists and statuses": `#board-settings` (statuses, reminders) and
  `#lists-editor` (controlled values, skill aliases, removal reasons; M9.2 part D,
  `tests/lists_pages.py`).
- prompts: ``/settings/prompts``, "Prompts" (M9.3 part B; the markup is in
  ``tests/prompt_pages.py``): ``#prompts-settings``. Placed after Lists and statuses and before
  Sessions, as in the design table. Besides the list, every task has a token-free shell page
  ``/settings/prompts/{task}`` (also a ``PAGE_TAG`` route, not an area) whose editor loads from
  the protected fragment ``/settings/prompts/{task}/editor``.
- sessions: ``/settings/sessions``, "Sessions": ``#sessions-settings``.
- your-details: ``/settings/your-details``, "Your details and CV templates":
  ``#details-settings`` and ``#templates-settings``.
- appearance: ``/settings/appearance``, "Appearance": ``#appearance-settings`` (part B).
- data: ``/settings/data``, "Data and privacy" (M9.1).
- keyboard: ``/settings/keyboard``, "Keyboard shortcuts": ``#shortcuts-settings``.

(The side list shows only areas whose route exists; Prompts joined it with M9.3 part B, so it
has ten links.)

Contract the tests rely on (the routes and templates implement it)

- Page and fragment: ``GET`` and ``HEAD`` of the page path answer 200 without the launch token.
  A request with ``HX-Request: true`` needs the token (403 without) and answers the main
  content only (no ``<html>``, no token in it), the same content the full page puts in
  ``<main>``. Reading writes nothing.
- The page's one ``<h1 id="page-heading">`` names the area (``/settings`` keeps "Settings").
  On a plain load only the ``<h1>`` has ``data-autofocus``. The page ``<title>`` is the area's
  name, then "Settings", then the app name ("Appearance · Settings · Job Search Pipeline");
  ``/settings`` is "Settings · Job Search Pipeline".
- A side list ``<nav aria-label="Settings areas">`` is part of the main content (so an htmx
  answer carries it). It has one link per area in the order of ``AREAS``, with the area's label as
  its text and its path as ``href``; the link of the current area has ``aria-current="page"`` and
  no other link has. The main sidebar's "Settings" entry has ``aria-current="page"`` on every area
  page. Every link of the list resolves (an area is listed only when its route exists).
- ``/settings`` shows Providers and AI: the ``#providers`` section comes first after the
  ``<h1>`` and the side list; the "Coming later" card (``.settings-later``) is gone. The old
  "Your data" card is gone too (the side list links to Data and privacy), so the main content
  holds exactly one link named "Data and privacy".
- Each control lives on its owning page only (``OWNED`` below).
- Job preferences shows the stored ``JOB_PREFERENCES`` read only, but not on the token-free page
  (spec 2.9 rule 13: roles, locations, salary, must-haves and deal-breakers are personal job-search
  data). The page is a shell: ``<section id="preferences-settings">`` with ``<h2>Job
  preferences</h2>`` and a loading line, which fetches the protected fragment ``GET
  /settings/preferences/summary`` (``hx-get``, ``hx-trigger="load"``, ``outerHTML``), and ``<a
  id="preferences-guide-link" href="/setup/preferences">`` (the guide's preferences step) outside
  that section. The fragment is the same ``<section id="preferences-settings">`` holding ``<dl
  id="preferences-summary">`` with the target roles, locations, work modes, minimum salary,
  must-haves, deal-breakers and CV country as visible text; with nothing stored the summary is
  replaced by ``<p id="preferences-empty">``. Neither has a form. The fragment needs the token
  (403 without) and writes nothing.
- Plain-form fallbacks answer 303 to the owning area; the in-app links for the budgets are
  ``/settings/budgets#usage-budgets``.
"""

from dataclasses import dataclass
from typing import Final

SETTINGS_NAV_LABEL: Final = "Settings areas"
SIDEBAR_LABEL: Final = "Main"


@dataclass(frozen=True)
class SettingsArea:
    slug: str
    path: str
    label: str  # the side list's link text and (but for providers, "Settings") the page's <h1>
    owned: tuple[str, ...] = ()  # ids of the sections that live on this page only


AREAS: Final[tuple[SettingsArea, ...]] = (
    SettingsArea(
        "providers", "/settings", "Providers and AI", ("providers", "testing-tools", "task-tiers")
    ),
    SettingsArea("budgets", "/settings/budgets", "Usage budgets", ("usage-budgets",)),
    SettingsArea(
        "preferences",
        "/settings/preferences",
        "Job preferences",
        ("preferences-settings", "preferences-guide-link"),  # the summary is a protected fragment
    ),
    SettingsArea(
        "lists", "/settings/lists", "Lists and statuses", ("board-settings", "lists-editor")
    ),
    SettingsArea("prompts", "/settings/prompts", "Prompts", ("prompts-settings",)),
    SettingsArea("sessions", "/settings/sessions", "Sessions", ("sessions-settings",)),
    SettingsArea(
        "your-details",
        "/settings/your-details",
        "Your details and CV templates",
        ("details-settings", "templates-settings"),
    ),
    SettingsArea("appearance", "/settings/appearance", "Appearance", ("appearance-settings",)),
    SettingsArea(
        "data",
        "/settings/data",
        "Data and privacy",
        ("data-export", "data-import", "data-delete"),
    ),
    SettingsArea("keyboard", "/settings/keyboard", "Keyboard shortcuts", ("shortcuts-settings",)),
)
BY_SLUG: Final = {area.slug: area for area in AREAS}
SLUGS: Final = tuple(area.slug for area in AREAS)

# Moved out of /settings: these ids must not be on the providers page any more.
MOVED_FROM_SETTINGS: Final = tuple(
    section for area in AREAS if area.slug != "providers" for section in area.owned
)

# Token-protected GET fragments that keep their paths (they are not pages): the areas load them.
FRAGMENT_PATHS: Final = (
    "/settings/usage",
    "/settings/details",
    "/settings/shortcuts",
    "/settings/cv-templates",
    "/settings/preferences/summary",
    "/settings/testing-tools",
    "/settings/data/location",
    "/settings/data/delete",
    # TD-342 (spec 2.9 rule 13): not an area, but the same pattern. The setup guide's preferences
    # step is a token-free shell; its form, filled from the stored preferences, is this fragment.
    "/setup/preferences/form",
    # M9.3 part B (spec 2.9 rule 13): a prompt override is user-written text that may hold personal
    # data, so the task page is a token-free shell and the editor is this protected fragment.
    "/settings/prompts/summarize_job/editor",
)

# Paths that were never pages and must stay refused without the token.
OLD_FRAGMENT_NAMES: Final = ("/settings/usage", "/settings/details", "/settings/shortcuts")

COMING_LATER: Final = "Coming later"
GUIDE_PREFERENCES: Final = "/setup/preferences"
DATA_LINK: Final = "Data and privacy"
