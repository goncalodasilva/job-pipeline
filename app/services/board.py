"""The applications board read model (M5.3): columns of cards, the collapsed Closed column and
the sorted, paged table. Reads change nothing and each runs exactly two SELECT statements.

Job titles and company names are untrusted text: they are returned as data, never logged.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.domain.clock import Clock
from app.modules.storage import Database, applications_repo
from app.modules.storage.applications_repo import CardRow

PAGE_SIZE = 50
SORTS = applications_repo.TABLE_SORTS


@dataclass(frozen=True)
class BoardStatus:
    key: str
    label: str
    phase: str


@dataclass(frozen=True)
class NextEvent:
    starts_at: datetime


@dataclass(frozen=True)
class BoardCard:
    application_id: str
    job_id: str
    company: str
    role: str
    status_key: str
    next_event: NextEvent | None
    days_since_activity: int


@dataclass(frozen=True)
class BoardColumn:
    status: BoardStatus
    cards: tuple[BoardCard, ...]


@dataclass(frozen=True)
class BoardView:
    columns: tuple[BoardColumn, ...]
    closed_count: int
    closed_cards: tuple[BoardCard, ...]


@dataclass(frozen=True)
class TableRow:
    application_id: str
    company: str
    role: str
    status_key: str


@dataclass(frozen=True)
class TablePage:
    rows: tuple[TableRow, ...]
    total: int
    page: int
    page_size: int


class BoardQuery:
    def __init__(self, database: Callable[[], Database | None], clock: Clock) -> None:
        self._database = database
        self._clock = clock

    def board(self, *, closed: bool = False) -> BoardView:
        """A column per preparing or active status, most recently active card first. Closed
        applications are only counted unless ``closed`` asks for their cards."""
        db = self._database()
        if db is None:
            return BoardView((), 0, ())
        now = self._clock.now().astimezone(UTC)
        with db.read() as conn:
            counts = applications_repo.status_counts(conn)
            rows = applications_repo.board_cards(conn, now=now.isoformat(), include_closed=closed)
        cards = _cards(rows, now)
        by_status: dict[str, list[BoardCard]] = {}
        for card in cards:
            by_status.setdefault(card.status_key, []).append(card)
        columns = tuple(
            BoardColumn(
                BoardStatus(item.status.key, item.status.label, item.status.phase),
                tuple(by_status.get(item.status.key, ())),
            )
            for item in counts
            if item.status.phase != "closed"
        )
        closed_keys = {item.status.key for item in counts if item.status.phase == "closed"}
        return BoardView(
            columns,
            sum(item.applications for item in counts if item.status.phase == "closed"),
            tuple(card for card in cards if card.status_key in closed_keys),
        )

    def table(self, sort: str, *, descending: bool = False, page: int = 1) -> TablePage:
        """One page (50) of every application, closed ones too, sorted by ``sort`` (one of
        ``company``, ``role``, ``status``, ``applied`` or ``activity``). ``page`` is clamped to
        the pages that exist."""
        wanted = page if type(page) is int else 1
        db = self._database()
        if db is None:
            return TablePage((), 0, 1, PAGE_SIZE)
        with db.read() as conn:
            total = applications_repo.count_applications(conn)
            # A page outside 1..last (or not a number) reads as the nearest page that exists.
            page = min(max(1, wanted), max(1, -(-total // PAGE_SIZE)))
            found = applications_repo.table_page(
                conn, sort, descending=descending, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE
            )
        rows = tuple(TableRow(r.application_id, r.company, r.role, r.status_key) for r in found)
        return TablePage(rows, total, page, PAGE_SIZE)


def _cards(rows: list[CardRow], now: datetime) -> list[BoardCard]:
    """Newest activity first; the id breaks ties so the order is stable."""
    stamped = sorted(
        ((_instant(row.updated_at), row) for row in rows), key=lambda pair: pair[1].application_id
    )
    stamped.sort(key=lambda pair: pair[0], reverse=True)
    return [
        BoardCard(
            row.application_id,
            row.job_id,
            row.company,
            row.role,
            row.status_key,
            NextEvent(_instant(row.next_event_at)) if row.next_event_at else None,
            max(0, (now - updated).days),
        )
        for updated, row in stamped
    ]


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
