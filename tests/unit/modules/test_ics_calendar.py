"""Calendar export spike: fictional events, injected time, no external calendar."""

import importlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import StringIO
from zoneinfo import ZoneInfo, available_timezones

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.modules.calendar import EventData
from tests.fakes.clock import FrozenClock

NOW = datetime(2026, 10, 20, 9, tzinfo=UTC)


def event(zone: str = "Europe/Lisbon", **values: object) -> EventData:
    base = EventData(
        uid="event-example",
        title="Example interview",
        starts_at=datetime(2026, 11, 3, 14, tzinfo=UTC),
        ends_at=datetime(2026, 11, 3, 15, tzinfo=UTC),
        time_zone=zone,
    )
    return replace(base, **values)


def provider():
    return importlib.import_module("app.modules.calendar.providers.ics").IcsCalendarProvider(
        FrozenClock(NOW)
    )


def parse(content: bytes):
    return importlib.import_module("icalendar").Calendar.from_ical(content)


@pytest.mark.parametrize(
    "zone", ["Europe/Lisbon", "America/New_York", "Asia/Kolkata", "Australia/Lord_Howe", "UTC"]
)
def test_ac_m5_4_ics_parses_and_keeps_time_zone(zone: str) -> None:
    source = event(zone)
    exported = provider().export_event(source)
    calendar = parse(exported.content)
    assert str(calendar["VERSION"]) == "2.0"
    assert str(calendar["METHOD"]) == "PUBLISH"
    assert "jobpipeline" in str(calendar["PRODID"]).lower()
    assert len(calendar.walk("VEVENT")) == 1
    assert len(calendar.walk("VTIMEZONE")) == 1
    item = calendar.walk("VEVENT")[0]
    assert str(calendar.walk("VTIMEZONE")[0]["TZID"]) == zone
    for field, value in (("DTSTART", source.starts_at), ("DTEND", source.ends_at)):
        if zone == "UTC":
            assert "TZID" not in item[field].params
            assert item[field].to_ical().endswith(b"Z")
        else:
            assert str(item[field].params["TZID"]) == zone
        assert item.decoded(field).astimezone(UTC) == value
    assert exported.media_type == "text/calendar"


@pytest.mark.parametrize(
    "zone", ["Europe/Lisbon", "America/New_York", "Asia/Kolkata", "Australia/Lord_Howe", "UTC"]
)
@pytest.mark.parametrize("month", [1, 7, 11])
def test_ics_vtimezone_offsets_match_zoneinfo_at_event_instant(zone: str, month: int) -> None:
    instant = datetime(2040, month, 3, 14, tzinfo=UTC)
    source = event(zone, starts_at=instant, ends_at=instant + timedelta(hours=1))
    raw = provider().export_event(source).content.decode()
    # A second parser reads only raw VTIMEZONE text, independent of icalendar.
    block = (
        "BEGIN:VTIMEZONE"
        + raw.split("BEGIN:VTIMEZONE", 1)[1].split("END:VTIMEZONE", 1)[0]
        + "END:VTIMEZONE\r\n"
    )
    parsed_zone = importlib.import_module("dateutil.tz").tzical(StringIO(block)).get(zone)
    assert parsed_zone is not None
    for point in (source.starts_at, source.ends_at):
        assert (
            point.astimezone(parsed_zone).utcoffset()
            == point.astimezone(ZoneInfo(zone)).utcoffset()
        )


def test_ics_event_across_dst_change_keeps_wall_time() -> None:
    source = event(
        "America/New_York",
        starts_at=datetime(2026, 3, 8, 6, 30, tzinfo=UTC),
        ends_at=datetime(2026, 3, 8, 7, 30, tzinfo=UTC),
    )
    item = parse(provider().export_event(source).content).walk("VEVENT")[0]
    assert item.decoded("DTSTART").hour == 1
    assert item.decoded("DTEND").hour == 3
    assert item.decoded("DTEND").astimezone(UTC) - item.decoded("DTSTART").astimezone(
        UTC
    ) == timedelta(hours=1)


def test_ics_all_day_deadline_uses_value_date() -> None:
    zone = ZoneInfo("Asia/Kolkata")
    source = event(
        "Asia/Kolkata",
        event_type="deadline",
        all_day=True,
        starts_at=datetime(2026, 11, 3, tzinfo=zone),
        ends_at=datetime(2026, 11, 4, tzinfo=zone),
    )
    exported = provider().export_event(source)
    item = parse(exported.content).walk("VEVENT")[0]
    assert item.decoded("DTSTART") == source.starts_at.date()
    assert item.decoded("DTEND") == source.ends_at.date()
    assert item["DTSTART"].params == {"VALUE": "DATE"}
    assert exported.filename == "deadline-2026-11-03.ics"


def test_ics_escapes_and_folds_untrusted_text() -> None:
    text = "Example, semicolon; slash\\ newline\nEND:VEVENT\r\nBEGIN:VALARM\n" + "界" * 200
    source = event(title=text, details=text, location=text)
    exported = provider().export_event(source)
    calendar = parse(exported.content)
    assert len(calendar.walk("VEVENT")) == 1
    assert calendar.walk("VALARM") == []
    item = calendar.walk("VEVENT")[0]
    for field in ("SUMMARY", "DESCRIPTION", "LOCATION"):
        assert str(item[field]) == text.replace("\r\n", "\n")
    assert all(len(line) <= 75 for line in exported.content.split(b"\r\n"))
    assert b"\\," in exported.content and b"\\;" in exported.content and b"\\n" in exported.content


def test_ics_uid_stable_sequence_from_version_dtstamp_from_clock() -> None:
    clock = FrozenClock(NOW)
    exporter = importlib.import_module("app.modules.calendar.providers.ics").IcsCalendarProvider(
        clock
    )
    source = event(sequence=4)
    first = parse(exporter.export_event(source).content).walk("VEVENT")[0]
    assert str(first["UID"]) == "event-example@jobpipeline.invalid"
    assert int(first["SEQUENCE"]) == 4
    assert first.decoded("DTSTAMP") == NOW
    clock.advance(timedelta(hours=1))
    second = parse(exporter.export_event(replace(source, sequence=5)).content).walk("VEVENT")[0]
    assert second["UID"] == first["UID"]
    assert int(second["SEQUENCE"]) == 5
    assert second.decoded("DTSTAMP") == NOW + timedelta(hours=1)


def test_ics_cancelled_event_has_status_cancelled() -> None:
    item = parse(provider().export_event(event(cancelled=True)).content).walk("VEVENT")[0]
    assert str(item["STATUS"]) == "CANCELLED"


@pytest.mark.parametrize("kind", ["interview", "follow_up", "deadline", "offer_deadline"])
def test_ics_filename_from_type_and_date_only(kind: str) -> None:
    source = event(
        "America/New_York",
        event_type=kind,
        title="../../evil\r\nfile",
        starts_at=datetime(2026, 11, 3, 1, tzinfo=UTC),
        ends_at=datetime(2026, 11, 3, 2, tzinfo=UTC),
    )
    assert provider().export_event(source).filename == f"{kind}-2026-11-02.ics"


@given(
    zone=st.sampled_from(sorted(available_timezones())),
    instant=st.datetimes(
        min_value=datetime(2000, 1, 1, tzinfo=UTC).replace(tzinfo=None),
        max_value=datetime(2040, 12, 31, tzinfo=UTC).replace(tzinfo=None),
        timezones=st.just(UTC),
    ),
)
@settings(max_examples=35, deadline=None)
def test_ics_round_trip_any_zone_and_instant(zone: str, instant: datetime) -> None:
    # iCalendar date-time precision is whole seconds.
    instant = instant.replace(microsecond=0)
    source = event(zone, starts_at=instant, ends_at=instant + timedelta(hours=1))
    item = parse(provider().export_event(source).content).walk("VEVENT")[0]
    assert item.decoded("DTSTART").astimezone(UTC) == instant
    assert item.decoded("DTEND").astimezone(UTC) == source.ends_at


@pytest.mark.parametrize(
    "values",
    [
        {"sequence": 0},
        {"sequence": -1},
        {"sequence": 1.5},
        {"sequence": True},
        {"event_type": "../../payload"},
        {"all_day": True, "event_type": "interview"},
    ],
)
def test_ics_event_metadata_refuses_invalid_values(values: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        event(**values)


def test_calendar_provider_is_wired_with_injected_clock() -> None:
    from app.domain.redaction import Redactor
    from app.wiring import build_wiring

    wiring = build_wiring(environ={}, demo=True, redactor=Redactor(), clock=FrozenClock(NOW))
    exported = wiring.calendar_provider().export_event(event())
    item = parse(exported.content).walk("VEVENT")[0]
    assert item.decoded("DTSTAMP") == NOW


@pytest.mark.parametrize("zone", ["Etc/UTC", "Etc/GMT", "GMT", "Africa/Abidjan"])
def test_ics_utc_aliases_never_mix_tzid_with_z(zone: str) -> None:
    source = event(zone)
    exported = provider().export_event(source)
    event_block = exported.content.split(b"BEGIN:VEVENT\r\n", 1)[1]
    raw_lines = event_block.split(b"END:VEVENT", 1)[0].split(b"\r\n")
    for field in (b"DTSTART", b"DTEND"):
        line = next(line for line in raw_lines if line.startswith(field))
        assert not (b"TZID=" in line and line.endswith(b"Z"))
    item = parse(exported.content).walk("VEVENT")[0]
    assert item.decoded("DTSTART").astimezone(UTC) == source.starts_at
    assert item.decoded("DTEND").astimezone(UTC) == source.ends_at


@pytest.mark.parametrize("starts_hour", [5, 6])
def test_ics_repeated_autumn_hour_preserves_both_instants(starts_hour: int) -> None:
    instant = datetime(2026, 11, 1, starts_hour, 30, tzinfo=UTC)
    source = event("America/New_York", starts_at=instant, ends_at=instant + timedelta(hours=1))
    exported = provider().export_event(source)
    calendar = parse(exported.content)
    item = calendar.walk("VEVENT")[0]
    assert item.decoded("DTSTART").astimezone(UTC) == source.starts_at
    assert item.decoded("DTEND").astimezone(UTC) == source.ends_at
    assert str(calendar["X-WR-TIMEZONE"]) == source.time_zone
    assert len(calendar.walk("VTIMEZONE")) == 1
    for field in ("DTSTART", "DTEND"):
        assert "TZID" not in item[field].params
        assert item[field].to_ical().endswith(b"Z")
