"""Local inbox organisation, using the existing M4 schema and short transactions."""

from collections.abc import Callable, Mapping
from typing import Any

from app.domain.clock import Clock
from app.domain.ids import IdFactory, is_safe_id
from app.domain.inbox_options import checked_name, selection
from app.modules.storage import Database
from app.modules.storage import inbox_organisation_repo as repo


class InboxOrganisation:
    def __init__(
        self, database: Callable[[], Database | None], clock: Clock, ids: IdFactory
    ) -> None:
        self._database, self._clock, self._ids = database, clock, ids

    def _db(self) -> Database:
        db = self._database()
        if db is None:
            raise LookupError
        return db

    def saved_views(self) -> tuple[dict[str, Any], ...]:
        db = self._database()
        if db is None:
            return ()
        with db.read() as conn:
            rows = repo.saved_views(conn)
        return tuple(self._shown(row) for row in rows)

    @staticmethod
    def _shown(row: dict[str, Any]) -> dict[str, Any]:
        # A stored view that no longer validates (edited by hand, or from a later version)
        # opens the plain inbox instead of breaking every inbox screen.
        try:
            query = selection(row["values"], strict=True).query(include_view=True)
        except ValueError:
            query = ""
        return {**row, "query": query, "url": "/inbox?" + query if query else "/inbox"}

    def create_view(self, name: str, values: Mapping[str, str]) -> dict[str, Any]:
        name, selected = checked_name(name), selection(values, strict=True)
        id_ = self._ids.new()
        with self._db().transaction() as conn:
            repo.create_view(conn, id_, name, selected.values(), self._clock.now().isoformat())
        return next(row for row in self.saved_views() if row["id"] == id_)

    def rename_view(self, id_: str, name: str, *, expected_version: int) -> dict[str, Any]:
        self._change_view(id_, checked_name(name), expected_version)
        return next(row for row in self.saved_views() if row["id"] == id_)

    def delete_view(self, id_: str, *, expected_version: int) -> None:
        self._change_view(id_, None, expected_version)

    def _change_view(self, id_: str, name: str | None, version: int) -> None:
        if not is_safe_id(id_):
            raise LookupError
        with self._db().transaction() as conn:
            repo.change_view(conn, id_, name, version, self._clock.now().isoformat())

    def options(self) -> dict[str, Any]:
        db = self._database()
        if db is None:
            return {
                "choices": {key: () for key in ("industry", "work_mode", "seniority", "company")},
                "pending_count": 0,
            }
        with db.read() as conn:
            return {"choices": repo.filter_choices(conn), "pending_count": repo.pending_count(conn)}

    def values(self) -> dict[str, Any]:
        db = self._database()
        if db is None:
            return {"pending": (), "controlled": ()}
        with db.read() as conn:
            rows, choices = repo.pending(conn), repo.controlled(conn)
        return {"pending": rows, "controlled": choices}

    def decide(self, id_: str, action: str, *, expected_version: int, target: str = "") -> None:
        if not is_safe_id(id_):
            raise LookupError
        with self._db().transaction() as conn:
            repo.decide(
                conn,
                id_,
                action,
                expected_version,
                target,
                self._ids,
                self._clock.now().isoformat(),
            )
