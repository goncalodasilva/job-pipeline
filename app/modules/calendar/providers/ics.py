"""Local iCalendar export (M5.1, ADR-019, ADR-102). No credentials or network.

The zone definition covers both endpoints with a year on either side, including events
beyond 2038. All-day DTEND is an exclusive local date, as required by RFC 5545.
"""

from datetime import UTC, date
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event, Timezone

from app.domain.clock import Clock
from app.domain.errors import CalendarSyncUnsupported
from app.modules.calendar import CalendarFile, EventData, ExternalRef


class IcsCalendarProvider:
    name = "ics"
    supports_sync = False

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def export_event(self, event: EventData) -> CalendarFile:
        zone = ZoneInfo(event.time_zone)
        start = event.starts_at.astimezone(zone)
        end = event.ends_at.astimezone(zone)
        calendar = Calendar()
        calendar.add("prodid", "-//JobPipeline//Calendar export//EN")
        calendar.add("version", "2.0")
        calendar.add("method", "PUBLISH")
        calendar.add("x-wr-timezone", event.time_zone)
        item = Event()
        item.add("uid", f"{event.uid}@jobpipeline.invalid")
        item.add("dtstamp", self._clock.now())
        item.add("sequence", event.sequence)
        if event.all_day:
            if end.date() <= start.date():
                raise ValueError("An all-day event needs an exclusive end date after its start")
            item.add("dtstart", start.date())
            item.add("dtend", end.date())
        else:
            calendar.add_component(
                Timezone.from_tzinfo(
                    zone,
                    tzid=event.time_zone,
                    first_date=date(max(1, start.year - 1), 1, 1),
                    last_date=date(min(9999, end.year + 1), 12, 31),
                )
            )
            # Let the library normalize UTC aliases to Z without a conflicting TZID.
            # X-WR-TIMEZONE and VTIMEZONE retain the original IANA name.
            # RFC 5545 interprets repeated TZID wall times as the first occurrence.
            # If either endpoint is the second, serialize both as exact UTC instants.
            if start.fold or end.fold:
                item.add("dtstart", start.astimezone(UTC))
                item.add("dtend", end.astimezone(UTC))
            else:
                item.add("dtstart", start)
                item.add("dtend", end)
        item.add("summary", event.title)
        item.add("description", event.details)
        item.add("location", event.location)
        if event.cancelled:
            item.add("status", "CANCELLED")
        calendar.add_component(item)
        return CalendarFile(
            filename=f"{event.event_type}-{start.date().isoformat()}.ics",
            content=calendar.to_ical(),
        )

    async def sync_event(self, event: EventData) -> ExternalRef:
        raise CalendarSyncUnsupported()
