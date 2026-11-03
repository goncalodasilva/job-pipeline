"""The Settings areas (M9.2 part A; ADR-131 item 7; TD-308; detailed design "Settings areas"),
shared by the integration and end-to-end tests. Every value is fictional. No app module is
imported, so a missing route or template fails the test that needs it, not the collection.

Settings is a set of area pages. Each one is a token-free, read-only page (``PAGE_TAG``) whose
controls are protected fragments and POSTs (spec 2.9 rules 2, 3, 12 and 13):

- providers: ``/settings``, "Providers and AI" (nav label): Providers, Testing tools, the setup
  guide link.
- budgets: ``/settings/budgets``, "Usage budgets": ``#usage-budgets`` (panel, budget forms,
  background tasks).
- preferences: ``/settings/preferences``, "Job preferences": ``#preferences-settings``.
- lists: ``/settings/lists``, "Lists and statuses": ``#board-settings`` (statuses, reminders).
- sessions: ``/settings/sessions``, "Sessions": ``#sessions-settings``.
- your-details: ``/settings/your-details``, "Your details and CV templates":
  ``#details-settings`` and ``#templates-settings``.
- appearance: ``/settings/appearance``, "Appearance" (its controls arrive in part B).
- data: ``/settings/data``, "Data and privacy" (M9.1).
- keyboard: ``/settings/keyboard``, "Keyboard shortcuts": ``#shortcuts-settings``.

(Prompts arrive in M9.3 and are not listed yet: the side list shows only areas whose route
exists.)

Contract the tests rely on (the routes and templates implement it)

- Page and fragment: ``GET`` and ``HEAD`` of the page path answer 200 without the launch token.
  A request with ``HX-Request: true`` needs the token (403 without) and answers the main
  content only (no ``<html>``, no token in it), the same content the full page puts in
  ``<main>``. Reading writes nothing.
- The page's one ``<h1 id="page-heading">`` names the area (``/settings`` keeps "Settings" or
  says "Providers and AI": the tests accept both). On a plain load only the ``<h1>`` has
  ``data-autofocus``.
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
- Job preferences shows the stored ``JOB_PREFERENCES`` read only: ``<section
  id="preferences-settings">`` with ``<h2>Job preferences</h2>`` and ``<dl
  id="preferences-summary">`` holding the target roles, locations, work modes, minimum salary,
  must-haves, deal-breakers and CV country as visible text, and ``<a id="preferences-guide-link"
  href="/setup/preferences">`` (the guide's preferences step). With nothing stored the summary is
  replaced by ``<p id="preferences-empty">`` and the link stays. The page has no form.
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
    label: str  # the side list's link text and (but for providers) the page's <h1>
    owned: tuple[str, ...] = ()  # ids of the sections that live on this page only


AREAS: Final[tuple[SettingsArea, ...]] = (
    SettingsArea("providers", "/settings", "Providers and AI", ("providers", "testing-tools")),
    SettingsArea("budgets", "/settings/budgets", "Usage budgets", ("usage-budgets",)),
    SettingsArea(
        "preferences",
        "/settings/preferences",
        "Job preferences",
        ("preferences-settings", "preferences-summary", "preferences-guide-link"),
    ),
    SettingsArea("lists", "/settings/lists", "Lists and statuses", ("board-settings",)),
    SettingsArea("sessions", "/settings/sessions", "Sessions", ("sessions-settings",)),
    SettingsArea(
        "your-details",
        "/settings/your-details",
        "Your details and CV templates",
        ("details-settings", "templates-settings"),
    ),
    SettingsArea("appearance", "/settings/appearance", "Appearance"),
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
    "/settings/testing-tools",
    "/settings/data/location",
    "/settings/data/delete",
)

# Paths that were never pages and must stay refused without the token.
OLD_FRAGMENT_NAMES: Final = ("/settings/usage", "/settings/details", "/settings/shortcuts")

COMING_LATER: Final = "Coming later"
GUIDE_PREFERENCES: Final = "/setup/preferences"
DATA_LINK: Final = "Data and privacy"
