"""A clock that only moves when a test says so (spec 4.2)."""

from datetime import UTC, datetime, timedelta


def _as_utc(instant: datetime) -> datetime:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("FrozenClock needs a timezone-aware datetime")
    return instant.astimezone(UTC)


class FrozenClock:
    def __init__(self, start: datetime) -> None:
        self._now = _as_utc(start)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta

    def set(self, instant: datetime) -> None:
        self._now = _as_utc(instant)
