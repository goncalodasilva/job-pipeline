"""Shared helpers for the M9.2 part B tests of Settings > Appearance (AC-M9.2; ADR-131 items 6-7;
ADR-042 follow-up; ADR-125 item 5; TD-085 density only). Every value is fictional. No application
module of part B is imported at module level, so a missing route, template or model fails the test
that needs it, not the collection.

What the tests rely on (the contract; the setting, the route and the templates implement it)

- The setting: ``app.domain.settings.AppearanceSettings`` (frozen, strict, ``extra="forbid"``) with
  ``theme`` in ``system | light | dark`` (default ``system``) and ``density`` in
  ``comfortable | compact`` (default ``comfortable``), and ``APPEARANCE = SettingSpec("appearance",
  AppearanceSettings, AppearanceSettings())``. It is stored by ``SettingsService`` as the versioned
  ``appearance`` setting (version 0 until saved). No migration.
- The root element of every page (the shell, the setup guide and the "not found" page of the
  guide included) is ``<html lang="en" data-theme="..." data-density="...">`` rendered from the
  stored setting: ``data-theme`` is ``system``, ``light`` or ``dark`` (``system`` is written as the
  word ``system``; the stylesheet treats anything but ``light`` and ``dark`` as "follow the
  device"), ``data-density`` is ``comfortable`` or ``compact``. A page that cannot read the
  setting shows the defaults.
- ``GET /settings/appearance`` is the token-free, read-only area page (part A). Its main content
  holds ``<section id="appearance-settings" class="settings-section"
  aria-labelledby="appearance-heading">`` with ``<h2 id="appearance-heading">`` and ``<form
  id="appearance-form" method="post" action="/settings/appearance"
  hx-post="/settings/appearance">``. The form has a hidden ``version`` (the stored version,
  ``0`` when nothing is stored), a
  ``<select id="appearance-theme" name="theme">`` named "Theme" (options ``system`` "System",
  ``light`` "Light", ``dark`` "Dark") and a ``<select id="appearance-density" name="density">``
  named "Density" (options ``comfortable`` "Comfortable", ``compact`` "Compact"), each with a
  ``<label for>``, the stored value selected, and one ``<button type="submit">`` named "Save".
- ``POST /settings/appearance`` (launch token and origin) with ``version``, ``theme`` and
  ``density``, each exactly once and nothing else (anything else is a plain-text 400). The
  version is checked before the values (ADR-125 item 5):
    * stale version -> 409, nothing saved, the section answers with
      ``<p id="appearance-conflict" role="alert" data-autofocus>`` holding the conflict message
      and a "Reload" link to ``/settings/appearance``, and no form that could carry a version;
    * a value outside the lists above (any other spelling, case or an empty value) -> 422,
      nothing saved, each refused select ``aria-invalid="true"`` and tied by
      ``aria-describedby`` to a ``role="alert"`` message (``appearance-theme-error``,
      ``appearance-density-error``), the first refused select the one ``data-autofocus`` element,
      the refused value never shown back, the live region told "... not saved ...";
    * success -> the setting is saved with ``expected_version=version``; htmx gets 200 with the
      section only (no ``<html>``): ``<p id="appearance-done">Appearance saved.</p>``, the
      heading (``h2``) is the one ``data-autofocus`` element, and ``HX-Trigger`` carries
      ``announce`` ("Appearance saved.") and ``appearance:changed`` with the detail
      ``{"theme": ..., "density": ...}``, which ``app.js`` applies to ``<html>`` at once. A plain
      request gets 303 to ``/settings/appearance``. A plain-form 409 or 422 shows the whole area
      page.
- Saving touches the ``appearance`` row only.
"""

import html
import json
import re
from collections.abc import Callable
from typing import Any, Final

from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.api.security import PAGE_TAG
from tests.board_pages import by_id, elements
from tests.integration.test_app_shell import Element

PATH: Final = "/settings/appearance"
SECTION_ID: Final = "appearance-settings"
HEADING_ID: Final = "appearance-heading"
DONE_ID: Final = "appearance-done"
CONFLICT_ID: Final = "appearance-conflict"
FORM_ID: Final = "appearance-form"
THEME_ID: Final = "appearance-theme"
DENSITY_ID: Final = "appearance-density"
SAVED: Final = "Appearance saved."
CHANGED_EVENT: Final = "appearance:changed"

THEMES: Final = {"system": "System", "light": "Light", "dark": "Dark"}
DENSITIES: Final = {"comfortable": "Comfortable", "compact": "Compact"}
DEFAULTS: Final = {"theme": "system", "density": "comfortable"}
GOOD: Final = {"theme": "dark", "density": "compact"}
HOSTILE: Final = '"><script id="injected">1</script>'

_HTML_TAG: Final = re.compile(r"<html\b([^>]*)>", re.IGNORECASE)
_ATTR: Final = re.compile(r'([a-zA-Z-]+)\s*=\s*"([^"]*)"')


def html_attrs(markup: str) -> dict[str, str]:
    """The attributes of the page's ``<html>`` element."""
    found = _HTML_TAG.search(markup)
    assert found is not None, "no <html> element in the answer"
    return {name: html.unescape(value) for name, value in _ATTR.findall(found.group(1))}


def appearance_attrs(markup: str) -> dict[str, str]:
    """``lang``, ``data-theme`` and ``data-density`` of the page's ``<html>``."""
    attrs = html_attrs(markup)
    return {name: attrs.get(name, "<missing>") for name in ("lang", "data-theme", "data-density")}


def expected_attrs(theme: str, density: str) -> dict[str, str]:
    return {"lang": "en", "data-theme": theme, "data-density": density}


def section(markup: str) -> Element:
    found = by_id(markup, SECTION_ID)
    assert found is not None, f"no #{SECTION_ID} in the answer"
    assert found.tag == "section"
    return found


def inside(markup: str, container: Element) -> list[Element]:
    return [e for e in elements(markup) if e.within(container)]


def control(markup: str, name: str) -> Element:
    found = [
        e
        for e in inside(markup, section(markup))
        if e.tag == "select" and e.attrs.get("name") == name
    ]
    assert len(found) == 1, f"expected one select named {name}, found {len(found)}"
    return found[0]


def options(markup: str, select: Element) -> list[tuple[str, str]]:
    return [
        (str(e.attrs.get("value")), e.text)
        for e in elements(markup)
        if e.tag == "option" and e.within(select)
    ]


def selected(markup: str, select: Element) -> list[str]:
    return [
        str(e.attrs.get("value"))
        for e in elements(markup)
        if e.tag == "option" and e.within(select) and "selected" in e.attrs
    ]


def shown(markup: str) -> dict[str, Any]:
    """What the form shows: the selected theme and density and the hidden version."""
    found = {name: selected(markup, control(markup, name)) for name in ("theme", "density")}
    assert all(len(values) == 1 for values in found.values()), found
    versions = [
        e
        for e in inside(markup, section(markup))
        if e.tag == "input" and e.attrs.get("name") == "version"
    ]
    assert len(versions) == 1, "the form carries one hidden version"
    assert versions[0].attrs.get("type") == "hidden"
    return {
        "theme": found["theme"][0],
        "density": found["density"][0],
        "version": versions[0].attrs.get("value"),
    }


def label_of(markup: str, field: Element) -> str:
    field_id = field.attrs.get("id")
    assert field_id, "the control has no id for its label"
    labels = [e for e in elements(markup) if e.tag == "label" and e.attrs.get("for") == field_id]
    assert len(labels) == 1, f"expected one label for #{field_id}"
    return labels[0].text


def focus_targets(markup: str) -> list[Element]:
    return [e for e in inside(markup, section(markup)) if "data-autofocus" in e.attrs]


def invalid_names(markup: str) -> list[str]:
    return [
        str(e.attrs.get("name"))
        for e in inside(markup, section(markup))
        if e.attrs.get("aria-invalid") == "true"
    ]


def error_text(markup: str, field: Element) -> str:
    """The ``role="alert"`` message a refused select names with ``aria-describedby``."""
    described = (field.attrs.get("aria-describedby") or "").split()
    texts = []
    for identifier in described:
        found = by_id(markup, identifier)
        if found is not None and found.attrs.get("role") == "alert":
            texts.append(found.text)
    assert len(texts) == 1 and texts[0], f"#{field.attrs.get('id')} has no error tied to it"
    return texts[0]


def triggers(headers: Any) -> dict[str, Any]:
    raw = headers.get("HX-Trigger")
    assert raw, "the answer has no HX-Trigger"
    value = json.loads(raw)
    assert isinstance(value, dict)
    return value


def seeded_page_paths(app: FastAPI) -> dict[str, str]:
    """Every ``PAGE_TAG`` route of ``app`` (a finished setup, the lifespan started) with its path
    parameters filled by fictional rows made through the app's own services:
    route path -> concrete path."""
    from app.modules.storage import applications_repo, sessions_repo
    from tests.fact_bank import seed_page_ids
    from tests.integration.test_setup_redirect import START
    from tests.job_fixtures import generate_jobs, seed_jobs

    params: dict[str, str] = {"step_id": "welcome", **seed_page_ids(app.state.facts)}
    with app.state.workspace.db.transaction() as conn:
        sessions_repo.insert_session(
            conn, id="session-page", focus_kind="most_useful", length=3, now=START.isoformat()
        )
    params["sid"] = "session-page"
    seed_jobs(app.state.workspace.db, generate_jobs(1))
    params["job_id"] = "job-0001"
    params["company_id"] = "company-0001"
    params["application_id"] = app.state.applications.apply_to_job(
        "job-0001", expected_job_version=1
    ).application.id
    with app.state.workspace.db.transaction() as conn:
        applications_repo.insert_event(
            conn,
            id="page-debrief-event",
            application_id=params["application_id"],
            type="interview",
            starts_at="2026-01-02T09:00:00+00:00",
            ends_at="2026-01-02T10:00:00+00:00",
            time_zone="UTC",
            now=START.isoformat(),
        )
    params["event_id"] = "page-debrief-event"

    paths: dict[str, str] = {}
    for route in app.router.routes:
        if not isinstance(route, APIRoute) or PAGE_TAG not in (route.tags or []):
            continue
        if "GET" not in route.methods:
            continue
        path = route.path
        for name in route.param_convertors:
            assert name in params, f"no value for {{{name}}} in {route.path}"
            path = path.replace(f"{{{name}}}", params[name])
        paths[route.path] = path
    return paths


def get_all(
    get: Callable[[str], Any], paths: dict[str, str]
) -> dict[str, tuple[int, dict[str, str]]]:
    """GET each path; route path -> (status, the ``<html>`` appearance attributes)."""
    found: dict[str, tuple[int, dict[str, str]]] = {}
    for route, path in paths.items():
        response = get(path)
        if response.status_code == 303:  # "/setup" sends the user on to the current step
            response = get(response.headers["location"])
        found[route] = (response.status_code, appearance_attrs(response.text))
    return found
