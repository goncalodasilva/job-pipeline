"""Health report for ``GET /health`` (ADR-023): version, database and runner status only.

Before the workspace exists the database is ``"not_created"``, which is healthy (ADR-044 item 3).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from app.modules.storage import Database

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HealthReport:
    version: str
    database: Literal["ok", "error", "not_created"]
    runner: str

    @property
    def ok(self) -> bool:
        return self.database != "error"


class HealthService:
    def __init__(
        self,
        database: Callable[[], Database | None],
        runner_status: Callable[[], str],
        version: str,
    ) -> None:
        self._database = database
        self._runner_status = runner_status
        self._version = version

    def check(self) -> HealthReport:
        return HealthReport(
            version=self._version,
            database=self._database_status(),
            runner=self._runner_status(),
        )

    def _database_status(self) -> Literal["ok", "error", "not_created"]:
        db = self._database()
        if db is None:
            return "not_created"
        return "ok" if _database_usable(db) else "error"


def _database_usable(db: Database) -> bool:
    try:
        with db.transaction() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as error:  # any failure means "not usable"; /health must never raise
        # Only the type: sqlite messages can contain file paths.
        _log.warning("health check: database not usable (%s)", type(error).__name__)
        return False
    return True
