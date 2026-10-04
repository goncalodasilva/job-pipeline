"""Setup-state persistence on a real SQLite database (M1.8; AC-M1.6; spec 2.7, 3.4, 3.5).

Each valid step submit is stored in ``setup_state`` so closing the app never loses progress.
``SetupStateService`` stores rows; ``SetupProgress`` applies the registry's rules (only
registered steps, required steps can't be skipped). Every value here is fictional.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.domain.errors import StepNotSkippable, UnknownSetupStep
from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from app.modules.storage.setup_state_repo import StoredStep, list_steps, upsert_step
from app.onboarding.progress import SetupProgress
from app.onboarding.registry import StepDefinition, StepRegistry, m1_registry
from app.services.setup_state import SetupStateService
from tests.fakes.clock import FrozenClock

pytestmark = pytest.mark.integration

START = datetime(2026, 1, 3, 9, 0, 0, tzinfo=UTC)
LATER = START + timedelta(hours=2, minutes=15)

FICTIONAL_OPTIONAL = StepDefinition(
    id="fictional_optional",
    title="Fictional optional step",
    required=False,
    order=35,
    template="fragments/setup/_fictional_optional.html",
)


# --- fixtures and helpers ---------------------------------------------------------


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(START)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "app.db"


@pytest.fixture
def db(tmp_path: Path, db_path: Path, clock: FrozenClock) -> Iterator[Database]:
    database = Database.open(db_path)
    apply_migrations(database, MIGRATIONS_DIR, tmp_path / "backups", clock)
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def service(db: Database, clock: FrozenClock) -> SetupStateService:
    return SetupStateService(db, clock)


def row(db: Database, step_id: str) -> tuple[Any, ...] | None:
    found: tuple[Any, ...] | None = db.connection.execute(
        "SELECT status, completed_at, created_at, updated_at, version "
        "FROM setup_state WHERE step_id = ?",
        (step_id,),
    ).fetchone()
    return found


def row_count(db: Database) -> int:
    count: int = db.connection.execute("SELECT COUNT(*) FROM setup_state").fetchone()[0]
    return count


# --- tests ----------------------------------------------------------------------------


def test_ac_m1_6_mark_done_persists_with_completed_at_from_clock(
    db: Database, service: SetupStateService
) -> None:
    assert service.statuses() == {}
    assert service.completed_at("welcome") is None

    service.mark_done("welcome")

    assert service.statuses() == {"welcome": "done"}
    assert service.completed_at("welcome") == START
    stored = row(db, "welcome")
    assert stored is not None
    status, completed_at, created_at, updated_at, version = stored
    assert status == "done"
    assert datetime.fromisoformat(completed_at) == START
    assert datetime.fromisoformat(completed_at).utcoffset() == timedelta(0)
    assert datetime.fromisoformat(created_at) == START
    assert datetime.fromisoformat(updated_at) == START
    assert version == 1


def test_mark_skipped_only_for_optional_steps(db: Database, service: SetupStateService) -> None:
    registry = StepRegistry([*m1_registry().steps, FICTIONAL_OPTIONAL])
    progress = SetupProgress(registry, service)

    with pytest.raises(StepNotSkippable):
        progress.skip("provider")
    assert row(db, "provider") is None
    assert row_count(db) == 0

    progress.skip("fictional_optional")

    assert service.statuses() == {"fictional_optional": "skipped"}
    assert progress.statuses() == {"fictional_optional": "skipped"}
    assert service.completed_at("fictional_optional") is None
    stored = row(db, "fictional_optional")
    assert stored is not None
    assert stored[0] == "skipped"
    assert stored[1] is None
    # Skipping an optional step does not finish any required step.
    assert progress.first_unfinished_required() == "welcome"


def test_reopen_keeps_status(db: Database, service: SetupStateService, clock: FrozenClock) -> None:
    service.mark_done("preferences")
    clock.set(LATER)

    service.mark_done("preferences")

    assert service.statuses() == {"preferences": "done"}
    assert service.completed_at("preferences") == START

    service.mark_skipped("preferences")

    assert service.statuses() == {"preferences": "done"}
    assert service.completed_at("preferences") == START
    stored = row(db, "preferences")
    assert stored is not None
    assert stored[0] == "done"
    assert datetime.fromisoformat(stored[1]) == START


def test_skipped_step_can_later_be_done(service: SetupStateService, clock: FrozenClock) -> None:
    service.mark_skipped("fictional_optional")
    clock.set(LATER)

    service.mark_done("fictional_optional")

    assert service.statuses() == {"fictional_optional": "done"}
    assert service.completed_at("fictional_optional") == LATER


def test_ac_m1_6_state_survives_reopening_database(
    tmp_path: Path, db_path: Path, clock: FrozenClock
) -> None:
    first = Database.open(db_path)
    apply_migrations(first, MIGRATIONS_DIR, tmp_path / "backups", clock)
    try:
        before = SetupStateService(first, clock)
        before.mark_done("welcome")
        before.mark_done("provider")
    finally:
        first.close()

    clock.set(LATER)
    reopened = Database.open(db_path)
    try:
        after = SetupStateService(reopened, clock)
        progress = SetupProgress(m1_registry(), after)

        assert after.statuses() == {"welcome": "done", "provider": "done"}
        assert after.completed_at("provider") == START
        assert progress.first_unfinished_required() == "preferences"
    finally:
        reopened.close()


def test_unknown_step_rows_ignored(db: Database, service: SetupStateService) -> None:
    service.mark_done("welcome")
    service.mark_done("retired_step")
    progress = SetupProgress(m1_registry(), service)

    assert service.statuses() == {"welcome": "done", "retired_step": "done"}
    assert progress.statuses() == {"welcome": "done"}
    assert progress.first_unfinished_required() == "provider"


def test_setup_state_repo_upsert_bumps_version(db: Database) -> None:
    first = START.isoformat()
    second = LATER.isoformat()
    with db.transaction() as conn:
        upsert_step(conn, "welcome", "pending", completed_at=None, now=first)
    with db.transaction() as conn:
        assert list_steps(conn) == [StoredStep("welcome", "pending", None, 1)]

    with db.transaction() as conn:
        upsert_step(conn, "welcome", "done", completed_at=second, now=second)
    with db.transaction() as conn:
        assert list_steps(conn) == [StoredStep("welcome", "done", second, 2)]

    stored = row(db, "welcome")
    assert stored is not None
    assert stored[2] == first  # created_at unchanged
    assert stored[3] == second  # updated_at moved
    assert stored[4] == 2


def test_setup_state_repo_is_reexported_from_storage() -> None:
    import app.modules.storage as storage

    assert storage.StoredStep is StoredStep
    assert storage.list_steps is list_steps
    assert storage.upsert_step is upsert_step


def test_mark_unregistered_step_raises_key_error(db: Database, service: SetupStateService) -> None:
    progress = SetupProgress(m1_registry(), service)

    with pytest.raises(UnknownSetupStep):
        progress.mark_done("retired_step")
    with pytest.raises(UnknownSetupStep):
        progress.skip("retired_step")
    assert row_count(db) == 0
