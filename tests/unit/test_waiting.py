"""Deadline-based waiting for tests (M2.3): ``tests/waiting.wait_until``.

Smoke, e2e and failure-injection tests wait on a condition with a deadline instead of
sleeping a fixed time. The helper takes injectable ``clock`` and ``sleep`` callables (defaults
``time.monotonic`` and ``time.sleep``), so these tests never wait for real.

Assumed API::

    wait_until(predicate, *, timeout, message, interval=..., clock=time.monotonic,
               sleep=time.sleep) -> T  # the predicate's first truthy value

It raises an ``AssertionError`` (so pytest reports a plain failure) whose text includes
``message`` and the timeout when the deadline passes.
"""

import importlib
import inspect
import time
from collections.abc import Callable
from types import ModuleType
from typing import Any

import pytest

MESSAGE = "the fictional task never finished"


def _waiting() -> ModuleType:
    return importlib.import_module("tests.waiting")


class FakeTime:
    """A monotonic clock that moves only when ``sleep`` is called."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.now += seconds


def _counting(results: list[Any]) -> tuple[Callable[[], Any], list[int]]:
    calls = [0]

    def predicate() -> Any:
        index = min(calls[0], len(results) - 1)
        calls[0] += 1
        return results[index]

    return predicate, calls


def test_wait_until_returns_at_once_when_already_true() -> None:
    fake = FakeTime()
    predicate, calls = _counting(["ready"])

    value = _waiting().wait_until(
        predicate, timeout=5.0, interval=0.1, message=MESSAGE, clock=fake.clock, sleep=fake.sleep
    )

    assert value == "ready"
    assert calls[0] == 1
    assert fake.sleeps == []


def test_wait_until_returns_as_soon_as_predicate_is_true() -> None:
    fake = FakeTime()
    predicate, calls = _counting([False, None, {"status": "done"}])

    value = _waiting().wait_until(
        predicate, timeout=5.0, interval=0.1, message=MESSAGE, clock=fake.clock, sleep=fake.sleep
    )

    assert value == {"status": "done"}
    assert calls[0] == 3
    assert fake.sleeps == [pytest.approx(0.1), pytest.approx(0.1)]


def test_wait_until_raises_with_message_after_deadline() -> None:
    fake = FakeTime()
    predicate, calls = _counting([False])

    with pytest.raises(AssertionError) as raised:
        _waiting().wait_until(
            predicate,
            timeout=2.0,
            interval=0.5,
            message=MESSAGE,
            clock=fake.clock,
            sleep=fake.sleep,
        )

    assert MESSAGE in str(raised.value)
    assert "2" in str(raised.value), "the timeout is part of the failure text"
    assert fake.now - 1000.0 == pytest.approx(2.0), "waits until the deadline, not past it"
    assert 4 <= calls[0] <= 6, "polls about once per interval, including at the deadline"


def test_wait_until_never_sleeps_past_the_deadline() -> None:
    fake = FakeTime()
    predicate, _calls = _counting([False])

    with pytest.raises(AssertionError):
        _waiting().wait_until(
            predicate,
            timeout=2.5,
            interval=1.0,
            message=MESSAGE,
            clock=fake.clock,
            sleep=fake.sleep,
        )

    assert sum(fake.sleeps) == pytest.approx(2.5)
    assert fake.sleeps[-1] == pytest.approx(0.5)


def test_wait_until_checks_once_more_at_the_deadline() -> None:
    fake = FakeTime()
    deadline = 1000.0 + 1.0

    def true_only_at_deadline() -> bool:
        return fake.now >= deadline

    assert (
        _waiting().wait_until(
            true_only_at_deadline,
            timeout=1.0,
            interval=0.3,
            message=MESSAGE,
            clock=fake.clock,
            sleep=fake.sleep,
        )
        is True
    )


def test_wait_until_deadline_uses_monotonic_clock_and_real_sleep_by_default() -> None:
    parameters = inspect.signature(_waiting().wait_until).parameters

    assert parameters["clock"].default is time.monotonic
    assert parameters["sleep"].default is time.sleep
    assert parameters["timeout"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["message"].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.parametrize(("timeout", "interval"), [(0.0, 0.1), (-1.0, 0.1), (1.0, 0.0)])
def test_wait_until_rejects_non_positive_timeout_or_interval(
    timeout: float, interval: float
) -> None:
    fake = FakeTime()

    with pytest.raises(ValueError):
        _waiting().wait_until(
            lambda: True,
            timeout=timeout,
            interval=interval,
            message=MESSAGE,
            clock=fake.clock,
            sleep=fake.sleep,
        )
