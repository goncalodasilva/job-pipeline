"""Application timeline and editors. Reads do not mutate storage or start work."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, cast
from zoneinfo import ZoneInfo

from app.domain.application_detail import EVENT_TYPES, DetailInvalid, checked_event, checked_note
from app.domain.clock import Clock
from app.domain.companies import safe_website
from app.domain.errors import ApplicationNotFound, EventTimeInvalid, VersionConflict
from app.domain.events import all_day_range, wall_time_to_utc
from app.domain.ids import IdFactory, is_safe_id
from app.modules.calendar import CalendarFile, CalendarProvider, EventData, EventType
from app.modules.storage import Database, applications_repo
from app.modules.storage import application_detail_repo as repo
from app.services.applications import _zone
from app.services.board_views import zone_names


class ApplicationDetail:
    def __init__(
        self,
        database: Callable[[], Database | None],
        clock: Clock,
        ids: IdFactory,
        calendar: CalendarProvider,
        zone: tzinfo = UTC,
    ) -> None:
        self._database, self._clock, self._ids, self._calendar = database, clock, ids, calendar
        self._zone = zone

    def _db(self, aid: str) -> Database:
        db = self._database()
        if db is None or not is_safe_id(aid):
            raise ApplicationNotFound()
        return db

    def view(self, aid: str, default_zone: str | None = None) -> dict[str, Any]:
        with self._db(aid).read() as conn:
            application = repo.header(conn, aid)
            if application is None:
                raise ApplicationNotFound()
            notes = repo.notes(conn, aid)
            events = repo.events(conn, aid)
            history = repo.history(conn, aid)
            interactions = repo.interactions(conn, aid)
            statuses = applications_repo.list_statuses(conn)
        application["applied_date"] = (
            datetime.fromisoformat(application["applied_at"])
            .astimezone(self._zone)
            .date()
            .isoformat()
            if application["applied_at"]
            else ""
        )
        for event in events:
            event.update(_event_view(event))
        upcoming = [
            e
            for e in events
            if e["status"] == "scheduled"
            and datetime.fromisoformat(e["ends_at"]) >= self._clock.now()
        ]
        next_event = min(upcoming, key=lambda e: (e["starts_at"], e["id"]), default=None)
        timeline = [dict(kind="status", at=h["changed_at"], id=h["id"], item=h) for h in history]
        timeline.extend(dict(kind=n["kind"], at=n["updated_at"], id=n["id"], item=n) for n in notes)
        timeline.extend(dict(kind="event", at=e["updated_at"], id=e["id"], item=e) for e in events)
        timeline.extend(
            dict(kind="interaction", at=i["happened_at"], id=i["id"], item=i) for i in interactions
        )
        timeline.sort(key=lambda entry: (entry["at"], entry["id"]), reverse=True)
        return {
            "application": application,
            "notes": notes,
            "events": events,
            "timeline": timeline,
            "next_event": next_event,
            "statuses": statuses,
            "zone_names": zone_names(),
            "default_zone": default_zone or "",
            "errors": {},
            "submitted": {},
            "message": "",
            "follow_up": None,
        }

    def note(
        self, aid: str, text: str, *, note_id: str | None = None, version: int | None = None
    ) -> None:
        now = self._clock.now().astimezone(UTC).isoformat()
        with self._db(aid).transaction() as conn:
            if repo.header(conn, aid) is None:
                raise ApplicationNotFound()
            if note_id is not None:
                existing = next((n for n in repo.notes(conn, aid) if n["id"] == note_id), None)
                if existing is None:
                    raise ApplicationNotFound()
                if existing["version"] != version:
                    raise VersionConflict()
            cleaned = checked_note(text)
            if note_id is None:
                applications_repo.insert_note(
                    conn, id=self._ids.new(), application_id=aid, kind="note", text=cleaned, now=now
                )
            else:
                if version is None:
                    raise VersionConflict()
                repo.update_note(conn, aid, note_id, text=cleaned, version=version, now=now)
            applications_repo.touch_application(conn, aid, now)

    def save_event(
        self,
        aid: str,
        values: Mapping[str, str],
        *,
        event_id: str | None = None,
        version: int | None = None,
    ) -> str:
        now = self._clock.now().astimezone(UTC).isoformat()
        with self._db(aid).transaction() as conn:
            if repo.header(conn, aid) is None:
                raise ApplicationNotFound()
            if event_id is not None:
                existing = repo.event(conn, aid, event_id)
                if existing is None:
                    raise ApplicationNotFound()
                if existing["version"] != version:
                    raise VersionConflict()
                if existing["status"] == "cancelled":
                    raise DetailInvalid(
                        {"type": "This event is cancelled. Add a new event instead."}
                    )
            value = checked_event(values)
            try:
                zone = _zone(value.zone)
            except EventTimeInvalid as error:
                raise DetailInvalid({"time_zone": error.user_message}) from None
            notice = ""
            try:
                if value.all_day:
                    start, end = all_day_range(value.day, zone)
                else:
                    if value.wall is None:
                        raise DetailInvalid({"time": "Choose a time."})
                    resolved = wall_time_to_utc(value.wall, zone)
                    start, end = (
                        resolved.instant,
                        resolved.instant + timedelta(minutes=value.minutes),
                    )
                    notice = resolved.notice or ""
            except (ValueError, OverflowError, OSError) as error:
                words = (
                    str(error)
                    if isinstance(error, ValueError)
                    else "Choose a date and time that can be saved."
                )
                raise DetailInvalid({"date" if value.all_day else "time": words}) from None
            stored = {
                "type": value.kind,
                "starts_at": start.isoformat(),
                "ends_at": end.isoformat(),
                "time_zone": value.zone,
                "all_day": int(value.all_day),
                "thank_you_reminder": int(value.thank_you),
                **value.text,
            }
            repo.save_event(
                conn, aid, event_id or self._ids.new(), stored, version=version, now=now
            )
            applications_repo.touch_application(conn, aid, now)
        return notice

    def cancel(self, aid: str, event_id: str, version: int) -> None:
        now = self._clock.now().astimezone(UTC).isoformat()
        with self._db(aid).transaction() as conn:
            if repo.header(conn, aid) is None or repo.event(conn, aid, event_id) is None:
                raise ApplicationNotFound()
            repo.cancel_event(conn, aid, event_id, version=version, now=now)
            applications_repo.touch_application(conn, aid, now)

    def export(self, aid: str, event_id: str) -> CalendarFile:
        with self._db(aid).read() as conn:
            application = repo.header(conn, aid)
            event = repo.event(conn, aid, event_id) if is_safe_id(event_id) else None
            if application is None or event is None:
                raise ApplicationNotFound()
        details = "\n".join(
            f"{label}: {event[key]}"
            for key, label in (
                ("round", "Round"),
                ("format", "Format"),
                ("interviewers", "Interviewers"),
                ("details", "Details"),
            )
            if event[key]
        )
        file = self._calendar.export_event(
            EventData(
                uid=event["id"],
                title=(
                    f"{EVENT_TYPES[event['type']]}: {application['title']} — "
                    f"{application['company'] or 'No company'}"
                ),
                starts_at=datetime.fromisoformat(event["starts_at"]),
                ends_at=datetime.fromisoformat(event["ends_at"]),
                time_zone=event["time_zone"],
                details=details,
                sequence=event["version"],
                all_day=bool(event["all_day"]),
                location=event["location"],
                cancelled=event["status"] == "cancelled",
                event_type=cast(EventType, event["type"]),
            )
        )
        return CalendarFile("jobpipeline-event.ics", file.content)


def _event_view(event: dict[str, Any]) -> dict[str, Any]:
    start = datetime.fromisoformat(event["starts_at"]).astimezone(ZoneInfo(event["time_zone"]))
    end = datetime.fromisoformat(event["ends_at"])
    return {
        "label": EVENT_TYPES[event["type"]],
        "date": start.date().isoformat(),
        "time": start.strftime("%H:%M"),
        "duration_minutes": str(int((end - start.astimezone(UTC)).total_seconds() // 60)),
        "local_text": f"{start:%Y-%m-%d}"
        + ("" if event["all_day"] else f" {start:%H:%M}")
        + f" {event['time_zone']}",
        "link": safe_website(event["location"]),
    }
