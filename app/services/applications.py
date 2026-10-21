"""Applications (M5.3): Apply and Undo from the inbox, status moves with their follow-up
offers, interview dates, rejection feedback and the editable status list.

Every write is one short transaction and checks the version first, so a stale page changes
nothing. Job text, notes and status labels are untrusted data: nothing here logs them.
Event times are stored in UTC with the IANA zone the user chose (ADR-102 item 6).
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from functools import cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from app.domain.applications import (
    ApplicationPhase,
    ApplicationStatus,
    FollowUp,
    HistorySource,
    create_status,
    ensure_status_deletable,
    move_application,
)
from app.domain.clock import Clock
from app.domain.errors import (
    ApplicationNotFound,
    EventTimeInvalid,
    JobNotFound,
    StatusNotAllowed,
    VersionConflict,
)
from app.domain.events import wall_time_to_utc
from app.domain.ids import IdFactory, is_safe_id
from app.domain.jobs import Triage, TriageAction, TriageState, apply, local_today
from app.modules.storage import Database, applications_repo, jobs_repo
from app.modules.storage.applications_repo import StoredApplication, StoredStatus
from app.modules.storage.jobs_repo import StoredJob

UNDO_REFUSED = "This application already has changes. Withdraw it on the board instead."
_UNDO_GUARD = "this application already has changes"
_MAX_DURATION_MINUTES = 24 * 60
# ``created`` and ``backfill`` are written by the system only, never by a move.
_MOVE_SOURCES: dict[str, HistorySource] = {"board": "board", "menu": "menu", "drawer": "drawer"}


@dataclass(frozen=True)
class ApplyResult:
    job: StoredJob
    application: StoredApplication


@dataclass(frozen=True)
class MoveResult:
    application: StoredApplication
    follow_up: FollowUp | None


class ApplicationService:
    def __init__(
        self,
        database: Callable[[], Database | None],
        clock: Clock,
        ids: IdFactory,
        zone: tzinfo = UTC,
    ) -> None:
        self._database = database
        self._clock = clock
        self._ids = ids
        self._zone = zone

    def apply_to_job(self, job_id: str, *, expected_job_version: int) -> ApplyResult:
        """Mark the job To apply and create its application in the first preparing status,
        with its ``created`` history row, in one transaction."""
        db = self._database()
        if db is None or not is_safe_id(job_id):
            raise JobNotFound()
        now = self._clock.now().astimezone(UTC)
        stamp = now.isoformat()
        with db.transaction() as conn:
            job, triage = self._triage_of(conn, job_id, expected_job_version)
            new = apply(triage, TriageAction.TO_APPLY, today=self._today(now))
            first = applications_repo.first_status_in_phase(conn, ApplicationPhase.PREPARING)
            if first is None:
                raise StatusNotAllowed()
            application_id = self._ids.new()
            history_id = self._ids.new()
            self._save_triage(conn, job, new, stamp)
            applications_repo.insert_application(
                conn, id=application_id, job_id=job_id, status_key=first.key, now=stamp
            )
            applications_repo.insert_history(
                conn,
                id=history_id,
                application_id=application_id,
                from_status=None,
                to_status=first.key,
                changed_at=stamp,
                source="created",
                now=stamp,
            )
            saved_job = jobs_repo.get_job(conn, job_id)
            application = applications_repo.get_application(conn, application_id)
            if saved_job is None or application is None:
                raise JobNotFound()
            return ApplyResult(saved_job, application)

    def undo_apply(self, job_id: str, *, expected_job_version: int) -> StoredJob:
        """Undo (and Back to queue): the job returns to the state before it was marked, and
        its application is deleted while untouched. A changed application is refused and
        nothing is written. Any other undo behaves as the triage Undo."""
        db = self._database()
        if db is None or not is_safe_id(job_id):
            raise JobNotFound()
        now = self._clock.now().astimezone(UTC)
        stamp = now.isoformat()
        with db.transaction() as conn:
            job, triage = self._triage_of(conn, job_id, expected_job_version)
            new = apply(triage, TriageAction.UNDO, today=self._today(now))
            if job.triage_state == TriageState.TO_APPLY.value:
                self._delete_untouched(conn, job_id)
            self._save_triage(conn, job, new, stamp)
            saved = jobs_repo.get_job(conn, job_id)
            if saved is None:
                raise JobNotFound()
            return saved

    def move(
        self, application_id: str, status_key: str, *, expected_version: int, source: str
    ) -> MoveResult:
        """Move to any status. The status, its history row and the first applied time are
        written together; a move to the current status is recorded too."""
        db = self._database()
        if db is None or not is_safe_id(application_id):
            raise ApplicationNotFound()
        history_source = _MOVE_SOURCES.get(source)
        if history_source is None or not is_safe_id(status_key):
            raise StatusNotAllowed()
        now = self._clock.now().astimezone(UTC)
        stamp = now.isoformat()
        with db.transaction() as conn:
            current = applications_repo.get_application(conn, application_id)
            if current is None:
                raise ApplicationNotFound()
            if current.version != expected_version:
                raise VersionConflict()
            target = applications_repo.get_status(conn, status_key)
            if target is None:
                raise StatusNotAllowed()
            transition = move_application(
                current.status_key,
                datetime.fromisoformat(current.applied_at) if current.applied_at else None,
                _domain_status(target),
                changed_at=now,
                source=history_source,
            )
            history_id = self._ids.new()
            applications_repo.update_application(
                conn,
                application_id,
                status_key=target.key,
                applied_at=transition.applied_at.isoformat() if transition.applied_at else None,
                now=stamp,
                expected_version=expected_version,
            )
            applications_repo.insert_history(
                conn,
                id=history_id,
                application_id=application_id,
                from_status=transition.from_status,
                to_status=transition.to_status,
                changed_at=transition.changed_at.isoformat(),
                source=transition.source,
                now=stamp,
            )
            saved = applications_repo.get_application(conn, application_id)
            if saved is None:
                raise ApplicationNotFound()
            return MoveResult(saved, transition.follow_up)

    def add_interview_date(
        self,
        application_id: str,
        *,
        starts_at: datetime,
        time_zone: str,
        duration_minutes: int = 60,
    ) -> None:
        """Add an interview event at a local wall time in ``time_zone``. A time inside a
        spring-forward gap is refused; a repeated hour takes its first occurrence."""
        db = self._database()
        if db is None or not is_safe_id(application_id):
            raise ApplicationNotFound()
        now = self._clock.now().astimezone(UTC).isoformat()
        with db.transaction() as conn:
            if applications_repo.get_application(conn, application_id) is None:
                raise ApplicationNotFound()
            zone = _zone(time_zone)
            if type(duration_minutes) is not int or not 1 <= duration_minutes <= (
                _MAX_DURATION_MINUTES
            ):
                raise EventTimeInvalid(
                    "duration_minutes", "Use a length between 1 minute and 24 hours."
                )
            try:
                resolved = wall_time_to_utc(starts_at, zone)
                ends = resolved.instant + timedelta(minutes=duration_minutes)
            except ValueError as error:
                raise EventTimeInvalid("starts_at", str(error)) from None
            except (OverflowError, OSError):
                raise EventTimeInvalid(
                    "starts_at", "Choose a date and time that can be saved."
                ) from None
            event_id = self._ids.new()
            applications_repo.insert_event(
                conn,
                id=event_id,
                application_id=application_id,
                type="interview",
                starts_at=resolved.instant.isoformat(),
                ends_at=ends.isoformat(),
                time_zone=time_zone,
                now=now,
            )
            applications_repo.touch_application(conn, application_id, now)

    def add_feedback(self, application_id: str, text: str) -> None:
        """Keep the rejection feedback as a note on the application."""
        db = self._database()
        if db is None or not is_safe_id(application_id):
            raise ApplicationNotFound()
        cleaned = text.strip()
        now = self._clock.now().astimezone(UTC).isoformat()
        with db.transaction() as conn:
            if applications_repo.get_application(conn, application_id) is None:
                raise ApplicationNotFound()
            if not cleaned:
                raise StatusNotAllowed("Write the feedback before saving it.")
            note_id = self._ids.new()
            applications_repo.insert_note(
                conn,
                id=note_id,
                application_id=application_id,
                kind="feedback",
                text=cleaned,
                now=now,
            )
            applications_repo.touch_application(conn, application_id, now)

    # --- helpers --------------------------------------------------------------------------

    def _today(self, now: datetime) -> date:
        return local_today(now, self._zone)

    @staticmethod
    def _triage_of(
        conn: sqlite3.Connection, job_id: str, expected_version: int
    ) -> tuple[StoredJob, Triage]:
        job = jobs_repo.get_job(conn, job_id)
        if job is None or job.merged_into_job_id:
            raise JobNotFound()
        if job.version != expected_version:
            raise VersionConflict()
        triage = Triage(
            TriageState(job.triage_state),
            TriageState(job.previous_state) if job.previous_state else None,
            date.fromisoformat(job.snooze_until) if job.snooze_until else None,
        )
        return job, triage

    @staticmethod
    def _save_triage(conn: sqlite3.Connection, job: StoredJob, new: Triage, stamp: str) -> None:
        jobs_repo.update_job_triage(
            conn,
            job.id,
            expected_version=job.version,
            triage_state=new.state.value,
            previous_state=new.previous_state.value if new.previous_state else None,
            snooze_until=new.snooze_until.isoformat() if new.snooze_until else None,
            removal_reason=None,
            removal_text=None,
            now=stamp,
        )

    @staticmethod
    def _delete_untouched(conn: sqlite3.Connection, job_id: str) -> None:
        application = applications_repo.get_application_for_job(conn, job_id)
        if application is None:
            return  # a job marked To apply before applications existed
        try:
            applications_repo.delete_application(conn, application.id)
        except sqlite3.IntegrityError as error:
            if _UNDO_GUARD not in str(error):
                raise
            raise StatusNotAllowed(UNDO_REFUSED) from None


class ApplicationStatuses:
    """The status list: add, rename, reorder inside a phase and delete unused custom ones."""

    def __init__(
        self, database: Callable[[], Database | None], clock: Clock, ids: IdFactory
    ) -> None:
        self._database = database
        self._clock = clock
        self._ids = ids

    def add(self, label: str, phase: str | None) -> StoredStatus:
        db = self._database()
        if db is None:
            raise StatusNotAllowed()
        now = self._clock.now().astimezone(UTC).isoformat()
        with db.transaction() as conn:
            existing = applications_repo.list_statuses(conn)
            checked = _checked(
                key=self._ids.new(),
                label=label,
                phase=phase,
                position=applications_repo.next_status_position(conn),
                taken=[status.label for status in existing],
            )
            applications_repo.insert_status(
                conn,
                key=checked.key,
                label=checked.label,
                phase=checked.phase.value,
                position=checked.position,
                now=now,
            )
            return _stored(conn, checked.key)

    def rename(self, key: str, label: str, *, expected_version: int) -> StoredStatus:
        db = self._database()
        if db is None:
            raise StatusNotAllowed()
        now = self._clock.now().astimezone(UTC).isoformat()
        with db.transaction() as conn:
            status = _existing(conn, key, expected_version)
            others = [s.label for s in applications_repo.list_statuses(conn) if s.key != key]
            checked = _checked(
                key=key,
                label=label,
                phase=status.phase,
                position=status.position,
                taken=others,
                built_in=status.built_in,
            )
            applications_repo.rename_status(
                conn, key, label=checked.label, now=now, expected_version=expected_version
            )
            return _stored(conn, key)

    def reorder(self, key: str, direction: str, *, expected_version: int) -> StoredStatus:
        """Swap the status with its neighbour inside its phase; at the end of the phase
        nothing changes."""
        db = self._database()
        if db is None or direction not in ("up", "down"):
            raise StatusNotAllowed()
        now = self._clock.now().astimezone(UTC).isoformat()
        with db.transaction() as conn:
            status = _existing(conn, key, expected_version)
            same_phase = applications_repo.statuses_in_phase(conn, status.phase)
            index = [s.key for s in same_phase].index(key)
            other = index - 1 if direction == "up" else index + 1
            if 0 <= other < len(same_phase):
                neighbour = same_phase[other]
                applications_repo.set_status_position(
                    conn,
                    key,
                    position=neighbour.position,
                    now=now,
                    expected_version=expected_version,
                )
                applications_repo.set_status_position(
                    conn,
                    neighbour.key,
                    position=status.position,
                    now=now,
                    expected_version=neighbour.version,
                )
            return _stored(conn, key)

    def delete(self, key: str, *, expected_version: int) -> None:
        db = self._database()
        if db is None:
            raise StatusNotAllowed()
        with db.transaction() as conn:
            status = _existing(conn, key, expected_version)
            try:
                ensure_status_deletable(
                    _domain_status(status), in_use=applications_repo.status_in_use(conn, key)
                )
            except ValueError as error:
                raise StatusNotAllowed(str(error)) from None
            applications_repo.delete_status(conn, key, expected_version=expected_version)


def _existing(conn: sqlite3.Connection, key: str, expected_version: int) -> StoredStatus:
    status = applications_repo.get_status(conn, key) if is_safe_id(key) else None
    if status is None:
        raise StatusNotAllowed()
    if status.version != expected_version:
        raise VersionConflict()
    return status


def _stored(conn: sqlite3.Connection, key: str) -> StoredStatus:
    status = applications_repo.get_status(conn, key)
    if status is None:
        raise StatusNotAllowed()
    return status


def _checked(
    *,
    key: str,
    label: str,
    phase: str | None,
    position: int,
    taken: list[str],
    built_in: bool = False,
) -> ApplicationStatus:
    if phase not in {member.value for member in ApplicationPhase}:
        raise StatusNotAllowed("Choose Preparing, Active or Closed for the status.")
    try:
        return create_status(
            key=key,
            label=label,
            phase=phase,
            position=position,
            built_in=built_in,
            existing_labels=taken,
        )
    except ValueError as error:
        raise StatusNotAllowed(str(error)) from None


def _domain_status(status: StoredStatus) -> ApplicationStatus:
    return ApplicationStatus(
        status.key, status.label, ApplicationPhase(status.phase), status.position, status.built_in
    )


@cache
def _zone_names() -> frozenset[str]:
    return frozenset(available_timezones())


def _zone(name: str) -> ZoneInfo:
    """Only a canonical name from the zone database: no other case, alias file or path."""
    if not isinstance(name, str) or name not in _zone_names():
        raise EventTimeInvalid("time_zone", "Choose a time zone from the list.")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise EventTimeInvalid("time_zone", "Choose a time zone from the list.") from None
