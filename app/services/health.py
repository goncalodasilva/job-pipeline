"""Health report for ``GET /health`` (ADR-023): version, database and runner status only."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from app.modules.storage import Database

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HealthReport:
    version: str
    database: Literal["ok", "error"]
    runner: str

    @property
    def ok(self) -> bool:
        return self.database == "ok"


class HealthService:
    def __init__(self, db: Database, runner_status: Callable[[], str], version: str) -> None:
        self._db = db
        self._runner_status = runner_status
        self._version = version

    def check(self) -> HealthReport:
        return HealthReport(
            version=self._version,
            database="ok" if self._database_usable() else "error",
            runner=self._runner_status(),
        )

    def _database_usable(self) -> bool:
        try:
            with self._db.transaction() as conn:
                conn.execute("SELECT 1").fetchone()
        except Exception as error:  # any failure means "not usable"; /health must never raise
            # Only the type: sqlite messages can contain file paths.
            _log.warning("health check: database not usable (%s)", type(error).__name__)
            return False
        return True
