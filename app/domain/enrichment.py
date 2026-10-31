"""Backlog ranking, outcomes and session suggestion windows (M7.2). Pure.

Every time is supplied by the caller. Later week/month choices retain local wall time;
cooldowns and suggestion windows are elapsed UTC time (ADR-119).
"""

import calendar
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Final, Literal

from app.domain.coverage import Strength
from app.domain.events import first_instant
from app.domain.settings import SessionSettings

ItemKind = Literal["cluster", "quality_hint", "capture_note"]
Outcome = Literal["answered", "skipped", "not_sure", "later"]
LaterChoice = Literal["next_session", "week", "month"] | date
CAPTURE_NOTE_RAISE: Final = 20
_JOB_WEIGHTS: Final = {"saved": 1, "preparing": 2, "active": 3}
_REQUIREMENT_WEIGHTS: Final = {"must_have": 3, "key": 2, "nice": 1}
_WEAKNESS: Final = {"none": 3, "thin": 2, "strong": 1}
_COOLDOWNS: Final = {"answered": 30, "skipped": 14, "not_sure": 21}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Use an aware instant.")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class BacklogItem:
    kind: ItemKind
    target_key: str
    strength: Strength = "none"
    focused: bool = False

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.target_key}"


@dataclass(frozen=True)
class JobDemand:
    job_id: str
    state: str = "saved"
    requirement_kind: str = "key"
    interview_at: datetime | None = None


@dataclass(frozen=True)
class ItemSignals:
    jobs: tuple[JobDemand, ...] = ()
    note_exists: bool = True
    note_resolved: bool = False
    fact_status: str = "confirmed"
    fact_privacy: str = "normal"
    hint_applies: bool = True


@dataclass(frozen=True)
class ItemState:
    status: Literal["open", "expired"] = "open"
    cooldown_until: datetime | None = None
    later_until: datetime | None = None
    later_next_session: bool = False
    not_sure_count: int = 0
    asked_count: int = 0
    last_outcome: Outcome | None = None
    last_asked_at: datetime | None = None
    earlier_wordings: tuple[str, ...] = ()


def expired(item: BacklogItem, signals: ItemSignals) -> bool:
    if item.kind == "cluster":
        return not any(job.state in _JOB_WEIGHTS for job in signals.jobs)
    if item.kind == "capture_note":
        return not signals.note_exists or signals.note_resolved
    return (
        signals.fact_status != "confirmed"
        or signals.fact_privacy == "never_use"
        or not signals.hint_applies
    )


def weighted_demand(signals: ItemSignals, now: datetime) -> int:
    """The summed per-job demand of an item (ADR-119), before the floor and weakness."""
    # Repeated requirements in one job use its strongest requirement, not extra votes.
    weights: dict[str, int] = {}
    for job in signals.jobs:
        weight = _JOB_WEIGHTS.get(job.state, 0)
        if not weight:
            continue
        if job.interview_at is not None and now <= _utc(job.interview_at) <= now + timedelta(
            days=14
        ):
            weight = 4
        demand = weight * _REQUIREMENT_WEIGHTS.get(job.requirement_kind, 0)
        weights[job.job_id] = max(weights.get(job.job_id, 0), demand)
    return sum(weights.values())


def score(
    item: BacklogItem,
    signals: ItemSignals,
    state: ItemState,
    now: datetime,
    *,
    include_cooldown: bool = False,
) -> int | None:
    instant = _utc(now)
    if state.status == "expired" or expired(item, signals):
        return None
    if item.kind == "cluster" and item.strength == "strong" and not item.focused:
        return None
    if state.later_until is not None and _utc(state.later_until) > instant:
        return None
    if (
        not include_cooldown
        and state.cooldown_until is not None
        and _utc(state.cooldown_until) > instant
    ):
        return None
    weakness = _WEAKNESS[item.strength] if item.kind == "cluster" else 1
    return max(1, weighted_demand(signals, instant)) * weakness + (
        CAPTURE_NOTE_RAISE if item.kind == "capture_note" else 0
    )


def order_backlog(
    items: Iterable[BacklogItem],
    signals: Mapping[str, ItemSignals],
    states: Mapping[str, ItemState],
    now: datetime,
) -> tuple[BacklogItem, ...]:
    """Fresh eligible items first; cooldown items only when no fresh items remain.

    Missing source signals are expired rather than silently offering an obsolete target.
    Input keys are unique; a duplicate key is refused so the order remains total.
    """
    ready: list[tuple[BacklogItem, ItemState, int]] = []
    cooling: list[tuple[BacklogItem, ItemState, int]] = []
    seen = set()
    for item in items:
        if item.key in seen:
            raise ValueError("Backlog keys must be unique.")
        seen.add(item.key)
        if item.key not in signals:
            continue
        state = states.get(item.key, ItemState())
        value = score(item, signals[item.key], state, now)
        if value is not None:
            ready.append((item, state, value))
        else:
            value = score(item, signals[item.key], state, now, include_cooldown=True)
            if value is not None:
                cooling.append((item, state, value))
    return tuple(
        item
        for item, _, _ in sorted(
            ready or cooling,
            key=lambda entry: (-entry[1].later_next_session, -entry[2], entry[0].key),
        )
    )


def _local_deadline(now: datetime, target: date, zone: tzinfo) -> datetime:
    wall = datetime.combine(target, now.astimezone(zone).time())
    candidates = [wall.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1)]
    valid = [value for value in candidates if value.astimezone(zone).replace(tzinfo=None) == wall]
    if valid:
        return min(valid)  # first occurrence of a repeated wall time
    # An automatic week/month delay in a gap moves forward by the gap's duration.
    after = [value for value in candidates if value.astimezone(zone).replace(tzinfo=None) > wall]
    return min(after)


def _later_deadline(choice: LaterChoice, now: datetime, zone: tzinfo) -> datetime | None:
    local = now.astimezone(zone)
    if type(choice) is date:
        if choice <= local.date():
            raise ValueError("Choose a date after today.")
        return first_instant(choice, zone)
    if choice == "next_session":
        return None
    if choice == "week":
        target = local.date() + timedelta(days=7)
    elif choice == "month":
        year, month = local.year + local.month // 12, local.month % 12 + 1
        target = date(year, month, min(local.day, calendar.monthrange(year, month)[1]))
    else:
        raise ValueError("Choose next session, a week, a month or a date.")
    return _local_deadline(now, target, zone)


def record_outcome(
    state: ItemState,
    outcome: Outcome,
    now: datetime,
    zone: tzinfo,
    later_choice: LaterChoice = "week",
    *,
    wording: str | None = None,
) -> ItemState:
    instant = _utc(now)
    if outcome not in (*_COOLDOWNS, "later"):
        raise ValueError("Choose an answer outcome.")
    wordings = state.earlier_wordings
    if outcome == "not_sure" and wording and wording.strip():
        wordings = (*wordings, wording)[-5:]
    later = _later_deadline(later_choice, instant, zone) if outcome == "later" else None
    return replace(
        state,
        status="open",
        asked_count=state.asked_count + 1,
        not_sure_count=state.not_sure_count + int(outcome == "not_sure"),
        last_outcome=outcome,
        last_asked_at=instant,
        earlier_wordings=wordings,
        cooldown_until=instant + timedelta(days=_COOLDOWNS[outcome])
        if outcome in _COOLDOWNS
        else None,
        later_until=later,
        later_next_session=outcome == "later" and later_choice == "next_session",
    )


def suggestion_due(
    settings: SessionSettings,
    last_reset: datetime | None,
    last_applied_at: datetime | None,
    now: datetime,
) -> bool:
    instant = _utc(now)
    if settings.paused or settings.frequency == "never":
        return False
    reset = _utc(last_reset) if last_reset is not None else None
    if settings.frequency == "after_application":
        applied = _utc(last_applied_at) if last_applied_at is not None else None
        return applied is not None and applied <= instant and (reset is None or applied > reset)
    window = timedelta(days=7) if settings.frequency == "weekly" else timedelta(hours=84)
    return reset is None or instant >= reset + window
