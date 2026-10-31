"""M7.8 Settings > Sessions: frequency, default length, the "Ask me later" default and the pause
switch, saved as one versioned setting with field-tied 422s and the 409 reload prompt (AC-M7.6;
ADR-117 item 8, ADR-119). The routes, markup and copy are in ``tests/session_settings_pages.py``;
Today's block that reads the setting is tested in ``test_session_suggestion.py``.

A real migrated SQLite database in a temp folder, the frozen clock, no network; every value is
fictional.
"""

from datetime import timedelta
from typing import Any

import pytest

from app.domain.errors import VersionConflict
from app.domain.settings import SESSIONS, SessionSettings
from app.services.settings import SettingsService
from tests.board_pages import by_id, elements, said_headers
from tests.cv_import_harness import Importing, importing
from tests.reminder_desk import all_tables
from tests.session_settings_pages import (
    ARABIC_INDIC_3,
    CONFLICT_ID,
    DEFAULTS,
    FIELD_ERRORS,
    HEADING_ID,
    INVALID,
    LABELS,
    NOT_SAVED,
    OPTIONS,
    SAVE_PATH,
    SAVED,
    SECTION_ID,
    SETTINGS,
    accessible_name,
    controls,
    disclosure_open,
    done_text,
    errors_in,
    fields_to_change,
    focus_targets,
    form,
    invalid_names,
    options,
    section,
    shown,
)

pytestmark = pytest.mark.integration

HTMX = {"HX-Request": "true"}
GOOD = {"frequency": "weekly", "length": "3", "ask_later_default": "week"}


def post(run: Importing, *, htmx: bool = True, **values: Any) -> Any:
    headers = run.run.action(**(HTMX if htmx else {}))
    return run.client.post(SAVE_PATH, data=values, headers=headers)


def post_data(run: Importing, data: Any) -> Any:
    return run.client.post(SAVE_PATH, data=data, headers=run.run.action(**HTMX))


def page(run: Importing) -> str:
    response = run.client.get(SETTINGS)
    assert response.status_code == 200, response.status_code
    return response.text


def tables(run: Importing) -> dict[str, list[Any]]:
    return all_tables(lambda sql: run.rows(sql))


def stored(run: Importing) -> tuple[SessionSettings, int]:
    loaded = SettingsService(run.app.state.workspace.db, run.clock).load(SESSIONS)
    return loaded.value, loaded.version


def setting_rows(run: Importing) -> list[dict[str, Any]]:
    return run.rows("SELECT * FROM settings WHERE key='sessions'")


def test_session_settings_saved_versioned_and_validated(tmp_path):
    with importing(tmp_path) as run:
        # --- the section on a fresh app: defaults, version 0, a read that writes nothing -------
        before = tables(run)
        full = run.client.get(SETTINGS)  # a page route: no token needed
        assert full.status_code == 200 and "<html" in full.text
        assert tables(run) == before and setting_rows(run) == []
        fragment = run.client.get(SETTINGS, headers=run.run.htmx())
        assert fragment.status_code == 200 and "<html" not in fragment.text
        for html in (full.text, fragment.text):
            section(html)
            assert not disclosure_open(html), "collapsed on a plain load: one tab stop"
            assert shown(html) == DEFAULTS
            found = controls(html)
            for name, expected in OPTIONS.items():
                assert options(html, found[name]) == expected, name
            for name, words in LABELS.items():
                assert words.lower() in accessible_name(html, found[name]).lower(), name
            buttons = [e for e in elements(html) if e.tag == "button" and e.within(form(html))]
            assert [b.text for b in buttons] == ["Save"]
        assert full.text.count(f'id="{SECTION_ID}"') == 1
        assert stored(run) == (SessionSettings(), 0)

        # --- a save: version 0 inserts; stored through the setting, announced, focus on the
        #     heading, every control shows what was saved --------------------------------------
        run.clock.advance(timedelta(minutes=5))
        saved = post(
            run, version="0", frequency="twice_weekly", length="5", ask_later_default="month",
            paused="1",
        )  # fmt: skip
        assert saved.status_code == 200, saved.text[:300]
        assert stored(run) == (
            SessionSettings(
                frequency="twice_weekly", length=5, ask_later_default="month", paused=True
            ),
            1,
        )
        [row] = setting_rows(run)
        assert row["version"] == 1 and row["updated_at"] == run.clock.now().isoformat()
        assert disclosure_open(saved.text), "open after a save so the message is visible"
        assert shown(saved.text) == {
            "version": "1",
            "frequency": "twice_weekly",
            "length": "5",
            "ask_later_default": "month",
            "paused": True,
        }
        assert done_text(saved.text) == SAVED
        assert SAVED in said_headers(saved.headers)
        [target] = focus_targets(saved.text)
        assert target.attrs["id"] == HEADING_ID
        assert "<html" not in saved.text
        assert shown(page(run))["frequency"] == "twice_weekly"

        # --- the next save carries version 1; unchecked means not paused -----------------------
        again = post(run, version="1", **GOOD)
        assert again.status_code == 200
        assert stored(run) == (SessionSettings(), 2), "all defaults again, now stored"
        assert shown(again.text) == {**DEFAULTS, "version": "2"}
        # Every listed value is accepted.
        version = 2
        for name, listed in OPTIONS.items():
            for value in listed:
                made = post(run, version=str(version), **{**GOOD, name: value})
                assert made.status_code == 200, (name, value, made.text[:200])
                version += 1
                assert getattr(stored(run)[0], name) == (int(value) if name == "length" else value)
        assert post(run, version=str(version), paused="1", **GOOD).status_code == 200
        assert stored(run)[0].paused is True
        version += 1
        assert stored(run)[1] == version

        # --- plain posts: a save goes back to Settings -----------------------------------------
        plain = post(run, htmx=False, version=str(version), **GOOD)
        assert plain.status_code == 303 and plain.headers["location"] == SETTINGS
        version += 1
        assert stored(run) == (SessionSettings(), version)


@pytest.mark.parametrize(
    "bad",
    [
        {"frequency": "daily"},
        {"frequency": ""},
        {"frequency": "Weekly"},
        {"frequency": " weekly"},
        {"length": "4"},
        {"length": "0"},
        {"length": "03"},
        {"length": "3.0"},
        {"length": "three"},
        {"length": ""},
        {"length": ARABIC_INDIC_3},
        {"ask_later_default": "year"},
        {"ask_later_default": "date"},
        {"ask_later_default": ""},
        {"frequency": '<script id="x">1</script>'},
    ],
    ids=lambda bad: "-".join(f"{k}={v!r}" for k, v in bad.items()),
)
def test_session_settings_refused_value_is_tied_to_its_field(tmp_path, bad):
    (field,) = bad
    with importing(tmp_path) as run:
        before = tables(run)
        sent = {"frequency": "never", "length": "5", "ask_later_default": "month", **bad}
        refused = post(run, version="0", paused="1", **sent)
        assert refused.status_code == 422, refused.text[:300]
        assert errors_in(refused.text) == {field: FIELD_ERRORS[field]}
        assert disclosure_open(refused.text), "open so the error and focus are visible"
        assert invalid_names(refused.text) == [field]
        # The other fields keep what was sent; the refused one is never shown back.
        kept = shown(refused.text)
        for name, value in sent.items():
            if name != field:
                assert kept[name] == value, name
        assert kept["paused"] is True and kept["version"] == "0"
        assert kept[field] in options(refused.text, controls(refused.text)[field])
        assert '<script id="x">' not in refused.text, "a refused value is never echoed"
        # Focus goes to the refused field, which is announced as needing a change.
        [target] = focus_targets(refused.text)
        assert target.attrs.get("name") == field
        assert said_headers(refused.headers).count(f"{NOT_SAVED} {fields_to_change(1)}") == 1
        assert done_text(refused.text) is None
        assert tables(run) == before and setting_rows(run) == []

        # Corrected, the same form saves (the version in the 422 answer is the stored one).
        fixed = {**sent, field: GOOD[field]}
        saved = post(run, version=kept["version"], paused="1", **fixed)
        assert saved.status_code == 200
        assert stored(run)[1] == 1


def test_session_settings_every_refused_field_is_marked(tmp_path):
    with importing(tmp_path) as run:
        before = tables(run)
        refused = post(
            run, version="0", frequency="daily", length="4", ask_later_default="year"
        )  # fmt: skip
        assert refused.status_code == 422
        assert errors_in(refused.text) == FIELD_ERRORS
        assert invalid_names(refused.text) == ["frequency", "length", "ask_later_default"]
        [target] = focus_targets(refused.text)
        assert target.attrs.get("name") == "frequency", "the first refused field in form order"
        assert fields_to_change(3) in said_headers(refused.headers)
        ids = [e.attrs.get("id") for e in elements(refused.text) if e.attrs.get("id")]
        assert len(ids) == len(set(ids)), "ids stay unique"

        # Without scripts the whole Settings page is the answer, and only one thing takes focus.
        plain = post(run, htmx=False, version="0", **{**GOOD, "frequency": "daily", "length": "5"})
        assert plain.status_code == 422 and "<html" in plain.text
        assert invalid_names(plain.text) == ["frequency"]
        heading = by_id(plain.text, "page-heading")
        assert heading is not None and "data-autofocus" not in heading.attrs
        assert len(focus_targets(plain.text)) == 1
        assert tables(run) == before


def test_session_settings_stale_version_gets_the_reload_prompt(tmp_path):
    with importing(tmp_path) as run:
        assert post(run, version="0", **GOOD).status_code == 200  # version 1
        assert post(run, version="1", **{**GOOD, "length": "5"}).status_code == 200  # version 2
        before = tables(run)
        for version in ("0", "1", "3", "99"):
            stale = post(run, version=version, frequency="never", length="3",
                         ask_later_default="month", paused="1")  # fmt: skip
            assert stale.status_code == 409, version
            assert VersionConflict.message in stale.text and "Reload" in stale.text
            prompt = by_id(stale.text, CONFLICT_ID)
            assert prompt is not None and prompt.attrs.get("role") == "alert"
            link = [e for e in elements(stale.text) if e.tag == "a" and e.within(prompt)]
            assert [(e.text, e.attrs.get("href")) for e in link] == [("Reload", SETTINGS)]
            assert [e.attrs.get("id") for e in focus_targets(stale.text)] == [CONFLICT_ID]
            section(stale.text)
            assert disclosure_open(stale.text), "open so the reload prompt is visible"
            assert done_text(stale.text) is None
        assert tables(run) == before
        assert stored(run) == (SessionSettings(length=5), 2)

        plain = post(run, htmx=False, version="0", **GOOD)
        assert plain.status_code == 409 and "<html" in plain.text
        assert tables(run) == before


def test_session_settings_stale_version_wins_over_a_refused_value(tmp_path):
    """The version is checked first: a stale version with a bad value is the 409 reload prompt
    (not a 422 whose form carries the current version, which a retry could overwrite with)."""
    with importing(tmp_path) as run:
        assert post(run, version="0", **GOOD).status_code == 200  # version 1
        assert post(run, version="1", **{**GOOD, "length": "5"}).status_code == 200  # version 2
        before = tables(run)
        for version in ("0", "1", "3"):
            bad = post(run, version=version, frequency="daily", length="4",
                       ask_later_default="year")  # fmt: skip
            assert bad.status_code == 409, version
            assert VersionConflict.message in bad.text and by_id(bad.text, CONFLICT_ID)
            assert [e.attrs.get("id") for e in focus_targets(bad.text)] == [CONFLICT_ID]
            assert 'name="version"' not in bad.text, "no form that could carry the version"
        assert tables(run) == before
        assert stored(run) == (SessionSettings(length=5), 2)
        # With the current version the same bad values are still a 422.
        assert post(run, version="2", frequency="daily", **{"length": "4"},
                    ask_later_default="year").status_code == 422  # fmt: skip


def test_session_settings_malformed_requests_are_a_400(tmp_path):
    with importing(tmp_path) as run:
        before = tables(run)
        for data in (
            {"version": "0", **GOOD, "paused": "yes"},
            {"version": "0", **GOOD, "paused": "0"},
            {"version": "0", **GOOD, "paused": ["1", "1"]},
            {"version": "0", **GOOD, "extra": "bad"},
            {"version": "0", "length": "3", "ask_later_default": "week"},
            {"version": "0", "frequency": "weekly", "ask_later_default": "week"},
            {"version": "0", "frequency": "weekly", "length": "3"},
            GOOD,
            {"version": "abc", **GOOD},
            {"version": "-1", **GOOD},
            {"version": "", **GOOD},
            {"version": ["0", "0"], **GOOD},
            {"version": "0", **GOOD, "length": ["3", "3"]},
            {"version": "0", **GOOD, "frequency": ["weekly", "never"]},
        ):
            answer = post_data(run, data)
            assert answer.status_code == 400, data
            assert INVALID in answer.text and "<html" not in answer.text
        assert tables(run) == before


def test_session_settings_page_carries_no_other_state(tmp_path):
    # The Sessions setting is its own versioned value: saving it touches no other setting.
    with importing(tmp_path) as run:
        before = tables(run)
        assert post(run, version="0", frequency="never", length="5",
                    ask_later_default="next_session", paused="1").status_code == 200  # fmt: skip
        after = tables(run)
        assert [name for name in after if after[name] != before[name]] == ["settings"]
        assert [r["key"] for r in after["settings"] if r not in before["settings"]] == ["sessions"]


def test_session_settings_routes_need_token_and_origin(tmp_path):
    # Both writes of M7.8: the Sessions setting and Today's Dismiss.
    dismiss = "/today/suggestion/dismiss"
    routes = [
        (SAVE_PATH, {"version": "0", "frequency": "never", "length": "5",
                     "ask_later_default": "month", "paused": "1"}),
        (dismiss, {}),
    ]  # fmt: skip
    with importing(tmp_path) as run:
        before = tables(run)
        for path, data in routes:
            assert run.client.post(path, data=data).status_code == 403, path
            no_origin = run.client.post(path, data=data, headers=run.run.with_token())
            assert no_origin.status_code == 403, path
            hostile = run.client.post(
                path, data=data, headers=run.run.action(Origin="https://hostile.example.test")
            )
            assert hostile.status_code == 403, path
            extra = run.client.post(
                path, data={**data, "extra": "bad"}, headers=run.run.action(**HTMX)
            )
            assert extra.status_code == 400, path
            # Routes are POST only: with the token a GET is not one of them.
            assert run.client.get(path, headers=run.run.htmx()).status_code == 405, path
        assert tables(run) == before
        # With the token and the origin each works, so the 403s were about those.
        for path, data in routes:
            reply = run.client.post(path, data=data, headers=run.run.action(**HTMX))
            assert reply.status_code == 200, (path, reply.status_code, reply.text[:200])
        assert stored(run) == (
            SessionSettings(frequency="never", length=5, ask_later_default="month", paused=True),
            1,
        )
        [reset] = run.rows("SELECT value FROM settings WHERE key='session_suggestion'")
        assert "last_reset" in reset["value"]


def now_line(html: str) -> str:
    found = [e for e in elements(html) if e.attrs.get("id") == "sessions-settings-now"]
    assert len(found) == 1
    assert not found[0].within(
        next(e for e in elements(html) if e.attrs.get("id") == "sessions-disclosure")
    ), "the current choices stay visible while the form is collapsed"
    return found[0].text


def test_session_settings_current_choices_visible_outside_the_disclosure(tmp_path):
    with importing(tmp_path) as run:
        assert now_line(page(run)) == "Now: Weekly, 3 questions."
        saved = post(
            run, version="0", frequency="after_application", length="5",
            ask_later_default="month", paused="1",
        )  # fmt: skip
        assert saved.status_code == 200
        assert now_line(saved.text) == (
            "Now: After each application, 5 questions. Suggestions are paused."
        )
        assert now_line(page(run)) == now_line(saved.text)
