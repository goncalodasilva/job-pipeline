"""FrozenClock and SequentialIdFactory fakes (spec 4.2; task M0.7)."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory

START = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def test_frozen_clock_advance() -> None:
    clock = FrozenClock(START)

    assert clock.now() == START
    assert clock.now() == START  # frozen: repeated calls don't move

    clock.advance(timedelta(minutes=90))
    assert clock.now() == START + timedelta(minutes=90)

    clock.advance(timedelta(days=1))
    assert clock.now() == START + timedelta(days=1, minutes=90)
    assert clock.now().tzinfo == UTC


def test_frozen_clock_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError):
        FrozenClock(datetime(2026, 3, 1, 12, 0))  # noqa: DTZ001 - naive on purpose

    clock = FrozenClock(START)
    with pytest.raises(ValueError):
        clock.set(datetime(2026, 4, 1, 8, 0))  # noqa: DTZ001 - naive on purpose
    assert clock.now() == START  # a rejected set leaves the clock unchanged


def test_frozen_clock_converts_to_utc() -> None:
    plus_two = timezone(timedelta(hours=2))

    clock = FrozenClock(datetime(2026, 3, 1, 12, 0, tzinfo=plus_two))

    now = clock.now()
    assert now == datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
    assert now.tzinfo == UTC
    assert now.hour == 10

    clock.set(datetime(2026, 5, 1, 9, 0, tzinfo=timezone(timedelta(hours=-5))))
    assert clock.now().tzinfo == UTC
    assert clock.now().hour == 14


def test_frozen_clock_set() -> None:
    clock = FrozenClock(START)
    target = datetime(2026, 6, 30, 23, 59, tzinfo=UTC)

    clock.set(target)
    assert clock.now() == target

    clock.set(START)  # going back in time is allowed for tests
    assert clock.now() == START

    clock.advance(timedelta(seconds=5))
    assert clock.now() == START + timedelta(seconds=5)


def test_sequential_ids_are_deterministic() -> None:
    first = SequentialIdFactory()
    second = SequentialIdFactory()

    first_ids = [first.new() for _ in range(3)]
    second_ids = [second.new() for _ in range(3)]

    assert first_ids == ["id-0001", "id-0002", "id-0003"]
    assert second_ids == first_ids


def test_sequential_ids_use_prefix() -> None:
    factory = SequentialIdFactory(prefix="job")

    assert [factory.new(), factory.new()] == ["job-0001", "job-0002"]
