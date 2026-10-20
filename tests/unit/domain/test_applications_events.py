"""Pure application and event rules, including gaps and repeated local hours."""

import importlib
from datetime import UTC, date, datetime, timedelta
from itertools import product
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

NOW = datetime(2026, 10, 20, 10, tzinfo=UTC)


def applications():
    return importlib.import_module("app.domain.applications")


def events():
    return importlib.import_module("app.domain.events")


@pytest.mark.parametrize("phase", [None, "", "other"])
def test_ac_m5_1_custom_status_must_belong_to_a_phase(phase: str | None) -> None:
    with pytest.raises(ValueError):
        applications().create_status(key="custom", label="Waiting", phase=phase, position=10)


@pytest.mark.parametrize("label", ["", "  ", "\u200b", "Ready\nnow"])
def test_status_needs_visible_single_line_label(label: str) -> None:
    with pytest.raises(ValueError):
        applications().create_status(key="custom", label=label, phase="active", position=10)


def test_status_label_unique_casefolded_and_builtins_rename_but_not_delete() -> None:
    module = applications()
    with pytest.raises(ValueError):
        module.create_status(
            key="custom",
            label=" STRASSE ",
            phase="active",
            position=10,
            existing_labels=("Straße",),
        )
    renamed = module.create_status(
        key="applied", label="Sent", phase="active", position=1, built_in=True
    )
    assert renamed.label == "Sent"
    with pytest.raises(ValueError):
        module.ensure_status_deletable(renamed, in_use=False)
    custom = module.create_status(key="custom", label="Waiting", phase="active", position=10)
    with pytest.raises(ValueError):
        module.ensure_status_deletable(custom, in_use=True)
    module.ensure_status_deletable(custom, in_use=False)


STATUSES = (
    ("preparing", "preparing"),
    ("applied", "active"),
    ("screening", "active"),
    ("interviewing", "active"),
    ("offer", "active"),
    ("rejected", "closed"),
    ("withdrawn", "closed"),
    ("accepted", "closed"),
    ("declined", "closed"),
)


@pytest.mark.parametrize("before,after", list(product(STATUSES, repeat=2)))
def test_status_moves(before: tuple[str, str], after: tuple[str, str]) -> None:
    module = applications()
    target = module.create_status(
        key=after[0], label=after[0].title(), phase=after[1], position=1, built_in=True
    )
    moved = module.move_application(before[0], None, target, changed_at=NOW, source="menu")
    assert (moved.from_status, moved.to_status, moved.changed_at, moved.source) == (
        before[0],
        after[0],
        NOW,
        "menu",
    )
    assert moved.applied_at == (NOW if after[1] == "active" else None)


def test_applied_at_set_once_on_first_active_status() -> None:
    module = applications()
    first = NOW - timedelta(days=7)
    for key, phase in STATUSES:
        target = module.create_status(
            key=key, label=key.title(), phase=phase, position=1, built_in=True
        )
        assert (
            module.move_application(
                "applied", first, target, changed_at=NOW, source="board"
            ).applied_at
            == first
        )


@pytest.mark.parametrize(
    "key,phase,expected",
    [
        ("interviewing", "active", "interview_date"),
        ("rejected", "closed", "feedback"),
        ("applied", "active", None),
        ("custom", "active", None),
    ],
)
def test_follow_up_offers_for_interviewing_and_rejected_only(
    key: str, phase: str, expected: str | None
) -> None:
    module = applications()
    target = module.create_status(
        key=key, label="Renamed", phase=phase, position=1, built_in=key != "custom"
    )
    assert (
        module.move_application(
            "preparing", None, target, changed_at=NOW, source="drawer"
        ).follow_up
        == expected
    )


@pytest.mark.parametrize(
    "zone,gap,repeated,expected",
    [
        (
            "Europe/Lisbon",
            datetime(2026, 3, 29, 1, 30, tzinfo=UTC).replace(tzinfo=None),
            datetime(2026, 10, 25, 1, 30, tzinfo=UTC).replace(tzinfo=None),
            datetime(2026, 10, 25, 0, 30, tzinfo=UTC),
        ),
        (
            "America/New_York",
            datetime(2026, 3, 8, 2, 30, tzinfo=UTC).replace(tzinfo=None),
            datetime(2026, 11, 1, 1, 30, tzinfo=UTC).replace(tzinfo=None),
            datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
        ),
    ],
)
def test_wall_time_gap_refused_and_fold_takes_first(
    zone: str, gap: datetime, repeated: datetime, expected: datetime
) -> None:
    module = events()
    with pytest.raises(ValueError, match="does not exist"):
        module.wall_time_to_utc(gap, ZoneInfo(zone))
    result = module.wall_time_to_utc(repeated.replace(fold=1), ZoneInfo(zone))
    assert result.instant == expected
    assert result.repeated is True
    assert "first occurrence" in result.notice


@given(
    instant=st.datetimes(
        min_value=datetime(2000, 1, 1, tzinfo=UTC).replace(tzinfo=None),
        max_value=datetime(2040, 1, 1, tzinfo=UTC).replace(tzinfo=None),
        timezones=st.just(UTC),
    ),
    zone=st.sampled_from(
        ["Europe/Lisbon", "America/New_York", "Asia/Kolkata", "Australia/Lord_Howe", "Pacific/Apia"]
    ),
)
@settings(max_examples=60, deadline=None)
def test_wall_time_round_trips_outside_gaps(instant: datetime, zone: str) -> None:
    local = instant.astimezone(ZoneInfo(zone))
    result = events().wall_time_to_utc(local.replace(tzinfo=None), ZoneInfo(zone))
    assert result.instant == (local.replace(fold=0).astimezone(UTC) if local.fold else instant)


@pytest.mark.parametrize("day,hours", [(date(2026, 3, 8), 23), (date(2026, 11, 1), 25)])
def test_all_day_dates_keep_local_midnights_across_dst(day: date, hours: int) -> None:
    start, end = events().all_day_range(day, ZoneInfo("America/New_York"))
    assert start.tzinfo == UTC and end.tzinfo == UTC
    assert start.astimezone(ZoneInfo("America/New_York")).date() == day
    assert end - start == timedelta(hours=hours)


def test_next_event_excludes_cancelled_and_finished_and_activity_uses_supplied_times() -> None:
    module = events()
    samples = [
        module.ScheduledEvent("past", NOW - timedelta(hours=2), NOW - timedelta(hours=1)),
        module.ScheduledEvent(
            "cancelled", NOW + timedelta(hours=1), NOW + timedelta(hours=2), "cancelled"
        ),
        module.ScheduledEvent("next", NOW + timedelta(days=1), NOW + timedelta(days=1, hours=1)),
        module.ScheduledEvent("later", NOW + timedelta(days=2), NOW + timedelta(days=2, hours=1)),
    ]
    assert module.next_event(samples, NOW).id == "next"
    assert module.next_event(samples[:2], NOW) is None
    assert module.last_activity([], fallback=NOW) == NOW
    assert (
        module.last_activity([NOW - timedelta(days=1), NOW], fallback=NOW - timedelta(days=2))
        == NOW
    )


@pytest.mark.parametrize("key,position", [("../invalid", 1), ("custom", -1), ("custom", True)])
def test_status_key_and_position_are_validated(key: str, position: int) -> None:
    with pytest.raises(ValueError):
        applications().create_status(key=key, label="Waiting", phase="active", position=position)


@pytest.mark.parametrize("invalid", ["changed_at", "applied_at", "source"])
def test_status_change_requires_aware_times_and_known_source(invalid: str) -> None:
    module = applications()
    target = module.create_status(key="applied", label="Applied", phase="active", position=1)
    changed = NOW.replace(tzinfo=None) if invalid == "changed_at" else NOW
    applied = NOW.replace(tzinfo=None) if invalid == "applied_at" else None
    source = "invalid" if invalid == "source" else "menu"
    with pytest.raises(ValueError):
        module.move_application("preparing", applied, target, changed_at=changed, source=source)


def test_wall_time_input_and_all_day_length_validated() -> None:
    with pytest.raises(ValueError):
        events().wall_time_to_utc(NOW, ZoneInfo("UTC"))
    with pytest.raises(ValueError):
        events().all_day_range(NOW.date(), ZoneInfo("UTC"), days=0)


@pytest.mark.parametrize("invalid", ["naive", "end_before_start", "status"])
def test_scheduled_event_rejects_invalid_times_and_status(invalid: str) -> None:
    start = NOW.replace(tzinfo=None) if invalid == "naive" else NOW
    end = NOW - timedelta(hours=1) if invalid == "end_before_start" else NOW + timedelta(hours=1)
    status = "unknown" if invalid == "status" else "scheduled"
    with pytest.raises(ValueError):
        events().ScheduledEvent("example", start, end, status)


def test_event_ordering_and_activity_compare_instants_during_repeated_hour() -> None:
    zone = ZoneInfo("America/New_York")
    first = datetime(2026, 11, 1, 1, 50, tzinfo=zone, fold=0)
    second = datetime(2026, 11, 1, 1, 10, tzinfo=zone, fold=1)
    finish = datetime(2026, 11, 1, 3, tzinfo=zone)
    module = events()
    samples = [
        module.ScheduledEvent("second", second, finish),
        module.ScheduledEvent("first", first, finish),
    ]
    assert module.next_event(samples, datetime(2026, 11, 1, 5, tzinfo=UTC)).id == "first"
    assert module.last_activity([first, second], fallback=first) == second


@pytest.mark.parametrize("query", ["next", "activity"])
def test_event_queries_require_aware_instants(query: str) -> None:
    naive = NOW.replace(tzinfo=None)
    with pytest.raises(ValueError):
        if query == "next":
            events().next_event([], naive)
        else:
            events().last_activity([], fallback=naive)


@pytest.mark.parametrize(
    "zone,day",
    [
        ("America/Havana", date(2026, 3, 8)),
        ("America/Sao_Paulo", date(2018, 11, 4)),
        ("Asia/Beirut", date(2026, 3, 29)),
    ],
)
@pytest.mark.parametrize("boundary", ["start", "end"])
def test_all_day_date_boundaries_resolve_skipped_midnight(
    zone: str, day: date, boundary: str
) -> None:
    selected = day if boundary == "start" else day - timedelta(days=1)
    zone_object = ZoneInfo(zone)
    start, end = events().all_day_range(selected, zone_object)
    shifted = start if boundary == "start" else end
    assert shifted.astimezone(zone_object).date() == day
    assert shifted.astimezone(zone_object).hour == 1
    assert start.astimezone(zone_object).date() == selected
    assert end.astimezone(zone_object).date() == selected + timedelta(days=1)


def test_all_day_wholly_skipped_date_is_refused() -> None:
    with pytest.raises(ValueError, match="date does not exist"):
        events().all_day_range(date(2011, 12, 30), ZoneInfo("Pacific/Apia"))
