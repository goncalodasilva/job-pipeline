"""Snapshot inbox reads: two queries per page, one for grouped counts."""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, tzinfo
from typing import Any

from app.domain.clock import Clock
from app.domain.inbox import InboxFilters, InboxGroup, InboxPage
from app.domain.jobs import local_today
from app.modules.storage import Database, inbox_card_repo, removal_learning_repo
from app.modules.storage import inbox_repo as repo
from app.services.removal_learning import flag_labels, hidden_jobs

PAGE_SIZE = 50
# SQLite binds offsets as signed 64-bit integers. Keep the whole last page representable.
MAX_PAGE = (2**63 - 1) // PAGE_SIZE + 1


@dataclass(frozen=True)
class InboxSnapshot:
    results: InboxPage
    groups: tuple[InboxGroup, ...]
    card: dict[str, Any] | None
    hidden_count: int = 0
    rules_active: bool = False


def _hidden(
    conn: sqlite3.Connection,
    today: date,
    rules: tuple[removal_learning_repo.Rule, ...] | None = None,
) -> frozenset[str]:
    active = removal_learning_repo.rules(conn, active_only=True) if rules is None else rules
    return (
        hidden_jobs(removal_learning_repo.signals(conn, today), active)
        if any(rule.mode == "hide" for rule in active)
        else frozenset()
    )


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
            return repo.page(conn, filters, today, page, hidden=_hidden(conn, today))

    def groups(self, filters: InboxFilters, *, by: str) -> tuple[InboxGroup, ...]:
        if by not in ("company", "industry", "band", "work_mode", "seniority"):
            raise ValueError("unknown grouping")
        db = self._database()
        if db is None:
            return ()
        today = local_today(self._clock.now(), self._zone)
        with db.read() as conn:
            return repo.groups(conn, filters, today, by, hidden=_hidden(conn, today))

    def snapshot(
        self, filters: InboxFilters, *, page: int = 1, selected: str = "", group_by: str = ""
    ) -> InboxSnapshot:
        """Read the queue, grouped counts and focused card in one SQLite snapshot.

        A summary can merge a duplicate concurrently. Reading its card after ending the
        queue's snapshot could otherwise turn a valid list entry into JobNotFound.
        """
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= MAX_PAGE:
            raise ValueError("page is outside the supported integer range")
        if group_by and group_by not in ("company", "industry", "band", "work_mode", "seniority"):
            raise ValueError("unknown grouping")
        db = self._database()
        if db is None:
            return InboxSnapshot(InboxPage((), 0, page, None), (), None)
        today = local_today(self._clock.now(), self._zone)
        with db.read() as conn:
            rules = removal_learning_repo.rules(conn, active_only=True)
            hidden = _hidden(conn, today, rules)
            results = repo.page(conn, filters, today, page, hidden=hidden)
            if not results.items and page > 1 and results.total > 0:
                page = (results.total + PAGE_SIZE - 1) // PAGE_SIZE
                results = repo.page(conn, filters, today, page, hidden=hidden)
            groups = repo.groups(conn, filters, today, group_by, hidden=hidden) if group_by else ()
            if group_by:
                field = {"company": "company_id", "band": "fit_band"}.get(group_by, group_by)
                results = replace(
                    results,
                    items=tuple(
                        item
                        for group in groups
                        for item in results.items
                        if item[field] == group.value
                    ),
                )
            ids = [item["id"] for item in results.items]
            focused = selected if selected in ids else (ids[0] if ids else "")
            card = inbox_card_repo.card(conn, focused) if focused else None
            if card is not None:
                card["rule_flags"] = flag_labels(conn, focused, today)
            return InboxSnapshot(results, groups, card, len(hidden), bool(rules))

    def explain(self, filters: InboxFilters) -> tuple[str, ...]:
        db = self._database()
        if db is None:
            return ()
        today = local_today(self._clock.now(), self._zone)
        with db.read() as conn:
            return repo.explain(conn, filters, today, hidden=_hidden(conn, today))
