"""Snapshot inbox reads: two queries per page, one for grouped counts."""

from collections.abc import Callable
from datetime import tzinfo

from app.domain.clock import Clock
from app.domain.inbox import InboxFilters, InboxGroup, InboxPage
from app.domain.jobs import local_today
from app.modules.storage import Database
from app.modules.storage import inbox_repo as repo

# SQLite binds offsets as signed 64-bit integers. Keep the whole last page representable.
MAX_PAGE = (2**63 - 1) // 50 + 1


class InboxQuery:
    def __init__(self, database: Callable[[], Database | None], clock: Clock, zone: tzinfo) -> None:
        self._database = database
        self._clock = clock
        self._zone = zone

    def page(self, filters: InboxFilters, *, page: int = 1) -> InboxPage:
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= MAX_PAGE:
            raise ValueError("page is outside the supported integer range")
        db = self._database()
        if db is None:
            return InboxPage((), 0, page, None)
        today = local_today(self._clock.now(), self._zone)
        with db.read() as conn:
            return repo.page(conn, filters, today, page)

    def groups(self, filters: InboxFilters, *, by: str) -> tuple[InboxGroup, ...]:
        if by not in ("company", "industry", "band", "work_mode", "seniority"):
            raise ValueError("unknown grouping")
        db = self._database()
        if db is None:
            return ()
        today = local_today(self._clock.now(), self._zone)
        with db.read() as conn:
            return repo.groups(conn, filters, today, by)

    def explain(self, filters: InboxFilters) -> tuple[str, ...]:
        db = self._database()
        if db is None:
            return ()
        today = local_today(self._clock.now(), self._zone)
        with db.read() as conn:
            return repo.explain(conn, filters, today)
