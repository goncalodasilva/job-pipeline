"""M5.7 pure reminder rules: occurrences, stable keys, Done, Dismiss and Snooze (AC-M5.5;
ADR-013 and ADR-102 item 7).

``app/domain/reminders.py`` is pure (no I/O, no framework, no clock reads: every function takes
``now`` and ``zone``). The tests fix this interface; the implementer follows it.

Inputs (frozen dataclasses; every instant is aware, naive ones raise ``ValueError``):

- ``StatusEntry(id, from_status, to_status, changed_at)``: one ``status_history`` row.
- ``ApplicationFacts(id, status_key, history)``: ``history`` is a tuple of ``StatusEntry``,
  oldest first.
- ``EventFacts(id, application_id, type, starts_at, ends_at, all_day=False, status="scheduled",
  thank_you_reminder=False)``: ``type`` is ``interview``, ``follow_up``, ``deadline`` or
  ``offer_deadline``. An all-day event's ``starts_at`` is already the first instant of its local
  date in its own time zone (``app.domain.events.all_day_range``).
- ``ReminderRule(status_key, days, enabled=True)``.

Output: ``Occurrence(key, kind, application_id, due_at)``. ``kind`` is ``rule``, ``follow_up``,
``deadline``, ``offer_deadline`` or ``thank_you``; ``due_at`` is an aware UTC instant; ``key``
is a safe id (``app.domain.ids.is_safe_id``; the inputs' ids are at most 48 characters), so it
can sit in a URL path. Keys depend only on ids: a rule occurrence on its ``status_history`` row
id, an event occurrence on the event id (and its kind). They never depend on the clock, the
zone, the rule's days or the order of the inputs.

- ``rule_occurrence``: see ``build_occurrences``.
- ``build_occurrences(*, applications, events, rules, zone) -> tuple[Occurrence, ...]``:
  * Rules: an application has at most one rule occurrence, for its current status when that
    status has an enabled rule. It belongs to the status entry, the latest history row with
    ``to_status == status_key`` whose ``from_status`` differs from its ``to_status`` (a move to
    the status it is already in is recorded but is not a new entry). It is due at the first
    instant of the local date (in ``zone``) that is ``days`` calendar days after the local date
    of the entry's ``changed_at`` ("entered N or more days ago", counted in local dates, so
    daylight saving never shifts it by an hour). The first instant of a date is the one
    ``app.domain.events.all_day_range`` gives, also when midnight is skipped or repeated. A
    status change ends the old status's occurrence (the application has none for it any more);
    coming back to the status is a new entry with a new key.
  * Events: a ``follow_up``, ``deadline`` or ``offer_deadline`` is due at ``starts_at`` (an
    all-day one at its local midnight, which is its stored ``starts_at``). An ``interview``
    makes a ``thank_you`` occurrence, due at ``ends_at``, only with ``thank_you_reminder``; it
    makes nothing at its start. A cancelled event makes nothing. The flag means nothing on other
    types.
- ``snooze_date(choice, *, now, zone) -> date``: ``tomorrow`` is the local date after today,
  ``next_week`` seven local days after today (as the inbox snooze); anything else raises
  ``ValueError``.
- ``ReminderState(key, state="open", snooze_until=None)``: ``state`` is ``open``, ``done``,
  ``dismissed`` or ``snoozed``; ``snooze_until`` is a ``date`` only while snoozed.
- ``done(key)``, ``dismiss(key)`` and ``snooze(key, choice, *, now, zone)`` return the new state
  for that key (``snooze_until`` from ``snooze_date``).
- ``is_due(occurrence, state, *, now, zone) -> bool``: ``state`` may be ``None``. Due when
  ``due_at <= now`` and the state is absent or open; never for ``done`` or ``dismissed``; for
  ``snoozed`` only once the local date of ``now`` has reached ``snooze_until`` (the first
  instant of that date).
- ``due_reminders(occurrences, states, *, now, zone) -> tuple[Occurrence, ...]``: ``states`` maps
  keys to states (keys with no occurrence are ignored); the due ones, newest ``due_at`` first,
  ties by key.
- ``parse_days(raw) -> int`` for a rule's days: a whole number from 1 to 365 written in ASCII
  digits (an ``int`` in range is accepted too; a ``bool`` is not); anything else raises
  ``ValueError(DAYS_MESSAGE)``. ``DAYS_MESSAGE`` is "Enter a whole number of days from 1 to 365.".

Everything here is fictional.
"""

import importlib
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.events import all_day_range
from app.domain.ids import is_safe_id

UTC_ZONE = ZoneInfo("UTC")
DAYS_MESSAGE = "Enter a whole number of days from 1 to 365."
ARABIC_INDIC_14 = chr(0x661) + chr(0x664)  # int() reads these, the form must not
FULLWIDTH_14 = chr(0xFF11) + chr(0xFF14)


def reminders() -> Any:
    return importlib.import_module("app.domain.reminders")


def at(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    assert value.tzinfo is not None
    return value.astimezone(UTC)


def entry(entry_id: str, before: str | None, after: str, when: str) -> Any:
    return reminders().StatusEntry(entry_id, before, after, at(when))


def facts(application_id: str, status: str, *entries: Any) -> Any:
    return reminders().ApplicationFacts(application_id, status, tuple(entries))


def rule(status: str, days: int, *, enabled: bool = True) -> Any:
    return reminders().ReminderRule(status, days, enabled)


def event(event_id: str, kind: str, start: str, end: str | None = None, **more: Any) -> Any:
    return reminders().EventFacts(
        event_id, more.pop("application_id", "a1"), kind, at(start), at(end or start), **more
    )


def build(
    *,
    applications: Any = (),
    events: Any = (),
    rules: Any = (),
    zone: ZoneInfo = UTC_ZONE,
) -> tuple[Any, ...]:
    return tuple(
        reminders().build_occurrences(
            applications=tuple(applications), events=tuple(events), rules=tuple(rules), zone=zone
        )
    )


# --- rules: due after N local days ------------------------------------------------------------

# case, zone, status entered (UTC), days, due (UTC): the first instant of the local date that is
# ``days`` local dates after the entry's local date.
RULE_TABLE = (
    ("utc", "UTC", "2026-10-01T09:00:00+00:00", 14, "2026-10-15T00:00:00+00:00"),
    (
        "utc_entered_late_is_due_next_midnight",
        "UTC",
        "2026-10-01T23:59:59+00:00",
        1,
        "2026-10-02T00:00:00+00:00",
    ),
    ("a_year", "UTC", "2026-01-01T00:00:00+00:00", 365, "2027-01-01T00:00:00+00:00"),
    # Lisbon is UTC+1 in summer time: 23:30 UTC on 1 October is 00:30 on 2 October there.
    (
        "lisbon_local_date_ahead_of_utc",
        "Europe/Lisbon",
        "2026-10-01T23:30:00+00:00",
        14,
        "2026-10-15T23:00:00+00:00",
    ),
    # Auckland is UTC+13 in summer: 12:00 UTC on 31 December is already 1 January.
    (
        "auckland_new_year",
        "Pacific/Auckland",
        "2026-12-31T12:00:00+00:00",
        30,
        "2027-01-30T11:00:00+00:00",
    ),
    (
        "kolkata_half_hour_offset",
        "Asia/Kolkata",
        "2026-06-10T18:45:00+00:00",
        3,
        "2026-06-13T18:30:00+00:00",
    ),
    # Clocks go back on 1 November 2026 in New York. Entered on the evening of 24 October (EDT),
    # ten local dates later is 3 November, which begins at 05:00 UTC (EST). Counting 240 hours
    # would land on 4 November; keeping the wall-clock time would land at 23:30.
    (
        "new_york_across_the_end_of_summer_time",
        "America/New_York",
        "2026-10-25T03:30:00+00:00",
        10,
        "2026-11-03T05:00:00+00:00",
    ),
    # Clocks go forward on 14 March 2027: seven dates after 9 March begins at 04:00 UTC (EDT).
    (
        "new_york_across_the_start_of_summer_time",
        "America/New_York",
        "2027-03-10T03:00:00+00:00",
        7,
        "2027-03-16T04:00:00+00:00",
    ),
    # Havana starts summer time at midnight: 8 March 2026 begins at 01:00 local, 05:00 UTC.
    (
        "havana_midnight_skipped",
        "America/Havana",
        "2026-03-01T15:00:00+00:00",
        7,
        "2026-03-08T05:00:00+00:00",
    ),
    # Havana repeats midnight on 1 November 2026: the date begins at its first midnight.
    (
        "havana_midnight_repeated",
        "America/Havana",
        "2026-10-18T12:00:00+00:00",
        14,
        "2026-11-01T04:00:00+00:00",
    ),
)


@pytest.mark.parametrize("table", [RULE_TABLE], ids=["table"])
def test_rule_occurrence_due_after_n_days(table: tuple[tuple[Any, ...], ...]) -> None:
    module = reminders()
    for case, zone_name, entered, days, due in table:
        zone = ZoneInfo(zone_name)
        found = build(
            applications=[facts("a1", "applied", entry("h1", "preparing", "applied", entered))],
            rules=[rule("applied", days)],
            zone=zone,
        )
        assert len(found) == 1, case
        occurrence = found[0]
        assert occurrence.kind == "rule" and occurrence.application_id == "a1", case
        assert occurrence.due_at == at(due), (case, occurrence.due_at.isoformat())
        assert occurrence.due_at.utcoffset() == timedelta(0), case
        assert is_safe_id(occurrence.key), case
        # The boundary itself: one second before is not due, the instant is.
        before = at(due) - timedelta(seconds=1)
        assert not module.is_due(occurrence, None, now=before, zone=zone), case
        assert module.is_due(occurrence, None, now=at(due), zone=zone), case
        assert module.is_due(occurrence, None, now=at(due) + timedelta(days=400), zone=zone), case


def test_a_rule_due_date_the_zone_skips_is_due_at_the_start_of_the_next_date() -> None:
    # Samoa skipped 30 December 2011 entirely (UTC-10 became UTC+14 at that midnight).
    zone = ZoneInfo("Pacific/Apia")
    rules = [rule("applied", 1)]
    before = facts("a1", "applied", entry("h1", None, "applied", "2011-12-29T10:00:00+00:00"))
    [found] = build(applications=[before], rules=rules, zone=zone)
    assert found.due_at == at("2011-12-30T10:00:00+00:00")  # 31 December begins at UTC+14
    # The day before the gap exists: it begins at midnight UTC-10, and its end is the gap.
    earlier = facts("a2", "applied", entry("h2", None, "applied", "2011-12-28T10:00:00+00:00"))
    [normal] = build(applications=[earlier], rules=rules, zone=zone)
    assert normal.due_at == at("2011-12-29T10:00:00+00:00")


def test_a_rule_that_is_off_or_for_another_status_makes_no_occurrence() -> None:
    application = facts("a1", "applied", entry("h1", None, "applied", "2026-10-01T09:00:00+00:00"))
    assert build(applications=[application], rules=[rule("applied", 14, enabled=False)]) == ()
    assert build(applications=[application], rules=[rule("screening", 14)]) == ()
    assert build(applications=[application], rules=[]) == ()
    # Several rules: only the current status's counts.
    found = build(applications=[application], rules=[rule("screening", 3), rule("applied", 5)])
    assert [o.due_at for o in found] == [at("2026-10-06T00:00:00+00:00")]


def test_an_application_without_an_entry_for_its_status_has_no_occurrence() -> None:
    odd = facts("a1", "applied", entry("h1", None, "preparing", "2026-10-01T09:00:00+00:00"))
    assert build(applications=[odd], rules=[rule("applied", 14)]) == ()
    assert build(applications=[facts("a2", "applied")], rules=[rule("applied", 14)]) == ()


def test_rule_key_belongs_to_the_status_entry_not_to_the_days_or_the_zone() -> None:
    application = facts("a1", "applied", entry("h1", None, "applied", "2026-10-01T09:00:00+00:00"))
    [first] = build(applications=[application], rules=[rule("applied", 14)])
    [fewer] = build(applications=[application], rules=[rule("applied", 3)])
    [elsewhere] = build(
        applications=[application], rules=[rule("applied", 14)], zone=ZoneInfo("Asia/Tokyo")
    )
    assert first.key == fewer.key == elsewhere.key
    assert fewer.due_at < first.due_at


# --- a status change ends the old status's occurrence ------------------------------------------


def test_status_change_ends_old_rule_occurrence() -> None:
    rules = [rule("applied", 14), rule("screening", 7)]
    h1 = entry("h1", None, "preparing", "2026-10-01T08:00:00+00:00")
    h2 = entry("h2", "preparing", "applied", "2026-10-02T09:00:00+00:00")
    h3 = entry("h3", "applied", "screening", "2026-10-05T10:00:00+00:00")
    h4 = entry("h4", "screening", "applied", "2026-10-08T12:00:00+00:00")

    [applied] = build(applications=[facts("a1", "applied", h1, h2)], rules=rules)
    assert applied.due_at == at("2026-10-16T00:00:00+00:00")

    # Moved on to Screening: the Applied occurrence is gone, the Screening one is new.
    [screening] = build(applications=[facts("a1", "screening", h1, h2, h3)], rules=rules)
    assert screening.due_at == at("2026-10-12T00:00:00+00:00")
    assert screening.key != applied.key

    # Preparing has no rule at all: nothing while it is there.
    assert build(applications=[facts("a1", "preparing", h1)], rules=rules) == ()

    # Back to Applied: a new entry, new key, counted from the new move.
    [again] = build(applications=[facts("a1", "applied", h1, h2, h3, h4)], rules=rules)
    assert again.due_at == at("2026-10-22T00:00:00+00:00")
    assert again.key not in {applied.key, screening.key}


def test_a_move_to_the_status_it_is_already_in_does_not_start_a_new_entry() -> None:
    rules = [rule("applied", 14)]
    h1 = entry("h1", "preparing", "applied", "2026-10-02T09:00:00+00:00")
    again = entry("h2", "applied", "applied", "2026-10-09T12:00:00+00:00")
    [before] = build(applications=[facts("a1", "applied", h1)], rules=rules)
    [after] = build(applications=[facts("a1", "applied", h1, again)], rules=rules)
    assert after.key == before.key and after.due_at == before.due_at


def test_each_application_has_its_own_occurrence() -> None:
    rules = [rule("applied", 14)]
    one = facts("a1", "applied", entry("h1", None, "applied", "2026-10-01T09:00:00+00:00"))
    two = facts("a2", "applied", entry("h2", None, "applied", "2026-10-03T09:00:00+00:00"))
    found = build(applications=[one, two], rules=rules)
    assert {(o.application_id, o.due_at) for o in found} == {
        ("a1", at("2026-10-15T00:00:00+00:00")),
        ("a2", at("2026-10-17T00:00:00+00:00")),
    }
    assert len({o.key for o in found}) == 2


# --- events -----------------------------------------------------------------------------------

JAPAN_DAY = all_day_range(date(2026, 11, 10), ZoneInfo("Asia/Tokyo"))
CASE_NAMES = (
    "follow_up",
    "deadline_all_day",
    "offer_deadline",
    "thank_you",
    "cancelled",
    "deadline_timed",
    "interview_without_thank_you",
    "thank_you_flag_means_nothing_on_a_follow_up",
)


def event_cases() -> dict[str, tuple[Any, list[tuple[str, str]]]]:
    return {
        "follow_up": (
            event("e1", "follow_up", "2026-11-02T10:00:00+00:00", "2026-11-02T10:30:00+00:00"),
            [("follow_up", "2026-11-02T10:00:00+00:00")],
        ),
        # Its local midnight in Tokyo is 15:00 UTC the day before; midnight UTC would be wrong.
        "deadline_all_day": (
            event(
                "e2", "deadline", JAPAN_DAY[0].isoformat(), JAPAN_DAY[1].isoformat(), all_day=True
            ),
            [("deadline", "2026-11-09T15:00:00+00:00")],
        ),
        "offer_deadline": (
            event("e3", "offer_deadline", "2026-11-12T16:30:00+00:00", "2026-11-12T17:00:00+00:00"),
            [("offer_deadline", "2026-11-12T16:30:00+00:00")],
        ),
        # Due when the interview ends, and nothing at its start.
        "thank_you": (
            event(
                "e4",
                "interview",
                "2026-11-04T14:00:00+00:00",
                "2026-11-04T15:00:00+00:00",
                thank_you_reminder=True,
            ),
            [("thank_you", "2026-11-04T15:00:00+00:00")],
        ),
        "cancelled": (
            event(
                "e5",
                "follow_up",
                "2026-11-02T10:00:00+00:00",
                "2026-11-02T10:30:00+00:00",
                status="cancelled",
            ),
            [],
        ),
        "deadline_timed": (
            event("e6", "deadline", "2026-11-05T08:15:00+00:00", "2026-11-05T09:00:00+00:00"),
            [("deadline", "2026-11-05T08:15:00+00:00")],
        ),
        "interview_without_thank_you": (
            event("e7", "interview", "2026-11-04T14:00:00+00:00", "2026-11-04T15:00:00+00:00"),
            [],
        ),
        "thank_you_flag_means_nothing_on_a_follow_up": (
            event(
                "e8",
                "follow_up",
                "2026-11-02T10:00:00+00:00",
                "2026-11-02T10:30:00+00:00",
                thank_you_reminder=True,
            ),
            [("follow_up", "2026-11-02T10:00:00+00:00")],
        ),
    }


@pytest.mark.parametrize("name", CASE_NAMES)
def test_event_occurrences(name: str) -> None:
    given_event, expected = event_cases()[name]
    found = build(events=[given_event])
    assert [(o.kind, o.due_at) for o in found] == [(kind, at(due)) for kind, due in expected], name
    for occurrence in found:
        assert occurrence.application_id == "a1"
        assert occurrence.due_at.utcoffset() == timedelta(0)
        assert is_safe_id(occurrence.key)


@pytest.mark.parametrize("kind", ["interview", "follow_up", "deadline", "offer_deadline"])
def test_cancelled_events_make_no_occurrence_of_any_kind(kind: str) -> None:
    cancelled = event(
        "e1",
        kind,
        "2026-11-04T14:00:00+00:00",
        "2026-11-04T15:00:00+00:00",
        status="cancelled",
        thank_you_reminder=True,
    )
    assert build(events=[cancelled]) == ()


def test_event_keys_differ_between_events_and_do_not_move_with_the_time() -> None:
    first = event("e1", "follow_up", "2026-11-02T10:00:00+00:00")
    second = event("e2", "follow_up", "2026-11-02T10:00:00+00:00")
    later = event("e1", "follow_up", "2026-11-09T10:00:00+00:00")
    [a], [b], [c] = (build(events=[x]) for x in (first, second, later))
    assert a.key != b.key
    assert a.key == c.key  # the same event, moved: the reminder is the same one


def test_naive_instants_are_refused() -> None:
    module = reminders()
    naive = datetime(2026, 11, 2, 10, 0)  # noqa: DTZ001
    with pytest.raises(ValueError):
        module.StatusEntry("h1", None, "applied", naive)
    with pytest.raises(ValueError):
        module.EventFacts("e1", "a1", "follow_up", naive, naive)
    state = None
    occurrence = build(events=[event("e1", "follow_up", "2026-11-02T10:00:00+00:00")])[0]
    with pytest.raises(ValueError):
        module.is_due(occurrence, state, now=naive, zone=UTC_ZONE)
    with pytest.raises(ValueError):
        module.snooze("k", "tomorrow", now=naive, zone=UTC_ZONE)


@pytest.mark.parametrize(
    ("state", "until"),
    [
        ("pending", None),
        ("", None),
        ("snoozed", None),  # snoozed needs the date it lasts until
        ("done", date(2026, 11, 6)),  # only a snoozed reminder has one
        ("open", date(2026, 11, 6)),
    ],
)
def test_a_reminder_state_refuses_an_unknown_state_or_a_misplaced_snooze_date(
    state: str, until: date | None
) -> None:
    with pytest.raises(ValueError):
        reminders().ReminderState("k", state, until)


@pytest.mark.parametrize("days", [0, -1, 366, 1000])
def test_a_rule_needs_days_from_1_to_365(days: int) -> None:
    with pytest.raises(ValueError):
        reminders().ReminderRule("applied", days)


# --- Done, Dismiss and Snooze, by occurrence key ----------------------------------------------


def two_occurrences() -> tuple[Any, Any]:
    found = build(
        events=[
            event("e1", "follow_up", "2026-11-02T10:00:00+00:00"),
            event("e2", "deadline", "2026-11-03T10:00:00+00:00"),
        ]
    )
    by_kind = {o.kind: o for o in found}
    return by_kind["follow_up"], by_kind["deadline"]


def test_done_dismiss_snooze_by_occurrence_key() -> None:
    module = reminders()
    follow_up, deadline = two_occurrences()
    now = at("2026-11-05T09:00:00+00:00")

    def due(states: dict[str, Any]) -> list[str]:
        return [
            o.kind
            for o in module.due_reminders([follow_up, deadline], states, now=now, zone=UTC_ZONE)
        ]

    # Newest due first.
    assert due({}) == ["deadline", "follow_up"]

    done = module.done(follow_up.key)
    assert (done.key, done.state, done.snooze_until) == (follow_up.key, "done", None)
    dismissed = module.dismiss(deadline.key)
    assert (dismissed.key, dismissed.state, dismissed.snooze_until) == (
        deadline.key,
        "dismissed",
        None,
    )
    # An action ends only its own occurrence.
    assert due({follow_up.key: done}) == ["deadline"]
    assert due({deadline.key: dismissed}) == ["follow_up"]
    assert due({follow_up.key: done, deadline.key: dismissed}) == []
    # A state for a key with no occurrence changes nothing; an open state is no state.
    assert due({"unknown-key": module.done("unknown-key")}) == ["deadline", "follow_up"]
    assert due({follow_up.key: module.ReminderState(follow_up.key)}) == ["deadline", "follow_up"]
    # Done and Dismiss stay ended however much later it is.
    later = at("2027-01-01T00:00:00+00:00")
    states = {follow_up.key: done, deadline.key: dismissed}
    assert module.due_reminders([follow_up, deadline], states, now=later, zone=UTC_ZONE) == ()

    # Snooze hides until a local date, then the reminder is due again, in the same place.
    snoozed = module.snooze(follow_up.key, "tomorrow", now=now, zone=UTC_ZONE)
    assert (snoozed.key, snoozed.state, snoozed.snooze_until) == (
        follow_up.key,
        "snoozed",
        date(2026, 11, 6),
    )
    assert due({follow_up.key: snoozed}) == ["deadline"]
    reappears = at("2026-11-06T00:00:00+00:00")
    after = module.due_reminders(
        [follow_up, deadline], {follow_up.key: snoozed}, now=reappears, zone=UTC_ZONE
    )
    assert [o.kind for o in after] == ["deadline", "follow_up"]
    still = module.due_reminders(
        [follow_up, deadline],
        {follow_up.key: snoozed},
        now=reappears - timedelta(seconds=1),
        zone=UTC_ZONE,
    )
    assert [o.kind for o in still] == ["deadline"]


def test_a_reminder_is_not_due_before_its_time() -> None:
    module = reminders()
    follow_up, _ = two_occurrences()
    just_before = follow_up.due_at - timedelta(seconds=1)
    assert module.due_reminders([follow_up], {}, now=just_before, zone=UTC_ZONE) == ()
    assert module.due_reminders([follow_up], {}, now=follow_up.due_at, zone=UTC_ZONE) == (
        follow_up,
    )


def test_equal_due_times_are_ordered_by_key() -> None:
    module = reminders()
    found = build(
        events=[
            event("e2", "follow_up", "2026-11-02T10:00:00+00:00"),
            event("e1", "follow_up", "2026-11-02T10:00:00+00:00"),
        ]
    )
    now = at("2026-11-05T09:00:00+00:00")
    shown = module.due_reminders(found, {}, now=now, zone=UTC_ZONE)
    assert [o.key for o in shown] == sorted(o.key for o in found)


# case, zone, now, choice, snoozed until, reappears (UTC)
SNOOZES = (
    (
        "utc_tomorrow",
        "UTC",
        "2026-10-20T23:30:00+00:00",
        "tomorrow",
        "2026-10-21",
        "2026-10-21T00:00:00+00:00",
    ),
    # It is already 21 October in Auckland, so tomorrow is the 22nd.
    (
        "auckland_is_a_day_ahead",
        "Pacific/Auckland",
        "2026-10-20T23:30:00+00:00",
        "tomorrow",
        "2026-10-22",
        "2026-10-21T11:00:00+00:00",
    ),
    # It is still 20 October in Los Angeles: seven local days on is the 27th, at 00:00 PDT.
    (
        "los_angeles_next_week",
        "America/Los_Angeles",
        "2026-10-21T03:00:00+00:00",
        "next_week",
        "2026-10-27",
        "2026-10-27T07:00:00+00:00",
    ),
    # Summer time ends on 1 November: 4 November begins at 05:00 UTC.
    (
        "new_york_next_week_across_the_clock_change",
        "America/New_York",
        "2026-10-28T12:00:00+00:00",
        "next_week",
        "2026-11-04",
        "2026-11-04T05:00:00+00:00",
    ),
)


@pytest.mark.parametrize(("case", "zone_name", "now", "choice", "until", "reappears"), SNOOZES)
def test_snooze_uses_the_local_date(
    case: str, zone_name: str, now: str, choice: str, until: str, reappears: str
) -> None:
    module = reminders()
    zone = ZoneInfo(zone_name)
    assert module.snooze_date(choice, now=at(now), zone=zone) == date.fromisoformat(until), case
    [follow_up] = build(events=[event("e9", "follow_up", "2026-01-05T10:00:00+00:00")])
    state = module.snooze(follow_up.key, choice, now=at(now), zone=zone)
    assert state.state == "snoozed" and state.snooze_until == date.fromisoformat(until)
    # The reminder was due long before: only the snooze hides it.
    assert follow_up.due_at < at(now)
    states = {follow_up.key: state}
    hidden = module.is_due(follow_up, state, now=at(reappears) - timedelta(seconds=1), zone=zone)
    shown = module.is_due(follow_up, state, now=at(reappears), zone=zone)
    assert (hidden, shown) == (False, True), case
    assert module.due_reminders([follow_up], states, now=at(now), zone=zone) == ()


@pytest.mark.parametrize("choice", ["", "later", "today", "Tomorrow", "month", None])
def test_snooze_refuses_other_choices(choice: Any) -> None:
    module = reminders()
    now = at("2026-10-20T10:00:00+00:00")
    with pytest.raises(ValueError):
        module.snooze_date(choice, now=now, zone=UTC_ZONE)
    with pytest.raises(ValueError):
        module.snooze("some-key", choice, now=now, zone=UTC_ZONE)


@pytest.mark.parametrize("raw", ["1", "14", "365", 1, 14, 365, " 30 "], ids=lambda v: repr(v))
def test_days_accepts_whole_numbers_from_1_to_365(raw: Any) -> None:
    assert reminders().parse_days(raw) == int(str(raw).strip())


@pytest.mark.parametrize(
    "raw",
    [
        "",
        " ",
        "0",
        "366",
        "-1",
        "+5",
        "1.5",
        "1e2",
        "abc",
        "14 days",
        ARABIC_INDIC_14,
        FULLWIDTH_14,
        0,
        366,
        True,
        2.0,
        None,
    ],
    ids=lambda v: repr(v),
)
def test_days_refuses_anything_else_with_the_fixed_message(raw: Any) -> None:
    module = reminders()
    assert module.DAYS_MESSAGE == DAYS_MESSAGE
    with pytest.raises(ValueError) as caught:
        module.parse_days(raw)
    assert str(caught.value) == DAYS_MESSAGE


# --- keys are stable and unique ----------------------------------------------------------------

STATUSES = ("preparing", "applied", "screening", "offer")
ZONES = ("UTC", "Europe/Lisbon", "Asia/Tokyo", "America/New_York", "America/Havana")
BASE = datetime(2026, 1, 1, tzinfo=UTC)
EVENT_TYPES = ("interview", "follow_up", "deadline", "offer_deadline")


@st.composite
def worlds(draw: st.DrawFn) -> tuple[list[Any], list[Any], list[Any]]:
    module = reminders()
    applications = []
    counter = 0
    count = draw(st.integers(0, 4))
    for number in range(count):
        moved = BASE + timedelta(minutes=draw(st.integers(0, 60 * 24 * 30)))
        sequence = draw(st.lists(st.sampled_from(STATUSES), min_size=1, max_size=5))
        entries = []
        before = None
        for status in sequence:
            counter += 1
            moved += timedelta(minutes=draw(st.integers(1, 60 * 24 * 20)))
            entries.append(module.StatusEntry(f"h{counter}", before, status, moved))
            before = status
        applications.append(module.ApplicationFacts(f"a{number}", sequence[-1], tuple(entries)))
    events = []
    for number in range(draw(st.integers(0, 6))):
        kind = draw(st.sampled_from(EVENT_TYPES))
        start = BASE + timedelta(minutes=draw(st.integers(0, 60 * 24 * 90)))
        end = start + timedelta(minutes=draw(st.integers(0, 600)))
        all_day = kind in ("deadline", "offer_deadline") and draw(st.booleans())
        events.append(
            module.EventFacts(
                f"e{number}",
                f"a{draw(st.integers(0, max(count - 1, 0)))}",
                kind,
                start,
                end,
                all_day=all_day,
                status=draw(st.sampled_from(("scheduled", "scheduled", "cancelled"))),
                thank_you_reminder=draw(st.booleans()),
            )
        )
    rules = [
        module.ReminderRule(status, draw(st.integers(1, 365)), draw(st.booleans()))
        for status in STATUSES
        if draw(st.booleans())
    ]
    return applications, events, rules


def summary(found: Any) -> set[tuple[str, str, str, datetime]]:
    return {(o.key, o.kind, o.application_id, o.due_at) for o in found}


@settings(max_examples=120, deadline=None)
@given(world=worlds(), zone_name=st.sampled_from(ZONES), data=st.data())
def test_occurrence_keys_stable_and_unique(
    world: tuple[list[Any], list[Any], list[Any]], zone_name: str, data: st.DataObject
) -> None:
    module = reminders()
    applications, events, rules = world
    zone = ZoneInfo(zone_name)
    found = build(applications=applications, events=events, rules=rules, zone=zone)

    keys = [o.key for o in found]
    assert len(keys) == len(set(keys)), "every occurrence has its own key"
    assert all(is_safe_id(key) for key in keys)
    assert all(o.due_at.utcoffset() == timedelta(0) for o in found)
    rule_owners = [o.application_id for o in found if o.kind == "rule"]
    assert len(rule_owners) == len(set(rule_owners)), "at most one rule occurrence each"

    # The same inputs give the same answer, whatever the order they come in.
    shuffled = build(
        applications=data.draw(st.permutations(applications)),
        events=data.draw(st.permutations(events)),
        rules=data.draw(st.permutations(rules)),
        zone=zone,
    )
    assert summary(shuffled) == summary(found)
    assert summary(
        build(applications=applications, events=events, rules=rules, zone=zone)
    ) == summary(found)

    # Keys depend on ids only: not on the zone, nor on the rules' days.
    other = ZoneInfo(data.draw(st.sampled_from(ZONES)))
    changed = [module.ReminderRule(r.status_key, 1 + (r.days % 365), r.enabled) for r in rules]
    rekeyed = build(applications=applications, events=events, rules=changed, zone=other)
    assert {o.key for o in rekeyed} == set(keys)
