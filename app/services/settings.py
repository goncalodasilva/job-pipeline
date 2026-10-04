"""Load and save typed settings with optimistic concurrency (spec 2.8, 3.5; ADR-027)."""

import logging
from dataclasses import dataclass
from typing import Generic, TypeVar, overload

from pydantic import BaseModel, ValidationError

from app.domain.clock import Clock
from app.domain.errors import VersionConflict
from app.domain.settings import OptionalSettingSpec, SettingSpec
from app.modules.storage import (
    Database,
    get_setting,
    insert_setting,
    update_setting,
)

_log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)
V = TypeVar("V")


@dataclass(frozen=True)
class Versioned(Generic[V]):
    value: V
    version: int  # 0: nothing stored yet


class SettingsService:
    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    @overload
    def load(self, spec: SettingSpec[T]) -> Versioned[T]: ...

    @overload
    def load(self, spec: OptionalSettingSpec[T]) -> Versioned[T | None]: ...

    def load(
        self, spec: SettingSpec[T] | OptionalSettingSpec[T]
    ) -> Versioned[T] | Versioned[T | None]:
        """The stored value, or the default when missing (version 0) or invalid."""
        with self._db.transaction() as conn:
            stored = get_setting(conn, spec.key)
        if stored is None:
            return Versioned(spec.default, 0)
        try:
            return Versioned(spec.model.model_validate_json(stored.value), stored.version)
        except ValidationError as error:
            # Only the type: pydantic's message repeats the stored value.
            _log.warning(
                "setting %s is not valid (%s); using the default",
                spec.key,
                type(error).__name__,
            )
            return Versioned(spec.default, stored.version)

    def save(
        self,
        spec: SettingSpec[T] | OptionalSettingSpec[T],
        value: T,
        *,
        expected_version: int,
    ) -> Versioned[T]:
        """Insert (``expected_version`` 0) or update; raises ``VersionConflict`` on a mismatch."""
        text = value.model_dump_json()
        now = self._clock.now().isoformat()
        with self._db.transaction() as conn:
            if expected_version == 0:
                if not insert_setting(conn, spec.key, text, now):
                    raise VersionConflict(detail=f"setting {spec.key} already exists")
            elif not update_setting(
                conn, spec.key, text, expected_version=expected_version, now=now
            ):
                raise VersionConflict(detail=f"setting {spec.key} changed elsewhere")
        return Versioned(value, expected_version + 1)
