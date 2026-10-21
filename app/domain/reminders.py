"""Pure reminder rules (M5.7; ADR-013, ADR-102 item 7).

A reminder is never stored when it falls due: it is computed from what is stored (status
history, events, rules) and the clock the caller passes in, so nothing runs while the app is
closed. This module does no I/O and never reads a clock: every function that needs "now" takes
it, and every one that counts local dates takes the zone.

Each reminder is an occurrence with a key that depends only on stored ids: a rule's on the
``status_history`` row that entered the status, an event's on the event id and its kind. Done,
Dismiss and Snooze are recorded against that key, so a status change (a new history row) or a
cancelled event ends the occurrence without any clean-up.
"""

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Final

from app.domain.events import first_instant

DAYS_MESSAGE: Final = "Enter a whole number of days from 1 to 365."
MIN_DAYS: Final = 1
MAX_DAYS: Final = 365
STATES: Final = ("open", "done", "dismissed", "snoozed")
SNOOZE_CHOICES: Final = {"tomorrow": 1, "next_week": 7}
_EVENT_KINDS: Final = ("follow_up", "deadline", "offer_deadline")
_MAX_DIGITS: Final = 6


def _aware(value: datetime, what: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{what} needs an aware instant.")
    return value.astimezone(UTC)


def parse_days(raw: object) -> int:
    """A rule's days: a whole number from 1 to 365, written in ASCII digits (or an ``int`` in
    range; never a ``bool``). Anything else is a ``ValueError`` with ``DAYS_MESSAGE``."""
    if isinstance(raw, bool):
        raise ValueError(DAYS_MESSAGE)
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if not (text.isascii() and text.isdigit() and len(text) <= _MAX_DIGITS):
            raise ValueError(DAYS_MESSAGE)
        value = int(text)
    else:
        raise ValueError(DAYS_MESSAGE)
    if not MIN_DAYS <= value <= MAX_DAYS:
        raise ValueError(DAYS_MESSAGE)
    return value


@dataclass(frozen=True)
class StatusEntry:
    """One ``status_history`` row."""

    id: str
    from_status: str | None
    to_status: str
    changed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "changed_at", _aware(self.changed_at, "A status entry"))


@dataclass(frozen=True)
class ApplicationFacts:
    """An application's current status and its history, oldest first."""

    id: str
    status_key: str
    history: tuple[StatusEntry, ...] = ()


@dataclass(frozen=True)
class EventFacts:
    """An event as the reminders need it. An all-day event's ``starts_at`` is already the
    first instant of its local date (``app.domain.events.all_day_range``)."""

    id: str
    application_id: str
    type: str
    starts_at: datetime
    ends_at: datetime
    all_day: bool = False
    status: str = "scheduled"
    thank_you_reminder: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "starts_at", _aware(self.starts_at, "An event"))
        object.__setattr__(self, "ends_at", _aware(self.ends_at, "An event"))


@dataclass(frozen=True)
class ReminderRule:
    """The rule: remind me if an application stays in a status for ``days`` days."""

    status_key: str
    days: int
    enabled: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "days", parse_days(self.days))


@dataclass(frozen=True)
class Occurrence:
    key: str
    kind: str
    application_id: str
    due_at: datetime


@dataclass(frozen=True)
class ReminderState:
    """What the user did with an occurrence. ``snooze_until`` is a local date, only while
    snoozed."""

    key: str
    state: str = "open"
    snooze_until: date | None = None

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError("A reminder state is open, done, dismissed or snoozed.")
        if (self.state == "snoozed") != (self.snooze_until is not None):
            raise ValueError("Only a snoozed reminder has a snooze date.")


# --- occurrences --------------------------------------------------------------------------


def _first_instant(day: date, zone: tzinfo) -> datetime:
    """The first instant of a local date; a date the zone skips entirely gives the next one."""
    for offset in range(3):
        try:
            return first_instant(day + timedelta(days=offset), zone)
        except ValueError:
            continue
    raise ValueError("This local date does not exist in the selected time zone.")


def _current_entry(application: ApplicationFacts) -> StatusEntry | None:
    """The latest entry into the current status; a move to the status it is already in is
    recorded but is not a new entry."""
    found: StatusEntry | None = None
    for entry in application.history:
        if entry.to_status != application.status_key or entry.from_status == entry.to_status:
            continue
        if found is None or entry.changed_at >= found.changed_at:
            found = entry
    return found


def _rule_occurrences(
    applications: Iterable[ApplicationFacts], rules: Iterable[ReminderRule], zone: tzinfo
) -> list[Occurrence]:
    days_by_status = {rule.status_key: rule.days for rule in rules if rule.enabled}
    found: list[Occurrence] = []
    for application in applications:
        days = days_by_status.get(application.status_key)
        entry = _current_entry(application)
        if days is None or entry is None:
            continue
        entered = entry.changed_at.astimezone(zone).date()
        due = _first_instant(entered + timedelta(days=days), zone)
        found.append(Occurrence(f"rule-{entry.id}", "rule", application.id, due.astimezone(UTC)))
    return found


def _event_occurrences(events: Iterable[EventFacts]) -> list[Occurrence]:
    found: list[Occurrence] = []
    for event in events:
        if event.status == "cancelled":
            continue
        if event.type in _EVENT_KINDS:
            found.append(
                Occurrence(
                    f"{event.type}-{event.id}", event.type, event.application_id, event.starts_at
                )
            )
        elif event.type == "interview" and event.thank_you_reminder:
            found.append(
                Occurrence(
                    f"thank_you-{event.id}", "thank_you", event.application_id, event.ends_at
                )
            )
    return found


def build_occurrences(
    *,
    applications: Collection[ApplicationFacts],
    events: Collection[EventFacts],
    rules: Collection[ReminderRule],
    zone: tzinfo,
) -> tuple[Occurrence, ...]:
    """Every reminder the stored facts make, due or not, in a fixed order (by key)."""
    found = [*_rule_occurrences(applications, rules, zone), *_event_occurrences(events)]
    return tuple(sorted(found, key=lambda occurrence: occurrence.key))


# --- Done, Dismiss, Snooze ----------------------------------------------------------------


def snooze_date(choice: object, *, now: datetime, zone: tzinfo) -> date:
    """The local date a snooze lasts until: tomorrow, or seven local days on."""
    days = SNOOZE_CHOICES.get(choice) if isinstance(choice, str) else None
    if days is None:
        raise ValueError("A reminder is snoozed until tomorrow or next week.")
    return _aware(now, "A snooze").astimezone(zone).date() + timedelta(days=days)


def done(key: str) -> ReminderState:
    return ReminderState(key, "done")


def dismiss(key: str) -> ReminderState:
    return ReminderState(key, "dismissed")


def snooze(key: str, choice: object, *, now: datetime, zone: tzinfo) -> ReminderState:
    return ReminderState(key, "snoozed", snooze_date(choice, now=now, zone=zone))


def is_due(
    occurrence: Occurrence, state: ReminderState | None, *, now: datetime, zone: tzinfo
) -> bool:
    instant = _aware(now, "The clock")
    if occurrence.due_at > instant:
        return False
    if state is None or state.state == "open":
        return True
    if state.state == "snoozed" and state.snooze_until is not None:
        return instant.astimezone(zone).date() >= state.snooze_until
    return False


def due_reminders(
    occurrences: Iterable[Occurrence],
    states: Mapping[str, ReminderState],
    *,
    now: datetime,
    zone: tzinfo,
) -> tuple[Occurrence, ...]:
    """The occurrences due now, newest ``due_at`` first and ties by key. A state for a key
    with no occurrence is ignored."""
    due = [o for o in occurrences if is_due(o, states.get(o.key), now=now, zone=zone)]
    by_key = sorted(due, key=lambda occurrence: occurrence.key)
    return tuple(sorted(by_key, key=lambda occurrence: occurrence.due_at, reverse=True))
