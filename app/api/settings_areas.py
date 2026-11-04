"""The Settings area pages other than Providers and AI and Data and privacy (M9.2 part A;
ADR-131 item 7; TD-308; detailed design "Settings areas").

Settings is a set of area pages (``app/api/settings_nav.py`` lists them). Each is a ``PAGE_TAG``
route: token-free and read only, and ``GET`` and ``HEAD`` write nothing (spec 2.9 rule 12). With
``HX-Request: true`` it answers the main content only, which needs the token. Everything that
changes data, and every part of a page that is loaded as a fragment, is a separate protected
route that keeps its path (spec 2.9 rules 2, 3 and 13):

- ``/settings/budgets`` Usage budgets (``app/api/settings_usage.py``).
- ``/settings/preferences`` Job preferences: a shell (heading, a loading section and a link to
  the setup guide's preferences step). The stored preferences are personal job-search data, so
  the page holds none of them (spec 2.9 rule 13): the section fetches the protected, read-only
  ``GET /settings/preferences/summary``, which answers the summary and needs the token. There
  is no form.
- ``/settings/lists`` Lists and statuses (``app/api/settings_board.py``).
- ``/settings/sessions`` Sessions (the ``POST`` on the same path is in
  ``app/api/settings_sessions.py``; the route that is dispatched decides whether the token
  is needed).
- ``/settings/your-details`` Your details (a placeholder that loads ``GET /settings/details``) and
  CV templates.
- ``/settings/appearance`` Appearance (the ``POST`` on the same path is in
  ``app/api/settings_appearance.py``).
- ``/settings/keyboard`` Keyboard shortcuts (saved by ``POST /settings/shortcuts``).

Providers and AI is ``/settings`` (``app/api/settings.py``) and Data and privacy
``/settings/data`` (``app/api/settings_data.py``). The answer to a form of an area is that
form's section for htmx, and the whole area page otherwise (a refused plain form), so this module
also renders the Usage budgets, Board and Sessions forms' answers. Rendering reads the database,
so these are plain functions that FastAPI runs in its thread pool.
"""

from collections.abc import Callable
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from app.api.appearance_forms import AppearanceState, appearance_context
from app.api.board_settings_forms import BoardSettingsState, board_context
from app.api.lists_forms import ListsState, lists_context
from app.api.pages import AREAS, area
from app.api.security import PAGE_TAG
from app.api.session_settings_forms import SessionSettingsState, sessions_context
from app.api.settings_nav import (
    AREA_FRAGMENT,
    AREA_PAGE,
    SETTINGS_AREAS,
    SettingsArea,
    area_ctx,
)
from app.api.settings_templates import section_context
from app.api.setup_redirect import setup_step_path
from app.api.templating import render, renders_fragment, templates
from app.api.usage_forms import UsageFormState, panel_context, usage_context
from app.domain.locales import COUNTRIES
from app.domain.settings import JobPreferences
from app.onboarding.steps.preferences import WORK_MODE_LABELS
from app.services.job_preferences import JobPreferencesView

BOARD_FRAGMENT: Final = "fragments/settings/_board.html"
LISTS_FRAGMENT: Final = "fragments/settings/_lists.html"
SESSIONS_FRAGMENT: Final = "fragments/settings/_sessions.html"
APPEARANCE_FRAGMENT: Final = "fragments/settings/_appearance.html"
PREFERENCES_STEP: Final = "preferences"
PREFERENCES_SUMMARY_PATH: Final = "/settings/preferences/summary"
PREFERENCES_SUMMARY_FRAGMENT: Final = "fragments/settings/_preferences_summary.html"
# Providers and AI and Data and privacy register their own pages.
OTHER_MODULES: Final = frozenset({"providers", "data"})
COUNTRY_NAMES: Final = dict(COUNTRIES)


def register_settings_area_pages(app: FastAPI) -> None:
    """Add the area pages directly to the app (not through an included router)."""
    for item in SETTINGS_AREAS:
        if item.slug in OTHER_MODULES:
            continue
        app.add_api_route(
            item.path,
            _page_for(item),
            methods=["GET", "HEAD"],
            tags=[PAGE_TAG],
            response_class=HTMLResponse,
            name=f"page_settings_{item.slug.replace('-', '_')}",
        )
    # Not a page (no PAGE_TAG): the token is required, and nothing is written.
    app.add_api_route(
        PREFERENCES_SUMMARY_PATH,
        show_preferences_summary,
        methods=["GET"],
        response_class=HTMLResponse,
        name="settings_preferences_summary",
    )


def _page_for(item: SettingsArea) -> Callable[[Request], HTMLResponse]:
    def page(request: Request) -> HTMLResponse:
        return render_area(request, item.slug)

    return page


def render_settings(request: Request, *, fragment: str, usage: UsageFormState) -> HTMLResponse:
    """The answer to one of the Usage budgets forms: ``fragment`` for htmx, the whole Usage
    budgets page otherwise, with ``usage`` saying what happened to the form. Blocking."""
    return render_area(request, "budgets", fragment=fragment, usage=usage)


def render_board_settings(request: Request, state: BoardSettingsState) -> HTMLResponse:
    """The answer to one of the Lists and statuses forms: the section for htmx, the whole page
    otherwise, with ``state`` saying what happened. Blocking."""
    return render_area(request, "lists", fragment=BOARD_FRAGMENT, board=state)


def render_lists_settings(request: Request, state: ListsState) -> HTMLResponse:
    """The answer to one of the lists editor's forms: the editor section for htmx, the whole
    page otherwise, with ``state`` saying what happened. Blocking."""
    return render_area(request, "lists", fragment=LISTS_FRAGMENT, lists=state)


def render_session_settings(request: Request, state: SessionSettingsState) -> HTMLResponse:
    """The answer to the Sessions form: the section for htmx, the whole page otherwise, with
    ``state`` saying what happened. Blocking."""
    return render_area(request, "sessions", fragment=SESSIONS_FRAGMENT, sessions=state)


def render_appearance_settings(request: Request, state: AppearanceState) -> HTMLResponse:
    """The answer to the Appearance form: the section for htmx, the whole page otherwise, with
    ``state`` saying what happened. Blocking."""
    return render_area(request, "appearance", fragment=APPEARANCE_FRAGMENT, appearance=state)


def render_area(
    request: Request,
    slug: str,
    *,
    fragment: str | None = None,
    usage: UsageFormState | None = None,
    board: BoardSettingsState | None = None,
    lists: ListsState | None = None,
    sessions: SessionSettingsState | None = None,
    appearance: AppearanceState | None = None,
) -> HTMLResponse:
    """An area's full page, or for htmx its main content (a page GET) or ``fragment`` (a form's
    section). What an area reads is read only when the answer shows it. Blocking."""
    whole = fragment is None or not renders_fragment(request)
    ctx: dict[str, Any] = {"area": area("settings"), "areas": AREAS}
    focus = True
    if slug == "budgets":
        if whole or usage is not None:
            ctx["usage"] = usage_context(request, usage)
            focus = not ctx["usage"]["target"]
        if whole:
            ctx["usage_panel"] = panel_context(request)
    elif slug == "lists":
        if whole or board is not None:
            ctx["board"] = board_context(request, board)
            focus = not ctx["board"]["attention"]
        if whole or lists is not None:
            ctx["lists"] = lists_context(request, lists)
            focus = focus and not ctx["lists"]["attention"]
    elif slug == "sessions":
        if whole or sessions is not None:
            ctx["sessions"] = sessions_context(request, sessions)
            focus = not ctx["sessions"]["attention"]
    elif slug == "appearance":
        ctx["appearance_form"] = appearance_context(request, appearance)
        focus = not ctx["appearance_form"]["attention"]
    elif slug == "your-details":
        ctx["cv_templates"] = section_context(request)  # holds no personal data
    elif slug == "keyboard":
        ctx.update(
            shortcuts=request.app.state.inbox_views.shortcuts(),
            shortcuts_saved=False,
            shortcuts_conflict=False,
            conflict_message="",
        )
    elif slug == "preferences":  # a shell: the values come with the protected fragment
        ctx["preferences_guide_url"] = setup_step_path(PREFERENCES_STEP)
        ctx["preferences_summary_url"] = PREFERENCES_SUMMARY_PATH
    ctx.update(area_ctx(slug, heading_focus=focus))
    return render(request, page=AREA_PAGE, fragment=fragment or AREA_FRAGMENT, ctx=ctx)


def show_preferences_summary(request: Request) -> HTMLResponse:
    """The protected summary section that replaces the placeholder on the Job preferences page.
    A plain function: FastAPI runs it in its thread pool (it reads the database)."""
    return templates.TemplateResponse(
        request, PREFERENCES_SUMMARY_FRAGMENT, {"preferences": preferences_context(request)}
    )


def preferences_context(request: Request) -> dict[str, Any] | None:
    """The stored job preferences as words, for the read-only summary (None when nothing is
    stored). Blocking (reads the database)."""
    reader: JobPreferencesView = request.app.state.job_preferences
    saved = reader.load()
    return None if saved is None else _summary(saved)


def _summary(saved: JobPreferences) -> dict[str, Any]:
    salary = saved.minimum_salary
    country = COUNTRY_NAMES.get(saved.cv_country)
    return {
        "target_roles": list(saved.target_roles),
        "locations": list(saved.locations),
        "work_modes": [WORK_MODE_LABELS.get(mode, mode) for mode in saved.work_modes],
        "minimum_salary": None if salary is None else f"{salary.amount:,} {salary.currency}",
        "must_haves": list(saved.must_haves),
        "deal_breakers": list(saved.deal_breakers),
        "cv_country": f"{country} ({saved.cv_country})" if country else saved.cv_country,
    }
