"""The Library's query over the fact bank (AC-M3.6): full-text search with role, skill and
status filters, in pages. What the user typed reaches FTS5 only as quoted tokens (spec 3.3)."""

import json
from dataclasses import dataclass

from app.domain.errors import FactInputInvalid
from app.domain.facts import FactStatus
from app.domain.fts import fts_query
from app.modules.storage import Database, facts_repo
from app.services.facts import FactView, fact_view

# With no status chosen the Library shows what can be used; drafts have their own tab and
# archived facts are asked for.
_DEFAULT_STATUSES = (FactStatus.CONFIRMED,)
_STATUS_UNKNOWN = "Choose a status from the list."


def checked_status(status: str) -> FactStatus:
    """The status a filter names. Raises ``FactInputInvalid`` (field ``status``) for a value
    that isn't one."""
    try:
        return FactStatus(status)
    except ValueError:
        raise FactInputInvalid("status", _STATUS_UNKNOWN) from None


def _statuses(status: FactStatus | None) -> tuple[FactStatus, ...]:
    if status is None:
        return _DEFAULT_STATUSES
    return (checked_status(status),)


@dataclass(frozen=True)
class LibraryQuery:
    """Every part narrows the result. ``text`` is what the user typed; blank means no text
    filter. ``status=None`` means confirmed facts; a status that doesn't exist raises
    ``FactInputInvalid`` when the query is run."""

    text: str = ""
    role_id: str | None = None
    skill_id: str | None = None
    status: FactStatus | None = None
    page: int = 1
    page_size: int = 25

    def __post_init__(self) -> None:
        if self.page < 1 or self.page_size < 1:
            raise ValueError("page and page_size start at 1")


@dataclass(frozen=True)
class LibraryPage:
    """``items`` are most recently changed first; ``total`` counts the matches on all pages."""

    items: tuple[FactView, ...]
    total: int
    page: int
    page_size: int


class FactLibrary:
    def __init__(self, db: Database) -> None:
        self._db = db

    def search(self, query: LibraryQuery) -> LibraryPage:
        statuses = _statuses(query.status)
        with self._db.read() as conn:
            found, total = facts_repo.search(
                conn,
                statuses=json.dumps([status.value for status in statuses]),
                role_id=query.role_id,
                skill_id=query.skill_id,
                match=fts_query(query.text),
                limit=query.page_size,
                offset=(query.page - 1) * query.page_size,
            )
            items = tuple(fact_view(conn, fact_id) for fact_id in found)
        return LibraryPage(items, total, query.page, query.page_size)
