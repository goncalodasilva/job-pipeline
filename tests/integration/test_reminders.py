"""M5.7 due reminders: computed on read, nothing scheduled, actions by occurrence key
(AC-M5.5; ADR-013, ADR-102 item 7).

The interface the tests fix is written at the top of ``tests/reminder_desk.py`` (services, errors
and routes) and ``tests/unit/domain/test_reminders.py`` (the pure rules). A real migrated SQLite
database in a temporary folder, the frozen clock, sequential ids, no network and no provider; every
value is fictional.
"""

import ast
import importlib
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.domain.errors import VersionConflict, format_short_day
from app.domain.events import all_day_range
from app.domain.ids import is_safe_id
from tests.board_pages import said_headers
from tests.reminder_desk import (
    INVALID,
    SCHEDULING_WORDS,
    ReminderDesk,
    instant,
    kinds,
    reminding,
)

pytestmark = pytest.mark.integration

TOKYO = ZoneInfo("Asia/Tokyo")
BANNED_IMPORTS = {
    "asyncio",
    "threading",
    "sched",
    "time",
    "multiprocessing",
    "concurrent",
    "subprocess",
    "signal",
    "apscheduler",
}


def error(name: str) -> type[Exception]:
    found = getattr(importlib.import_module("app.domain.errors"), name)
    assert isinstance(found, type)
    return found


@pytest.fixture
def desk(tmp_path):
    working = ReminderDesk(tmp_path)  # UTC; the clock starts at 2026-10-20T09:00Z
    yield working
    working.close()


def applied(desk: ReminderDesk, job_id: str = "job-0001") -> Any:
    """An application moved to Applied at 09:01 UTC on 20 October (its rule is due 3 November)."""
    desk.add_job(job_id, "Greenhouse Technician")
    application = desk.apply(job_id).application
    desk.tick(minutes=1)
    return desk.move(application, "applied").application


# --- due reminders are computed when read ------------------------------------------------------


def test_ac_m5_5_due_reminders_computed_on_read_with_frozen_clock(desk):
    a = applied(desk)
    desk.event("e-follow", a, "follow_up", "2026-10-25T10:00:00+00:00")
    desk.event(
        "e-interview",
        a,
        "interview",
        "2026-10-26T14:00:00+00:00",
        ends_at="2026-10-26T15:00:00+00:00",
        thank_you=True,
    )
    day = all_day_range(date(2026, 10, 28), TOKYO)  # 28 October in Tokyo begins on the 27th UTC
    desk.event(
        "e-deadline",
        a,
        "deadline",
        day[0].isoformat(),
        ends_at=day[1].isoformat(),
        all_day=True,
        zone="Asia/Tokyo",
    )
    desk.event("e-offer", a, "offer_deadline", "2026-10-29T12:00:00+00:00")
    before = desk.tables()

    follow = ("follow_up", "2026-10-25T10:00:00+00:00")
    thanks = ("thank_you", "2026-10-26T15:00:00+00:00")
    deadline = ("deadline", "2026-10-27T15:00:00+00:00")
    offer = ("offer_deadline", "2026-10-29T12:00:00+00:00")
    rule = ("rule", "2026-11-03T00:00:00+00:00")
    # The clock is the only thing that changes; newest due first at every step.
    steps = [
        ("2026-10-20T09:02:00+00:00", []),
        ("2026-10-25T09:59:59+00:00", []),
        ("2026-10-25T10:00:00+00:00", [follow]),
        ("2026-10-26T14:59:59+00:00", [follow]),  # the interview itself is never a reminder
        ("2026-10-26T15:00:00+00:00", [thanks, follow]),
        ("2026-10-27T15:00:00+00:00", [deadline, thanks, follow]),
        ("2026-10-29T12:00:00+00:00", [offer, deadline, thanks, follow]),
        ("2026-11-02T23:59:59+00:00", [offer, deadline, thanks, follow]),
        ("2026-11-03T00:00:00+00:00", [rule, offer, deadline, thanks, follow]),
        ("2026-10-25T10:00:00+00:00", [follow]),  # back in time: nothing was remembered
    ]
    for when, expected in steps:
        desk.at(when)
        shown = desk.reminders.due()
        assert [(r.kind, r.due_at) for r in shown] == [(k, instant(d)) for k, d in expected], when
        assert all(r.application_id == a.id and r.version == 0 for r in shown), when
        assert all(is_safe_id(r.key) for r in shown), when
        assert len({r.key for r in shown}) == len(shown), when
    assert desk.tables() == before, "reading wrote nothing"
    assert desk.rows("SELECT count(*) FROM reminder_states") == [(0,)]
    assert desk.rows("SELECT count(*) FROM tasks") == [(0,)]


def test_a_status_change_ends_the_rule_reminder_and_coming_back_starts_a_new_one(desk):
    a = applied(desk)
    desk.at("2026-11-03T00:00:00+00:00")
    [old] = desk.reminders.due()
    assert (old.kind, old.application_id) == ("rule", a.id)

    desk.move(a, "screening")  # no rule for Screening
    assert desk.reminders.due() == ()
    desk.at("2026-12-01T00:00:00+00:00")
    assert desk.reminders.due() == (), "the old status's reminder never comes back by itself"

    desk.move(a, "applied")  # a new entry: counted again from today (1 December)
    assert desk.reminders.due() == ()
    desk.at("2026-12-14T23:59:59+00:00")
    assert desk.reminders.due() == ()
    desk.at("2026-12-15T00:00:00+00:00")
    [new] = desk.reminders.due()
    assert new.kind == "rule" and new.key != old.key
    # The old occurrence can no longer be acted on.
    with pytest.raises(error("ReminderNotFound")):
        desk.reminders.done(old.key, expected_version=0)


def test_a_move_to_the_same_status_keeps_the_original_entry(desk):
    a = applied(desk)
    desk.at("2026-11-03T00:00:00+00:00")
    [first] = desk.reminders.due()
    desk.move(a, "applied")  # recorded in the history, but the application did not move
    [again] = desk.reminders.due()
    assert (again.key, again.due_at) == (first.key, first.due_at)


def test_rules_decide_what_is_due_and_events_that_are_cancelled_make_none(desk):
    a = applied(desk)
    desk.event("e-follow", a, "follow_up", "2026-10-25T10:00:00+00:00", status="cancelled")
    desk.at("2026-10-26T00:00:00+00:00")
    assert desk.reminders.due() == ()
    with desk.db.transaction() as conn:
        conn.execute("UPDATE events SET status='scheduled' WHERE id='e-follow'")
    assert kinds(desk.reminders.due()) == ["follow_up"]

    # The seeded rule is "Applied for 14 days", on.
    [seeded] = desk.rules.listing()
    assert (seeded.status_key, seeded.days, seeded.enabled, seeded.version) == (
        "applied",
        14,
        True,
        1,
    )
    # Fewer days: due earlier, on the same occurrence, computed when read.
    desk.at("2026-10-28T00:00:00+00:00")
    assert kinds(desk.reminders.due()) == ["follow_up"]
    desk.rules.save("applied", days=7, enabled=True, expected_version=1)
    assert kinds(desk.reminders.due()) == ["rule", "follow_up"]
    # Switched off: no occurrence at all; the rule is kept with its days.
    desk.rules.save("applied", days=7, enabled=False, expected_version=2)
    assert kinds(desk.reminders.due()) == ["follow_up"]
    [kept] = desk.rules.listing()
    assert (kept.days, kept.enabled, kept.version) == (7, False, 3)
    # A rule for a status the application is not in changes nothing for it.
    desk.rules.save("screening", days=1, enabled=True, expected_version=0)
    assert kinds(desk.reminders.due()) == ["follow_up"]


# --- actions on a reminder, by occurrence key ---------------------------------------------------


def test_actions_end_or_hide_a_reminder_and_each_change_bumps_its_version(tmp_path):
    lisbon = ReminderDesk(tmp_path, zone="Europe/Lisbon")  # 20 October is summer time, UTC+1
    try:
        a = applied(lisbon)
        for number in (1, 2, 3):
            lisbon.event(f"e-{number}", a, "follow_up", f"2026-10-2{number}T10:00:00+00:00")
        lisbon.at("2026-10-24T12:00:00+00:00")
        by_due = {r.due_at: r for r in lisbon.reminders.due()}
        one, two, three = (by_due[instant(f"2026-10-2{n}T10:00:00+00:00")] for n in (1, 2, 3))
        assert (one.version, two.version, three.version) == (0, 0, 0)

        # A stale or wrong version loses and writes nothing.
        before = lisbon.tables()
        with pytest.raises(VersionConflict):
            lisbon.reminders.done(one.key, expected_version=1)
        assert lisbon.tables() == before

        done = lisbon.reminders.done(one.key, expected_version=0)
        assert (done.key, done.state, done.version) == (one.key, "done", 1)
        assert done.snooze_until is None
        dismissed = lisbon.reminders.dismiss(two.key, expected_version=0)
        assert (dismissed.state, dismissed.version) == ("dismissed", 1)
        assert [r.key for r in lisbon.reminders.due()] == [three.key]
        rows = lisbon.rows(
            "SELECT occurrence_key, state, snooze_until, seen_at, version, created_at, updated_at "
            "FROM reminder_states ORDER BY occurrence_key"
        )
        now = lisbon.clock.now().isoformat()
        assert sorted(rows) == sorted(
            [
                (one.key, "done", None, None, 1, now, now),
                (two.key, "dismissed", None, None, 1, now, now),
            ]
        )
        # Done and Dismiss are final for that occurrence: repeating needs the new version, and
        # a tab that still holds version 0 is told to reload.
        with pytest.raises(VersionConflict):
            lisbon.reminders.done(one.key, expected_version=0)
        assert lisbon.rows(
            "SELECT version FROM reminder_states WHERE occurrence_key=?", (one.key,)
        ) == [(1,)]

        # Snooze hides until a local date: tomorrow in Lisbon is 25 October, which begins at
        # 23:00 UTC on the 24th.
        snoozed = lisbon.reminders.snooze(three.key, "tomorrow", expected_version=0)
        assert (snoozed.state, snoozed.version) == ("snoozed", 1)
        assert str(snoozed.snooze_until) == "2026-10-25"
        assert lisbon.rows(
            "SELECT snooze_until FROM reminder_states WHERE occurrence_key=?", (three.key,)
        ) == [("2026-10-25",)]
        assert lisbon.reminders.due() == ()
        lisbon.at("2026-10-24T22:59:59+00:00")
        assert lisbon.reminders.due() == ()
        lisbon.at("2026-10-24T23:00:00+00:00")
        [back] = lisbon.reminders.due()
        assert (back.key, back.version) == (three.key, 1)
        # Snooze again, then finish it: each change moves the version on by one.
        again = lisbon.reminders.snooze(three.key, "next_week", expected_version=1)
        assert (again.version, str(again.snooze_until)) == (2, "2026-11-01")
        lisbon.at("2026-11-01T00:00:00+00:00")  # Lisbon is on winter time from 25 October
        [back] = lisbon.reminders.due()
        finished = lisbon.reminders.done(three.key, expected_version=back.version)
        assert (finished.state, finished.version) == ("done", 3)
        assert finished.snooze_until is None
        assert lisbon.rows(
            "SELECT snooze_until FROM reminder_states WHERE occurrence_key=?", (three.key,)
        ) == [(None,)]
        assert lisbon.reminders.due() == ()

        # Keys that are no current occurrence are refused, whatever the version.
        for key in ("unknown-key", "", "bad.key", "../x", "a" * 80):
            for act in (
                lambda k: lisbon.reminders.done(k, expected_version=0),
                lambda k: lisbon.reminders.dismiss(k, expected_version=0),
                lambda k: lisbon.reminders.snooze(k, "tomorrow", expected_version=0),
            ):
                with pytest.raises(error("ReminderNotFound")):
                    act(key)
        assert lisbon.rows("SELECT count(*) FROM reminder_states") == [(3,)]
    finally:
        lisbon.close()


def test_reminder_actions_versioned(tmp_path):
    with reminding(tmp_path) as r:  # Lisbon, summer time until 25 October
        a = r.applied("rem-job-1")
        for number in (1, 2, 3):
            r.event(f"e-{number}", a, "follow_up", f"2026-10-2{number}T10:00:00+00:00")
        r.at("2026-10-23T10:00:00+00:00")
        by_due = {d.due_at: d for d in r.due()}
        one, two, three = (by_due[instant(f"2026-10-2{n}T10:00:00+00:00")] for n in (1, 2, 3))
        paths = {kind: f"/reminders/{one.key}/{kind}" for kind in ("done", "dismiss", "snooze")}

        # Token and origin: nothing without them, and nothing is written.
        before = r.tables()
        for path, data in (
            (paths["done"], {"version": "0"}),
            (paths["dismiss"], {"version": "0"}),
            (paths["snooze"], {"version": "0", "choice": "tomorrow"}),
        ):
            assert r.client.post(path, data=data).status_code == 403, path
            no_origin = r.client.post(path, data=data, headers=r.run.with_token())
            assert no_origin.status_code == 403, path
            hostile = r.client.post(
                path, data=data, headers=r.run.action(Origin="https://hostile.example.test")
            )
            assert hostile.status_code == 403, path
        assert r.tables() == before

        # Malformed forms are a plain-text 400.
        for name, path, data in (
            ("extra field", paths["done"], {"version": "0", "extra": "bad"}),
            ("no version", paths["done"], {}),
            ("text version", paths["dismiss"], {"version": "abc"}),
            ("negative version", paths["dismiss"], {"version": "-1"}),
            ("fractional version", paths["done"], {"version": "1.5"}),
            ("empty version", paths["done"], {"version": ""}),
            ("repeated version", paths["done"], {"version": ["0", "0"]}),
            ("snooze needs a choice", paths["snooze"], {"version": "0"}),
            ("unknown choice", paths["snooze"], {"version": "0", "choice": "later"}),
            ("a month is not offered", paths["snooze"], {"version": "0", "choice": "month"}),
            ("done takes no choice", paths["done"], {"version": "0", "choice": "tomorrow"}),
        ):
            answer = r.post_data(path, data)
            assert answer.status_code == 400, name
            assert answer.headers["content-type"].startswith("text/plain"), name
            assert answer.text == INVALID, name
        assert r.tables() == before

        # Unknown and unsafe keys are the fixed 404 and are never echoed back.
        not_found = error("ReminderNotFound").message
        for key in ("unknown-key", "bad.key", "x" * 90):
            for kind, extra in (("done", {}), ("dismiss", {}), ("snooze", {"choice": "tomorrow"})):
                answer = r.post(f"/reminders/{key}/{kind}", version="0", **extra)
                assert answer.status_code == 404, (key, kind)
                assert answer.text == not_found and key not in answer.text
        assert r.tables() == before

        # A stale version is a 409 with the reload prompt and changes nothing.
        stale = r.post(paths["done"], version="1")
        assert stale.status_code == 409 and VersionConflict.message in stale.text
        assert "Reload" in stale.text
        assert r.tables() == before

        # Done: 200 with the change event and the announcement; the reminder is gone.
        done = r.post(paths["done"], version="0")
        assert done.status_code == 200
        said = said_headers(done.headers)
        assert "reminders:changed" in said and "Reminder marked done." in said
        assert r.rows("SELECT occurrence_key, state, version FROM reminder_states") == [
            {"occurrence_key": one.key, "state": "done", "version": 1}
        ]
        assert one.key not in [d.key for d in r.due()]
        # The same click from a second tab (still version 0) is a conflict, not a second write.
        again = r.post(paths["done"], version="0")
        assert again.status_code == 409 and VersionConflict.message in again.text
        assert r.rows("SELECT version FROM reminder_states") == [{"version": 1}]
        # With the version it holds now, an action goes through (and bumps it).
        assert r.post(paths["dismiss"], version="1").status_code == 200
        assert r.rows("SELECT state, version FROM reminder_states") == [
            {"state": "dismissed", "version": 2}
        ]

        # Dismiss and Snooze on the others.
        dismissed = r.post(f"/reminders/{two.key}/dismiss", version="0")
        assert dismissed.status_code == 200
        assert "Reminder dismissed." in said_headers(dismissed.headers)
        assert "reminders:changed" in said_headers(dismissed.headers)
        snooze_path = f"/reminders/{three.key}/snooze"
        snoozed = r.post(snooze_path, version="0", choice="tomorrow")
        assert snoozed.status_code == 200
        until = date(2026, 10, 24)  # 23 October is the local date (11:00 in Lisbon)
        assert f"Reminder snoozed until {format_short_day(until)}." in said_headers(snoozed.headers)
        assert "reminders:changed" in said_headers(snoozed.headers)
        assert r.rows(
            "SELECT state, snooze_until, version FROM reminder_states WHERE occurrence_key=?",
            (three.key,),
        ) == [{"state": "snoozed", "snooze_until": "2026-10-24", "version": 1}]
        assert r.due() == ()
        # Snoozing a snoozed reminder needs its current version, too.
        assert r.post(snooze_path, version="0", choice="next_week").status_code == 409
        later = r.post(snooze_path, version="1", choice="next_week")
        assert later.status_code == 200
        assert f"Reminder snoozed until {format_short_day(date(2026, 10, 30))}." in said_headers(
            later.headers
        )

        # Without htmx the browser is sent back to the app.
        r.at("2026-10-30T00:00:00+00:00")
        [back] = r.due()
        plain = r.post(
            snooze_path.replace("snooze", "dismiss"), htmx=False, version=str(back.version)
        )
        assert plain.status_code == 303 and plain.headers["location"] == "/"
        assert r.due() == ()


def test_actions_are_post_only(tmp_path):
    with reminding(tmp_path) as r:
        a = r.applied("rem-job-1")
        r.event("e-1", a, "follow_up", "2026-10-21T10:00:00+00:00")
        r.at("2026-10-22T10:00:00+00:00")
        [one] = r.due()
        before = r.tables()
        for kind in ("done", "dismiss", "snooze"):
            assert r.get(f"/reminders/{one.key}/{kind}").status_code == 405
        assert r.tables() == before


# --- nothing is scheduled ------------------------------------------------------------------------


def imported_names(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def sleeps_or_timers(path: Path) -> list[str]:
    return [
        node.attr
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Attribute) and node.attr in {"sleep", "Timer", "call_later"}
    ]


def test_ac_m5_5_nothing_scheduled_no_task_or_timer(tmp_path):
    from app.modules.ai.tasks import TASK_SPECS

    # No scheduler in the code that builds or reads reminders.
    for name in ("app.domain.reminders", "app.services.reminders"):
        source = Path(importlib.import_module(name).__file__)
        assert imported_names(source) & BANNED_IMPORTS == set(), name
        assert sleeps_or_timers(source) == [], name

    with reminding(tmp_path) as r:
        # No AI task spec, no task type or handler for reminders.
        registered = [name for name, _spec in r.app.state.task_registry.specs()]
        # Exactly the task types of before M5.7 (the practice task is offered with fakes).
        assert sorted(registered) == [
            "assess_fit",
            "draft_cv_template",
            "extract_cv",
            "gap_check",
            "practice",
            "propose_angles",
            "refine_fact",
            "summarize_job",
            "tailor_cv",
        ]
        assert sorted(TASK_SPECS) == [
            "assess_fit",
            "draft_cv_template",
            "extract_cv",
            "gap_check",
            "propose_angles",
            "refine_fact",
            "summarize_job",
            "tailor_cv",
        ]
        for name in (*registered, *TASK_SPECS):
            assert not any(word in name.lower() for word in SCHEDULING_WORDS), name
        # The lifespan started no thread or timer of its own for them, and nothing on the app
        # state is one.
        assert not [t.name for t in threading.enumerate() if "remind" in t.name.lower()]
        for attribute, value in vars(r.app.state)["_state"].items():
            assert not isinstance(value, threading.Thread | threading.Timer), attribute

        a = r.applied("rem-job-1")
        r.event("e-1", a, "follow_up", "2026-10-21T10:00:00+00:00")
        r.at("2026-11-05T00:00:00+00:00")
        tables_before = r.tables()
        tasks_before = r.rows("SELECT * FROM tasks")

        # Reading, as often as it likes and from wherever, writes no row (no state row, no seen
        # mark, no task) and starts no thread.
        for _ in range(3):
            assert kinds(r.due()) == ["rule", "follow_up"]
        for path in ("/settings", "/board", "/"):
            assert r.get(path, token=False).status_code == 200, path
            assert r.get(path, htmx=True).status_code == 200, path
        assert r.tables() == tables_before
        assert r.rows("SELECT * FROM tasks") == tasks_before == []
        assert r.rows("SELECT count(*) AS n FROM reminder_states") == [{"n": 0}]
        assert not [t.name for t in threading.enumerate() if "remind" in t.name.lower()]

        # Time passing changes only what the next read computes: no job ran in between.
        r.clock.advance(timedelta(days=90))
        assert r.tables() == tables_before


def test_ac_m5_5_reminder_due_after_restart_with_later_clock(tmp_path):
    watched = ("reminder_states", "reminder_rules", "tasks", "events", "status_history")
    with reminding(tmp_path, now="2026-10-20T09:00:00+00:00") as first:
        a = first.applied("rem-job-1")
        first.event("e-follow", a, "follow_up", "2026-10-23T10:00:00+00:00")
        assert first.due() == ()
        stored = first.tables()

    # The app is closed for weeks: nothing runs, nothing is written. Opening it again on the same
    # folders with a later clock shows what became due meanwhile, computed from the stored rows.
    with reminding(tmp_path, now="2026-11-09T08:00:00+00:00", seed=False) as second:
        reopened = second.tables()
        for name in watched:
            assert reopened[name] == stored[name], f"{name} was changed by closing and opening"
        assert second.rows("SELECT count(*) AS n FROM reminder_states") == [{"n": 0}]
        due = second.due()
        assert [(d.kind, d.due_at) for d in due] == [
            ("rule", instant("2026-11-03T00:00:00+00:00")),  # Applied for 14 days, in Lisbon
            ("follow_up", instant("2026-10-23T10:00:00+00:00")),
        ]
        assert all(d.application_id == a.id and d.version == 0 for d in due)
        assert second.tables()["reminder_states"] == stored["reminder_states"]
        assert second.tables()["tasks"] == stored["tasks"]
        # Computed from the clock alone: an earlier clock on the same data shows less.
        second.at("2026-10-22T00:00:00+00:00")
        assert second.due() == ()
        second.at("2026-10-23T10:00:00+00:00")
        assert kinds(second.due()) == ["follow_up"]
