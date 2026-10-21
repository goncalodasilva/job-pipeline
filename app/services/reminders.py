"""Reminders (M5.7; ADR-013, ADR-102 item 7): due reminders computed when read, the actions on
them, and the per-status reminder domain.

Nothing is scheduled: no task type, timer or thread. ``ReminderService.due`` reads the rules, the
status history, the events and the saved states, and applies the clock it was given; a reminder
that was never acted on has no row. Done, Dismiss and Snooze write one state row per occurrence
key, each in one short transaction that checks the row's version first. Reminder keys are ids and
are never logged with any job, company or contact text.
"""

import sqlite3
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, tzinfo

from app.domain import reminders as domain
from app.domain.clock import Clock
from app.domain.errors import (
    ReminderNotFound,
    ReminderRuleInvalid,
    StatusNotAllowed,
    VersionConflict,
)
from app.domain.ids import IdFactory, is_safe_id
from app.modules.storage import Database, applications_repo, reminders_repo
from app.modules.storage.reminders_repo import StoredRule

DONE = "done"
DISMISSED = "dismissed"
SNOOZED = "snoozed"


@dataclass(frozen=True)
class DueReminder:
    key: str
    kind: str
    application_id: str
    due_at: datetime
    version: int  # the state row's version; 0 while the reminder was never acted on


@dataclass(frozen=True)
class ReminderStateView:
    key: str
    state: str
    snooze_until: date | None
    version: int


@dataclass(frozen=True)
class NotificationItem:
    key: str
    application_id: str
    label: str
    company: str
    role: str
    due_at: str
    version: int
    unseen: bool


@dataclass(frozen=True)
class Notifications:
    items: tuple[NotificationItem, ...] = ()

    @property
    def unseen(self) -> int:
        return sum(item.unseen for item in self.items)

    @property
    def due(self) -> int:
        return len(self.items)

    @property
    def application_ids(self) -> frozenset[str]:
        return frozenset(item.application_id for item in self.items)


LABELS = {
    "rule": "Follow-up due",
    "follow_up": "Follow-up due",
    "deadline": "Deadline due",
    "offer_deadline": "Offer deadline due",
    "thank_you": "Thank-you reminder",
}


class ReminderService:
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

    def due(self) -> tuple[DueReminder, ...]:
        """The reminders due now, newest ``due_at`` first. Reads only."""
        db = self._database()
        if db is None:
            return ()
        with db.read() as conn:
            occurrences = self._occurrences(conn)
            stored = {state.occurrence_key: state for state in reminders_repo.list_states(conn)}
        states = {key: _domain_state(state) for key, state in stored.items()}
        found = domain.due_reminders(occurrences, states, now=self._clock.now(), zone=self._zone)
        return tuple(
            DueReminder(
                o.key,
                o.kind,
                o.application_id,
                o.due_at,
                stored[o.key].version if o.key in stored else 0,
            )
            for o in found
        )

    def done(self, key: str, *, expected_version: int) -> ReminderStateView:
        return self._act(key, expected_version, lambda now: domain.done(key))

    def notifications(self) -> Notifications:
        db = self._database()
        if db is None:
            return Notifications()
        with db.read() as conn:
            return self._notifications(conn)

    def _notifications(self, conn: sqlite3.Connection) -> Notifications:
        snapshot = reminders_repo.notification_snapshot(conn)
        history: dict[str, list[domain.StatusEntry]] = defaultdict(list)
        for id, app, before, after, stamp in snapshot["history"]:
            history[app].append(
                domain.StatusEntry(id, before, after, datetime.fromisoformat(stamp))
            )
        occurrences = domain.build_occurrences(
            applications=tuple(
                domain.ApplicationFacts(a, s, tuple(history[a]))
                for a, s, _r, _c in snapshot["apps"]
            ),
            rules=tuple(domain.ReminderRule(s, d, bool(e)) for s, d, e, _v in snapshot["rules"]),
            events=tuple(
                domain.EventFacts(
                    i,
                    a,
                    t,
                    datetime.fromisoformat(s),
                    datetime.fromisoformat(e),
                    bool(day),
                    status,
                    bool(thanks),
                )
                for i, a, t, s, e, day, status, thanks in snapshot["events"]
            ),
            zone=self._zone,
        )
        stored = {r[0]: r for r in snapshot["states"]}
        states = {
            key: domain.ReminderState(key, r[1], date.fromisoformat(r[2]) if r[2] else None)
            for key, r in stored.items()
        }
        due = domain.due_reminders(occurrences, states, now=self._clock.now(), zone=self._zone)
        names = {a: (role, company or "No company") for a, _s, role, company in snapshot["apps"]}
        return Notifications(
            tuple(
                NotificationItem(
                    o.key,
                    o.application_id,
                    LABELS[o.kind],
                    names[o.application_id][1],
                    names[o.application_id][0],
                    o.due_at.astimezone(self._zone).isoformat(),
                    stored[o.key][3] if o.key in stored else 0,
                    o.key not in stored or stored[o.key][4] is None,
                )
                for o in due
                if o.application_id in names and is_safe_id(o.application_id)
            )
        )

    def seen(self, keys: tuple[str, ...]) -> None:
        db = self._database()
        if db is None or any(not is_safe_id(key) for key in keys):
            raise ValueError("Invalid reminder keys")
        with db.transaction() as conn:
            current = {item.key for item in self._notifications(conn).items}
            if not set(keys) <= current:
                raise ValueError("Invalid reminder keys")
            stamp = self._clock.now().astimezone(UTC).isoformat()
            for key in set(keys):
                reminders_repo.mark_seen(conn, id=self._ids.new(), key=key, now=stamp)

    def dismiss(self, key: str, *, expected_version: int) -> ReminderStateView:
        return self._act(key, expected_version, lambda now: domain.dismiss(key))

    def snooze(self, key: str, choice: str, *, expected_version: int) -> ReminderStateView:
        """Hide the reminder until tomorrow or next week (``choice``), counted in local dates."""
        return self._act(
            key,
            expected_version,
            lambda now: domain.snooze(key, choice, now=now, zone=self._zone),
        )

    # --- helpers --------------------------------------------------------------------------

    def _act(
        self,
        key: str,
        expected_version: int,
        change: Callable[[datetime], domain.ReminderState],
    ) -> ReminderStateView:
        db = self._database()
        if db is None or not is_safe_id(key):
            raise ReminderNotFound()
        now = self._clock.now().astimezone(UTC)
        stamp = now.isoformat()
        with db.transaction() as conn:
            if key not in {o.key for o in self._occurrences(conn)}:
                raise ReminderNotFound()
            current = reminders_repo.get_state(conn, key)
            seen_only = current is not None and current.state == "open" and current.version == 1
            if (current.version if current else 0) != expected_version and not (
                seen_only and expected_version == 0
            ):
                raise VersionConflict()
            new = change(now)
            until = new.snooze_until.isoformat() if new.snooze_until else None
            if current is None:
                reminders_repo.insert_state(
                    conn,
                    id=self._ids.new(),
                    key=key,
                    state=new.state,
                    snooze_until=until,
                    now=stamp,
                )
            else:
                reminders_repo.update_state(
                    conn,
                    key,
                    state=new.state,
                    snooze_until=until,
                    now=stamp,
                    expected_version=current.version,
                )
            saved = reminders_repo.get_state(conn, key)
            if saved is None:
                raise ReminderNotFound()
            return ReminderStateView(
                saved.occurrence_key,
                saved.state,
                date.fromisoformat(saved.snooze_until) if saved.snooze_until else None,
                saved.version,
            )

    def _occurrences(self, conn: sqlite3.Connection) -> tuple[domain.Occurrence, ...]:
        """Every occurrence the stored facts make now (due or not)."""
        stored_rules = reminders_repo.list_rules(conn)
        history: dict[str, list[domain.StatusEntry]] = defaultdict(list)
        for row in reminders_repo.history_rows(conn):
            history[row.application_id].append(
                domain.StatusEntry(
                    row.id,
                    row.from_status,
                    row.to_status,
                    datetime.fromisoformat(row.changed_at),
                )
            )
        applications = tuple(
            domain.ApplicationFacts(id, status, tuple(history[id]))
            for id, status in reminders_repo.ruled_applications(conn)
        )
        events = tuple(
            domain.EventFacts(
                row.id,
                row.application_id,
                row.type,
                datetime.fromisoformat(row.starts_at),
                datetime.fromisoformat(row.ends_at),
                row.all_day,
                row.status,
                row.thank_you_reminder,
            )
            for row in reminders_repo.event_rows(conn)
        )
        return domain.build_occurrences(
            applications=applications,
            events=events,
            rules=tuple(domain.ReminderRule(r.status_key, r.days, r.enabled) for r in stored_rules),
            zone=self._zone,
        )


class ReminderRules:
    """The reminder rule of each status: "Remind me if an application stays in <status> for N
    days", on or off."""

    def __init__(self, database: Callable[[], Database | None], clock: Clock, ids: IdFactory):
        self._database = database
        self._clock = clock
        self._ids = ids

    def listing(self) -> tuple[StoredRule, ...]:
        """Every saved rule. Reads only; a status without a rule has none listed."""
        db = self._database()
        if db is None:
            return ()
        with db.read() as conn:
            return tuple(reminders_repo.list_rules(conn))

    def save(
        self, status_key: str, *, days: object, enabled: bool, expected_version: int
    ) -> StoredRule:
        """Save the rule of a status; ``expected_version`` is 0 while it has none. ``days`` is
        an ``int`` or ASCII digits (``ReminderRuleInvalid`` otherwise)."""
        db = self._database()
        if db is None or not is_safe_id(status_key):
            raise StatusNotAllowed()
        stamp = self._clock.now().astimezone(UTC).isoformat()
        with db.transaction() as conn:
            if applications_repo.get_status(conn, status_key) is None:
                raise StatusNotAllowed()
            current = reminders_repo.get_rule(conn, status_key)
            if (current.version if current else 0) != expected_version:
                raise VersionConflict()
            try:
                number = domain.parse_days(days)
            except ValueError:
                raise ReminderRuleInvalid("days", domain.DAYS_MESSAGE) from None
            if current is None:
                reminders_repo.insert_rule(
                    conn,
                    id=self._ids.new(),
                    status_key=status_key,
                    days=number,
                    enabled=enabled,
                    now=stamp,
                )
            else:
                reminders_repo.update_rule(
                    conn,
                    status_key,
                    days=number,
                    enabled=enabled,
                    now=stamp,
                    expected_version=expected_version,
                )
            saved = reminders_repo.get_rule(conn, status_key)
            if saved is None:
                raise StatusNotAllowed()
            return saved


def _domain_state(state: reminders_repo.StoredState) -> domain.ReminderState:
    return domain.ReminderState(
        state.occurrence_key,
        state.state,
        date.fromisoformat(state.snooze_until) if state.snooze_until else None,
    )
