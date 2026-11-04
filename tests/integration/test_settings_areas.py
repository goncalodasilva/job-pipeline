"""M9.2 part A: Settings becomes area pages (AC-M9.2; ADR-131 items 6-7; TD-308; detailed design
"Settings and usage budgets", the Settings areas table; spec 2.9 rules 2, 3, 12 and 13).

The areas, the paths, the side list and the markup these tests rely on are described in the
docstring of ``tests/settings_areas.py``. Real SQLite in a temp folder, the app through
``TestClient``, no network. Every value is fictional. New names are imported inside the tests
that need them, so each test fails on its own while they are missing.

The tests that moved sections into areas also update the existing Settings tests; the list is in
the task Evidence of M9.2 part A.
"""

import re
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.api.security import PAGE_TAG
from app.domain.settings import JobPreferences, Salary
from app.modules.secrets.providers.keyring_store import SERVICE_NAME, KeyringSecretStore
from tests.board_pages import by_id, elements
from tests.data_pages import get_token, make_app, page_text, prepare, state_of
from tests.fakes.clock import FrozenClock
from tests.fakes.keyring_backends import MemoryKeyring
from tests.integration.test_data_erasure import SECRET_VALUES, planted_keys
from tests.integration.test_setup_preferences_done import SAVED as PREFERENCES
from tests.integration.test_setup_redirect import BASE_URL, START, Running
from tests.settings_areas import (
    AREAS,
    BY_SLUG,
    COMING_LATER,
    DATA_LINK,
    FRAGMENT_PATHS,
    GUIDE_PREFERENCES,
    MOVED_FROM_SETTINGS,
    SETTINGS_NAV_LABEL,
    SIDEBAR_LABEL,
    SLUGS,
)
from tests.setup_harness import seed_workspace

pytestmark = pytest.mark.integration

HTMX = {"HX-Request": "true"}
HOSTILE_ROLE = "<b>Fictional</b> role <script>alert(1)</script>"


@contextmanager
def area_app(
    root: Path,
    *,
    preferences: JobPreferences | None = PREFERENCES,
    backend: MemoryKeyring | None = None,
) -> Iterator[Running]:
    """The app on a finished setup with the fake provider and, unless ``preferences`` is None,
    job preferences saved."""
    data = prepare(root, empty=False)
    if preferences is not None:
        seed_workspace(data, clock=FrozenClock(START), preferences=preferences)
    app = make_app(root, backend=backend)
    with TestClient(app, base_url=BASE_URL, follow_redirects=False) as client:
        yield Running(app=app, client=client, hits=Counter())


def main_of(markup: str) -> list[Any]:
    """The elements of the main content: inside ``<main>`` on a full page, all of an htmx
    answer."""
    found = elements(markup)
    mains = [e for e in found if e.tag == "main"]
    if not mains:
        return found
    return [e for e in found if e.within(mains[0])]


def settings_nav(markup: str) -> Any:
    navs = [
        e
        for e in main_of(markup)
        if e.tag == "nav" and e.attrs.get("aria-label") == SETTINGS_NAV_LABEL
    ]
    assert len(navs) == 1, (
        f"expected one <nav aria-label={SETTINGS_NAV_LABEL!r}> in the main content"
    )
    return navs[0]


def nav_links(markup: str) -> list[Any]:
    nav = settings_nav(markup)
    return [e for e in main_of(markup) if e.tag == "a" and e.within(nav)]


def token_only_in_body_attribute(text: str, token: str) -> bool:
    """A full page carries the launch token only in ``<body hx-headers>`` (how htmx sends it)."""
    head, body_open, rest = text.partition("<body")
    tag, _, after = rest.partition(">")
    return bool(body_open) and token not in head and token not in after and token in tag


# --- the pages: token-free, read only --------------------------------------------------------


@pytest.mark.parametrize("slug", SLUGS)
def test_settings_areas_each_token_free_page_and_read_only(tmp_path: Path, slug: str) -> None:
    """Every area is a page that loads without the token and reads only; its htmx content needs
    the token. The launch token is only in the body's ``hx-headers`` of a full page (never in
    the htmx content), and neither the data folder's path nor a secret is in any of it."""
    area = BY_SLUG[slug]
    root = tmp_path / "fictional-fjord-root-4417"
    backend = MemoryKeyring()
    planted_keys(KeyringSecretStore(SERVICE_NAME, backend=backend))
    with area_app(root, backend=backend) as run:
        data = root / "data"
        before = state_of(run, data)
        needles = {str(data), str(data.resolve()), root.name, *SECRET_VALUES.values()}

        for method in ("GET", "HEAD"):
            assert run.client.request(method, area.path).status_code == 200, (method, area.path)
        full = run.client.get(area.path)
        assert full.status_code == 200 and "<html" in full.text
        assert token_only_in_body_attribute(full.text, run.token), "the token is in hx-headers only"

        assert run.client.get(area.path, headers=HTMX).status_code == 403, "htmx needs the token"
        part = get_token(run, area.path, htmx=True)
        assert part.status_code == 200 and "<html" not in part.text
        assert run.token not in part.text, "the htmx content holds no token"
        plain_with_token = get_token(run, area.path)
        assert plain_with_token.status_code == 200

        for label, answer in {"page": full, "htmx": part, "token only": plain_with_token}.items():
            for needle in needles:
                assert needle not in answer.text, f"{label} holds {needle[:12]}"
            for name, value in answer.headers.items():
                assert not any(n in value for n in needles), f"{label} header {name}"

        # Only the verbs a page is for; a refused verb changes nothing either.
        token_headers = run.action()
        for method in ("PUT", "PATCH", "DELETE"):
            answer = run.client.request(method, area.path, headers=token_headers)
            assert answer.status_code in (404, 405), (method, area.path)
        assert state_of(run, data) == before, "reading an area writes nothing"


def test_settings_area_pages_are_page_routes_and_fragments_are_not(tmp_path: Path) -> None:
    """Exactly the areas are ``PAGE_TAG`` routes under ``/settings`` (ADR-131 item 7); the
    fragment paths (``/settings/usage``, ``/settings/details``, ``/settings/shortcuts``, ...)
    stay token-protected GETs and are not pages."""
    with area_app(tmp_path / "root") as run:
        pages = {
            route.path
            for route in run.app.routes
            if isinstance(route, APIRoute)
            and route.path.startswith("/settings")
            and PAGE_TAG in (route.tags or [])
        }
        assert pages == {area.path for area in AREAS}, pages
        for route in run.app.routes:
            if isinstance(route, APIRoute) and route.path in FRAGMENT_PATHS:
                assert PAGE_TAG not in (route.tags or []), route.path


def test_settings_area_fragments_need_token(tmp_path: Path) -> None:
    """Each protected GET fragment of the areas answers 403 without the token (with or without
    ``HX-Request``) and the fragment with it; the pages load them, and reading writes nothing.
    ``/settings/usage``, ``/settings/details`` and ``/settings/shortcuts`` are fragments, not
    area pages."""
    with area_app(tmp_path / "root") as run:
        data = tmp_path / "root" / "data"
        before = state_of(run, data)
        for path in FRAGMENT_PATHS:
            for headers in ({}, HTMX):
                refused = run.client.get(path, headers=headers)
                assert refused.status_code == 403, (path, headers)
                assert run.token not in refused.text
            for htmx in (False, True):
                answer = get_token(run, path, htmx=htmx)
                assert answer.status_code == 200, (path, htmx, answer.status_code)
                assert "<html" not in answer.text, (path, "a fragment is never a full page")
        assert state_of(run, data) == before, "reading a fragment writes nothing"

        # The areas fetch their protected parts; the pages themselves hold none of the values.
        details = by_id(run.client.get(BY_SLUG["your-details"].path).text, "details-settings")
        assert details is not None and details.attrs.get("hx-get") == "/settings/details"
        assert details.attrs.get("hx-trigger") == "load"
        panel = by_id(run.client.get(BY_SLUG["budgets"].path).text, "usage-panel")
        assert panel is not None and panel.attrs.get("hx-get") == "/settings/usage"


# --- the side list of areas -------------------------------------------------------------------


@pytest.mark.parametrize("slug", SLUGS)
def test_settings_area_nav_marks_current(tmp_path: Path, slug: str) -> None:
    """A side list of the areas is part of the main content (so an htmx answer carries it):
    one link per area in order, ``aria-current="page"`` on the current area only, and the main
    sidebar's Settings entry stays current on every area page. One ``<h1 id="page-heading">``
    names the area and is the only focus target of a plain load."""
    area = BY_SLUG[slug]
    with area_app(tmp_path / "root") as run:
        full = run.client.get(area.path)
        part = get_token(run, area.path, htmx=True)
        assert full.status_code == 200 and part.status_code == 200
        for label, text in (("page", full.text), ("htmx", part.text)):
            links = nav_links(text)
            listed = [(link.text, link.attrs.get("href")) for link in links]
            wanted = [(a.label, a.path) for a in AREAS]
            order = [listed.index(pair) for pair in wanted if pair in listed]
            assert len(order) == len(wanted), f"{label}: every area is listed: {listed}"
            assert order == sorted(order), f"{label}: in the order of the design: {listed}"
            current = [link for link in links if "aria-current" in link.attrs]
            assert [
                (c.text, c.attrs.get("href"), c.attrs.get("aria-current")) for c in current
            ] == [(area.label, area.path, "page")], label

            headings = [e for e in main_of(text) if e.tag == "h1"]
            assert len(headings) == 1, f"{label}: one <h1>"
            assert headings[0].attrs.get("id") == "page-heading"
            wanted = "Settings" if slug == "providers" else area.label
            assert headings[0].text == wanted, (headings[0].text, wanted)
            focused = [e for e in main_of(text) if "data-autofocus" in e.attrs]
            assert focused == headings, f"{label}: a plain load focuses the <h1> only"

        sidebar = [
            e
            for e in elements(full.text)
            if e.tag == "nav" and e.attrs.get("aria-label") == SIDEBAR_LABEL
        ]
        assert len(sidebar) == 1
        entries = [
            e
            for e in elements(full.text)
            if e.tag == "a" and e.within(sidebar[0]) and e.attrs.get("aria-current") == "page"
        ]
        assert [(e.text, e.attrs.get("href")) for e in entries] == [("Settings", "/settings")]


def test_settings_area_nav_lists_only_routes_that_exist(tmp_path: Path) -> None:
    """Every link of the side list resolves without the token (an area appears only once its
    route exists; Prompts is added by M9.3)."""
    with area_app(tmp_path / "root") as run:
        for link in nav_links(run.client.get("/settings").text):
            href = str(link.attrs.get("href"))
            assert href.startswith("/settings"), href
            assert run.client.get(href).status_code == 200, href


# --- /settings is Providers and AI ----------------------------------------------------------


def test_settings_slash_shows_providers_and_ai(tmp_path: Path) -> None:
    """``/settings`` holds Providers and AI: the providers section first after the heading and
    the side list, Testing tools and the setup guide link; the "Coming later" card is gone, and
    the other areas' sections are not on it."""
    with area_app(tmp_path / "root") as run:
        for htmx in (False, True):
            response = (
                get_token(run, "/settings", htmx=True) if htmx else run.client.get("/settings")
            )
            assert response.status_code == 200
            found = main_of(response.text)
            sections = [
                e
                for e in found
                if e.tag == "section"
                and "settings-section" in str(e.attrs.get("class") or "").split()
            ]
            assert sections, "settings sections"
            assert sections[0].attrs.get("id") == "providers", "Providers and AI comes first"
            heading = next(e for e in found if e.tag == "h1")
            assert found.index(heading) < found.index(sections[0])
            assert by_id(response.text, "testing-tools") is not None, "Testing tools stays here"
            guide = [
                e for e in found if e.tag == "a" and "re-run the setup guide" in e.text.lower()
            ]
            assert len(guide) == 1
            assert COMING_LATER.lower() not in page_text(response.text).lower()
            assert "settings-later" not in response.text
            for owned in MOVED_FROM_SETTINGS:
                assert by_id(response.text, owned) is None, f"#{owned} moved to its own area"
            links = [e for e in found if e.tag == "a" and e.text == DATA_LINK]
            assert len(links) == 1, "one link to Data and privacy: the side list"


# --- every moved control lives on its owning page only ----------------------------------------


@pytest.mark.parametrize("slug", [a.slug for a in AREAS if a.owned])
def test_moved_sections_are_on_their_owning_area_page_only(tmp_path: Path, slug: str) -> None:
    """Each section is on the page of the area that owns it (full page and htmx content) and on
    no other area page."""
    owner = BY_SLUG[slug]
    with area_app(tmp_path / "root") as run:
        for area in AREAS:
            for label, response in (
                ("page", run.client.get(area.path)),
                ("htmx", get_token(run, area.path, htmx=True)),
            ):
                assert response.status_code == 200, (area.path, label)
                for section in owner.owned:
                    found = by_id(response.text, section)
                    if area is owner:
                        assert found is not None, (
                            f"#{section} is missing from {area.path} ({label})"
                        )
                    else:
                        assert found is None, f"#{section} must not be on {area.path} ({label})"


def test_forms_and_loaders_sit_on_the_page_of_their_area(tmp_path: Path) -> None:
    """The POST routes keep their paths; the form that posts to each is on the owning page only
    (the provider forms stay on ``/settings``)."""
    owned_actions = {
        "budgets": {"/settings/usage/budgets", "/settings/usage/concurrency"},
        "lists": {"/settings/board/statuses"},
        "sessions": {"/settings/sessions"},
        "keyboard": set(),
    }
    provider_actions = {"/settings/providers", "/settings/tiers", "/settings/providers/test"}
    with area_app(tmp_path / "root") as run:

        def actions(path: str) -> set[str]:
            found = elements(run.client.get(path).text)
            # A form's action, and the action a submit button overrides ("Test connection"
            # posts to /settings/providers/test through its formaction attribute).
            return {
                str(e.attrs.get("action"))
                for e in found
                if e.tag == "form" and e.attrs.get("action")
            } | {
                str(e.attrs.get("formaction"))
                for e in found
                if e.tag == "button" and e.attrs.get("formaction")
            }

        for slug, wanted in owned_actions.items():
            assert wanted <= actions(BY_SLUG[slug].path), slug
        assert provider_actions <= actions("/settings")
        moved = {a for group in owned_actions.values() for a in group}
        assert not moved & actions("/settings"), "the moved forms are not on /settings"
        for slug, wanted in owned_actions.items():
            assert not provider_actions & actions(BY_SLUG[slug].path), slug
            others = moved - wanted
            assert not others & actions(BY_SLUG[slug].path), slug
        keyboard_forms = [
            e for e in elements(run.client.get("/settings/keyboard").text) if e.tag == "form"
        ]
        assert any(e.attrs.get("hx-post") == "/settings/shortcuts" for e in keyboard_forms)
        assert not any(
            e.attrs.get("hx-post") == "/settings/shortcuts"
            for e in elements(run.client.get("/settings").text)
        )


# --- plain-form fallbacks and in-app links ----------------------------------------------------


def test_plain_form_fallbacks_redirect_to_the_owning_area(tmp_path: Path) -> None:
    """Without htmx a successful save answers 303 to the area that owns the control."""
    from tests.details_pages import GOOD as DETAILS
    from tests.integration.test_settings_usage_budgets import (
        FAKE_SERVICE,
        VALID,
        post_budget,
        post_concurrency,
    )
    from tests.integration.test_setup_provider import FAKE_KIND

    with area_app(tmp_path / "root") as run:
        answers = {
            "/settings/budgets": [
                post_budget(run, FAKE_SERVICE, VALID),
                post_concurrency(run, "2"),
            ],
            "/settings/lists": [
                run.client.post(
                    "/settings/board/statuses",
                    data={"label": "Plain", "phase": "closed"},
                    headers=run.action(),
                )
            ],
            "/settings/sessions": [
                run.client.post(
                    "/settings/sessions",
                    data={
                        "version": "0",
                        "frequency": "weekly",
                        "length": "3",
                        "ask_later_default": "week",
                    },
                    headers=run.action(),
                )
            ],
            "/settings/your-details": [
                run.client.post(
                    "/settings/details", data={"version": "0", **DETAILS}, headers=run.action()
                )
            ],
            "/settings": [
                run.client.post(
                    "/settings/providers",
                    data={"kind": FAKE_KIND, "credential": ""},
                    headers=run.action(),
                )
            ],
        }
        for target, responses in answers.items():
            for response in responses:
                assert response.status_code == 303, (target, response.status_code)
                location = response.headers["location"]
                assert location.split("#")[0].split("?")[0] == target, (target, location)


def test_budget_links_point_to_the_budgets_area() -> None:
    """The link every budget refusal and warning offers ("Change limits", the blocked-action
    reason) is the Usage budgets section on its own page (ADR-131 item 7)."""
    from app.services.budget_gate import LIMITS_URL

    assert LIMITS_URL == "/settings/budgets#usage-budgets"


def test_stale_shortcuts_save_reloads_the_keyboard_area(tmp_path: Path) -> None:
    """The reload prompt of a stale save goes back to the area that owns the control."""
    with area_app(tmp_path / "root") as run:
        headers = run.action(**HTMX)
        assert run.client.post("/settings/shortcuts", data={"version": "0"}, headers=headers)
        stale = run.client.post("/settings/shortcuts", data={"version": "0"}, headers=headers)
        assert stale.status_code == 409, stale.status_code
        reloads = [e for e in elements(stale.text) if e.tag == "a" and "reload" in e.text.lower()]
        assert [e.attrs.get("href") for e in reloads] == ["/settings/keyboard"]


# --- Job preferences: a read-only summary, loaded protected ------------------------------------

SUMMARY = "/settings/preferences/summary"


def summary_text(markup: str) -> str:
    summary = by_id(markup, "preferences-summary")
    assert summary is not None and summary.tag == "dl"
    return " ".join(e.text for e in elements(markup) if e.within(summary) and e.text)


def test_job_preferences_page_is_a_shell_that_loads_the_summary_protected(tmp_path: Path) -> None:
    """Spec 2.9 rule 13: the token-free page carries no preference value. It holds the heading,
    the guide link and a placeholder that fetches ``GET /settings/preferences/summary`` as soon
    as it loads; the page's htmx content is the same shell. It has no form and a read writes
    nothing. (Before the fix pass the summary was in the page itself; see the Evidence.)"""
    root = tmp_path / "root"
    with area_app(root) as run:
        before = state_of(run, root / "data")
        for label, response in (
            ("page", run.client.get("/settings/preferences")),
            ("htmx", get_token(run, "/settings/preferences", htmx=True)),
        ):
            assert response.status_code == 200
            holder = by_id(response.text, "preferences-settings")
            assert holder is not None and holder.tag == "section", label
            assert holder.attrs.get("hx-get") == SUMMARY, label
            assert "load" in str(holder.attrs.get("hx-trigger")), label
            assert holder.attrs.get("hx-swap") == "outerHTML", label
            assert by_id(response.text, "preferences-summary") is None, label
            assert by_id(response.text, "preferences-empty") is None, label
            assert by_id(response.text, "preferences-heading") is not None, label
            link = by_id(response.text, "preferences-guide-link")
            assert link is not None and link.tag == "a", label
            assert link.attrs.get("href") == GUIDE_PREFERENCES
            assert not link.within(holder), "the link stays when the section is replaced"
            assert not [e for e in main_of(response.text) if e.tag in ("form", "input", "textarea")]
            text = page_text(response.text)
            for value in (*PREFERENCES.target_roles, *PREFERENCES.locations):
                assert value not in text, f"{label}: {value!r} is not on the token-free page"
        assert state_of(run, root / "data") == before


def test_job_preferences_summary_fragment_shows_the_stored_summary(tmp_path: Path) -> None:
    """The protected fragment holds the stored preferences as words, read only and with no
    form, and keeps the ids of the section; it writes nothing and is not a full page."""
    root = tmp_path / "root"
    with area_app(root) as run:
        before = state_of(run, root / "data")
        for htmx in (False, True):
            response = get_token(run, SUMMARY, htmx=htmx)
            assert response.status_code == 200 and "<html" not in response.text
            section = by_id(response.text, "preferences-settings")
            assert section is not None and section.tag == "section"
            assert "hx-get" not in section.attrs, "the answer must not reload itself"
            inner = summary_text(response.text)
            for value in (
                *PREFERENCES.target_roles,
                *PREFERENCES.locations,
                *PREFERENCES.must_haves,
                *PREFERENCES.deal_breakers,
            ):
                assert value in inner, f"{value!r} is shown"
            assert re.search(r"45[,.\s]?000", inner) and "EUR" in inner, inner
            assert re.search(r"remote", inner, re.IGNORECASE), "a work mode is shown"
            assert re.search(r"on[\s_-]?site", inner, re.IGNORECASE), "a work mode is shown"
            assert "PT" in inner or "Portugal" in inner, "the CV country is shown"
            assert by_id(response.text, "preferences-empty") is None
            assert not [e for e in elements(response.text) if e.tag in ("form", "input")]
            assert run.token not in response.text
        assert state_of(run, root / "data") == before


def test_job_preferences_summary_fragment_without_preferences_says_so(tmp_path: Path) -> None:
    with area_app(tmp_path / "root", preferences=None) as run:
        page_response = run.client.get("/settings/preferences")
        assert page_response.status_code == 200
        link = by_id(page_response.text, "preferences-guide-link")
        assert link is not None and link.attrs.get("href") == GUIDE_PREFERENCES
        response = get_token(run, SUMMARY)
        assert response.status_code == 200
        empty = by_id(response.text, "preferences-empty")
        assert empty is not None and empty.text, "an empty state in words"
        assert by_id(response.text, "preferences-summary") is None
        assert by_id(response.text, "preferences-settings") is not None


def test_job_preferences_summary_is_escaped(tmp_path: Path) -> None:
    """Stored text is data: hostile markup in a preference stays text."""
    hostile = JobPreferences(
        target_roles=(HOSTILE_ROLE,),
        cv_country="PT",
        minimum_salary=Salary(amount=1, currency="EUR"),
    )
    with area_app(tmp_path / "root", preferences=hostile) as run:
        for response in (get_token(run, SUMMARY), get_token(run, SUMMARY, htmx=True)):
            assert "<script>alert(1)</script>" not in response.text
            assert "<b>Fictional</b>" not in response.text
            assert "Fictional" in page_text(response.text)


SALARY_MARKER = 91_377
LOCATION_MARKER = "Fictional-Fjordtown-4417"
ROLE_MARKER = "Fictional Glacier Surveyor 5521"
MUST_MARKER = "fictional-must-have-6633"
BREAKER_MARKER = "fictional-deal-breaker-7745"


def test_preferences_values_never_reach_a_token_free_page_only_the_protected_fragment(
    tmp_path: Path,
) -> None:
    """Spec 2.9 rule 13: plant distinctive values in the stored preferences. Every token-free page
    of the app (each area page and every other page route, plain, which is what the leak scan
    visits) is free of them; the same pages' htmx content with the token is too; the protected
    fragment holds them; and without the token the fragment is refused."""
    planted = JobPreferences(
        target_roles=(ROLE_MARKER,),
        locations=(LOCATION_MARKER,),
        work_modes=("remote",),
        minimum_salary=Salary(amount=SALARY_MARKER, currency="EUR"),
        must_haves=(MUST_MARKER,),
        deal_breakers=(BREAKER_MARKER,),
        cv_country="PT",
    )
    needles = (
        ROLE_MARKER,
        LOCATION_MARKER,
        MUST_MARKER,
        BREAKER_MARKER,
        str(SALARY_MARKER),
        f"{SALARY_MARKER:,}",
    )
    with area_app(tmp_path / "root", preferences=planted) as run:
        # The fragment holds every marker; it is refused without the token.
        for headers in ({}, HTMX):
            refused = run.client.get(SUMMARY, headers=headers)
            assert refused.status_code == 403, headers
            assert not any(n in refused.text for n in needles)
        loaded = get_token(run, SUMMARY)
        assert loaded.status_code == 200
        for needle in (ROLE_MARKER, LOCATION_MARKER, MUST_MARKER, BREAKER_MARKER):
            assert needle in loaded.text, f"the protected fragment shows {needle!r}"
        assert f"{SALARY_MARKER:,}" in summary_text(loaded.text), "the salary is shown in words"

        # Every token-free page route, as the leak scan visits them.
        paths = sorted(
            {
                route.path
                for route in run.app.routes
                if isinstance(route, APIRoute)
                and PAGE_TAG in (route.tags or [])
                and "GET" in route.methods
                and "{" not in route.path
            }
            | {area.path for area in AREAS}
        )
        assert "/settings/preferences" in paths and "/settings" in paths, paths
        for path in paths:
            for label, response in (
                ("plain", run.client.get(path, follow_redirects=True)),
                ("token only", get_token(run, path)),
                ("htmx", get_token(run, path, htmx=True)),
            ):
                for needle in needles:
                    assert needle not in response.text, f"{path} ({label}) holds {needle!r}"
                for name, value in response.headers.items():
                    assert not any(n in value for n in needles), f"{path} header {name}"


def test_sessions_page_and_save_share_a_path_but_not_the_token_rule(tmp_path: Path) -> None:
    """``GET /settings/sessions`` is the token-free area page; ``POST`` on the same path still
    needs the token and the origin (the route that is dispatched decides, spec 2.9 rule 12)."""
    form = {"version": "0", "frequency": "weekly", "length": "3", "ask_later_default": "week"}
    root = tmp_path / "root"
    with area_app(root) as run:
        before = state_of(run, root / "data")
        assert run.client.get("/settings/sessions").status_code == 200
        for headers in (
            {},
            run.with_token(),
            {"origin": "http://evil.example", **run.with_token()},
        ):
            refused = run.client.post("/settings/sessions", data=form, headers=headers)
            assert refused.status_code == 403, headers
        assert state_of(run, root / "data") == before, "a refused save changes nothing"
        saved = run.client.post("/settings/sessions", data=form, headers=run.action(**HTMX))
        assert saved.status_code == 200


def test_shortcuts_help_links_to_the_keyboard_area_in_both_states(tmp_path: Path) -> None:
    """The inbox help says where to turn the single-key shortcuts off or on: the Keyboard
    shortcuts area (M9.2), not the Providers page."""
    target = "/settings/keyboard#shortcuts-settings"
    with area_app(tmp_path / "root") as run:
        for state in ("on", "off"):
            if state == "off":  # a save without ``enabled`` switches the keys off
                saved = run.client.post(
                    "/settings/shortcuts", data={"version": "0"}, headers=run.action(**HTMX)
                )
                assert saved.status_code == 200
            found = elements(run.client.get("/inbox").text)
            links = [e for e in found if e.tag == "a" and e.text == "Settings"]
            hrefs = [e.attrs.get("href") for e in links if "shortcuts-settings" in str(e.attrs)]
            assert hrefs == [target], (state, hrefs)


# --- the page title keeps "Settings"; hash links land on the heading ---------------------------


@pytest.mark.parametrize("slug", SLUGS)
def test_settings_area_page_title_names_the_area_and_keeps_settings(
    tmp_path: Path, slug: str
) -> None:
    """``<Area> · Settings · Job Search Pipeline`` (``/settings`` is "Settings · Job Search
    Pipeline"), so a tab or history entry of any area is still found under Settings."""
    area = BY_SLUG[slug]
    wanted = (
        "Settings · Job Search Pipeline"
        if slug == "providers"
        else f"{area.label} · Settings · Job Search Pipeline"
    )
    with area_app(tmp_path / "root") as run:
        for response in (run.client.get(area.path), get_token(run, area.path)):
            titles = re.findall(r"<title>(.*?)</title>", response.text, re.DOTALL)
            assert titles == [wanted], (area.path, titles)


@pytest.mark.parametrize(
    ("path", "section"),
    [
        ("/settings", "providers"),
        ("/settings/keyboard", "shortcuts-settings"),
        ("/settings/your-details", "details-settings"),
        ("/settings/budgets", "usage-budgets"),
    ],
)
def test_sections_that_other_pages_link_to_are_marked_for_hash_focus(
    tmp_path: Path, path: str, section: str
) -> None:
    """A link such as ``/settings#providers`` or ``/settings/keyboard#shortcuts-settings`` puts
    focus on the section's heading (``data-hash-focus``, app.js), as the budgets link does."""
    with area_app(tmp_path / "root") as run:
        for response in (run.client.get(path), get_token(run, path, htmx=True)):
            found = by_id(response.text, section)
            assert found is not None and "data-hash-focus" in found.attrs, (path, section)
            assert found.attrs.get("aria-labelledby"), "the heading is what takes focus"


def test_your_details_keeps_hash_focus_after_the_lazy_load_swaps_it(tmp_path: Path) -> None:
    with area_app(tmp_path / "root") as run:
        for response in (
            get_token(run, "/settings/details"),
            get_token(run, "/settings/details", htmx=True),
        ):
            found = by_id(response.text, "details-settings")
            assert found is not None and "data-hash-focus" in found.attrs
