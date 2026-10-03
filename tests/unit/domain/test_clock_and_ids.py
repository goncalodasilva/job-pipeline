"""Clock and IdFactory in the domain (spec 2.4, 3.4; task M0.7)."""

import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.domain.clock import Clock, SystemClock
from app.domain.ids import IdFactory, UuidIdFactory
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory


def stamp(clock: Clock) -> datetime:
    """Stand-in for a service that only knows the Clock protocol."""
    return clock.now()


def make_ids(factory: IdFactory, count: int) -> list[str]:
    """Stand-in for a service that only knows the IdFactory protocol."""
    return [factory.new() for _ in range(count)]


def test_system_clock_returns_aware_utc() -> None:
    clock = SystemClock()

    first = clock.now()
    second = clock.now()

    assert first.tzinfo is not None
    assert first.utcoffset() == timedelta(0)
    assert first.tzinfo == UTC
    assert second.tzinfo == UTC
    assert second >= first


def test_uuid_factory_ids_unique() -> None:
    factory = UuidIdFactory()

    ids = [factory.new() for _ in range(1000)]

    assert len(set(ids)) == 1000
    for value in ids:
        assert isinstance(value, str)
        parsed = uuid.UUID(value)
        assert parsed.version == 4
        assert len(value) == 36
        assert str(parsed) == value  # canonical lowercase hyphenated form


def test_system_and_frozen_clock_usable_as_clock() -> None:
    instant = datetime(2026, 1, 15, 9, 30, tzinfo=UTC)

    assert stamp(FrozenClock(instant)) == instant
    assert stamp(SystemClock()).tzinfo == UTC


def test_uuid_and_sequential_factories_usable_as_id_factory() -> None:
    assert make_ids(SequentialIdFactory(), 2) == ["id-0001", "id-0002"]
    generated = make_ids(UuidIdFactory(), 2)
    assert len(generated) == 2
    assert all(uuid.UUID(value).version == 4 for value in generated)


REPO_ROOT = Path(__file__).resolve().parents[3]

CONFORMANCE = """
from datetime import UTC, datetime

from app.domain.clock import Clock, SystemClock
from app.domain.ids import IdFactory, UuidIdFactory
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory

clocks: list[Clock] = [SystemClock(), FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))]
factories: list[IdFactory] = [UuidIdFactory(), SequentialIdFactory()]
"""

NOT_A_CLOCK = """
from app.domain.clock import Clock


class Wrong:
    def now(self) -> str:
        return "noon"


clock: Clock = Wrong()
"""


def _mypy(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    sample = tmp_path / "conformance.py"
    sample.write_text(source, encoding="utf-8")
    env = {**os.environ, "MYPYPATH": str(REPO_ROOT)}
    # Fixed argv to the venv's own interpreter; no shell, no untrusted input.
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "mypy", "--strict", "--no-incremental", str(sample)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_implementations_and_fakes_satisfy_the_protocols_under_mypy(tmp_path: Path) -> None:
    result = _mypy(tmp_path, CONFORMANCE)

    assert result.returncode == 0, result.stdout + result.stderr


def test_mypy_rejects_a_clock_with_the_wrong_signature(tmp_path: Path) -> None:
    result = _mypy(tmp_path, NOT_A_CLOCK)

    assert result.returncode != 0
    assert "Clock" in result.stdout
