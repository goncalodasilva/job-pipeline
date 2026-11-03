"""M9.2 part B: Settings > Appearance (AC-M9.2; ADR-131 items 6-7; ADR-042 follow-up; ADR-125
item 5; TD-085 density only).

A versioned ``appearance`` setting (``theme`` system, light or dark; ``density`` comfortable or
compact) is saved by ``POST /settings/appearance`` and rendered as ``data-theme`` and
``data-density`` on ``<html>`` of every page. The route, the markup and the copy are described in
``tests/appearance_pages.py``. Real SQLite in a temp folder, the app through ``TestClient``, no
network. Every value is fictional. Application names are imported inside the tests that need
them, so each test fails on its own while they are missing.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute

from app.api.security import PAGE_TAG
from tests.appearance_pages import (
    CHANGED_EVENT,
    CONFLICT_ID,
    DEFAULTS,
    DENSITIES,
    DONE_ID,
    FORM_ID,
    GOOD,
    HEADING_ID,
    HOSTILE,
    PATH,
    SAVED,
    THEMES,
    appearance_attrs,
    control,
    error_text,
    expected_attrs,
    focus_targets,
    get_all,
    invalid_names,
    label_of,
    options,
    section,
    seeded_page_paths,
    shown,
    triggers,
)
from tests.board_pages import by_id, elements
from tests.data_pages import get_token, state_of
from tests.integration.test_settings_areas import area_app
from tests.integration.test_setup_redirect import BASE_URL, START, Running, running

pytestmark = pytest.mark.integration

HTMX = {"HX-Request": "true"}
RELOAD = "Reload"


def post(run: Running, *, htmx: bool = True, **values: Any) -> Any:
    headers = run.action(**(HTMX if htmx else {}))
    return run.client.post(PATH, data=values, headers=headers)


def stored(run: Running) -> tuple[Any, int]:
    from app.domain.settings import APPEARANCE
    from app.services.settings import SettingsService
    from tests.fakes.clock import FrozenClock

    loaded = SettingsService(run.app.state.workspace.db, FrozenClock(START)).load(APPEARANCE)
    return loaded.value, loaded.version


def setting_rows(run: Running) -> list[tuple[Any, ...]]:
    with run.app.state.workspace.db.transaction() as conn:
        return list(conn.execute("SELECT key, value, version FROM settings ORDER BY key"))


def appearance_rows(run: Running) -> list[tuple[Any, ...]]:
    return [row for row in setting_rows(run) if row[0] == "appearance"]


def model(**values: str) -> Any:
    from app.domain.settings import AppearanceSettings

    return AppearanceSettings(**values)


# --- the page: token-free, read only, the stored choices shown ---------------------------------


def test_appearance_page_shows_the_defaults_and_reading_writes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "root"
    with area_app(root) as run:
        before = state_of(run, root / "data")
        rows = setting_rows(run)
        full = run.client.get(PATH)
        part = get_token(run, PATH, htmx=True)
        assert full.status_code == 200 and "<html" in full.text
        assert part.status_code == 200 and "<html" not in part.text
        assert run.client.get(PATH, headers=HTMX).status_code == 403, "htmx needs the token"
        for markup in (full.text, part.text):
            box = section(markup)
            assert box.attrs.get("class") and "settings-section" in str(box.attrs.get("class"))
            heading = by_id(markup, "appearance-heading")
            assert heading is not None and heading.tag == "h2" and heading.within(box)
            assert box.attrs.get("aria-labelledby") == "appearance-heading"
            form = by_id(markup, FORM_ID)
            assert form is not None and form.tag == "form" and form.within(box)
            assert form.attrs.get("action") == PATH and form.attrs.get("hx-post") == PATH
            assert (form.attrs.get("method") or "").lower() == "post"
            assert shown(markup) == {**DEFAULTS, "version": "0"}
            theme, density = control(markup, "theme"), control(markup, "density")
            assert options(markup, theme) == list(THEMES.items())
            assert options(markup, density) == list(DENSITIES.items())
            assert label_of(markup, theme) == "Theme"
            assert label_of(markup, density) == "Density"
            buttons = [e for e in elements(markup) if e.tag == "button" and e.within(form)]
            assert [(b.text, b.attrs.get("type", "submit")) for b in buttons] == [
                ("Save", "submit")
            ]
            assert not [
                e for e in elements(markup) if e.within(box) and "data-autofocus" in e.attrs
            ]
            assert by_id(markup, DONE_ID) is None and by_id(markup, CONFLICT_ID) is None
        assert stored(run) == (model(), 0)
        assert state_of(run, root / "data") == before and setting_rows(run) == rows


def test_appearance_page_shows_what_is_stored(tmp_path: Path) -> None:
    with area_app(tmp_path / "root") as run:
        assert post(run, version="0", **GOOD).status_code == 200
        for markup in (run.client.get(PATH).text, get_token(run, PATH, htmx=True).text):
            assert shown(markup) == {**GOOD, "version": "1"}


# --- save, versioned ---------------------------------------------------------------------------


def test_appearance_saved_versioned_and_409(tmp_path: Path) -> None:
    from app.domain.errors import VersionConflict

    with area_app(tmp_path / "root") as run:
        # --- version 0 inserts: stored through the setting, announced, applied at once ---------
        saved = post(run, version="0", **GOOD)
        assert saved.status_code == 200, saved.text[:300]
        assert stored(run) == (model(**GOOD), 1)
        [(key, value, version)] = appearance_rows(run)
        assert (key, json.loads(value), version) == ("appearance", GOOD, 1)
        assert "<html" not in saved.text, "the section only"
        assert shown(saved.text) == {**GOOD, "version": "1"}
        done = by_id(saved.text, DONE_ID)
        assert done is not None and done.text == SAVED
        [target] = focus_targets(saved.text)
        assert target.attrs.get("id") == "appearance-heading" == HEADING_ID
        sent = triggers(saved.headers)
        assert sent["announce"] == SAVED
        assert sent[CHANGED_EVENT] == GOOD, "app.js applies it to <html> without a reload"
        assert "location" not in saved.headers

        # --- the next save carries version 1; every listed value is accepted -------------------
        version = 1
        for theme in THEMES:
            for density in DENSITIES:
                made = post(run, version=str(version), theme=theme, density=density)
                assert made.status_code == 200, (theme, density, made.text[:200])
                version += 1
                assert stored(run) == (model(theme=theme, density=density), version)
                assert triggers(made.headers)[CHANGED_EVENT] == {
                    "theme": theme,
                    "density": density,
                }

        # --- a stale version: 409 with the reload prompt, nothing saved, even for good values --
        before = setting_rows(run)
        for stale in ("0", "1", str(version - 1), str(version + 1), "99"):
            refused = post(run, version=stale, theme="light", density="comfortable")
            assert refused.status_code == 409, (stale, refused.status_code)
            assert VersionConflict.message in refused.text
            prompt = by_id(refused.text, CONFLICT_ID)
            assert prompt is not None and prompt.attrs.get("role") == "alert"
            links = [e for e in elements(refused.text) if e.tag == "a" and e.within(prompt)]
            assert [(e.text, e.attrs.get("href")) for e in links] == [(RELOAD, PATH)]
            assert [e.attrs.get("id") for e in focus_targets(refused.text)] == [CONFLICT_ID]
            assert by_id(refused.text, DONE_ID) is None
            assert 'name="version"' not in refused.text, "no form that could carry the version"
            assert "appearance:changed" not in refused.headers.get("HX-Trigger", "")
            assert "<html" not in refused.text
        assert setting_rows(run) == before
        assert stored(run)[1] == version

        # --- plain forms: 303 back to the area; a stale plain form shows the whole page --------
        plain = post(run, htmx=False, version=str(version), theme="system", density="compact")
        assert plain.status_code == 303 and plain.headers["location"] == PATH
        assert stored(run) == (model(theme="system", density="compact"), version + 1)
        version += 1
        stale_plain = post(run, htmx=False, version="0", **GOOD)
        assert stale_plain.status_code == 409 and "<html" in stale_plain.text
        assert by_id(stale_plain.text, CONFLICT_ID) is not None
        assert stored(run)[1] == version


def test_appearance_stale_version_wins_over_a_refused_value(tmp_path: Path) -> None:
    """The version is checked first (ADR-125 item 5): a stale version with a bad value is the
    409 reload prompt, not a 422 whose form carries the current version."""
    with area_app(tmp_path / "root") as run:
        assert post(run, version="0", **GOOD).status_code == 200
        assert post(run, version="1", theme="light", density="comfortable").status_code == 200
        before = setting_rows(run)
        for version in ("0", "1", "3"):
            bad = post(run, version=version, theme="neon", density="tiny")
            assert bad.status_code == 409, version
            assert by_id(bad.text, CONFLICT_ID) is not None
            assert "aria-invalid" not in bad.text, "a stale save is not judged on its values"
            assert 'name="version"' not in bad.text
        assert setting_rows(run) == before
        # With the current version the same values are still refused as values.
        assert post(run, version="2", theme="neon", density="tiny").status_code == 422
        assert setting_rows(run) == before


# --- refused values ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"theme": "Dark"},
        {"theme": "DARK"},
        {"theme": " dark"},
        {"theme": "auto"},
        {"theme": "high-contrast"},
        {"theme": ""},
        {"theme": HOSTILE},
        {"density": "Compact"},
        {"density": "cozy"},
        {"density": ""},
        {"density": "24"},
        {"density": HOSTILE},
    ],
    ids=lambda bad: "-".join(f"{k}={v!r}"[:30] for k, v in bad.items()),
)
def test_appearance_refused_value_is_tied_to_its_field(tmp_path: Path, bad: dict[str, str]) -> None:
    (field,) = bad
    with area_app(tmp_path / "root") as run:
        before = setting_rows(run)
        sent = {"theme": "light", "density": "comfortable", **bad}
        refused = post(run, version="0", **sent)
        assert refused.status_code == 422, refused.text[:300]
        assert "<html" not in refused.text
        assert invalid_names(refused.text) == [field]
        assert error_text(refused.text, control(refused.text, field))
        other = "density" if field == "theme" else "theme"
        assert control(refused.text, other).attrs.get("aria-invalid") != "true"
        [target] = focus_targets(refused.text)
        assert target.attrs.get("name") == field, "focus goes to the refused field"
        # The other field keeps what was sent; the refused one shows what is saved, never the
        # rejected text.
        kept = shown(refused.text)
        assert kept[other] == sent[other]
        assert kept[field] == DEFAULTS[field]
        assert kept["version"] == "0"
        assert "injected" not in refused.text
        # The refused text is never shown back. Searching the markup for it is not a test: a
        # refused "Dark", "Compact" or "auto" is also fixed copy (an option label, "data-autofocus"
        # and so on). The answer to a refused value is the same whatever the value was, so it
        # cannot hold it: another refused value gives the identical section.
        other_refused = post(run, version="0", **{**sent, field: "zz-refused"})
        assert other_refused.status_code == 422
        assert other_refused.text == refused.text
        assert other_refused.headers["HX-Trigger"] == refused.headers["HX-Trigger"]
        assert by_id(refused.text, DONE_ID) is None
        assert "appearance:changed" not in refused.headers.get("HX-Trigger", "")
        said = triggers(refused.headers)["announce"]
        assert "not saved" in said.lower(), said
        assert setting_rows(run) == before and stored(run) == (model(), 0)

        # Corrected, the same form saves.
        fixed = {**sent, field: GOOD[field]}
        assert post(run, version=kept["version"], **fixed).status_code == 200
        assert stored(run)[1] == 1


def test_appearance_every_refused_field_is_marked_and_a_plain_form_gets_the_page(
    tmp_path: Path,
) -> None:
    with area_app(tmp_path / "root") as run:
        before = setting_rows(run)
        refused = post(run, version="0", theme="neon", density="tiny")
        assert refused.status_code == 422
        assert invalid_names(refused.text) == ["theme", "density"]
        [target] = focus_targets(refused.text)
        assert target.attrs.get("name") == "theme", "the first refused field in form order"
        ids = [e.attrs.get("id") for e in elements(refused.text) if e.attrs.get("id")]
        assert len(ids) == len(set(ids)), "ids stay unique"

        plain = post(run, htmx=False, version="0", theme="neon", density="comfortable")
        assert plain.status_code == 422 and "<html" in plain.text
        assert invalid_names(plain.text) == ["theme"]
        heading = by_id(plain.text, "page-heading")
        assert heading is not None and "data-autofocus" not in heading.attrs
        assert len(focus_targets(plain.text)) == 1, "one focus target on a whole page"
        assert setting_rows(run) == before


@pytest.mark.parametrize(
    "data",
    [
        {"version": "0", "theme": "dark"},
        {"version": "0", "density": "compact"},
        {"theme": "dark", "density": "compact"},
        {"version": "0", "theme": "dark", "density": "compact", "font_size": "20"},
        {"version": "0", "theme": ["dark", "light"], "density": "compact"},
        {"version": "0", "theme": "dark", "density": ["compact", "compact"]},
        {"version": ["0", "0"], "theme": "dark", "density": "compact"},
        {"version": "abc", "theme": "dark", "density": "compact"},
        {"version": "-1", "theme": "dark", "density": "compact"},
        {"version": "", "theme": "dark", "density": "compact"},
        {"version": "1.5", "theme": "dark", "density": "compact"},
    ],
    ids=[
        "no-density",
        "no-theme",
        "no-version",
        "unknown-field",
        "repeated-theme",
        "repeated-density",
        "repeated-version",
        "version-text",
        "version-negative",
        "version-empty",
        "version-fraction",
    ],
)
def test_appearance_malformed_requests_are_a_400(tmp_path: Path, data: dict[str, Any]) -> None:
    with area_app(tmp_path / "root") as run:
        before = setting_rows(run)
        answer = run.client.post(PATH, data=data, headers=run.action(**HTMX))
        assert answer.status_code == 400, (answer.status_code, answer.text[:200])
        assert "<html" not in answer.text and "injected" not in answer.text
        assert setting_rows(run) == before


# --- the route's guards, and what a save leaves alone ------------------------------------------


def test_appearance_post_needs_token_and_origin_and_only_the_page_reads(tmp_path: Path) -> None:
    form = {"version": "0", **GOOD}
    root = tmp_path / "root"
    with area_app(root) as run:
        before = setting_rows(run)
        for headers in (
            {},
            run.with_token(),
            {"origin": BASE_URL},
            {"origin": "http://evil.example", **run.with_token()},
        ):
            assert run.client.post(PATH, data=form, headers=headers).status_code == 403, headers
        for method in ("PUT", "PATCH", "DELETE"):
            answer = run.client.request(method, PATH, headers=run.action())
            assert answer.status_code in (404, 405), method
        assert setting_rows(run) == before, "a refused request changes nothing"
        # Control: with both, the same request is accepted.
        assert run.client.post(PATH, data=form, headers=run.action()).status_code in (200, 303)
        assert stored(run)[1] == 1
        # The page and the POST share a path; only the page is token-free (spec 2.9 rule 12).
        pages = {
            route.path
            for route in run.app.routes
            if isinstance(route, APIRoute) and PAGE_TAG in (route.tags or [])
        }
        assert PATH in pages
        assert run.client.get(PATH).status_code == 200


def test_saving_appearance_does_not_touch_other_settings(tmp_path: Path) -> None:
    with area_app(tmp_path / "root") as run:
        before = setting_rows(run)
        assert len(before) > 1, "the finished setup stores other settings"
        assert post(run, version="0", **GOOD).status_code == 200
        after = setting_rows(run)
        others_before = [row for row in before if row[0] != "appearance"]
        assert [row for row in after if row[0] != "appearance"] == others_before
        assert [row[0] for row in after if row not in before] == ["appearance"]
        # Saving other settings leaves the appearance alone as well.
        assert post(run, version="1", theme="light", density="comfortable").status_code == 200
        sessions = run.client.post(
            "/settings/sessions",
            data={"version": "0", "frequency": "never", "length": "5", "ask_later_default": "week"},
            headers=run.action(**HTMX),
        )
        assert sessions.status_code == 200
        assert stored(run) == (model(theme="light", density="comfortable"), 2)


# --- every page carries the stored choice ------------------------------------------------------


def test_appearance_attributes_on_every_page(tmp_path: Path) -> None:
    """``<html lang="en" data-theme data-density>`` on every page route (the setup guide and the
    settings areas included), the guide's "not found" page and the 404 of an unknown path's
    page; the defaults first, then the saved choice on a second page."""
    with area_app(tmp_path / "root") as run:
        paths = seeded_page_paths(run.app)
        assert "/settings/appearance" in paths and "/setup/{step_id}" in paths, paths
        assert {"/settings/data", "/settings", "/"} <= set(paths), "settings areas and Today"

        def get(path: str) -> Any:
            return run.client.get(path)

        first = get_all(get, paths)
        not_found = run.client.get("/setup/no-such-step")
        assert not_found.status_code == 404 and "<html" in not_found.text
        assert appearance_attrs(not_found.text) == expected_attrs(**_defaults())
        for route, (status, attrs) in first.items():
            assert status == 200, route
            assert attrs == expected_attrs(**_defaults()), f"defaults on {route}: {attrs}"

        assert post(run, version="0", **GOOD).status_code == 200
        second = get_all(get, paths)
        for route, (status, attrs) in second.items():
            assert status == 200, route
            assert attrs == expected_attrs("dark", "compact"), f"saved choice on {route}: {attrs}"
        saved_404 = run.client.get("/setup/no-such-step")
        assert appearance_attrs(saved_404.text) == expected_attrs("dark", "compact")

        # A later change shows on every page again (nothing is cached per page).
        assert post(run, version="1", theme="light", density="comfortable").status_code == 200
        for route, (status, attrs) in get_all(get, paths).items():
            assert status == 200, route
            assert attrs == expected_attrs("light", "comfortable"), route


def test_appearance_attributes_on_the_setup_guide_before_anything_is_saved(
    tmp_path: Path,
) -> None:
    """A fresh app (setup not done) still renders the defaults on the guide and its 404."""
    with running(tmp_path) as run:
        for path in ("/setup/welcome", "/setup/no-such-step"):
            answer = run.client.get(path)
            assert answer.status_code in (200, 404), (path, answer.status_code)
            assert appearance_attrs(answer.text) == expected_attrs(**_defaults()), path


def test_a_stored_value_that_is_not_valid_shows_the_defaults(tmp_path: Path) -> None:
    """An invalid stored row (edited by hand, or from another version) never reaches the markup:
    the page shows the defaults, as every other setting does (``SettingsService.load``)."""
    with area_app(tmp_path / "root") as run:
        with run.app.state.workspace.db.transaction() as conn:
            conn.execute(
                "INSERT INTO settings(key, value, created_at, updated_at, version) "
                "VALUES ('appearance', ?, ?, ?, 1)",
                (
                    json.dumps({"theme": '"><script>1</script>', "density": "huge"}),
                    START.isoformat(),
                    START.isoformat(),
                ),
            )
        answer = run.client.get("/settings/appearance")
        assert appearance_attrs(answer.text) == expected_attrs(**_defaults())
        assert "<script>1</script>" not in answer.text
        assert shown(answer.text) == {**DEFAULTS, "version": "1"}


def _defaults() -> dict[str, str]:
    return {"theme": DEFAULTS["theme"], "density": DEFAULTS["density"]}


# --- export and restore carry the new key ------------------------------------------------------


def test_appearance_is_exported_and_restored(tmp_path: Path) -> None:
    """The setting is a row of ``settings``, so the private export holds it and a restore into an
    empty workspace brings it back with its version (there is no allow-list of keys to extend)."""
    from app.domain.settings import APPEARANCE
    from app.services.settings import SettingsService
    from tests.fakes.clock import FrozenClock
    from tests.integration.test_data_portability import service
    from tests.integration.test_portability import fresh

    source, target = fresh(tmp_path / "source"), fresh(tmp_path / "target")
    try:
        settings = SettingsService(source, FrozenClock(START))
        settings.save(APPEARANCE, model(**GOOD), expected_version=0)
        archive = service(source).export()
        exported = json.loads(archive.read_bytes())["tables"]["settings"]
        keys = [row[exported["columns"].index("key")] for row in exported["rows"]]
        assert "appearance" in keys
        service(target).restore(archive)
        loaded = SettingsService(target, FrozenClock(START)).load(APPEARANCE)
        assert (loaded.value, loaded.version) == (model(**GOOD), 1)
    finally:
        source.close()
        target.close()
