"""Jobs: triage moves, the snooze that falls due on the user's local date, the derived
"Processing" state and the field merge (M4.2; ADR-098 items 3 and 8).

Pure: no I/O and no clock. Callers pass the local date (``local_today``) and the active task
dedupe keys; the stored values match migration 0004's CHECKs on ``jobs``.
"""

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from enum import StrEnum
from typing import Final

from app.domain.errors import TriageNotAllowed
from app.domain.tasks import dedupe_key


class TriageState(StrEnum):
    """Where a job is in the inbox (``jobs.triage_state``)."""

    NEW = "new"
    SEEN = "seen"
    SNOOZED = "snoozed"
    REMOVED = "removed"
    TO_APPLY = "to_apply"


class TriageAction(StrEnum):
    """What the user (or showing a card) does to a job."""

    SHOW = "show"
    SNOOZE = "snooze"
    REMOVE = "remove"
    TO_APPLY = "to_apply"
    UNDO = "undo"
    RESTORE = "restore"


@dataclass(frozen=True)
class Triage:
    """A job's stored triage: its state, the state Undo goes back to (New or Seen, set by
    Remove, To apply and Snooze) and the local date a snooze ends."""

    state: TriageState
    previous_state: TriageState | None = None
    snooze_until: date | None = None


# The states a card in the queue reads as; Undo always goes back to one of them.
_OPEN: Final = frozenset({TriageState.NEW, TriageState.SEEN})


def local_today(now: datetime, zone: tzinfo) -> date:
    """The user's local date at ``now`` (the Clock's aware UTC instant) in ``zone``."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("local_today needs a timezone-aware datetime")
    return now.astimezone(zone).date()


def effective_state(triage: Triage, today: date) -> TriageState:
    """The state to show on ``today`` (the local date): a snooze whose date has come reads as
    New. Reading never writes; the stored value changes only through ``apply``."""
    if (
        triage.state is TriageState.SNOOZED
        and triage.snooze_until is not None
        and triage.snooze_until <= today
    ):
        return TriageState.NEW
    return triage.state


def apply(
    triage: Triage,
    action: TriageAction,
    *,
    today: date,
    snooze_until: date | None = None,
) -> Triage:
    """The triage after ``action`` on the local date ``today``, or ``TriageNotAllowed``.

    Every move reads the state as of ``today``, so a due snooze counts as New. Show marks a
    New or Seen card Seen. Snooze needs a date after today and a Seen card, a due snooze, or a
    snooze not yet due (its date changes). Remove and To apply take a New, Seen or snoozed
    card. Each of these keeps the state Undo goes back to: the state as read, or Seen for a
    snooze not yet due. Undo goes back to it once, from Removed, To apply or a snooze not yet
    due (unsnooze). Restore takes a removed job to Seen whatever it was before.
    """
    current = effective_state(triage, today)
    # A snooze not yet due was snoozed from Seen (or reached Seen by being shown).
    kept = TriageState.SEEN if current is TriageState.SNOOZED else current
    if action is TriageAction.SHOW:
        if current in _OPEN:
            return Triage(TriageState.SEEN)
    elif action is TriageAction.SNOOZE:
        # Not from a plain New: a card is marked seen when shown, before it can be snoozed.
        snoozable = current is TriageState.SEEN or triage.state is TriageState.SNOOZED
        if snoozable and snooze_until is not None and snooze_until > today:
            return Triage(TriageState.SNOOZED, kept, snooze_until)
    elif action in (TriageAction.REMOVE, TriageAction.TO_APPLY):
        if current in (*_OPEN, TriageState.SNOOZED):
            target = TriageState.REMOVED if action is TriageAction.REMOVE else TriageState.TO_APPLY
            return Triage(target, kept)
    elif action is TriageAction.UNDO:
        previous = triage.previous_state
        undoable = current in (TriageState.REMOVED, TriageState.TO_APPLY, TriageState.SNOOZED)
        if undoable and previous is not None and previous in _OPEN:
            return Triage(previous)
    elif action is TriageAction.RESTORE and triage.state is TriageState.REMOVED:
        return Triage(TriageState.SEEN)
    raise TriageNotAllowed(detail=f"{action.value} from {triage.state.value}")


SUMMARIZE_JOB: Final = "summarize_job"


def is_processing(job_id: str, active_keys: Collection[str]) -> bool:
    """Whether the job's summary is being made: its ``summarize_job`` task is queued or
    running. Derived, never stored, so a failed, cancelled or interrupted task can't leave a
    card stuck (ADR-098 item 8). ``active_keys`` are the dedupe keys of the active tasks."""
    return dedupe_key(SUMMARIZE_JOB, job_id) in active_keys


# The fields a summary fills and the user can correct, in display order.
JOB_FIELDS: Final = (
    "title",
    "company",
    "industry",
    "department",
    "work_mode",
    "locations",
    "employment_type",
    "seniority",
    "experience_min",
    "experience_max",
    "experience_text",
    "salary_min",
    "salary_max",
    "salary_currency",
    "salary_period",
    "posted_date",
)


def merge_fields(
    current: Mapping[str, object], new: Mapping[str, object], corrected: Iterable[str]
) -> dict[str, object]:
    """Every field in ``JOB_FIELDS``: a field the user corrected keeps its current value; any
    other takes the new summary's value, or None when the new summary lacks it. Names that
    aren't job fields are ignored; the inputs are not changed."""
    kept = frozenset(corrected)
    return {name: current.get(name) if name in kept else new.get(name) for name in JOB_FIELDS}
