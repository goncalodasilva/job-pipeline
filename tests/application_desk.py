"""A real SQLite workspace for the M5.3 application, status and board tests.

The services are built lazily (``desk.applications`` and so on) so a missing module fails the
test that needs it, not the fixture. Everything here is fictional.

Assumed interface (the implementer matches it):

- ``app.services.applications.ApplicationService(database, clock, ids)`` with
  ``apply_to_job(job_id, *, expected_job_version) -> ApplyResult(job, application)``,
  ``undo_apply(job_id, *, expected_job_version) -> StoredJob``,
  ``move(application_id, status_key, *, expected_version, source) -> MoveResult(application,
  follow_up)``, ``add_interview_date(application_id, *, starts_at, time_zone, duration_minutes=60)``
  (``starts_at`` is a naive local wall time) and ``add_feedback(application_id, text)``.
- ``app.services.applications.ApplicationStatuses(database, clock, ids)`` with
  ``add(label, phase)``, ``rename(key, label, *, expected_version)``,
  ``reorder(key, direction, *, expected_version)`` (``"up"`` or ``"down"`` inside its phase) and
  ``delete(key, *, expected_version)``; each answer a status with ``key``, ``label``, ``phase``,
  ``position``, ``built_in`` and ``version``.
- ``app.services.board.BoardQuery(database, clock)`` with ``board(*, closed=False)`` and
  ``table(sort, *, descending=False, page=1)``.
"""

import importlib
from datetime import UTC, datetime, timedelta
from functools import cached_property
from pathlib import Path
from typing import Any

from app.domain.job_normalization import company_key
from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations, jobs_repo
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory

START = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
STAMP = START.isoformat()
UNDO_REFUSED = "This application already has changes. Withdraw it on the board instead."

# Every table an application change may touch; a refused change leaves them as they were.
WATCHED = (
    "jobs",
    "applications",
    "status_history",
    "application_notes",
    "events",
    "application_statuses",
)


def wall(text: str) -> datetime:
    """A local wall time without a zone, as the event form supplies it (``2026-11-03T14:00``)."""
    value = datetime.fromisoformat(text)
    assert value.tzinfo is None
    return value


def error(name: str) -> type[Exception]:
    """An error class from ``app.domain.errors``, looked up when the test runs."""
    found = getattr(importlib.import_module("app.domain.errors"), name)
    assert isinstance(found, type)
    return found


class Desk:
    def __init__(self, path: Path) -> None:
        self.clock = FrozenClock(START)
        self.ids = SequentialIdFactory("id")
        self.status_ids = SequentialIdFactory("status")
        self.db = Database.open(path / "app.db")
        apply_migrations(self.db, MIGRATIONS_DIR, path / "backups", self.clock)
        self._companies: dict[str, str] = {}

    def close(self) -> None:
        self.db.close()

    # --- services -------------------------------------------------------------------------------

    @cached_property
    def applications(self) -> Any:
        module = importlib.import_module("app.services.applications")
        return module.ApplicationService(lambda: self.db, self.clock, self.ids)

    @cached_property
    def statuses(self) -> Any:
        module = importlib.import_module("app.services.applications")
        return module.ApplicationStatuses(lambda: self.db, self.clock, self.status_ids)

    @cached_property
    def board(self) -> Any:
        module = importlib.import_module("app.services.board")
        return module.BoardQuery(lambda: self.db, self.clock)

    # --- fictional data -------------------------------------------------------------------------

    def add_job(self, job_id: str, title: str, company: str | None = "Larkspur Orchards") -> str:
        with self.db.transaction() as conn:
            company_id = None
            if company is not None:
                company_id = self._companies.get(company)
                if company_id is None:
                    company_id = f"company-{len(self._companies) + 1:04d}"
                    self._companies[company] = company_id
                    jobs_repo.insert_company(
                        conn, id=company_id, name=company, name_key=company_key(company), now=STAMP
                    )
            jobs_repo.insert_job(
                conn,
                id=job_id,
                title=title,
                snapshot_text=f"Fictional posting for {title}.",
                company_id=company_id,
                captured_at=STAMP,
                found_at=STAMP,
                now=STAMP,
            )
        return job_id

    def job(self, job_id: str) -> Any:
        with self.db.read() as conn:
            return jobs_repo.get_job(conn, job_id)

    def apply(self, job_id: str) -> Any:
        """Apply for a job at its current version; the result has ``job`` and ``application``."""
        return self.applications.apply_to_job(job_id, expected_job_version=self.job(job_id).version)

    def move(self, application: Any, status_key: str, source: str = "board") -> Any:
        """Move an application at its current version."""
        version = self.rows("SELECT version FROM applications WHERE id=?", (application.id,))[0][0]
        return self.applications.move(
            application.id, status_key, expected_version=version, source=source
        )

    def tick(self, **delta: float) -> None:
        self.clock.advance(timedelta(**delta))

    # --- reading --------------------------------------------------------------------------------

    def rows(self, sql: str, params: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
        return self.db.connection.execute(sql, params).fetchall()

    def snapshot(self) -> dict[str, list[tuple[Any, ...]]]:
        return {
            table: self.rows(f"SELECT * FROM {table} ORDER BY 1")  # noqa: S608
            for table in WATCHED
        }

    def history(self, application_id: str) -> list[tuple[Any, ...]]:
        return self.rows(
            "SELECT from_status, to_status, changed_at, source FROM status_history "
            "WHERE application_id=? ORDER BY changed_at, id",
            (application_id,),
        )

    def order(self, phase: str) -> list[str]:
        return [
            key
            for (key,) in self.rows(
                "SELECT key FROM application_statuses WHERE phase=? ORDER BY position, key",
                (phase,),
            )
        ]
