"""Setup guide progress stored in ``setup_state`` (AC-M1.6; ADR-018).

Every valid submit saves, so closing the browser or the app never loses progress. The rules
about which steps exist and which may be skipped live in ``app.onboarding``.
"""

from datetime import datetime
from typing import Literal, cast

from app.domain.clock import Clock
from app.modules.storage import Database, StoredStep, list_steps, upsert_step

StepStatusText = Literal["pending", "done", "skipped"]


class SetupStateService:
    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    def statuses(self) -> dict[str, StepStatusText]:
        return {row.step_id: cast(StepStatusText, row.status) for row in self._rows()}

    def completed_at(self, step_id: str) -> datetime | None:
        row = next((row for row in self._rows() if row.step_id == step_id), None)
        if row is None or row.completed_at is None:
            return None
        return datetime.fromisoformat(row.completed_at)

    def mark_done(self, step_id: str) -> None:
        """Done now; a step already done keeps its first completion time."""
        now = self._clock.now().isoformat()
        with self._db.transaction() as conn:
            current = _find(list_steps(conn), step_id)
            if current is not None and current.status == "done":
                return
            upsert_step(conn, step_id, "done", completed_at=now, now=now)

    def mark_skipped(self, step_id: str) -> None:
        """Skipped for now; never undoes a step that is already done."""
        now = self._clock.now().isoformat()
        with self._db.transaction() as conn:
            current = _find(list_steps(conn), step_id)
            if current is not None and current.status == "done":
                return
            upsert_step(conn, step_id, "skipped", completed_at=None, now=now)

    def _rows(self) -> list[StoredStep]:
        with self._db.transaction() as conn:
            return list_steps(conn)


def _find(rows: list[StoredStep], step_id: str) -> StoredStep | None:
    return next((row for row in rows if row.step_id == step_id), None)
