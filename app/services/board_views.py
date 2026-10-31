"""What the board screen shows (M5.4): columns and cards with their words, the sortable table,
the status menu and the two follow-up questions, and the actions behind them.

Reads change nothing; the writes go through ``ApplicationService``. Job titles, company names
and notes are untrusted data: nothing here logs them, and the templates escape them.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from functools import cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from app.domain.applications import ApplicationPhase
from app.domain.board import (
    EVENT_LABELS,
    MAX_FEEDBACK,
    MAX_FEEDBACK_MESSAGE,
    clean_feedback,
    days_in_words,
)
from app.domain.clock import Clock
from app.domain.errors import (
    ApplicationNotFound,
    EventTimeInvalid,
    StatusNotAllowed,
    format_short_day,
)
from app.modules.storage.applications_repo import StoredStatus
from app.services.applications import ApplicationService, ApplicationStatuses
from app.services.board import PAGE_SIZE, BoardCard, BoardQuery, NextEvent

NO_COMPANY = "No company"
ERROR_FIELDS = ("starts_at", "time_zone", "duration_minutes")
DEFAULT_DURATION = "60"
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
_TIME = re.compile(r"[0-9]{2}:[0-9]{2}", re.ASCII)
_MINUTES = re.compile(r"[0-9]{1,5}", re.ASCII)
_DURATION_WORDS = "Use a length between 1 minute and 24 hours."


class FollowUpInvalid(Exception):
    """A follow-up answer that can't be saved: each message is tied to its form field
    (``starts_at``, ``time_zone``, ``duration_minutes`` or ``text``)."""

    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = errors
        super().__init__("Check the highlighted fields.")


@dataclass(frozen=True)
class MenuStatus:
    key: str
    label: str


@dataclass(frozen=True)
class CardView:
    application_id: str
    version: int
    status_key: str
    company: str
    role: str
    event_label: str
    event_at: str
    event_text: str
    activity: str
    closed_label: str
    focus: bool


@dataclass(frozen=True)
class ColumnView:
    key: str
    label: str
    count: int
    cards: tuple[CardView, ...]


@dataclass(frozen=True)
class ClosedView:
    count: int
    expanded: bool
    cards: tuple[CardView, ...]


@dataclass(frozen=True)
class FollowUpView:
    """A follow-up question: ``kind`` is ``interview_date`` or ``feedback``."""

    kind: str
    application_id: str
    time_zone: str
    duration: str
    moved_to: str
    zone_names: tuple[str, ...]
    submitted: Mapping[str, str]
    errors: Mapping[str, str]
    job_id: str = ""  # the moved application's job: what the rejection's session offer is about


@dataclass(frozen=True)
class BoardScreen:
    empty: bool
    columns: tuple[ColumnView, ...]
    closed: ClosedView
    statuses: tuple[MenuStatus, ...]
    focus_closed: bool
    follow_up: FollowUpView | None


@dataclass(frozen=True)
class TableRowView:
    application_id: str
    company: str
    role: str
    status: str
    applied_at: str
    applied_text: str
    activity_at: str
    activity: str


@dataclass(frozen=True)
class TableScreen:
    empty: bool
    rows: tuple[TableRowView, ...]
    page: int
    pages: int
    total: int


@dataclass(frozen=True)
class MoveOutcome:
    label: str
    follow_up: str | None
    job_id: str = ""


@cache
def zone_names() -> tuple[str, ...]:
    return tuple(sorted(available_timezones()))


class BoardViews:
    def __init__(
        self,
        applications: ApplicationService,
        statuses: ApplicationStatuses,
        query: BoardQuery,
        clock: Clock,
        zone: tzinfo,
    ) -> None:
        self._applications = applications
        self._statuses = statuses
        self._query = query
        self._clock = clock
        self._zone = zone

    # --- reading ----------------------------------------------------------------------------

    def board(
        self,
        *,
        closed: bool,
        focus_id: str = "",
        focus_closed: bool = False,
        follow_up: FollowUpView | None = None,
    ) -> BoardScreen:
        """The columns, the Closed count (its cards only when ``closed``) and the status menu.
        The card of ``focus_id`` takes focus after a swap; when it is not on screen (it moved
        into the collapsed Closed column) the Closed button does."""
        shown = self._query.board(closed=closed)
        statuses = self._statuses.listing()
        labels = {status.key: status.label for status in statuses}
        closed_keys = {s.key for s in statuses if s.phase == ApplicationPhase.CLOSED.value}

        def view(card: BoardCard) -> CardView:
            return _card(card, labels, closed_keys, focus_id)

        columns = tuple(
            ColumnView(
                column.status.key,
                column.status.label,
                len(column.cards),
                tuple(view(card) for card in column.cards),
            )
            for column in shown.columns
        )
        closed_cards = tuple(view(card) for card in shown.closed_cards)
        on_screen = any(card.focus for column in columns for card in column.cards) or any(
            card.focus for card in closed_cards
        )
        return BoardScreen(
            empty=shown.closed_count == 0 and all(not column.cards for column in columns),
            columns=columns,
            closed=ClosedView(shown.closed_count, closed, closed_cards),
            statuses=tuple(MenuStatus(s.key, s.label) for s in statuses),
            focus_closed=focus_closed or (bool(focus_id) and not on_screen),
            follow_up=follow_up,
        )

    def table(self, sort: str, direction: str, page: int) -> TableScreen:
        shown = self._query.table(sort, descending=direction == "desc", page=page)
        labels = {s.key: s for s in self._statuses.listing()}
        now = self._clock.now().astimezone(UTC)
        rows = tuple(
            TableRowView(
                row.application_id,
                row.company or NO_COMPANY,
                row.role,
                _status_text(labels.get(row.status_key), row.status_key),
                _day(row.applied_at, self._zone),
                _day(row.applied_at, self._zone) or "Not yet",
                row.updated_at.isoformat() if row.updated_at else "",
                days_in_words(max(0, (now - row.updated_at).days)) if row.updated_at else "",
            )
            for row in shown.rows
        )
        return TableScreen(
            empty=shown.total == 0,
            rows=rows,
            page=shown.page,
            pages=max(1, -(-shown.total // PAGE_SIZE)),
            total=shown.total,
        )

    def follow_up(
        self,
        kind: str,
        application_id: str,
        *,
        time_zone: str | None = None,
        submitted: Mapping[str, str] | None = None,
        errors: Mapping[str, str] | None = None,
        moved_to: str = "",
        job_id: str = "",
    ) -> FollowUpView:
        """The question to ask after a move; ``time_zone`` is the computer's, when known."""
        return FollowUpView(
            kind,
            application_id,
            time_zone or "",
            DEFAULT_DURATION,
            moved_to,
            zone_names(),
            submitted or {},
            errors or {},
            job_id,
        )

    # --- writing ----------------------------------------------------------------------------

    def move(
        self, application_id: str, status_key: str, *, version: int, source: str
    ) -> MoveOutcome:
        """Move the application; ``ApplicationNotFound``, ``VersionConflict`` and
        ``StatusNotAllowed`` come from the service unchanged."""
        moved = self._applications.move(
            application_id, status_key, expected_version=version, source=source
        )
        label = next(
            (s.label for s in self._statuses.listing() if s.key == moved.application.status_key),
            moved.application.status_key,
        )
        return MoveOutcome(label, moved.follow_up, moved.application.job_id)

    def exists(self, application_id: str) -> bool:
        return self._applications.exists(application_id)

    def add_interview_date(self, application_id: str, values: Mapping[str, str]) -> None:
        """Save the interview date from the dialog's fields. ``ApplicationNotFound`` for an
        unknown application, ``FollowUpInvalid`` for answers that can't be saved."""
        if not self._applications.exists(application_id):
            raise ApplicationNotFound()
        errors: dict[str, str] = {}
        starts_at = _wall_time(values.get("date", ""), values.get("time", ""))
        if starts_at is None:
            errors["starts_at"] = "Choose both a date and a time."
        zone = values.get("time_zone", "")
        if not zone.strip():
            errors["time_zone"] = "Enter the time zone, for example Europe/Lisbon."
        minutes = values.get("duration_minutes", "")
        if not _MINUTES.fullmatch(minutes):
            errors["duration_minutes"] = _DURATION_WORDS
        if errors or starts_at is None:
            raise FollowUpInvalid(errors)
        try:
            self._applications.add_interview_date(
                application_id,
                starts_at=starts_at,
                time_zone=zone,
                duration_minutes=int(minutes),
            )
        except EventTimeInvalid as error:
            field = error.field if error.field in ERROR_FIELDS else "starts_at"
            raise FollowUpInvalid({field: error.user_message}) from None

    def add_feedback(self, application_id: str, text: str) -> None:
        """Keep the rejection feedback as a note. ``ApplicationNotFound`` for an unknown
        application, ``FollowUpInvalid`` for blank or too long feedback."""
        if not self._applications.exists(application_id):
            raise ApplicationNotFound()
        cleaned = clean_feedback(text)
        if len(cleaned) > MAX_FEEDBACK:
            raise FollowUpInvalid({"text": MAX_FEEDBACK_MESSAGE})
        try:
            self._applications.add_feedback(application_id, cleaned)
        except StatusNotAllowed as refused:
            raise FollowUpInvalid({"text": refused.user_message}) from None

    # --- helpers ----------------------------------------------------------------------------


def _card(
    card: BoardCard, labels: Mapping[str, str], closed_keys: set[str], focus_id: str
) -> CardView:
    label = labels.get(card.status_key, card.status_key)
    event_label, event_at, event_text = _event_line(card.next_event)
    return CardView(
        application_id=card.application_id,
        version=card.version,
        status_key=card.status_key,
        company=card.company or NO_COMPANY,
        role=card.role,
        event_label=event_label,
        event_at=event_at,
        event_text=event_text,
        activity=days_in_words(card.days_since_activity),
        closed_label=f"Closed: {label.lower()}" if card.status_key in closed_keys else "",
        focus=card.application_id == focus_id,
    )


def _wall_time(date_text: str, time_text: str) -> datetime | None:
    if not (_DATE.fullmatch(date_text) and _TIME.fullmatch(time_text)):
        return None
    try:
        return datetime.fromisoformat(f"{date_text}T{time_text}")
    except ValueError:
        return None


def _event_line(event: NextEvent | None) -> tuple[str, str, str]:
    """The next event as its type, its moment (ISO, in its own zone) and the words for it."""
    if event is None:
        return "", "", ""
    try:
        zone: tzinfo = ZoneInfo(event.time_zone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        zone = UTC
    local = event.starts_at.astimezone(zone)
    words = f"{format_short_day(local.date())}, {local:%H:%M} {local.tzname() or ''}".strip()
    return EVENT_LABELS.get(event.event_type, "Event"), local.isoformat(), words


def _status_text(status: StoredStatus | None, key: str) -> str:
    if status is None:
        return key
    if status.phase == ApplicationPhase.CLOSED.value:
        return f"Closed: {status.label.lower()}"
    return status.label


def _day(moment: datetime | None, zone: tzinfo) -> str:
    return moment.astimezone(zone).date().isoformat() if moment else ""
