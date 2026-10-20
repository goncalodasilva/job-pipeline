"""The calendar contract is local export; the ics provider never syncs."""

import importlib
from datetime import UTC, datetime, timedelta

import pytest

from app.modules.calendar import CalendarProvider, EventData
from tests.fakes.clock import FrozenClock

pytestmark = pytest.mark.contract


@pytest.mark.parametrize("kind", ["ics"])
async def test_calendar_provider_contract(kind: str) -> None:
    module = importlib.import_module(f"app.modules.calendar.providers.{kind}")
    provider: CalendarProvider = module.IcsCalendarProvider(
        FrozenClock(datetime(2026, 10, 20, tzinfo=UTC))
    )
    starts = datetime(2026, 11, 3, 10, tzinfo=UTC)
    event = EventData(
        "event-example", "Example interview", starts, starts + timedelta(hours=1), "Europe/Lisbon"
    )
    assert provider.name == kind
    assert provider.supports_sync is False
    exported = provider.export_event(event)
    assert exported.media_type == "text/calendar"
    assert exported.filename == "interview-2026-11-03.ics"
    assert exported.content.startswith(b"BEGIN:VCALENDAR\r\n")
    assert exported.content.endswith(b"END:VCALENDAR\r\n")
    assert provider.export_event(event) == exported
    errors = importlib.import_module("app.domain.errors")
    with pytest.raises(errors.CalendarSyncUnsupported):
        await provider.sync_event(event)
