"""The app's usage service and the connection test's record under cancellation (M2.16;
spec 3.7; ADR-076 items 3 and 8).

``create_app(..., tz=...)`` builds one ``UsageService`` per open workspace
(``app.state.usage``, None before) with the injected time zone; the default is
``app.wiring.local_timezone()``, the computer's own zone with its daylight-saving rules. A
connection test whose request is cancelled still completes its usage record. Temp folders,
fictional data, no network.
"""

import asyncio
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.domain.budgets import cycle_bounds
from app.modules.storage import Database
from app.services.usage import UsageService
from app.wiring import local_timezone
from tests.integration.test_usage_on_calls import (
    EXISTING_KIND,
    START,
    TZ,
    HangingProvider,
    _connected,
    _rows,
    _setup,
    data_dir,
    db,
    db_path,
    usage,
)

pytestmark = pytest.mark.integration
__all__ = ["data_dir", "db", "db_path", "usage"]  # fixtures shared with the M2.16 tests


@pytest.fixture
def new_york(monkeypatch: pytest.MonkeyPatch) -> Iterator[ZoneInfo]:
    """The process's local zone set to a fictional user's (restored afterwards)."""
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        yield ZoneInfo("America/New_York")
    finally:
        monkeypatch.undo()
        time.tzset()


INSTANTS = [
    datetime(2026, 1, 15, 12, 0, tzinfo=UTC),  # winter
    datetime(2026, 7, 15, 12, 0, tzinfo=UTC),  # summer
    datetime(2026, 3, 8, 6, 59, tzinfo=UTC),  # the minute before clocks go forward
    datetime(2026, 3, 8, 7, 0, tzinfo=UTC),
    datetime(2026, 11, 1, 5, 30, tzinfo=UTC),  # the repeated hour, first pass
    datetime(2026, 11, 1, 6, 30, tzinfo=UTC),  # and second pass
]


def test_local_timezone_follows_the_computers_zone_and_its_dst(new_york: ZoneInfo) -> None:
    local = local_timezone()
    for instant in INSTANTS:
        ours, theirs = instant.astimezone(local), instant.astimezone(new_york)
        assert ours.replace(tzinfo=None) == theirs.replace(tzinfo=None), instant
        assert (ours.utcoffset(), ours.tzname(), ours.fold) == (
            theirs.utcoffset(),
            theirs.tzname(),
            theirs.fold,
        ), instant
        assert ours.astimezone(UTC) == instant, "and back again"
    assert (local.utcoffset(None), local.dst(None), local.tzname(None)) == (None, None, None)
    assert INSTANTS[1].astimezone(local).dst() is None, "only the offset is known"
    assert repr(local) == "local_timezone()"


def test_local_timezone_gives_local_midnight_cycles_across_dst(new_york: ZoneInfo) -> None:
    """A cycle from 1 March to 1 April spans the change to summer time (ADR-076 item 8)."""
    now = datetime(2026, 3, 20, 15, 0, tzinfo=UTC)
    ours, theirs = cycle_bounds(now, 1, local_timezone()), cycle_bounds(now, 1, new_york)
    assert (ours.start, ours.end) == (theirs.start, theirs.end)
    elapsed = ours.end.astimezone(UTC) - ours.start.astimezone(UTC)
    assert elapsed == timedelta(days=31) - timedelta(hours=1)


def test_app_builds_one_usage_service_per_workspace_with_the_injected_zone(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    from app.config import RuntimeConfig
    from app.main import create_app
    from tests.fakes.clock import FrozenClock

    def config(folder: Path, source: str) -> RuntimeConfig:
        return RuntimeConfig(
            data_dir=folder / "data",
            config_dir=folder / "cfg",
            port=8000,
            log_level="INFO",
            demo=False,
            setup=False,
            no_browser=False,
            data_dir_source=source,  # type: ignore[arg-type]
        )

    opened = create_app(config(tmp_path / "a", "cli"), clock=FrozenClock(START), tz=TZ)
    with TestClient(opened):
        service = opened.state.usage
        assert isinstance(service, UsageService)
        cycle = service.status("fake").cycle
        assert cycle.start == datetime(2026, 2, 1, tzinfo=TZ), "local midnight in the zone"
        assert cycle.start.astimezone(UTC).hour == 5

    first_run = create_app(config(tmp_path / "b", "default"), clock=FrozenClock(START), tz=TZ)
    with TestClient(first_run):
        assert first_run.state.usage is None, "no workspace yet"
        first_run.state.open_workspace(tmp_path / "b" / "data")
        chosen = first_run.state.usage
        assert isinstance(chosen, UsageService)
        assert chosen.status("fake").cycle.start == datetime(2026, 2, 1, tzinfo=TZ)


async def test_cancelled_connection_test_still_completes_its_record(
    db: Database, usage: UsageService, data_dir: Path
) -> None:
    started = asyncio.Event()

    class Announcing(HangingProvider):
        async def test_connection(self, model: str) -> object:  # type: ignore[override]
            started.set()
            return await super().test_connection(model)

    setup = _setup(db, usage, Announcing())
    task = asyncio.create_task(setup.test_connection(EXISTING_KIND, "", data_dir=data_dir))
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _rows(usage, "claude_subscription") == [("fast", "connection_test", 1, 0)]


def test_meter_refuses_an_unknown_kind(usage: UsageService) -> None:
    from app.services.usage_meter import ServiceUsageMeter

    meter = ServiceUsageMeter(usage, "fake")
    with pytest.raises(ValueError):
        meter.reserve(tier="fast", task_type="fictional_task", task_id=None, kind="upload")  # type: ignore[arg-type]
    assert usage.status("fake").local_count == 0
    assert usage.status("web_search").local_count == 0


async def test_failed_completion_never_changes_the_connection_result(
    db: Database, data_dir: Path
) -> None:
    """The record stays counted (unfinished); the user still gets the test's answer."""
    from tests.fakes.clock import FrozenClock
    from tests.fakes.ids import SequentialIdFactory
    from tests.integration.test_usage_on_calls import FAKE_KIND, fake_key

    class BrokenComplete(UsageService):
        def complete(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("a fictional storage failure")

    broken = BrokenComplete(db, FrozenClock(START), SequentialIdFactory("usage"), TZ)

    result = await _setup(db, broken).test_connection(
        FAKE_KIND, fake_key("fake-connected"), data_dir=data_dir
    )

    assert result.ok is True
    assert _rows(broken, "fake") == [("fast", "connection_test", 1, 1)]


async def test_cancel_while_the_connection_test_reserves_leaves_no_open_record(
    db: Database, data_dir: Path
) -> None:
    """Cancelled while the reservation is being written: the record is completed before the
    cancellation goes on, and the provider is never called."""
    from tests.fakes.clock import FrozenClock
    from tests.fakes.ids import SequentialIdFactory

    entered, release = threading.Event(), threading.Event()

    class SlowReserve(UsageService):
        def reserve(self, *args: object, **kwargs: object) -> str:
            record_id = super().reserve(*args, **kwargs)  # type: ignore[arg-type]
            entered.set()
            assert release.wait(5.0)
            return record_id

    slow = SlowReserve(db, FrozenClock(START), SequentialIdFactory("usage"), TZ)
    provider = _connected()
    setup = _setup(db, slow, provider)
    task = asyncio.create_task(setup.test_connection(EXISTING_KIND, "", data_dir=data_dir))
    assert await asyncio.to_thread(entered.wait, 5.0)

    task.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done(), "the cancellation waits for the reservation"

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert provider.calls == 0
    assert _rows(slow, "claude_subscription") == [("fast", "connection_test", 1, 0)]


async def test_connection_test_with_a_failing_reserve_never_calls_the_provider(
    db: Database, data_dir: Path
) -> None:
    """Every provider call is counted: when counting fails, no call is made."""
    from tests.fakes.clock import FrozenClock
    from tests.fakes.ids import SequentialIdFactory

    completions: list[object] = []

    class BrokenReserve(UsageService):
        def reserve(self, *args: object, **kwargs: object) -> str:
            raise RuntimeError("a fictional storage failure")

        def complete(self, *args: object, **kwargs: object) -> None:
            completions.append(args)

    broken = BrokenReserve(db, FrozenClock(START), SequentialIdFactory("usage"), TZ)
    provider = _connected()

    result = await _setup(db, broken, provider).test_connection(
        EXISTING_KIND, "", data_dir=data_dir
    )

    assert (result.ok, result.failure) == (False, "other")
    assert "fictional" not in result.message
    assert provider.calls == 0 and completions == []
    assert _rows(broken, "claude_subscription") == []


def test_reserve_helper_is_part_of_the_ai_modules_public_interface() -> None:
    import app.modules.ai as ai
    from app.services import provider_setup

    assert callable(ai.reserve_uninterrupted) and callable(ai.settled)
    assert provider_setup.reserve_uninterrupted is ai.reserve_uninterrupted
