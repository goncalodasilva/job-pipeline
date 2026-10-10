"""Crash recovery when a workspace opens (M2.9; AC-M2.2; spec 5 M2 reliability rule 4;
ADR-076 item 10).

``app.main._prepare_workspace`` (run by both open paths, the lifespan and step 1's
``app.state.open_workspace``, before the task service is handed to the runner) moves every
``running`` task to ``interrupted`` in one transaction, through
``tasks_repo.interrupt_running(conn, now) -> list[str]`` (the ids it changed) with the app's
injected clock. Interrupted rows get ``finished_at`` and ``updated_at`` from that clock, a
bumped ``version``, ``error`` left NULL (the UI maps "interrupted" by status, M2.19) and the
same ``attempt``. Queued tasks stay queued and run; failed and interrupted tasks are never
retried automatically; usage records (unfinished: ``success IS NULL``) are never touched.

A "crashed" database is seeded before the app opens it: migrated, then a ``TaskService`` with
a seed clock enqueues and claims tasks, so a row is left ``running`` as a killed process would
leave it. Fictional data only; folders under ``tmp_path``; waits use deadlines, never sleeps.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.main as app_main
from app.config import DATABASE_FILE, RuntimeConfig
from app.domain.errors import DataFolderUnavailable, TaskFailed
from app.domain.tasks import TaskStatus, TaskTypeSpec
from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations, tasks_repo
from app.services.tasks import TaskService
from app.tasks import TaskRunner
from app.tasks.handlers import HandlerRegistry
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory
from tests.waiting import wait_until

pytestmark = pytest.mark.integration

CRASH_TIME = datetime(2026, 4, 1, 9, 30, 0, tzinfo=UTC)  # the earlier, killed run
RESTART_TIME = datetime(2026, 4, 2, 11, 0, 0, tzinfo=UTC)  # the app's injected clock
BASE_URL = "http://127.0.0.1:8000"
DEADLINE_S = 10.0
ECHO = "fictional_echo"  # sentinel type: proves the runner is active
WATCH = "fictional_watch"  # type of the crashed, failed and interrupted tasks
OPEN_PATHS = ["lifespan", "step1"]

_USAGE_COLUMNS = (
    "id, provider, task_type, tier, units, requested_at, success, created_at, updated_at, "
    "version, task_id, service, input_tokens, output_tokens"
)


# --- seeding a crashed database -----------------------------------------------------------


@dataclass(frozen=True)
class Seeded:
    running: str
    failed: str | None = None
    interrupted: str | None = None
    queued: str | None = None


def _folder(tmp_path: Path, path: str) -> Path:
    return tmp_path / ("data" if path == "lifespan" else "chosen")


@contextmanager
def _seed_db(folder: Path) -> Iterator[TaskService]:
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    clock = FrozenClock(CRASH_TIME)
    with Database.open(folder / DATABASE_FILE) as db:
        apply_migrations(db, MIGRATIONS_DIR, folder / "backups", clock)
        yield TaskService(db, clock, SequentialIdFactory("seed"), lambda _change: None)


def _start_running(service: TaskService, task_type: str, target: str) -> str:
    task_id = service.enqueue(task_type, target, input_ref="fictional-input").id
    claimed = service.claim_next()
    assert claimed is not None and claimed.id == task_id, "seed: claim the task just queued"
    return task_id


def _seed_crash(folder: Path, *, finished: bool = False, queued: bool = False) -> Seeded:
    """A database as a killed process leaves it: one task still running; optionally a failed
    and an interrupted task (finished before the crash) and a queued sentinel."""
    with _seed_db(folder) as service:
        failed = interrupted = queued_id = None
        if finished:
            failed = _start_running(service, WATCH, "fictional-failed")
            assert service.finish(failed, TaskStatus.FAILED, error=TaskFailed(detail="Fake"))
            interrupted = _start_running(service, WATCH, "fictional-interrupted")
            assert service.finish(interrupted, TaskStatus.INTERRUPTED)
        running = _start_running(service, WATCH, "fictional-running")
        if queued:
            queued_id = service.enqueue(ECHO, "fictional-queued").id
    return Seeded(running=running, failed=failed, interrupted=interrupted, queued=queued_id)


def _insert_unfinished_usage(folder: Path, task_id: str) -> None:
    stamp = CRASH_TIME.isoformat()
    with Database.open(folder / DATABASE_FILE) as db, db.transaction() as conn:
        conn.execute(
            "INSERT INTO usage_records (id, provider, task_type, tier, units, requested_at, "
            "success, created_at, updated_at, version, task_id, service) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, 1, ?, ?)",
            ("usage-0001", "fake", WATCH, "standard", 1, stamp, stamp, stamp, task_id, "fake"),
        )


def _read(folder: Path, sql: str, params: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
    """Read through a separate connection, as another process would see the committed data."""
    conn = sqlite3.connect(folder / DATABASE_FILE)
    try:
        return list(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


def _task(folder: Path, task_id: str) -> dict[str, Any]:
    rows = _read(
        folder,
        "SELECT status, attempt, error, finished_at, updated_at, version FROM tasks WHERE id = ?",
        (task_id,),
    )
    assert len(rows) == 1, f"task {task_id} exists"
    status, attempt, error, finished_at, updated_at, version = rows[0]
    return {
        "status": status,
        "attempt": attempt,
        "error": error,
        "finished_at": finished_at,
        "updated_at": updated_at,
        "version": version,
    }


# --- the app ------------------------------------------------------------------------------


def _config(tmp_path: Path, source: str) -> RuntimeConfig:
    return RuntimeConfig(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        port=8000,
        log_level="INFO",
        demo=False,
        setup=False,
        no_browser=False,
        data_dir_source=source,  # type: ignore[arg-type]
    )


class Recorder:
    """A handler that records every call; returns a fictional result ref."""

    def __init__(self) -> None:
        self.task_ids: list[str] = []

    async def __call__(self, ctx: Any) -> str:
        self.task_ids.append(ctx.task_id)
        return f"fictional-result-{ctx.task_id}"


def _app(tmp_path: Path, path: str, echo: Recorder, watch: Recorder) -> FastAPI:
    registry = HandlerRegistry()
    spec = TaskTypeSpec(timeout=timedelta(minutes=10), label="Fictional")
    registry.register(ECHO, spec, echo)
    registry.register(WATCH, spec, watch)
    source = "cli" if path == "lifespan" else "default"  # "default": a first run
    return app_main.create_app(
        _config(tmp_path, source),
        clock=FrozenClock(RESTART_TIME),
        migrations_dir=MIGRATIONS_DIR,
        task_registry=registry,
    )


def _open(app: FastAPI, tmp_path: Path, path: str) -> None:
    """With the app's lifespan running: open the workspace through step 1 when that is the
    path under test (the lifespan opened it already otherwise)."""
    if path == "step1":
        assert app.state.workspace is None
        app.state.open_workspace(_folder(tmp_path, path))  # as step 1 does, off the loop
    assert app.state.task_service is not None


def _wait_succeeded(app: FastAPI, task_id: str) -> Any:
    def done() -> Any:
        row = app.state.task_service.get(task_id)
        return row if row is not None and row.status == TaskStatus.SUCCEEDED else None

    return wait_until(done, timeout=DEADLINE_S, message=f"task {task_id} succeeded")


# --- AC-M2.2 ------------------------------------------------------------------------------


@pytest.mark.parametrize("path", OPEN_PATHS)
def test_ac_m2_2_running_task_interrupted_when_workspace_opens(tmp_path: Path, path: str) -> None:
    folder = _folder(tmp_path, path)
    seeded = _seed_crash(folder)
    before = _task(folder, seeded.running)
    assert before["status"] == "running"

    app = _app(tmp_path, path, Recorder(), watch := Recorder())
    with TestClient(app, base_url=BASE_URL):
        _open(app, tmp_path, path)
        after = _task(folder, seeded.running)  # committed when the open returns

    assert after["status"] == "interrupted"
    assert after["finished_at"] == RESTART_TIME.isoformat(), "finished_at from the app clock"
    assert after["updated_at"] == RESTART_TIME.isoformat()
    assert after["version"] == before["version"] + 1
    assert after["attempt"] == before["attempt"] == 1, "not retried"
    assert after["error"] is None, "interrupted is shown by status; no error kind stored"
    assert watch.task_ids == [], "the crashed task's handler never runs again"


@pytest.mark.parametrize("path", OPEN_PATHS)
def test_ac_m2_2_interrupted_and_failed_not_retried_after_restart(
    tmp_path: Path, path: str
) -> None:
    folder = _folder(tmp_path, path)
    seeded = _seed_crash(folder, finished=True, queued=True)
    assert seeded.failed is not None and seeded.interrupted is not None
    assert seeded.queued is not None
    failed_before = _task(folder, seeded.failed)
    interrupted_before = _task(folder, seeded.interrupted)

    echo, watch = Recorder(), Recorder()
    app = _app(tmp_path, path, echo, watch)
    with TestClient(app, base_url=BASE_URL):
        _open(app, tmp_path, path)
        # Sentinels prove the runner was active: the seeded queued one and one enqueued now,
        # which the runner claims only after everything older.
        later = app.state.task_service.enqueue(ECHO, "fictional-later").id
        _wait_succeeded(app, seeded.queued)
        _wait_succeeded(app, later)
        assert echo.task_ids == [seeded.queued, later]

        crashed = _task(folder, seeded.running)
        assert crashed["status"] == "interrupted", "the crashed task ends interrupted"
        assert crashed["attempt"] == 1
        assert _task(folder, seeded.failed) == failed_before, "failed stays as it was"
        assert _task(folder, seeded.interrupted) == interrupted_before, "interrupted untouched"
        assert watch.task_ids == [], "no automatic retry of failed or interrupted tasks"

        # A manual retry still works (AC-M2.2 "with a working retry").
        retried = app.state.task_service.retry(seeded.running)
        assert retried is not None and retried.attempt == 2
        _wait_succeeded(app, seeded.running)
        assert watch.task_ids == [seeded.running]


@pytest.mark.parametrize("path", OPEN_PATHS)
def test_queued_task_after_restart_resumes(tmp_path: Path, path: str) -> None:
    folder = _folder(tmp_path, path)
    seeded = _seed_crash(folder, queued=True)
    assert seeded.queued is not None

    echo, watch = Recorder(), Recorder()
    app = _app(tmp_path, path, echo, watch)
    with TestClient(app, base_url=BASE_URL):
        _open(app, tmp_path, path)
        row = _wait_succeeded(app, seeded.queued)

    assert row.attempt == 1, "a queued task never started, so it runs as its first attempt"
    assert row.result_ref == f"fictional-result-{seeded.queued}"
    assert echo.task_ids == [seeded.queued]
    assert _task(folder, seeded.running)["status"] == "interrupted"
    assert watch.task_ids == []


@pytest.mark.parametrize("path", OPEN_PATHS)
def test_recovery_runs_before_first_claim(
    tmp_path: Path, path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _folder(tmp_path, path)
    seeded = _seed_crash(folder, queued=True)
    assert seeded.queued is not None

    seen_at_claim: list[str] = []
    real_claim = TaskService.claim_next

    def spying_claim(self: TaskService) -> Any:
        # What another connection sees committed when the runner claims.
        seen_at_claim.append(_task(folder, seeded.running)["status"])
        return real_claim(self)

    seen_at_attach: list[str] = []
    real_attach = TaskRunner.attach

    def spying_attach(self: TaskRunner, service: Any, **kwargs: Any) -> None:
        # Deterministic: what is committed when the runner gets the task service.
        seen_at_attach.append(_task(folder, seeded.running)["status"])
        real_attach(self, service, **kwargs)

    monkeypatch.setattr(TaskService, "claim_next", spying_claim)
    monkeypatch.setattr(TaskRunner, "attach", spying_attach)
    echo, watch = Recorder(), Recorder()
    app = _app(tmp_path, path, echo, watch)
    with TestClient(app, base_url=BASE_URL):
        _open(app, tmp_path, path)
        _wait_succeeded(app, seeded.queued)

    assert seen_at_attach == ["interrupted"], "recovery committed before the runner hand-off"
    assert seen_at_claim, "the runner claimed at least once"
    assert seen_at_claim[0] == "interrupted", "recovery committed before the first claim"
    assert set(seen_at_claim) == {"interrupted"}
    assert watch.task_ids == [], "the crashed task's handler is never called"


@pytest.mark.parametrize("path", OPEN_PATHS)
def test_recovery_leaves_unfinished_usage_counted(tmp_path: Path, path: str) -> None:
    folder = _folder(tmp_path, path)
    seeded = _seed_crash(folder)
    _insert_unfinished_usage(folder, seeded.running)
    usage_before = _read(folder, f"SELECT {_USAGE_COLUMNS} FROM usage_records")  # noqa: S608

    app = _app(tmp_path, path, Recorder(), Recorder())
    with TestClient(app, base_url=BASE_URL):
        _open(app, tmp_path, path)
        assert _task(folder, seeded.running)["status"] == "interrupted"
        usage_after = _read(folder, f"SELECT {_USAGE_COLUMNS} FROM usage_records")  # noqa: S608

    assert usage_after == usage_before, "the usage row is untouched, column for column"
    unfinished = _read(
        folder,
        "SELECT count(*) FROM usage_records WHERE success IS NULL AND task_id = ?",
        (seeded.running,),
    )
    assert unfinished == [(1,)], "still unfinished, still counted, still linked to the task"


# --- the repository function the open paths use -------------------------------------------


def test_interrupt_running_marks_only_running_tasks(tmp_path: Path) -> None:
    folder = tmp_path / "data"
    seeded = _seed_crash(folder, finished=True, queued=True)
    assert seeded.queued is not None
    others = [seeded.failed, seeded.interrupted, seeded.queued]
    before = {task_id: _task(folder, task_id) for task_id in others if task_id is not None}

    now = RESTART_TIME.isoformat()
    with Database.open(folder / DATABASE_FILE) as db, db.transaction() as conn:
        changed = tasks_repo.interrupt_running(conn, now)

    assert changed == [seeded.running]
    after = _task(folder, seeded.running)
    assert after["status"] == "interrupted"
    assert after["finished_at"] == now
    assert after["error"] is None
    assert {task_id: _task(folder, task_id) for task_id in before} == before
    with Database.open(folder / DATABASE_FILE) as db, db.transaction() as conn:
        assert tasks_repo.interrupt_running(conn, now) == [], "nothing left to recover"


def test_recovery_database_error_maps_to_data_folder_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _folder(tmp_path, "step1")
    seeded = _seed_crash(folder)

    def broken(_conn: sqlite3.Connection, _now: str) -> list[str]:
        raise sqlite3.OperationalError("fictional failure")

    monkeypatch.setattr(tasks_repo, "interrupt_running", broken)
    app = _app(tmp_path, "step1", Recorder(), Recorder())
    with TestClient(app, base_url=BASE_URL):
        with pytest.raises(DataFolderUnavailable) as caught:
            app.state.open_workspace(folder)
        assert app.state.workspace is None
        assert app.state.task_service is None

    assert str(caught.value) == "can't recover tasks (OperationalError)"
    assert _task(folder, seeded.running)["status"] == "running", "nothing committed"
