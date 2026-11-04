"""The controlled-value lists of Settings > Lists and statuses (M9.2 part D; ADR-131 items 6-7;
ADR-125 item 5): add a value to industry, seniority or employment type, replace its other
spellings, delete it while no job holds it. Work mode is listed but never edited (it is tied to
``WorkMode``). Rules for the texts: ``app.domain.controlled_values``.

Each change is one short write transaction. The stored version is checked first
(``VersionConflict``), so a stale save is never judged on its values; then the values are checked
(``ControlledValueInvalid``, fixed copy); a value some job holds is not deleted
(``ControlledValueInUse``). An id that names no editable value is ``ControlledValueNotFound``.
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.domain.clock import Clock
from app.domain.controlled_values import (
    EDITABLE_FIELDS,
    LISTED_FIELDS,
    clean_aliases,
    clean_value,
)
from app.domain.errors import (
    ControlledValueInUse,
    ControlledValueInvalid,
    ControlledValueNotFound,
    VersionConflict,
)
from app.domain.ids import IdFactory, is_safe_id
from app.domain.job_normalization import value_key
from app.modules.storage import Database
from app.modules.storage import controlled_values_repo as repo


@dataclass(frozen=True)
class ControlledValueView:
    id: str
    field: str
    value: str
    aliases: tuple[str, ...]
    version: int
    jobs: int  # how many jobs hold the value

    @property
    def label(self) -> str:
        return self.value.replace("_", " ")


def _view(row: dict[str, Any]) -> ControlledValueView:
    return ControlledValueView(
        id=str(row["id"]),
        field=str(row["field"]),
        value=str(row["value"]),
        aliases=tuple(str(a) for a in row["aliases"]),
        version=int(row["version"]),
        jobs=int(row.get("jobs", 0)),
    )


class ControlledLists:
    def __init__(
        self, database: Callable[[], Database | None], clock: Clock, ids: IdFactory
    ) -> None:
        self._database, self._clock, self._ids = database, clock, ids

    def _db(self) -> Database:
        db = self._database()
        if db is None:
            raise ControlledValueNotFound(detail="no workspace is open")
        return db

    def groups(self) -> dict[str, tuple[ControlledValueView, ...]]:
        """Every listed field's values by value, with how many jobs hold each."""
        db = self._database()
        if db is None:
            return {field: () for field in LISTED_FIELDS}
        with db.read() as conn:
            return {
                field: tuple(_view(row) for row in repo.values_with_use(conn, field))
                for field in LISTED_FIELDS
            }

    def get(self, value_id: str) -> ControlledValueView | None:
        """The editable value with this id, or None (unsafe or unknown id, or a work mode)."""
        if not is_safe_id(value_id):
            return None
        db = self._database()
        if db is None:
            return None
        with db.read() as conn:
            row = repo.get_value(conn, value_id)
            if row is None or row["field"] not in EDITABLE_FIELDS:
                return None
            row["jobs"] = repo.jobs_holding(conn, row["field"], row["value"])
        return _view(row)

    def add(self, field: str, text: str) -> ControlledValueView:
        """Add ``text`` (trimmed and lower-cased) to an editable field."""
        if field not in EDITABLE_FIELDS:
            raise ControlledValueNotFound(detail="not an editable list")
        now = self._clock.now().isoformat()
        with self._db().transaction() as conn:
            taken = self._keys(conn, field)
            shown = clean_value(text, taken)
            value_id = self._ids.new()
            try:
                repo.add_value(conn, value_id=value_id, field=field, value=shown, now=now)
            except sqlite3.IntegrityError:  # the same text, whatever its key
                raise ControlledValueInvalid(
                    {"value": "This list already has that value."}
                ) from None
        return ControlledValueView(value_id, field, shown, (), 1, 0)

    def set_aliases(
        self, value_id: str, text: str, *, expected_version: int
    ) -> ControlledValueView:
        """Replace the other spellings of a value with the lines of ``text``."""
        now = self._clock.now().isoformat()
        with self._db().transaction() as conn:
            row = repo.get_value(conn, value_id) if is_safe_id(value_id) else None
            if row is None or row["field"] not in EDITABLE_FIELDS:
                raise ControlledValueNotFound(detail="no such editable value")
            if row["version"] != expected_version:
                raise VersionConflict(detail=f"controlled value {value_id} changed elsewhere")
            taken = self._keys(conn, row["field"], excluding=value_id)
            aliases = clean_aliases(text, row["value"], taken)
            if not repo.set_aliases(
                conn, value_id, aliases, expected_version=expected_version, now=now
            ):
                raise VersionConflict(detail=f"controlled value {value_id} changed elsewhere")
            jobs = repo.jobs_holding(conn, row["field"], row["value"])
        return ControlledValueView(
            value_id, row["field"], row["value"], tuple(aliases), expected_version + 1, jobs
        )

    def delete(self, value_id: str, *, expected_version: int) -> ControlledValueView:
        """Delete a value no job holds; returns the deleted value. ``ControlledValueInUse`` names
        how many jobs hold it."""
        with self._db().transaction() as conn:
            row = repo.get_value(conn, value_id) if is_safe_id(value_id) else None
            if row is None or row["field"] not in EDITABLE_FIELDS:
                raise ControlledValueNotFound(detail="no such editable value")
            held = repo.delete_unused(conn, value_id, expected_version=expected_version)
            if held:
                raise ControlledValueInUse(held, detail=f"value {value_id} is held by jobs")
        return _view(row)

    @staticmethod
    def _keys(conn: sqlite3.Connection, field: str, *, excluding: str = "") -> set[str]:
        """The ``value_key`` of every value and spelling of the field (but the one excluded)."""
        keys: set[str] = set()
        for row in repo.values_with_use(conn, field):
            if row["id"] != excluding:
                keys.update(value_key(text) for text in (row["value"], *row["aliases"]))
        return keys
