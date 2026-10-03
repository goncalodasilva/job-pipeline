"""Calendar module interface (spec 2.4, ADR-019). The first provider exports .ics files."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class EventData:
    uid: str  # stable event id (the event row id), so re-exports update instead of duplicating
    title: str
    starts_at: datetime  # aware
    ends_at: datetime  # aware
    time_zone: str  # IANA name, e.g. "Europe/Lisbon"
    details: str = ""

    def __post_init__(self) -> None:
        if not self.uid:
            raise ValueError("EventData.uid must not be empty")
        for name, value in (("starts_at", self.starts_at), ("ends_at", self.ends_at)):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"EventData.{name} must be timezone-aware")
        if self.ends_at < self.starts_at:
            raise ValueError("EventData.ends_at must not be before starts_at")


@dataclass(frozen=True)
class CalendarFile:
    filename: str
    content: bytes  # .ics bytes
    media_type: str = "text/calendar"


@dataclass(frozen=True)
class ExternalRef:
    provider: str
    external_id: str


class CalendarProvider(Protocol):
    # Providers take an injected Clock for DTSTAMP; they never read the wall clock.
    @property
    def name(self) -> str: ...

    @property
    def supports_sync(self) -> bool: ...

    def export_event(self, event: EventData) -> CalendarFile: ...

    async def sync_event(self, event: EventData) -> ExternalRef: ...  # later providers
