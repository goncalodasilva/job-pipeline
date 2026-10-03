"""App factory, lifespan, task runner stub and /health (M0.13, AC-M0.1 part, ADR-023, ADR-038)."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app as app_package
from app.config import RuntimeConfig
from app.main import create_app
from app.modules.storage import Database, MigrationError
from tests.fakes.clock import FrozenClock

pytestmark = pytest.mark.integration

START = datetime(2026, 1, 3, 9, 0, 0, tzinfo=UTC)
TABLES_FROM_0001 = {"schema_migrations", "settings", "setup_state", "tasks", "usage_records"}


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        port=8000,
        log_level="INFO",
        demo=False,
        setup=False,
        no_browser=False,
    )


def _app(tmp_path: Path) -> FastAPI:
    return create_app(_config(tmp_path), clock=FrozenClock(START))


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def test_lifespan_creates_db_and_applies_migrations_in_tmp_data_dir(tmp_path: Path) -> None:
    app = _app(tmp_path)
    db_path = tmp_path / "data" / "app.db"
    assert not db_path.exists(), "the database is opened by the lifespan, not by create_app"

    with TestClient(app):
        assert db_path.exists()
        with closing(_read_only(db_path)) as conn:
            versions = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            tables = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        assert 1 in versions
        assert tables >= TABLES_FROM_0001

    backups = tmp_path / "data" / "backups"
    assert not backups.exists() or list(backups.iterdir()) == [], "no backup on a fresh database"


def test_lifespan_sets_app_state(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app):
        assert app.state.db.path == tmp_path / "data" / "app.db"
        assert app.state.runner.status == "running"
        assert app.state.health is not None


def test_health_returns_version_database_ok_runner_running(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "version": app_package.__version__,
        "database": "ok",
        "runner": "running",
    }


def test_health_needs_no_launch_token_and_exposes_no_paths(tmp_path: Path) -> None:
    """ADR-023: reachable without a token, and never includes personal data such as paths."""
    with TestClient(_app(tmp_path)) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert set(response.json()) == {"version", "database", "runner"}
    assert str(tmp_path) not in response.text


def test_shutdown_stops_runner_and_closes_db(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app):
        db = app.state.db
        runner = app.state.runner
        assert runner.status == "running"

    assert runner.status == "stopped"
    with pytest.raises(sqlite3.ProgrammingError):
        db.connection.execute("SELECT 1")


def test_health_reports_database_error_with_503(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        app.state.db.close()
        response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {
        "version": app_package.__version__,
        "database": "error",
        "runner": "running",
    }


def test_lifespan_migration_failure_raises_and_closes_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = tmp_path / "migrations"
    broken.mkdir()
    (broken / "0001_broken.sql").write_text("THIS IS NOT VALID SQL;\n", encoding="utf-8")
    opened: list[Database] = []
    real_open = Database.open

    def recording_open(path: Path) -> Database:
        db = real_open(path)
        opened.append(db)
        return db

    monkeypatch.setattr("app.main.Database.open", recording_open)
    app = create_app(_config(tmp_path), clock=FrozenClock(START), migrations_dir=broken)

    with pytest.raises(MigrationError), TestClient(app):
        pass

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].connection.execute("SELECT 1")
    assert not hasattr(app.state, "health")
    assert not hasattr(app.state, "runner")
