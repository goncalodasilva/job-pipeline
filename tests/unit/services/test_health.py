"""Health service: version, database and runner status, never raising (M0.13, ADR-023).

Since M1.5 the service takes a callable returning the current database, or ``None`` before the
workspace exists (ADR-044 item 3: ``database: "not_created"`` is healthy).
"""

import dataclasses
import logging
from pathlib import Path

import pytest

from app.modules.storage import Database
from app.services.health import HealthReport, HealthService


def test_health_service_reports_ok_with_working_database(tmp_path: Path) -> None:
    with Database.open(tmp_path / "app.db") as db:
        service = HealthService(lambda: db, lambda: "running", "1.2.3")
        report = service.check()
    assert report == HealthReport(version="1.2.3", database="ok", runner="running")
    assert report.ok is True


def test_health_service_reports_error_when_database_closed(tmp_path: Path) -> None:
    db = Database.open(tmp_path / "app.db")
    db.close()
    service = HealthService(lambda: db, lambda: "stopped", "1.2.3")
    report = service.check()  # must not raise
    assert report == HealthReport(version="1.2.3", database="error", runner="stopped")
    assert report.ok is False


def test_health_service_reads_runner_status_on_each_check(tmp_path: Path) -> None:
    status = {"value": "running"}
    with Database.open(tmp_path / "app.db") as db:
        service = HealthService(lambda: db, lambda: status["value"], "1.2.3")
        assert service.check().runner == "running"
        status["value"] = "stopped"
        assert service.check().runner == "stopped"


def test_health_service_runs_its_query_inside_a_transaction(tmp_path: Path) -> None:
    """ADR-038: database access goes through ``Database.transaction()``."""
    with Database.open(tmp_path / "app.db") as db:
        calls: list[str] = []
        original = db.transaction

        def tracking_transaction():  # type: ignore[no-untyped-def]
            calls.append("transaction")
            return original()

        db.transaction = tracking_transaction  # type: ignore[method-assign]
        report = HealthService(lambda: db, lambda: "running", "1.2.3").check()
    assert report.database == "ok"
    assert calls == ["transaction"]


def test_health_report_is_frozen() -> None:
    report = HealthReport(version="1.2.3", database="ok", runner="running")
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.database = "error"  # type: ignore[misc]


def test_health_service_logs_failure_type_without_details(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db = Database.open(tmp_path / "data" / "app.db")
    db.close()
    service = HealthService(lambda: db, lambda: "running", "1.2.3")

    with caplog.at_level(logging.WARNING, logger="app.services.health"):
        service.check()

    messages = [record.getMessage() for record in caplog.records]
    assert any("ProgrammingError" in message for message in messages)
    assert not any(str(tmp_path) in message for message in messages)
    assert all(record.exc_info is None for record in caplog.records)


def test_health_not_created_is_ok() -> None:
    service = HealthService(lambda: None, lambda: "running", "1.2.3")

    report = service.check()

    assert report == HealthReport(version="1.2.3", database="not_created", runner="running")
    assert report.ok is True


def test_health_service_reads_current_database_on_each_check(tmp_path: Path) -> None:
    current: dict[str, Database | None] = {"db": None}
    service = HealthService(lambda: current["db"], lambda: "running", "1.2.3")
    assert service.check().database == "not_created"

    with Database.open(tmp_path / "app.db") as db:
        current["db"] = db
        assert service.check().database == "ok"
    assert service.check().database == "error"  # closed now
    assert service.check().ok is False
