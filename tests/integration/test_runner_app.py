"""The task runner in the app (M2.8; spec 5 M2 reliability rule 9; ADR-034, ADR-044 item 3).

API assumed: ``create_app(config, ..., task_registry=HandlerRegistry)`` injects the handlers
(default: the empty registry). Whenever a workspace opens, at lifespan start or later through
step 1's ``app.state.open_workspace(folder)`` (a worker thread), the app builds a
``TaskService`` on its database, exposes it as ``app.state.task_service`` (None before), wires
its ``on_change`` to wake the runner for queued tasks, loads ``TASK_RUNNER`` settings and
hands both to ``app.state.runner`` (thread-safe). Enqueueing through that service is enough
for the task to run.

Fictional handlers only; folders under ``tmp_path``; waits use ``tests.waiting.wait_until``
with a deadline.
"""

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.main as app_main
from app.config import DATABASE_FILE, RuntimeConfig
from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from app.services.settings import SettingsService
from tests.fakes.clock import FrozenClock
from tests.waiting import wait_until

pytestmark = pytest.mark.integration

START = datetime(2026, 4, 2, 11, 0, 0, tzinfo=UTC)
BASE_URL = "http://127.0.0.1:8000"
DEADLINE_S = 10.0
ECHO = "fictional_echo"


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


def _registry(handler: Any) -> Any:
    from app.domain.tasks import TaskTypeSpec
    from app.tasks.handlers import HandlerRegistry

    registry = HandlerRegistry()
    registry.register(ECHO, TaskTypeSpec(timeout=timedelta(minutes=10), label="Echo"), handler)
    return registry


def _app(tmp_path: Path, source: str, handler: Any) -> FastAPI:
    return app_main.create_app(
        _config(tmp_path, source),
        clock=FrozenClock(START),
        migrations_dir=MIGRATIONS_DIR,
        task_registry=_registry(handler),
    )


class Echo:
    """Returns a result ref naming the task; records the thread it ran on."""

    def __init__(self) -> None:
        self.task_ids: list[str] = []
        self.threads: list[int] = []

    async def __call__(self, ctx: Any) -> str:
        self.task_ids.append(ctx.task_id)
        self.threads.append(threading.get_ident())
        return f"fictional-result-{ctx.task_id}"


def _wait_succeeded(app: FastAPI, task_id: str) -> Any:
    def done() -> Any:
        row = app.state.task_service.get(task_id)
        return row if row is not None and str(row.status) == "succeeded" else None

    return wait_until(done, timeout=DEADLINE_S, message=f"task {task_id} succeeded")


@pytest.mark.parametrize("path", ["lifespan", "step1"])
def test_lifespan_and_step1_open_both_start_processing(tmp_path: Path, path: str) -> None:
    handler = Echo()
    source = "cli" if path == "lifespan" else "default"  # "default": a first run
    app = _app(tmp_path, source, handler)
    with TestClient(app, base_url=BASE_URL):
        assert app.state.runner.status == "running"
        if path == "step1":
            assert app.state.workspace is None
            assert app.state.task_service is None
            app.state.open_workspace(tmp_path / "chosen")  # as step 1 does, off the loop
        ref = app.state.task_service.enqueue(ECHO, "run-1", input_ref="fictional-input")
        assert ref.created is True
        row = _wait_succeeded(app, ref.id)
        assert row.result_ref == f"fictional-result-{ref.id}"
        assert row.attempt == 1
        assert handler.task_ids == [ref.id]
        assert handler.threads[0] != threading.get_ident(), "handlers run on the app's loop"
    assert app.state.runner.status == "stopped"


def test_lifespan_reads_max_concurrent_from_settings(tmp_path: Path) -> None:
    from app.domain.settings import TASK_RUNNER, TaskRunnerSettings

    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    with Database.open(data / DATABASE_FILE) as db:
        apply_migrations(db, MIGRATIONS_DIR, data / "backups", FrozenClock(START))
        SettingsService(db, FrozenClock(START)).save(
            TASK_RUNNER, TaskRunnerSettings(max_concurrent=2), expected_version=0
        )

    started: list[str] = []
    release = threading.Event()

    async def held(ctx: Any) -> str:
        started.append(ctx.task_id)
        await asyncio.to_thread(release.wait, DEADLINE_S)
        return "fictional-result"

    app = _app(tmp_path, "cli", held)
    with TestClient(app, base_url=BASE_URL):
        service = app.state.task_service
        first = service.enqueue(ECHO, "run-1").id
        second = service.enqueue(ECHO, "run-2").id
        try:
            wait_until(
                lambda: len(started) == 2,
                timeout=DEADLINE_S,
                message="two tasks running at once (max_concurrent=2)",
            )
        finally:
            release.set()
        _wait_succeeded(app, first)
        _wait_succeeded(app, second)


def test_shutdown_interrupts_running_task(tmp_path: Path) -> None:
    started = threading.Event()

    async def forever(ctx: Any) -> str:
        started.set()
        await asyncio.Event().wait()
        return "never"

    app = _app(tmp_path, "cli", forever)
    with TestClient(app, base_url=BASE_URL):
        task_id = app.state.task_service.enqueue(ECHO, "run-1").id
        assert started.wait(DEADLINE_S), "the handler started"

    with Database.open(tmp_path / "data" / DATABASE_FILE) as db:
        status = db.connection.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
    assert status == "interrupted"


def test_failed_task_hand_off_closes_workspace_and_retry_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If handing the task service to the runner fails after the folder opened, the database
    closes (releasing the folder lock) and step 1 can try again."""
    real_attach = app_main._attach_tasks
    calls: list[int] = []

    def attach_fails_once(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("fictional settings read failure")
        return real_attach(*args, **kwargs)

    monkeypatch.setattr(app_main, "_attach_tasks", attach_fails_once)
    handler = Echo()
    app = _app(tmp_path, "default", handler)
    folder = tmp_path / "chosen"
    with TestClient(app, base_url=BASE_URL):
        with pytest.raises(RuntimeError):
            app.state.open_workspace(folder)
        assert app.state.workspace is None
        assert app.state.task_service is None

        app.state.open_workspace(folder)  # the lock was released, so this works
        ref = app.state.task_service.enqueue(ECHO, "run-1")
        _wait_succeeded(app, ref.id)
    assert len(calls) == 2


@pytest.mark.parametrize("timeout", [timedelta(seconds=60), timedelta(minutes=5)])
def test_create_app_refuses_injected_type_within_provider_run_timeout(
    tmp_path: Path, timeout: timedelta
) -> None:
    """Every task type's timeout must outlast one provider run (PROVIDER_RUN_TIMEOUT), even
    in a registry built without that floor."""
    from app.domain.tasks import TaskTypeSpec
    from app.tasks.handlers import HandlerRegistry

    registry = HandlerRegistry()
    registry.register(ECHO, TaskTypeSpec(timeout=timeout, label="Echo"), Echo())
    with pytest.raises(ValueError):
        app_main.create_app(
            _config(tmp_path, "cli"),
            clock=FrozenClock(START),
            migrations_dir=MIGRATIONS_DIR,
            task_registry=registry,
        )


def test_create_app_default_registry_refuses_type_within_provider_run_timeout(
    tmp_path: Path,
) -> None:
    from app.domain.tasks import TaskTypeSpec

    app = app_main.create_app(
        _config(tmp_path, "cli"), clock=FrozenClock(START), migrations_dir=MIGRATIONS_DIR
    )
    registry = app.state.task_registry
    with pytest.raises(ValueError):
        registry.register(ECHO, TaskTypeSpec(timeout=timedelta(seconds=60), label="Echo"), Echo())
    assert registry.get(ECHO) is None
    registry.register(ECHO, TaskTypeSpec(timeout=timedelta(minutes=6), label="Echo"), Echo())
    assert registry.get(ECHO) is not None


def _registry_with(timeout: timedelta) -> Any:
    from app.domain.tasks import TaskTypeSpec
    from app.tasks.handlers import HandlerRegistry

    registry = HandlerRegistry()
    registry.register(ECHO, TaskTypeSpec(timeout=timeout, label="Echo"), Echo())
    return registry


def test_create_app_refuses_type_shorter_than_run_plus_close(tmp_path: Path) -> None:
    """A provider call may last PROVIDER_RUN_TIMEOUT plus the client's bounded close, so a
    type just past the run timeout is still too short (TASK_TIMEOUT_FLOOR)."""
    from app.wiring import PROVIDER_RUN_TIMEOUT

    with pytest.raises(ValueError):
        app_main.create_app(
            _config(tmp_path, "cli"),
            clock=FrozenClock(START),
            migrations_dir=MIGRATIONS_DIR,
            task_registry=_registry_with(PROVIDER_RUN_TIMEOUT + timedelta(seconds=10)),
        )


def test_create_app_accepts_type_above_task_timeout_floor(tmp_path: Path) -> None:
    from app.modules.ai.providers.claude_sdk import DISCONNECT_TIMEOUT_SECONDS
    from app.wiring import PROVIDER_RUN_TIMEOUT, TASK_TIMEOUT_FLOOR

    assert PROVIDER_RUN_TIMEOUT + timedelta(seconds=DISCONNECT_TIMEOUT_SECONDS) < TASK_TIMEOUT_FLOOR
    app = app_main.create_app(
        _config(tmp_path, "cli"),
        clock=FrozenClock(START),
        migrations_dir=MIGRATIONS_DIR,
        task_registry=_registry_with(TASK_TIMEOUT_FLOOR + timedelta(seconds=1)),
    )
    assert app.state.task_registry.get(ECHO) is not None
