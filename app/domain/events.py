"""Pure event time rules. Callers supply zone objects; this module does no I/O."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Literal


@dataclass(frozen=True)
class ResolvedTime:
    instant: datetime
    repeated: bool
    notice: str | None = None


def _instant(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("An event query needs an aware instant.")
    return value.astimezone(UTC)


def wall_time_to_utc(wall: datetime, zone: tzinfo) -> ResolvedTime:
    """Refuse gaps; repeated hours use fold 0, with an explanation for the form."""
    if wall.tzinfo is not None:
        raise ValueError("Enter a local date and time without a UTC offset.")
    first = wall.replace(tzinfo=zone, fold=0)
    instant = first.astimezone(UTC)
    back = instant.astimezone(zone).replace(tzinfo=None)
    if back != wall:
        raise ValueError("This local time does not exist because the clocks move forward.")
    second = wall.replace(tzinfo=zone, fold=1)
    repeated = first.utcoffset() != second.utcoffset()
    notice = "This time occurs twice. The first occurrence will be used." if repeated else None
    return ResolvedTime(instant, repeated, notice)


def all_day_range(day: date, zone: tzinfo, *, days: int = 1) -> tuple[datetime, datetime]:
    """First instants of local dates; a date need not begin at midnight."""
    if type(days) is not int or days < 1:
        raise ValueError("An all-day event needs at least one date.")
    end_day = day + timedelta(days=days)
    start = _date_boundary(day, zone)
    end = _date_boundary(end_day, zone)
    return start, end


def _date_boundary(day: date, zone: tzinfo) -> datetime:
    """Find the first real instant on a date, choosing the first repeated midnight.

    The two fold interpretations bracket a midnight gap. Binary search finds the
    exact transition even when a historical gap spans midnight instead of starting
    there. A wholly skipped date cannot be represented by our UTC-plus-zone storage.
    """
    midnight = datetime.combine(day, time.min)
    candidates = tuple(midnight.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1))
    low, high = min(candidates), max(candidates)
    if low.astimezone(zone).date() == day:
        return low
    while high - low > timedelta(microseconds=1):
        middle = low + (high - low) // 2
        if middle.astimezone(zone).date() < day:
            low = middle
        else:
            high = middle
    if high.astimezone(zone).date() != day:
        raise ValueError("This local date does not exist in the selected time zone.")
    return high


@dataclass(frozen=True)
class ScheduledEvent:
    id: str
    starts_at: datetime
    ends_at: datetime
    status: Literal["scheduled", "cancelled"] = "scheduled"

    def __post_init__(self) -> None:
        for instant in (self.starts_at, self.ends_at):
            if instant.tzinfo is None or instant.utcoffset() is None:
                raise ValueError("An event needs aware instants.")
        if self.ends_at.astimezone(UTC) < self.starts_at.astimezone(UTC):
            raise ValueError("The event must end after it starts.")
        if self.status not in {"scheduled", "cancelled"}:
            raise ValueError("The event status is invalid.")


def next_event(events: Iterable[ScheduledEvent], now: datetime) -> ScheduledEvent | None:
    """Include an ongoing event; finished and cancelled events are never next."""
    instant = _instant(now)
    return min(
        (
            event
            for event in events
            if event.status == "scheduled" and event.ends_at.astimezone(UTC) >= instant
        ),
        key=lambda event: (event.starts_at.astimezone(UTC), event.id),
        default=None,
    )


def last_activity(instants: Iterable[datetime], *, fallback: datetime) -> datetime:
    """Use change times supplied by storage, never the scheduled date of a future event."""
    _instant(fallback)
    return max(instants, default=fallback, key=_instant)
