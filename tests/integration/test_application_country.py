"""M6.6 the CV country of an application: it defaults to ``JobPreferences.cv_country`` and a select
changes it per application, stored on the workspace row with a version check (ADR-106 item 6).

Assumed interface (markup is shared with the M6.5 tab, see ``tests/tailoring_pages.py``):

- Every step of ``#tailoring`` (the page ``/applications/{id}?tab=tailoring`` and the fragment
  ``/applications/{id}/tailoring/{step}``) holds ``<form id="country-form" method="post"
  action="/applications/{id}/tailoring/country">`` with a hidden ``version`` (the workspace
  version as last seen, ``0`` while the application has no workspace row) and ``<select
  name="cv_country">`` (accessible name starts "CV country"; each option's value is a two-letter
  upper-case ISO 3166-1 code and its text a country name; at least PT, DE, US, GB and FR are
  listed) with a submit button "Save country". The selected option is the workspace's
  ``cv_country`` or, while that is NULL or there is no workspace, ``JobPreferences.cv_country``
  (read at display time: it is never copied). A GET writes nothing.
- ``POST /applications/{id}/tailoring/country`` (token and origin): ``version`` and ``cv_country``
  only (each once; anything else, a repeat or a missing or non-numeric version is a plain-text
  400). It stores the code through ``tailoring_repo.update_workspace`` (or creates the workspace
  row with no angle when the application has none, ``version`` 0), leaving the angle, letter tone
  and length as they are. 200 with the ``#tailoring`` fragment and ``{"announce": "CV country
  saved."}``; a code that is not two upper-case letters, or is not in the select, is a 422 tied to
  ``cv_country`` that changes nothing; a stale ``version`` is a 409 with the reload prompt.
- Saving an angle (M6.5) keeps the country the workspace has.
"""

import json
from typing import Any

import pytest

from app.domain.errors import VersionConflict
from tests.board_pages import elements
from tests.settings_board_pages import accessible_name, controls_in, errors_in, invalid_names
from tests.tailoring_pages import (
    Tailoring,
    announced,
    by_id,
    fragment_url,
    hidden_value,
    tailoring_app,
)

pytestmark = pytest.mark.integration

PREFERRED = "PT"  # the finished setup's saved JobPreferences.cv_country


@pytest.fixture
def tw(tmp_path):
    with tailoring_app(tmp_path) as desk:
        yield desk


def country_url(aid: str) -> str:
    return f"/applications/{aid}/tailoring/country"


def country_form(page: str):
    return by_id(page, "country-form")


def selected_country(page: str) -> str:
    form = country_form(page)
    options = [e for e in elements(page) if e.tag == "option" and e.within(form)]
    chosen = [o for o in options if "selected" in o.attrs]
    assert len(chosen) == 1, f"expected one selected option, found {len(chosen)}"
    return str(chosen[0].attrs["value"])


def offered(page: str) -> list[str]:
    form = country_form(page)
    return [
        str(e.attrs["value"])
        for e in elements(page)
        if e.tag == "option" and e.within(form) and e.attrs.get("value")
    ]


def form_version(page: str) -> str:
    return hidden_value(page, country_form(page), "version")


def workspace_row(tw: Tailoring, aid: str) -> dict[str, Any] | None:
    found = tw.rows("SELECT * FROM workspaces WHERE application_id=?", (aid,))
    return found[0] if found else None


def post_country(tw: Tailoring, aid: str, **values: str):
    return tw.post_fields(country_url(aid), values)


def set_preference(tw: Tailoring, code: str) -> None:
    row = tw.rows("SELECT value FROM settings WHERE key='job_preferences'")[0]
    changed = json.dumps({**json.loads(row["value"]), "cv_country": code})
    tw.r.run.execute("UPDATE settings SET value=? WHERE key='job_preferences'", (changed,))


def test_application_country_defaults_to_preference_and_changes(tw: Tailoring) -> None:
    app = tw.ready()
    other = tw.another_application("country-other-job")

    # The default is the preference, shown on the page and in every fragment; nothing is written.
    for shown in (
        tw.page(app.id, 1).text,
        tw.fragment(app.id, 2).text,
        tw.fragment(app.id, 5).text,
    ):
        assert selected_country(shown) == PREFERRED
        assert {PREFERRED, "DE", "US", "GB", "FR"} <= set(offered(shown))
        assert all(len(code) == 2 and code.isupper() for code in offered(shown))
        select = controls_in(shown, country_form(shown))["cv_country"]
        assert accessible_name(shown, select).startswith("CV country")
        assert form_version(shown) == "0"
    assert workspace_row(tw, app.id) is None, "reading creates no workspace"

    # It follows the preference until the application has its own country.
    set_preference(tw, "FR")
    assert selected_country(tw.page(app.id, 1).text) == "FR"
    set_preference(tw, PREFERRED)

    # A change is stored on the workspace row (created now, no angle), for this application only.
    changed = post_country(tw, app.id, version="0", cv_country="DE")
    assert changed.status_code == 200, changed.text[:300]
    assert announced(changed) == ["CV country saved."]
    row = workspace_row(tw, app.id)
    assert row is not None
    assert (row["cv_country"], row["angle_id"], row["version"]) == ("DE", None, 1)
    assert selected_country(changed.text) == "DE" and form_version(changed.text) == "1"
    assert selected_country(tw.page(app.id, 1).text) == "DE"
    assert selected_country(tw.page(other.id, 1).text) == PREFERRED
    assert workspace_row(tw, other.id) is None

    # A stored country stays when the preference changes; changing it again bumps the version.
    set_preference(tw, "US")
    assert selected_country(tw.page(app.id, 1).text) == "DE"
    again = post_country(tw, app.id, version="1", cv_country="GB")
    assert again.status_code == 200
    row = workspace_row(tw, app.id)
    assert row is not None and (row["cv_country"], row["version"]) == ("GB", 2)
    assert row["letter_tone"] is None and row["letter_length"] is None

    # Saving an angle afterwards keeps the country the workspace has.
    angle_version = tw.angle_version(app.id)
    assert angle_version == "2"
    saved = tw.post(
        app.id, 2, "angle", name="Reliable planner", description="Lead with planning.",
        version=angle_version,
    )  # fmt: skip
    assert saved.status_code == 200, saved.text[:300]
    row = workspace_row(tw, app.id)
    assert row is not None and row["cv_country"] == "GB" and row["angle_id"] is not None
    assert tw.requests("draft_cv_template") == [], "choosing a country asks the AI for nothing"


def test_application_country_refuses_bad_codes_stale_versions_and_odd_requests(
    tw: Tailoring,
) -> None:
    app = tw.ready()
    assert post_country(tw, app.id, version="0", cv_country="DE").status_code == 200
    before = workspace_row(tw, app.id)

    for bad in ("de", "DEU", "D", "", " DE", "Portugal", "ZZ", "D1", "DE\n", "<b>", "--"):
        refused = post_country(tw, app.id, version="1", cv_country=bad)
        assert refused.status_code == 422, repr(bad)
        assert invalid_names(refused.text) == ["cv_country"], repr(bad)
        assert errors_in(refused.text)["cv_country"], repr(bad)
        assert bad.strip() == "" or bad not in errors_in(refused.text)["cv_country"]
        assert workspace_row(tw, app.id) == before

    stale = post_country(tw, app.id, version="0", cv_country="FR")
    assert stale.status_code == 409 and VersionConflict.message in stale.text
    assert "Reload" in stale.text
    assert workspace_row(tw, app.id) == before

    for data in (
        {"cv_country": "FR"},
        {"version": "abc", "cv_country": "FR"},
        {"version": "1"},
        {"version": "1", "cv_country": "FR", "angle_id": "x"},
        {"version": "1", "cv_country": ["FR", "DE"]},
    ):
        assert tw.post_fields(country_url(app.id), data).status_code == 400, data
    assert workspace_row(tw, app.id) == before

    # An unknown or unsafe application: the fixed 404, nothing created.
    for aid in ("no-such-application", "..%2Fx", "a b"):
        missing = tw.post_fields(country_url(aid), {"version": "0", "cv_country": "DE"})
        assert missing.status_code == 404, aid
    assert len(tw.rows("SELECT id FROM workspaces")) == 1


def test_application_country_needs_token_and_origin(tw: Tailoring) -> None:
    app = tw.ready()
    run = tw.r.run.run
    body = {"version": "0", "cv_country": "DE"}
    origin = run.action()["origin"]
    for label, headers in {
        "none": {},
        "origin only": {"origin": origin},
        "token only": {"X-JP-Token": run.token},
        "foreign origin": {"X-JP-Token": run.token, "origin": "http://evil.example"},
    }.items():
        response = tw.r.client.post(country_url(app.id), data=body, headers=headers)
        assert response.status_code == 403, label
    assert workspace_row(tw, app.id) is None
    assert tw.r.client.get(fragment_url(app.id, 1)).status_code == 403

    trusted = tw.post_fields(country_url(app.id), body)  # token and origin: accepted
    assert trusted.status_code == 200 and workspace_row(tw, app.id) is not None


def test_application_country_form_explains_what_saving_does(tw: Tailoring) -> None:
    app = tw.ready()
    shown = tw.page(app.id, 3).text

    select = controls_in(shown, country_form(shown))["cv_country"]
    described = str(select.attrs.get("aria-describedby")).split()
    assert "cv-country-note" in described, "the select is described by the note"
    note = by_id(shown, "cv-country-note").text
    assert "keeps this application on your list" in note, "a saved country makes it 'touched'"
    assert "save" in note.lower() and "typed" in note.lower(), "saving refreshes the step"
    assert by_id(shown, "country-row").tag == "div", "select and button sit on one row"
