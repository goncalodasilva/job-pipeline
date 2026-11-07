"""Every page the app serves, as reached in the shipped demo (M9.6 part A; AC-M9.5).

``PAGE_CASES`` holds, for each page route (``app.api.security.PAGE_TAG``), the screens that show
it in the demo dataset: a concrete address and the screen's primary action. Some routes have
more than one screen (the application's tabs, each setup step). A screen the demo has no row for
is reached through its parent page (``via``: open ``url``, press that control, arrive at
``arrives``). ``tests/e2e/test_m9_pages.py`` runs axe on every screen (light and dark, desktop and
phone) and walks to each primary action by keyboard;
``test_ac_m9_5_every_page_route_has_an_axe_case`` fails when a page route has no screen here.

``OVERLAY_CASES`` adds the dialogs, drawers, menus and panels a page opens. The audit opens each
on its page and runs axe the same way, and never confirms anything in them, because the module
shares one demo. Keyboard use inside them, and the empty, refused and AI-working states of each
area, belong to that area's own e2e tests, which run axe too (for example
``test_last_card_gone_moves_focus_to_the_empty_state_and_z_undoes``,
``test_lists_editor_refused_and_stale_answers_pass_axe``, ``tests/e2e/test_tasks_panel.py``,
``tests/e2e/test_palette.py`` and ``tests/e2e/test_inbox_organisation.py`` for a value to map,
which the demo has none of).
"""

import json
from dataclasses import dataclass
from functools import cache
from typing import Any

from app.services.demo import DATASET

# The settings areas that are pages of their own (M9.2), with their primary action.
SETTINGS_AREAS = (
    ("/settings", "settings-providers", ("button", "Test connection")),
    ("/settings/appearance", "settings-appearance", ("button", "Save")),
    ("/settings/budgets", "settings-budgets", ("css", "#budget-fake-field-allowance")),
    ("/settings/data", "settings-data", ("button", "Export")),
    ("/settings/keyboard", "settings-keyboard", ("css", "#shortcuts-enabled")),
    ("/settings/lists", "settings-lists", ("button", "Rename Preparing")),
    ("/settings/preferences", "settings-preferences", ("button", "Save threshold")),
    ("/settings/prompts", "settings-prompts", ("css", "#prompt-link-summarize_job")),
    ("/settings/sessions", "settings-sessions", ("css", "#sessions-summary")),
    ("/settings/your-details", "settings-your-details", ("css", "#details-summary")),
)
SETUP_STEPS = (
    ("welcome", ("button", "Use this folder and continue")),
    ("provider", ("button", "Test connection")),
    ("web_search", ("button", "Continue")),
    ("cv_import", ("css", "#setup-cv-start")),
    ("preferences", ("button", "Save and continue")),
    ("done", ("css", "#done-today")),
)


@dataclass(frozen=True)
class PageCase:
    """One screen: ``route`` is the registered path, ``url`` where it is in the demo."""

    name: str
    route: str
    url: str
    primary: tuple[str, str]  # ("css", selector) or (ARIA role, accessible name)
    via: tuple[str, str] | None = None
    arrives: str | None = None  # a regular expression for the path reached through ``via``
    ready: str | None = None  # a selector shown once the screen has settled (a polling panel)


@dataclass(frozen=True)
class OverlayCase:
    """A dialog, drawer, menu or panel: open ``url``, click the first ``opener``, type ``typed``
    (field selector, text) when given, then wait for ``ready`` (and ``has``, when given)."""

    name: str
    url: str
    opener: str
    ready: str
    typed: tuple[str, str] | None = None
    has: str | None = None


# Notifications marks its rows seen when it first opens, so it comes last.
OVERLAY_CASES = (
    OverlayCase(
        "palette-results",
        "/",
        "[data-palette-open]",
        "#palette[open] #palette-results [data-palette-status]",
        typed=("#palette-input", "green"),
        has="#palette [data-palette-result]",
    ),
    OverlayCase("quick-add", "/", '[data-global-open="quick-add"]', "#quick-add[open]"),
    OverlayCase(
        "shortcut-list", "/inbox", '[data-global-open="shortcut-help"]', "#shortcut-help[open]"
    ),
    OverlayCase("remove-dialog", "/inbox", "#inbox-remove", "#remove-dialog[open]"),
    OverlayCase("job-drawer", "/inbox", "#inbox-details-button", "#job-details[open]"),
    OverlayCase(
        "board-status-menu",
        "/board",
        ".board-menu-button",
        '.board-menu-button[aria-expanded="true"] + form [role="menu"]:not([hidden])',
    ),
    OverlayCase(
        "application-drawer",
        "/board",
        ".board-card a[href^='/applications/']",
        "#application-drawer[open]",
    ),
    OverlayCase(
        "tasks-panel",
        "/",
        "#tasks-toggle",
        "#tasks-popover:not([hidden]) #tasks-panel > :not(#tasks-loading)",
    ),
    OverlayCase(
        "delete-all-data", "/settings/data", "#data-delete-open", "#data-delete-dialog[open]"
    ),
    OverlayCase(
        "notifications-panel",
        "/",
        "#notifications-toggle",
        "#notifications-popover:not([hidden]) #notifications-heading",
    ),
)


@cache
def _demo() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(DATASET.read_bytes())
    return payload


def demo_rows(table: str) -> list[dict[str, Any]]:
    found = _demo()["tables"][table]
    return [dict(zip(found["columns"], row, strict=True)) for row in found["rows"]]


def _application(status: str) -> str:
    return str(next(a["id"] for a in demo_rows("applications") if a["status_key"] == status))


def _interview() -> tuple[str, str]:
    event = next(e for e in demo_rows("events") if e["type"] == "interview")
    return str(event["application_id"]), str(event["id"])


def _fact(status: str) -> str:
    return str(next(f["id"] for f in demo_rows("facts") if f["status"] == status))


def page_cases() -> tuple[PageCase, ...]:
    interviewing = _application("interviewing")
    with_interview, interview = _interview()
    cases = [
        # Today lists what is due: its first action (earlier screens may have changed the list).
        PageCase("today", "/", "/", ("css", "#today-content :is(a[href], button)")),
        # The first card's Apply, so the walk passes the add form and reaches a card.
        PageCase("inbox", "/inbox", "/inbox", ("css", "#inbox-card [data-action='apply']")),
        PageCase("inbox-values", "/inbox/values", "/inbox/values", ("link", "Back to Job inbox")),
        PageCase("board", "/board", "/board", ("css", ".board-card a[href^='/applications/']")),
        PageCase(
            "companies", "/companies", "/companies", ("link", "Brightwater Fictional Greenhouses")
        ),
        PageCase(
            "company",
            "/companies/{company_id}",
            "/companies/demo-company-brightwater",
            ("css", "#company-brief-start"),
        ),
        PageCase("facts", "/facts", "/facts", ("link", "Add a fact")),
        PageCase("fact-new", "/facts/new", "/facts/new", ("button", "Save")),
        PageCase(
            "fact-edit",
            "/facts/{fact_id}/edit",
            f"/facts/{_fact('confirmed')}/edit",
            ("button", "Save"),
        ),
        PageCase(
            "fact-merge",
            "/facts/{draft_id}/merge",
            f"/facts/{_fact('draft')}/merge",
            ("css", "#merge-q"),
        ),
        PageCase(
            "job", "/jobs/{job_id}", "/jobs/demo-job-01", ("css", "#job-fit-start-demo-job-01")
        ),
        PageCase(
            "application",
            "/applications/{application_id}",
            f"/applications/{interviewing}",
            ("css", "#detail-status-button"),
        ),
        PageCase(
            "application-tailoring",
            "/applications/{application_id}",
            f"/applications/{interviewing}?tab=tailoring",
            ("css", "#gap-check-start"),
        ),
        PageCase(
            "application-prep",
            "/applications/{application_id}",
            f"/applications/{interviewing}?tab=prep",
            ("css", "#prep-generate"),
        ),
        PageCase(
            "debrief",
            "/applications/{application_id}/debriefs/{event_id}",
            f"/applications/{with_interview}/debriefs/{interview}",
            ("css", "#debrief-save"),
        ),
        # Start, or continue the session an earlier screen started.
        PageCase(
            "session-new",
            "/sessions/new",
            "/sessions/new",
            ("css", "#session-start, main a[href^='/sessions/']"),
        ),
        PageCase(
            "session",
            "/sessions/{sid}",
            "/sessions/new",
            ("css", "#session-panel textarea"),
            via=("css", "#session-start"),
            arrives=r"/sessions/(?!new$)[A-Za-z0-9_-]+$",
            ready="#session-panel textarea",
        ),
        PageCase(
            "settings-prompt",
            "/settings/prompts/{task}",
            "/settings/prompts/summarize_job",
            ("css", "#prompt-save"),
        ),
    ]
    cases.extend(PageCase(name, url, url, primary) for url, name, primary in SETTINGS_AREAS)
    cases.append(PageCase("setup", "/setup", "/setup", SETUP_STEPS[0][1]))
    cases.extend(
        PageCase(f"setup-{step}", "/setup/{step_id}", f"/setup/{step}", primary)
        for step, primary in SETUP_STEPS
    )
    return tuple(cases)


PAGE_CASES = page_cases()
