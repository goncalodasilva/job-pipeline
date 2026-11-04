"""The Settings areas (M9.2 part A; ADR-131 item 7; detailed design "Settings areas").

Settings is a set of area pages. Each is a token-free, read-only page whose controls are
protected fragments and POSTs. This table is the one list of them: the side list
(``fragments/settings/_nav.html``), every page's heading and the template that holds an area's
sections all come from it. A page is listed only when its route exists (Prompts arrives with
M9.3). Kept apart from the routes so every area module can use it without importing the others.
"""

from dataclasses import dataclass
from typing import Any, Final

AREA_FRAGMENT: Final = "fragments/settings/_area.html"  # an area's whole main content
AREA_PAGE: Final = "pages/settings_area.html"


@dataclass(frozen=True)
class SettingsArea:
    slug: str
    label: str  # the side list's link
    path: str
    content: str  # the template holding the area's sections
    heading: str = ""  # the page's <h1> and title; the label when empty

    @property
    def title(self) -> str:
        return self.heading or self.label

    @property
    def page_title(self) -> str:
        """The ``<title>`` text before the app name: the area, then "Settings" (so a tab or a
        history entry of any area is found under Settings); ``/settings`` is just "Settings"."""
        return self.title if self.title == "Settings" else f"{self.title} · Settings"


SETTINGS_AREAS: Final[tuple[SettingsArea, ...]] = (
    # /settings is also the landing page of Settings, so its heading stays "Settings".
    SettingsArea(
        "providers",
        "Providers and AI",
        "/settings",
        "fragments/settings/_area_providers.html",
        heading="Settings",
    ),
    SettingsArea(
        "budgets", "Usage budgets", "/settings/budgets", "fragments/settings/_area_budgets.html"
    ),
    SettingsArea(
        "preferences",
        "Job preferences",
        "/settings/preferences",
        "fragments/settings/_area_preferences.html",
    ),
    SettingsArea(
        "lists", "Lists and statuses", "/settings/lists", "fragments/settings/_area_lists.html"
    ),
    SettingsArea(
        "sessions", "Sessions", "/settings/sessions", "fragments/settings/_area_sessions.html"
    ),
    SettingsArea(
        "your-details",
        "Your details and CV templates",
        "/settings/your-details",
        "fragments/settings/_area_your_details.html",
    ),
    SettingsArea(
        "appearance",
        "Appearance",
        "/settings/appearance",
        "fragments/settings/_area_appearance.html",
    ),
    SettingsArea("data", "Data and privacy", "/settings/data", "fragments/settings/_data.html"),
    SettingsArea(
        "keyboard",
        "Keyboard shortcuts",
        "/settings/keyboard",
        "fragments/settings/_area_keyboard.html",
    ),
)


def settings_area(slug: str) -> SettingsArea:
    """The area with this slug."""
    return next(item for item in SETTINGS_AREAS if item.slug == slug)


def area_ctx(slug: str, *, heading_focus: bool = True) -> dict[str, Any]:
    """What ``_area.html`` needs: the current area, the side list, and whether the page's
    ``<h1>`` is the one focus target (a result, error or saved status takes it instead)."""
    return {
        "settings_area": settings_area(slug),
        "settings_areas": SETTINGS_AREAS,
        "heading_focus": heading_focus,
    }
