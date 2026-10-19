"""Roles: the jobs and other positions facts belong to (ADR-089 item 11). A role the user
types is confirmed; a draft role from an import is confirmed with its first confirmed fact
(``app.services.facts``)."""

import sqlite3
from dataclasses import dataclass

from app.domain.clock import Clock
from app.domain.errors import FactInputInvalid, FactNotFound
from app.domain.facts import RoleStatus
from app.domain.ids import IdFactory
from app.domain.partial_dates import checked_partial_date
from app.modules.storage import Database, roles_repo

_NAME_NEEDED = "Add an employer or a title."


@dataclass(frozen=True)
class RoleView:
    id: str
    employer: str
    title: str
    start_date: str | None
    end_date: str | None
    status: RoleStatus
    version: int


def _view(row: roles_repo.StoredRole) -> RoleView:
    return RoleView(
        row.id,
        row.employer,
        row.title,
        row.start_date,
        row.end_date,
        RoleStatus(row.status),
        row.version,
    )


def checked_role(
    employer: str, title: str, start_date: str | None, end_date: str | None
) -> tuple[str, str, str | None, str | None]:
    """A role's fields as stored. Raises ``FactInputInvalid`` unless there is an employer or
    a title and the dates are real."""
    cleaned = (employer.strip(), title.strip())
    if not any(cleaned):
        raise FactInputInvalid("employer", _NAME_NEEDED)
    return (
        *cleaned,
        checked_partial_date("start_date", start_date),
        checked_partial_date("end_date", end_date),
    )


def insert_role(
    conn: sqlite3.Connection,
    role_id: str,
    fields: tuple[str, str, str | None, str | None],
    status: RoleStatus,
    now: str,
) -> None:
    """Store a role from ``checked_role`` fields, in the caller's transaction."""
    employer, title, start_date, end_date = fields
    roles_repo.insert_role(
        conn,
        role_id=role_id,
        employer=employer,
        title=title,
        start_date=start_date,
        end_date=end_date,
        status=status.value,
        now=now,
    )


class RoleService:
    def __init__(self, db: Database, clock: Clock, ids: IdFactory) -> None:
        self._db = db
        self._clock = clock
        self._ids = ids

    def create(
        self,
        *,
        employer: str,
        title: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> RoleView:
        """Add a role the user typed: confirmed at once."""
        fields = checked_role(employer, title, start_date, end_date)
        now = self._clock.now().isoformat()
        role_id = self._ids.new()
        with self._db.transaction() as conn:
            insert_role(conn, role_id, fields, RoleStatus.CONFIRMED, now)
        return RoleView(role_id, *fields, RoleStatus.CONFIRMED, 1)

    def get(self, role_id: str) -> RoleView:
        with self._db.read() as conn:
            row = roles_repo.get_role(conn, role_id)
        if row is None:
            raise FactNotFound(detail=f"role {role_id} not found")
        return _view(row)

    def list(self) -> tuple[RoleView, ...]:
        """Every role, draft or confirmed, oldest first."""
        with self._db.read() as conn:
            return tuple(_view(row) for row in roles_repo.list_roles(conn))
