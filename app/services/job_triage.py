"""Versioned triage writes. A local date ends a snooze; reads never persist that change."""

import calendar
from collections.abc import Callable
from datetime import date, timedelta, tzinfo

from app.domain.clock import Clock
from app.domain.errors import JobNotFound, JobTriageInputInvalid, VersionConflict
from app.domain.ids import is_safe_id
from app.domain.jobs import Triage, TriageAction, TriageState, apply, local_today
from app.modules.storage import Database, jobs_repo
from app.modules.storage.jobs_repo import StoredJob


class JobTriageService:
    def __init__(self, database: Callable[[], Database | None], clock: Clock, zone: tzinfo) -> None:
        self._database = database
        self._clock = clock
        self._zone = zone

    def mark_seen(self, job_id: str, *, expected_version: int) -> StoredJob:
        return self._move(job_id, TriageAction.SHOW, expected_version=expected_version)

    def remove(
        self,
        job_id: str,
        *,
        expected_version: int,
        reason: str | None = None,
        text: str | None = None,
    ) -> StoredJob:
        return self._move(
            job_id, TriageAction.REMOVE, expected_version=expected_version, reason=reason, text=text
        )

    def to_apply(self, job_id: str, *, expected_version: int) -> StoredJob:
        return self._move(job_id, TriageAction.TO_APPLY, expected_version=expected_version)

    def undo(self, job_id: str, *, expected_version: int) -> StoredJob:
        return self._move(job_id, TriageAction.UNDO, expected_version=expected_version)

    def restore(self, job_id: str, *, expected_version: int) -> StoredJob:
        return self._move(job_id, TriageAction.RESTORE, expected_version=expected_version)

    def snooze(
        self,
        job_id: str,
        *,
        expected_version: int,
        choice: str,
        picked: date | str | None = None,
    ) -> StoredJob:
        return self._move(
            job_id,
            TriageAction.SNOOZE,
            expected_version=expected_version,
            choice=choice,
            picked=picked,
        )

    def _move(
        self,
        job_id: str,
        action: TriageAction,
        *,
        expected_version: int,
        reason: str | None = None,
        text: str | None = None,
        choice: str = "",
        picked: date | str | None = None,
    ) -> StoredJob:
        db = self._database()
        if db is None or not is_safe_id(job_id):
            raise JobNotFound()
        now = self._clock.now()
        today = local_today(now, self._zone)
        with db.transaction() as conn:
            job = jobs_repo.get_job(conn, job_id)
            if job is None or job.merged_into_job_id:
                raise JobNotFound()
            if job.version != expected_version:
                raise VersionConflict()
            until = _snooze_date(today, choice, picked) if action is TriageAction.SNOOZE else None
            if action is TriageAction.REMOVE:
                reason, text = _removal(reason, text)
            old = Triage(
                TriageState(job.triage_state),
                TriageState(job.previous_state) if job.previous_state else None,
                date.fromisoformat(job.snooze_until) if job.snooze_until else None,
            )
            new = apply(old, action, today=today, snooze_until=until)
            if new == old:
                return job
            jobs_repo.update_job_triage(
                conn,
                job_id,
                expected_version=expected_version,
                triage_state=new.state.value,
                previous_state=new.previous_state.value if new.previous_state else None,
                snooze_until=new.snooze_until.isoformat() if new.snooze_until else None,
                removal_reason=reason,
                removal_text=text,
                now=now.isoformat(),
            )
            saved = jobs_repo.get_job(conn, job_id)
            if saved is None:
                raise JobNotFound()
            return saved


def _removal(reason: str | None, text: str | None) -> tuple[str | None, str | None]:
    reason = reason or None
    text = text.strip() if text else None
    text = text or None
    if reason not in (
        None,
        "too_junior",
        "too_senior",
        "location",
        "salary",
        "industry",
        "company",
        "not_interested",
        "other",
    ):
        raise JobTriageInputInvalid("reason", "Choose one of the listed reasons, or skip it.")
    if text is not None and len(text) > 500:
        raise JobTriageInputInvalid("text", "Keep the reason to 500 characters or fewer.")
    if text is not None and reason not in ("not_interested", "other"):
        raise JobTriageInputInvalid("text", "Add text only for Not interested or Other.")
    return reason, text


def _snooze_date(today: date, choice: str, picked: date | str | None) -> date:
    if choice == "tomorrow":
        return today + timedelta(days=1)
    if choice == "next_week":
        return today + timedelta(days=7)
    if choice == "month":
        year = today.year + (today.month == 12)
        month = today.month % 12 + 1
        return date(year, month, min(today.day, calendar.monthrange(year, month)[1]))
    if choice == "date":
        chosen = picked
        if isinstance(picked, str):
            # Typed text is read here, after the job was found, so an unknown job stays a 404.
            try:
                chosen = date.fromisoformat(picked)
            except ValueError:
                raise JobTriageInputInvalid("picked", "Choose a valid date after today.") from None
        if isinstance(chosen, date) and chosen > today:
            return chosen
    raise JobTriageInputInvalid("picked", "Choose a date after today.")
