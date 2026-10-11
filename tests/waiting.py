"""Deadline-based waiting for smoke, e2e and failure-injection tests (M2.3, spec 4.1).

Tests wait on a condition with a deadline instead of sleeping a fixed time. ``clock`` and
``sleep`` are injectable so the helper's own tests never wait for real.
"""

import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")

DEFAULT_INTERVAL_SECONDS = 0.05


def wait_until(
    predicate: Callable[[], T | None],
    *,
    timeout: float,
    message: str,
    interval: float = DEFAULT_INTERVAL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Poll ``predicate`` until it returns a truthy value, and return that value.

    Polls about once per ``interval`` and once more at the deadline, never sleeping past it.
    Raises ``AssertionError`` naming ``message`` and ``timeout`` when the deadline passes."""
    if timeout <= 0:
        raise ValueError(f"timeout must be positive, got {timeout!r}")
    if interval <= 0:
        raise ValueError(f"interval must be positive, got {interval!r}")
    deadline = clock() + timeout
    while True:
        value = predicate()
        if value is not None and value:
            return value
        remaining = deadline - clock()
        if remaining <= 0:
            raise AssertionError(f"{message} (timed out after {timeout} s)")
        sleep(min(interval, remaining))
