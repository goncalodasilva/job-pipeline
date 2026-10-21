"""M5.7 Settings > Board: statuses (add with a phase, rename, move, delete unused custom ones) and
reminder rules per status; both versioned, with the 409 reload prompt and field-tied 422s
(AC-M5.1; ADR-102 items 5 and 10, ADR-104).

The routes and markup are described in ``tests/settings_board_pages.py``; the service interface in
``tests/reminder_desk.py``. A real migrated SQLite database in a temporary folder, the frozen
clock, no network, no provider call; every value is fictional.
"""

import importlib
from datetime import datetime, timedelta
from typing import Any

import pytest
from httpx import Response

from app.domain.errors import StatusNotAllowed, VersionConflict
from app.domain.ids import is_safe_id
from tests.board_pages import Seeder, by_id, elements, said_headers, stamp_after, ticker
from tests.cv_import_harness import Importing, importing
from tests.reminder_desk import all_tables
from tests.settings_board_pages import (
    ARABIC_INDIC_14,
    BUILT_IN_DELETE,
    DAYS_ERROR,
    DONE_ID,
    FULLWIDTH_14,
    IN_USE,
    OPEN_KEYS,
    PHASE_ERROR,
    PHASES,
    SETTINGS,
    STATUS_KEYS,
    STATUSES_PATH,
    accessible_name,
    autofocus_count,
    controls_in,
    done_text,
    errors_in,
    form_actions_in,
    forms_in,
    has_delete_form,
    invalid_names,
    row_version,
    rule_fields,
    rule_path,
    rule_rows,
    section,
    status_order,
    status_path,
    status_rows,
)

pytestmark = pytest.mark.integration

BUILT_IN_ORDER = list(STATUS_KEYS)
HTMX = {"HX-Request": "true"}


def post(run: Importing, path: str, *, htmx: bool = True, **values: Any) -> Response:
    headers = run.run.action(**(HTMX if htmx else {}))
    return run.client.post(path, data=values, headers=headers)


def post_data(run: Importing, path: str, data: Any) -> Response:
    return run.client.post(path, data=data, headers=run.run.action(**HTMX))


def page(run: Importing) -> str:
    response = run.client.get(SETTINGS)
    assert response.status_code == 200, response.status_code
    return response.text


def tables(run: Importing) -> dict[str, list[Any]]:
    return all_tables(lambda sql: run.rows(sql))


def seeder(run: Importing) -> Seeder:
    app = run.app
    return Seeder.make(
        app.state.workspace.db,
        app.state.applications,
        app.state.application_statuses,
        ticker(run.clock),
        stamp_after(run.clock),
    )


def label_of(run: Importing, key: str) -> str:
    return str(run.rows("SELECT label FROM application_statuses WHERE key=?", (key,))[0]["label"])


def order_in_db(run: Importing, phase: str) -> list[str]:
    return [
        row["key"]
        for row in run.rows(
            "SELECT key FROM application_statuses WHERE phase=? ORDER BY position, key", (phase,)
        )
    ]


def key_of(run: Importing, label: str) -> str:
    return str(run.rows("SELECT key FROM application_statuses WHERE label=?", (label,))[0]["key"])


def add(run: Importing, label: str, phase: str, **more: Any) -> str:
    answer = post(run, STATUSES_PATH, label=label, phase=phase, **more)
    assert answer.status_code == 200, (label, answer.status_code, answer.text[:300])
    return key_of(run, label.strip())


def error(name: str) -> type[Exception]:
    found = getattr(importlib.import_module("app.domain.errors"), name)
    assert isinstance(found, type)
    return found


# --- the section ---------------------------------------------------------------------


def test_settings_page_shows_the_board_section_without_a_token_and_writes_nothing(tmp_path):
    with importing(tmp_path) as run:
        before = tables(run)
        full = run.client.get(SETTINGS)  # a page route: no token needed
        assert full.status_code == 200 and "<html" in full.text
        html = full.text
        section(html)
        assert status_order(html) == BUILT_IN_ORDER
        rows = status_rows(html)
        for key, phase in PHASES.items():
            assert rows[key].attrs["data-phase"] == phase, key
            assert rows[key].attrs["id"] == f"status-{key}"
            assert phase.capitalize() in rows[key].text, key  # the phase in words
            assert f"/settings/board/statuses/{key}/rename" in form_actions_in(html, rows[key])
            assert f"/settings/board/statuses/{key}/move" in form_actions_in(html, rows[key])
            controls = controls_in(html, rows[key])
            assert controls["label"].attrs["value"] == label_of(run, key)
            assert controls["direction"].attrs["value"] in ("up", "down")
            assert hidden_versions(html, key) == {"1"}, key
            # Built-in statuses can be renamed and moved but never deleted.
            assert not has_delete_form(html, key), key

        # The add form: a name and a phase that must be chosen.
        [add_form] = [
            f for f in forms_in(html, section(html)) if f.attrs.get("id") == "status-add-form"
        ]
        assert (
            add_form.attrs.get("action") == STATUSES_PATH and add_form.attrs.get("method") == "post"
        )
        fields = controls_in(html, add_form)
        assert set(fields) >= {"label", "phase"}
        assert accessible_name(html, fields["label"]) == "New status name"
        assert accessible_name(html, fields["phase"]) == "Phase"
        options = options_of(html, fields["phase"])
        assert options == [
            ("", "Choose a phase"),
            ("preparing", "Preparing"),
            ("active", "Active"),
            ("closed", "Closed"),
        ]
        # The fragment (htmx) carries the same section.
        fragment = run.client.get(SETTINGS, headers=run.run.htmx())
        assert fragment.status_code == 200 and "<html" not in fragment.text
        assert status_order(fragment.text) == BUILT_IN_ORDER
        assert tables(run) == before


def hidden_versions(html: str, key: str) -> set[str]:
    row = status_rows(html)[key]
    return {
        str(e.attrs["value"])
        for e in elements(html)
        if e.tag == "input"
        and e.within(row)
        and e.attrs.get("type") == "hidden"
        and e.attrs.get("name") == "version"
    }


def options_of(html: str, select: Any) -> list[tuple[str, str]]:
    return [
        (str(e.attrs.get("value", "")), e.text)
        for e in elements(html)
        if e.tag == "option" and e.within(select)
    ]


# --- statuses: a phase is needed, and only unused custom ones are deleted -----------


def test_ac_m5_1_settings_status_needs_phase_and_unused_delete(tmp_path):
    with importing(tmp_path) as run:
        # A status without a phase (or with one that does not exist) is refused, tied to the phase.
        before = tables(run)
        for phase in ("", "bogus", "ACTIVE"):
            refused = post(run, STATUSES_PATH, label="Waiting", phase=phase)
            assert refused.status_code == 422, phase
            assert errors_in(refused.text) == {"phase": PHASE_ERROR}, phase
            assert invalid_names(refused.text) == ["phase"], phase
            assert 'value="Waiting"' in refused.text, "the typed name is kept"
            assert section(refused.text)
        assert tables(run) == before

        # Name rules, each tied to the name field; a phase error is not added when it is chosen.
        for label, expected in (
            ("", ["label"]),
            ("   ", ["label"]),
            ("Applied", ["label"]),
            ("  sCrEeNiNg ", ["label"]),
            ("x" * 101, ["label"]),
            ("Two\nlines", ["label"]),
            ("​", ["label"]),
        ):
            refused = post(run, STATUSES_PATH, label=label, phase="active")
            assert refused.status_code == 422, repr(label)
            assert invalid_names(refused.text) == expected, repr(label)
        both = post(run, STATUSES_PATH, label="", phase="")
        assert both.status_code == 422 and sorted(invalid_names(both.text)) == ["label", "phase"]
        assert tables(run) == before

        # A valid add: trimmed, last in its phase, built_in off, version 1, a safe key.
        added = post(run, STATUSES_PATH, label="  Waiting  ", phase="active")
        assert added.status_code == 200
        row = run.rows("SELECT * FROM application_statuses WHERE label='Waiting'")[0]
        waiting = row["key"]
        assert is_safe_id(waiting)
        assert (row["phase"], row["built_in"], row["version"]) == ("active", 0, 1)
        assert datetime.fromisoformat(row["created_at"]) == run.clock.now()
        assert order_in_db(run, "active") == [
            "applied",
            "screening",
            "interviewing",
            "offer",
            waiting,
        ]
        assert status_order(added.text) == [*BUILT_IN_ORDER[:5], waiting, *BUILT_IN_ORDER[5:]]
        assert status_rows(added.text)[waiting].attrs["data-phase"] == "active"
        assert has_delete_form(added.text, waiting)
        assert not any(has_delete_form(added.text, key) for key in STATUS_KEYS)
        assert done_text(added.text) == "Added the status Waiting."
        assert "Added the status Waiting." in said_headers(added.headers)
        assert autofocus_count(added.text) == 1
        assert row_version(added.text, waiting) == "1"
        # Without htmx the browser goes back to the page.
        plain = post(run, STATUSES_PATH, htmx=False, label="Plain", phase="closed")
        assert plain.status_code == 303 and plain.headers["location"].startswith(SETTINGS)
        assert "Plain" in page(run)

        # Delete: a built-in never (a forged request is refused and says why).
        before = tables(run)
        for key in ("withdrawn", "applied", "preparing"):
            refused = post(run, status_path(key, "delete"), version="1")
            assert refused.status_code == 422, key
            assert BUILT_IN_DELETE in refused.text, key
            assert key in status_rows(refused.text)
        assert tables(run) == before

        # A custom status an application is in, or has been in, is in use.
        seed = seeder(run)
        application = seed.application("board-job-1", "Seed Buyer", "Larkspur Orchards", waiting)
        version = row_version(page(run), waiting)
        used = post(run, status_path(waiting, "delete"), version=version)
        assert used.status_code == 422 and IN_USE in used.text
        assert waiting in status_rows(used.text)
        moved_version = run.rows("SELECT version FROM applications WHERE id=?", (application.id,))
        run.app.state.applications.move(
            application.id,
            "applied",
            expected_version=moved_version[0]["version"],
            source="board",
        )
        history_use = post(run, status_path(waiting, "delete"), version=version)
        assert history_use.status_code == 422 and IN_USE in history_use.text
        assert run.rows(
            "SELECT count(*) AS n FROM application_statuses WHERE key=?", (waiting,)
        ) == [{"n": 1}]

        # An unused custom status is deleted (a stale version loses first).
        spare = add(run, "Spare", "closed")
        spare_version = row_version(page(run), spare)
        stale = post(run, status_path(spare, "delete"), version=str(int(spare_version) + 3))
        assert stale.status_code == 409 and VersionConflict.message in stale.text
        assert "Reload" in stale.text and f'href="{SETTINGS}"' in stale.text
        assert spare in status_rows(stale.text)
        deleted = post(run, status_path(spare, "delete"), version=spare_version)
        assert deleted.status_code == 200
        assert spare not in status_rows(deleted.text)
        assert run.rows("SELECT count(*) AS n FROM application_statuses WHERE key=?", (spare,)) == [
            {"n": 0}
        ]
        assert done_text(deleted.text) == "Deleted the status Spare."
        assert "Deleted the status Spare." in said_headers(deleted.headers)
        assert autofocus_count(deleted.text) == 1

        # A custom status whose only link is its reminder rule is still unused: both go.
        parked = add(run, "Parked", "active")
        saved = post(run, rule_path(parked), version="0", days="10", enabled="1")
        assert saved.status_code == 200
        assert run.rows(
            "SELECT days, enabled FROM reminder_rules WHERE status_key=?", (parked,)
        ) == [{"days": 10, "enabled": 1}]
        gone = post(run, status_path(parked, "delete"), version=row_version(page(run), parked))
        assert gone.status_code == 200 and parked not in status_rows(gone.text)
        assert run.rows(
            "SELECT count(*) AS n FROM reminder_rules WHERE status_key=?", (parked,)
        ) == [{"n": 0}]
        assert run.rows("SELECT status_key FROM reminder_rules") == [{"status_key": "applied"}]

        # Unknown or unsafe keys: the fixed 404, never echoed.
        for key in ("missing", "bad.key", "x" * 90):
            answer = post(run, status_path(key, "delete"), version="1")
            assert answer.status_code == 404, key
            assert answer.text == StatusNotAllowed.message and key not in answer.text


def test_service_deletes_the_rule_of_an_unused_custom_status_and_keeps_a_used_ones(tmp_path):
    with importing(tmp_path) as run:
        statuses = run.app.state.application_statuses
        rules = run.app.state.reminder_rules
        unused = statuses.add("Unused", "active")
        rules.save(unused.key, days=5, enabled=True, expected_version=0)
        statuses.delete(unused.key, expected_version=unused.version)
        assert run.rows(
            "SELECT count(*) AS n FROM reminder_rules WHERE status_key=?", (unused.key,)
        ) == [{"n": 0}]
        used = statuses.add("Used", "active")
        rules.save(used.key, days=5, enabled=True, expected_version=0)
        seeder(run).application("board-job-2", "Frost Watcher", "Fern Fictional Labs", used.key)
        with pytest.raises(error("StatusNotAllowed")):
            statuses.delete(used.key, expected_version=used.version)
        assert run.rows(
            "SELECT count(*) AS n FROM reminder_rules WHERE status_key=?", (used.key,)
        ) == [{"n": 1}]


def test_status_rename_and_move_are_versioned_and_tied_to_their_fields(tmp_path):
    with importing(tmp_path) as run:
        html = page(run)
        version = row_version(html, "applied")
        rename = status_path("applied", "rename")

        # A built-in is renamed; its key and phase stay; version and updated_at move.
        run.clock.advance(timedelta(minutes=5))
        renamed = post(run, rename, version=version, label="  Sent  ")
        assert renamed.status_code == 200
        row = run.rows("SELECT * FROM application_statuses WHERE key='applied'")[0]
        assert (row["label"], row["phase"], row["built_in"], row["version"]) == (
            "Sent",
            "active",
            1,
            2,
        )
        assert datetime.fromisoformat(row["updated_at"]) == run.clock.now()
        assert (
            controls_in(renamed.text, status_rows(renamed.text)["applied"])["label"].attrs["value"]
            == "Sent"
        )
        assert row_version(renamed.text, "applied") == "2"
        assert done_text(renamed.text) == "Renamed to Sent."
        assert "Renamed to Sent." in said_headers(renamed.headers)
        assert autofocus_count(renamed.text) == 1
        assert status_order(renamed.text) == BUILT_IN_ORDER

        # Refused names are tied to the name field and change nothing; the typing stays.
        before = tables(run)
        for label in ("", "   ", "screening", "SCREENING", "x" * 101, "Two\nlines"):
            refused = post(run, rename, version="2", label=label)
            assert refused.status_code == 422, repr(label)
            assert invalid_names(refused.text) == ["label"], repr(label)
            assert errors_in(refused.text)["label"], repr(label)
        kept = post(run, rename, version="2", label="Screening")
        assert 'value="Screening"' in kept.text
        assert tables(run) == before

        # Its own name is not a clash; a stale version is the reload prompt.
        assert post(run, rename, version="2", label="Sent").status_code == 200
        stale = post(run, rename, version="2", label="Lost change")
        assert stale.status_code == 409 and VersionConflict.message in stale.text
        assert "Reload" in stale.text and label_of(run, "applied") == "Sent"

        # Malformed requests: an unknown field (the phase cannot change), a missing or odd version.
        before = tables(run)
        for data in (
            {"version": "3", "label": "X", "phase": "closed"},
            {"label": "X"},
            {"version": "abc", "label": "X"},
            {"version": "-1", "label": "X"},
            {"version": "3"},
            {"version": ["3", "3"], "label": "X"},
        ):
            assert post_data(run, rename, data).status_code == 400, data
        assert tables(run) == before
        for key in ("missing", "bad.key"):
            answer = post(run, status_path(key, "rename"), version="1", label="X")
            assert answer.status_code == 404 and answer.text == StatusNotAllowed.message

        # Move: one step inside the phase, each way; the order is the board's column order.
        html = page(run)
        move = status_path("applied", "move")
        down = post(run, move, version=row_version(html, "applied"), direction="down")
        assert down.status_code == 200
        assert order_in_db(run, "active") == ["screening", "applied", "interviewing", "offer"]
        assert status_order(down.text)[:5] == [
            "preparing",
            "screening",
            "applied",
            "interviewing",
            "offer",
        ]
        assert "Moved Sent down." in said_headers(down.headers)
        assert done_text(down.text) == "Moved Sent down."
        assert autofocus_count(down.text) == 1
        current = row_version(down.text, "applied")
        up = post(run, move, version=current, direction="up")
        assert up.status_code == 200
        assert order_in_db(run, "active") == ["applied", "screening", "interviewing", "offer"]
        assert "Moved Sent up." in said_headers(up.headers)
        # At the end of a phase nothing moves (and nothing is refused); phases never mix.
        top = post(run, move, version=row_version(up.text, "applied"), direction="up")
        assert top.status_code == 200
        assert order_in_db(run, "active") == ["applied", "screening", "interviewing", "offer"]
        only = post(run, status_path("preparing", "move"), version="1", direction="down")
        assert only.status_code == 200 and order_in_db(run, "preparing") == ["preparing"]
        assert order_in_db(run, "closed") == ["rejected", "withdrawn", "accepted", "declined"]
        # A stale version, a bad direction and unknown fields.
        before = tables(run)
        lost = post(run, move, version="1", direction="down")
        assert lost.status_code == 409 and VersionConflict.message in lost.text
        for data in (
            {"version": "5", "direction": "sideways"},
            {"version": "5", "direction": ""},
            {"version": "5"},
            {"version": "5", "direction": "up", "to": "closed"},
        ):
            assert post_data(run, move, data).status_code == 400, data
        assert tables(run) == before
        assert (
            post(run, status_path("missing", "move"), version="1", direction="up").status_code
            == 404
        )


def test_a_renamed_or_added_status_shows_on_the_board(tmp_path):
    with importing(tmp_path) as run:
        key = add(run, "Waiting", "active")
        # The board shows its columns once there is an application.
        seeder(run).application("board-job-3", "Seed Buyer", "Larkspur Orchards", "applied")
        post(
            run,
            status_path("applied", "rename"),
            version=row_version(page(run), "applied"),
            label="Sent",
        )
        board = run.client.get("/board")
        assert board.status_code == 200
        assert f'data-status="{key}"' in board.text
        headings = [e.text for e in elements(board.text) if e.tag == "h2"]
        assert "Waiting (0)" in headings and "Sent (1)" in headings


def test_status_labels_are_escaped(tmp_path):
    attack = '<script>fictional()</script> "quoted" & {{ 7 * 7 }}'
    with importing(tmp_path) as run:
        added = post(run, STATUSES_PATH, label=attack, phase="active")
        assert added.status_code == 200
        for html in (added.text, page(run)):
            assert "<script>fictional()" not in html
            assert "&lt;script&gt;fictional()&lt;/script&gt;" in html
            assert "{{ 7 * 7 }}" in html, "template syntax in a name is plain text"
        assert label_of(run, key_of(run, attack.strip())) == attack.strip()


# --- reminder rules --------------------------------------------------------------------


def test_the_rule_rows_show_the_seeded_rule_and_none_for_other_statuses(tmp_path):
    with importing(tmp_path) as run:
        html = page(run)
        rows = rule_rows(html)
        for key in OPEN_KEYS:
            assert key in rows, key
        applied = rule_fields(html, "applied")
        assert applied == {
            "enabled": True,
            "days": "14",
            "version": "1",
            "action": "/settings/board/reminders/applied",
        }
        for key in ("preparing", "screening", "interviewing", "offer"):
            assert rule_fields(html, key) == {
                "enabled": False,
                "days": "14",
                "version": "0",
                "action": f"/settings/board/reminders/{key}",
            }, key
        for key in OPEN_KEYS:
            controls = controls_in(html, rows[key])
            label = label_of(run, key)
            assert accessible_name(html, controls["enabled"]).startswith(
                f"Remind me if an application stays in {label}"
            ), key
            name = accessible_name(html, controls["days"]).lower()
            assert "days" in name and label.lower() in name, key
            assert controls["days"].attrs.get("type") == "number", key
        assert "Remind me if an application stays in Applied for" in " ".join(
            rows["applied"].text.split()
        )


def test_reminder_rule_days_validated_and_versioned(tmp_path):
    with importing(tmp_path) as run:
        path = rule_path("applied")
        seeded = run.rows("SELECT * FROM reminder_rules")[0]
        assert (seeded["status_key"], seeded["days"], seeded["enabled"], seeded["version"]) == (
            "applied",
            14,
            1,
            1,
        )

        # Days are a whole number from 1 to 365 in ASCII digits; the 422 is tied to the field,
        # keeps the typing and changes nothing (also while the rule is being switched off).
        before = tables(run)
        for days in (
            "0",
            "366",
            "-1",
            "+5",
            "1.5",
            "1e2",
            "abc",
            "",
            "14 days",
            ARABIC_INDIC_14,
            FULLWIDTH_14,
        ):
            for extra in ({"enabled": "1"}, {}):
                refused = post(run, path, version="1", days=days, **extra)
                assert refused.status_code == 422, (days, extra)
                assert errors_in(refused.text) == {"days": DAYS_ERROR}, (days, extra)
                assert invalid_names(refused.text) == ["days"], (days, extra)
                typed = rule_fields(refused.text, "applied")["days"]
                assert typed == days, (days, typed)
                assert section(refused.text)
        assert tables(run) == before

        # A save: version 2, the clock's updated_at, announced and shown.
        run.clock.advance(timedelta(minutes=5))
        saved = post(run, path, version="1", days="30", enabled="1")
        assert saved.status_code == 200
        row = run.rows("SELECT * FROM reminder_rules WHERE status_key='applied'")[0]
        assert (row["days"], row["enabled"], row["version"]) == (30, 1, 2)
        assert datetime.fromisoformat(row["updated_at"]) == run.clock.now()
        assert row["id"] == "applied-14-days", "the seeded rule keeps its id"
        assert rule_fields(saved.text, "applied") == {
            "enabled": True,
            "days": "30",
            "version": "2",
            "action": "/settings/board/reminders/applied",
        }
        assert done_text(saved.text) == "Reminder for Applied saved."
        assert "Reminder for Applied saved." in said_headers(saved.headers)
        assert autofocus_count(saved.text) == 1
        assert is_safe_id(row["id"])

        # Off: the rule stays with its days; announced differently.
        off = post(run, path, version="2", days="30")
        assert off.status_code == 200
        row = run.rows("SELECT days, enabled, version FROM reminder_rules")[0]
        assert row == {"days": 30, "enabled": 0, "version": 3}
        assert rule_fields(off.text, "applied")["enabled"] is False
        assert done_text(off.text) == "Reminder for Applied turned off."
        assert "Reminder for Applied turned off." in said_headers(off.headers)

        # The limits themselves are fine.
        assert post(run, path, version="3", days="1", enabled="1").status_code == 200
        assert post(run, path, version="4", days="365", enabled="1").status_code == 200
        assert run.rows("SELECT days, version FROM reminder_rules") == [{"days": 365, "version": 5}]

        # Stale versions (also 0 for a rule that exists) get the reload prompt and change nothing.
        before = tables(run)
        for version in ("1", "4", "0", "6"):
            stale = post(run, path, version=version, days="20", enabled="1")
            assert stale.status_code == 409, version
            assert VersionConflict.message in stale.text and "Reload" in stale.text
            assert f'href="{SETTINGS}"' in stale.text
        assert tables(run) == before

        # A status without a rule: version 0 creates it (a safe id, version 1); a second tab that
        # also holds 0 is told to reload.
        screening = rule_path("screening")
        created = post(run, screening, version="0", days="10", enabled="1")
        assert created.status_code == 200
        [rule] = run.rows("SELECT * FROM reminder_rules WHERE status_key='screening'")
        assert is_safe_id(rule["id"]) and (rule["days"], rule["enabled"], rule["version"]) == (
            10,
            1,
            1,
        )
        assert datetime.fromisoformat(rule["created_at"]) == run.clock.now()
        assert rule_fields(created.text, "screening")["version"] == "1"
        assert post(run, screening, version="0", days="12", enabled="1").status_code == 409
        # A rule for a status that has none can also be created switched off, with its days.
        offered = post(run, rule_path("offer"), version="0", days="21")
        assert offered.status_code == 200
        assert run.rows("SELECT days, enabled FROM reminder_rules WHERE status_key='offer'") == [
            {"days": 21, "enabled": 0}
        ]

        # A custom status gets a rule row of its own.
        waiting = add(run, "Waiting", "active")
        assert rule_fields(page(run), waiting)["version"] == "0"
        assert post(run, rule_path(waiting), version="0", days="7", enabled="1").status_code == 200

        # Malformed requests are a 400; unknown or unsafe statuses the fixed 404.
        before = tables(run)
        current = rule_fields(page(run), "applied")["version"]
        for data in (
            {"version": current, "days": "9", "enabled": "yes"},
            {"version": current, "days": "9", "enabled": "0"},
            {"version": current, "days": "9", "extra": "bad"},
            {"version": current, "enabled": "1"},
            {"days": "9", "enabled": "1"},
            {"version": "abc", "days": "9"},
            {"version": "-1", "days": "9"},
            {"version": [current, current], "days": "9"},
            {"version": current, "days": ["9", "9"]},
            {"version": current, "days": "9", "enabled": ["1", "1"]},
        ):
            assert post_data(run, path, data).status_code == 400, data
        assert tables(run) == before
        for key in ("missing", "bad.key", "x" * 90):
            answer = post(run, rule_path(key), version="0", days="9", enabled="1")
            assert answer.status_code == 404, key
            assert answer.text == StatusNotAllowed.message and key not in answer.text
        assert tables(run) == before


def test_rule_service_validates_days_and_checks_versions(tmp_path):
    with importing(tmp_path) as run:
        rules = run.app.state.reminder_rules
        before = tables(run)
        for days in (0, 366, -1, True, 1.5, "abc", "", None, ARABIC_INDIC_14):
            with pytest.raises(error("ReminderRuleInvalid")) as caught:
                rules.save("applied", days=days, enabled=True, expected_version=1)
            assert caught.value.field == "days" and caught.value.user_message == DAYS_ERROR
        with pytest.raises(VersionConflict):
            rules.save("applied", days=3, enabled=True, expected_version=2)
        with pytest.raises(error("StatusNotAllowed")):
            rules.save("missing", days=3, enabled=True, expected_version=0)
        assert tables(run) == before
        saved = rules.save("applied", days="3", enabled=False, expected_version=1)
        assert (saved.status_key, saved.days, saved.enabled, saved.version) == (
            "applied",
            3,
            False,
            2,
        )
        assert [(r.status_key, r.days, r.enabled, r.version) for r in rules.listing()] == [
            ("applied", 3, False, 2)
        ]


# --- token and origin ------------------------------------------------------------------


def test_settings_board_routes_need_token_and_origin(tmp_path):
    with importing(tmp_path) as run:
        spare = run.app.state.application_statuses.add("Spare", "closed")
        routes = [
            (STATUSES_PATH, {"label": "Waiting", "phase": "active"}),
            (status_path("applied", "rename"), {"version": "1", "label": "Sent"}),
            (status_path("screening", "move"), {"version": "1", "direction": "down"}),
            (status_path(spare.key, "delete"), {"version": str(spare.version)}),
            (rule_path("offer"), {"version": "0", "days": "20", "enabled": "1"}),
        ]
        before = tables(run)
        for path, data in routes:
            assert run.client.post(path, data=data).status_code == 403, path
            no_origin = run.client.post(path, data=data, headers=run.run.with_token())
            assert no_origin.status_code == 403, path
            hostile = run.client.post(
                path, data=data, headers=run.run.action(Origin="https://hostile.example.test")
            )
            assert hostile.status_code == 403, path
            assert post(run, path, extra="bad", **data).status_code == 400, path
            # Routes are POST only: with the token a GET is not one of them.
            assert run.client.get(path, headers=run.run.htmx()).status_code == 405, path
        assert tables(run) == before
        # With the token and the origin each works, so the 403s were about those.
        for path, data in routes:
            assert post(run, path, **data).status_code == 200, path


def test_adding_a_status_needs_both_fields_in_the_form(tmp_path):
    with importing(tmp_path) as run:
        before = tables(run)
        for data in ({"label": "Waiting"}, {"phase": "active"}, {}):
            assert post_data(run, STATUSES_PATH, data).status_code == 400, data
        assert tables(run) == before


# --- review changes: moves at the edge, focus, where the line is, names, hints -----------


def autofocused(html: str) -> list[Any]:
    box = section(html)
    return [e for e in elements(html) if "data-autofocus" in e.attrs and e.within(box)]


def move_buttons(html: str, key: str) -> dict[str, Any]:
    row = status_rows(html)[key]
    return {
        str(e.attrs["value"]): e
        for e in elements(html)
        if e.tag == "button" and e.attrs.get("name") == "direction" and e.within(row)
    }


def test_a_move_at_the_edge_of_a_phase_is_a_no_op_that_says_so(tmp_path):
    with importing(tmp_path) as run:
        html = page(run)
        # The first and last status of each phase have the matching button aria-disabled, with a
        # reason it is described by; the others have neither.
        edges = {
            "preparing": ("up", "down"),
            "applied": ("up",),
            "offer": ("down",),
            "rejected": ("up",),
            "declined": ("down",),
            "screening": (),
        }
        for key, disabled in edges.items():
            for direction, button in move_buttons(html, key).items():
                if direction in disabled:
                    assert button.attrs.get("aria-disabled") == "true", (key, direction)
                    reason = by_id(html, str(button.attrs.get("aria-describedby")))
                    assert reason is not None and "already" in reason.text, (key, direction)
                else:
                    assert button.attrs.get("aria-disabled") is None, (key, direction)

        before = tables(run)
        move = status_path("applied", "move")
        version = row_version(html, "applied")
        up = post(run, move, version=version, direction="up")
        assert up.status_code == 200
        assert done_text(up.text) == "Applied is already first in Active."
        assert "Applied is already first in Active." in said_headers(up.headers)
        assert tables(run) == before, "nothing is written"
        [target] = autofocused(up.text)
        assert (target.tag, target.attrs.get("value")) == ("button", "down")  # the other button
        down = post(run, status_path("offer", "move"), version="1", direction="down")
        assert done_text(down.text) == "Offer is already last in Active."
        assert tables(run) == before
        # A stale version still loses first.
        assert post(run, move, version="9", direction="up").status_code == 409
        assert tables(run) == before


def test_focus_after_a_move_stays_on_a_move_button_and_after_a_rename_on_the_name(tmp_path):
    with importing(tmp_path) as run:
        html = page(run)
        move = status_path("screening", "move")
        up = post(run, move, version=row_version(html, "screening"), direction="up")
        [target] = autofocused(up.text)
        # Screening is now first in Active: the same button would be disabled, so the other one.
        assert (target.tag, target.attrs.get("value")) == ("button", "down")
        assert target.within(status_rows(up.text)["screening"])
        again = post(run, move, version=row_version(up.text, "screening"), direction="down")
        [target] = autofocused(again.text)
        assert (target.tag, target.attrs.get("value")) == ("button", "down")  # now second
        renamed = post(
            run,
            status_path("screening", "rename"),
            version=row_version(again.text, "screening"),
            label="Phone screen",
        )
        [target] = autofocused(renamed.text)
        assert (target.tag, target.attrs.get("name")) == ("input", "label")
        assert target.within(status_rows(renamed.text)["screening"])


def test_the_done_line_sits_in_the_row_it_is_about(tmp_path):
    with importing(tmp_path) as run:
        added = post(run, STATUSES_PATH, label="Waiting", phase="active")
        waiting = key_of(run, "Waiting")
        line = by_id(added.text, DONE_ID)
        assert line is not None and line.within(status_rows(added.text)[waiting])
        renamed = post(
            run,
            status_path(waiting, "rename"),
            version=row_version(added.text, waiting),
            label="Pending",
        )
        line = by_id(renamed.text, DONE_ID)
        assert line is not None and line.within(status_rows(renamed.text)[waiting])
        moved = post(
            run,
            status_path(waiting, "move"),
            version=row_version(renamed.text, waiting),
            direction="up",
        )
        line = by_id(moved.text, DONE_ID)
        assert line is not None and line.within(status_rows(moved.text)[waiting])
        saved = post(run, rule_path("applied"), version="1", days="20", enabled="1")
        line = by_id(saved.text, DONE_ID)
        assert line is not None and line.within(rule_rows(saved.text)["applied"])
        deleted = post(
            run, status_path(waiting, "delete"), version=row_version(moved.text, waiting)
        )
        line = by_id(deleted.text, DONE_ID)
        heading = by_id(deleted.text, "board-statuses-heading")
        assert line is not None and heading is not None
        assert not line.within(status_list(deleted.text)), "no row is left to hold it"


def status_list(html: str) -> Any:
    found = by_id(html, "status-list")
    assert found is not None
    return found


def test_a_refused_delete_is_announced_once_and_tied_to_its_button(tmp_path):
    with importing(tmp_path) as run:
        key = add(run, "Waiting", "active")
        seeder(run).application("board-job-9", "Seed Buyer", "Larkspur Orchards", key)
        refused = post(run, status_path(key, "delete"), version=row_version(page(run), key))
        assert refused.status_code == 422
        error = by_id(refused.text, f"status-{key}-error")
        assert error is not None and error.text == IN_USE
        assert error.attrs.get("role") is None, "the announcement says it, not an alert too"
        assert IN_USE in said_headers(refused.headers)
        [target] = autofocused(refused.text)
        assert target is error
        [button] = [
            e
            for e in elements(refused.text)
            if e.tag == "button"
            and e.within(status_rows(refused.text)[key])
            and e.attrs.get("aria-label") == "Delete Waiting"
        ]
        assert button.attrs.get("aria-describedby") == f"status-{key}-error"


def test_buttons_name_the_status_or_the_rule_they_belong_to(tmp_path):
    with importing(tmp_path) as run:
        html = page(run)
        names = {
            e.attrs.get("aria-label"): e.text
            for e in elements(html)
            if e.tag == "button" and e.attrs.get("aria-label")
        }
        assert names["Rename Applied"] == "Rename"
        assert names["Save reminder for Applied"] == "Save"
        assert names["Move up Applied"] == "Move up"


def test_a_stale_section_says_it_now_shows_the_latest(tmp_path):
    with importing(tmp_path) as run:
        stale = post(run, status_path("applied", "rename"), version="7", label="Lost")
        assert stale.status_code == 409
        assert VersionConflict.message in stale.text
        assert "This list changed in another tab and now shows the latest." in stale.text
        assert "Check it and try again." in stale.text
        assert f'href="{SETTINGS}"' in stale.text and "Reload" in stale.text


def test_hints_explain_phases_moves_and_where_reminders_show(tmp_path):
    with importing(tmp_path) as run:
        html = page(run)
        phase = controls_in(html, by_id(html, "status-add-form"))["phase"]
        hint = by_id(html, str(phase.attrs.get("aria-describedby")))
        assert hint is not None
        assert "can't change a status's phase later" in hint.text
        for word in ("Preparing", "Active", "Closed"):
            assert word in hint.text
        text = section(html)
        assert "inside the phase" in " ".join(
            e.text for e in elements(html) if e.tag == "p" and e.within(text)
        )
        reminders = by_id(html, "reminder-list")
        assert reminders is not None
        paragraphs = [e.text for e in elements(html) if e.tag == "p" and e.within(section(html))]
        assert any("Due reminders appear on Today." in p for p in paragraphs)
        # A refused phase keeps the error first, then the hint.
        refused = post(run, STATUSES_PATH, label="Waiting", phase="")
        field = controls_in(refused.text, by_id(refused.text, "status-add-form"))["phase"]
        described = str(field.attrs.get("aria-describedby")).split()
        assert described[0] == "status-add-phase-error" and len(described) == 2
