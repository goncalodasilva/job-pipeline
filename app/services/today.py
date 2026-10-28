"""Today's attention blocks; reads only. Setup status is added by the page route."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

from app.domain.clock import Clock
from app.domain.errors import format_day, format_day_time
from app.domain.ids import is_safe_id
from app.modules.storage import Database, document_usage_repo, today_repo
from app.modules.storage.document_usage_repo import DocumentDraft
from app.modules.storage.today_repo import JobRow
from app.services.reminders import Notifications, ReminderService


@dataclass(frozen=True)
class Interview:
    id: str
    application_id: str
    company: str
    role: str
    instant: str
    local: str
    zone: str


@dataclass(frozen=True)
class TodayView:
    interviews: tuple[Interview, ...]
    reminders: Notifications
    unseen: int
    strong: tuple[JobRow, ...]
    drafts: int
    statuses: tuple[tuple[str, int], ...]
    document_drafts: tuple[DocumentDraft, ...] = ()

    @property
    def empty(self) -> bool:
        return not (
            self.interviews
            or self.reminders.due
            or self.unseen
            or self.drafts
            or self.document_drafts
        )


class TodayService:
    def __init__(
        self,
        database: Callable[[], Database | None],
        clock: Clock,
        reminders: ReminderService,
        zone: tzinfo = UTC,
    ) -> None:
        self._database = database
        self._clock = clock
        self._reminders = reminders
        self._zone = zone

    def view(self) -> TodayView:
        db = self._database()
        if db is None:
            return TodayView((), Notifications(), 0, (), 0, ())
        now = self._clock.now().astimezone(UTC)
        with db.read() as conn:
            rows = today_repo.read(
                conn,
                now=now.isoformat(),
                until=(now + timedelta(days=7)).isoformat(),
                today=now.astimezone(self._zone).date().isoformat(),
            )
            documents = document_usage_repo.document_drafts(conn)
        interviews = []
        for row in rows.interviews:
            if not is_safe_id(row.application_id):
                continue
            local = datetime.fromisoformat(row.starts_at).astimezone(ZoneInfo(row.time_zone))
            interviews.append(
                Interview(
                    row.id,
                    row.application_id,
                    row.company or "No company",
                    row.role,
                    local.isoformat(),
                    f"{format_day(local.date())} (all day)"
                    if row.all_day
                    else format_day_time(local),
                    row.time_zone,
                )
            )
        return TodayView(
            tuple(interviews),
            self._reminders.notifications(),
            rows.unseen,
            tuple(j for j in rows.strong if is_safe_id(j.id)),
            rows.drafts,
            rows.statuses,
            tuple(d for d in documents if is_safe_id(d.application_id)),
        )
