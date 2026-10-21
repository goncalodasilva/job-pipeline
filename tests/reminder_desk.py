"""Fictional data, a real-SQLite desk and a running-app harness for the M5.7 reminder tests.

M5.7 builds reminders that are computed when read (ADR-013, ADR-102 item 7) and the Settings >
Board section. The tests share these names; the implementer follows them. The pure rules and
their interface are described in ``tests/unit/domain/test_reminders.py``; the settings markup is
in ``tests/settings_board_pages.py``.

Services (``app/services/reminders.py``; migration 0005 already holds the tables, no migration):

- ``ReminderService(database, clock, ids, zone=UTC)``: ``database`` is a callable returning the
  open ``Database`` (or ``None``), as the other services take it.
  * ``due() -> tuple[DueReminder, ...]`` computes the due reminders now, newest ``due_at`` first,
    from the clock, the rules, ``status_history`` (the latest entry into the application's
    current status), ``events`` and ``reminder_states``. It only reads: no row is written, no
    task made and no timer started. ``DueReminder`` has ``key``, ``kind`` (``rule``,
    ``follow_up``, ``deadline``, ``offer_deadline`` or ``thank_you``), ``application_id``,
    ``due_at`` (aware UTC) and ``version`` (the state row's version, ``0`` while the reminder was
    never acted on).
  * ``done(key, *, expected_version)``, ``dismiss(key, *, expected_version)`` and
    ``snooze(key, choice, *, expected_version)`` (``choice`` is ``tomorrow`` or ``next_week``)
    write ``reminder_states`` in one transaction and return the stored state with ``key``,
    ``state`` (``done``, ``dismissed`` or ``snoozed``), ``snooze_until`` (a ``date`` or its ISO
    text, only while snoozed) and ``version``. The first action creates the row (version 1, ids
    from the injected factory, times from the clock); each later one changes it (version + 1).
    A ``expected_version`` that is not the row's current one (``0`` when there is no row) raises
    ``VersionConflict`` and writes nothing. A key that is not a current occurrence (unknown,
    unsafe, ended by a status change, a cancelled event) raises ``ReminderNotFound``
    (``app.domain.errors``; ``message`` "This reminder wasn't found. It may have been dealt with
    or removed."). Snooze counts local dates in ``zone``.
- ``ReminderRules(database, clock, ids)``: ``listing() -> tuple[rule, ...]`` (each with
  ``status_key``, ``days``, ``enabled``, ``version``) and ``save(status_key, *, days, enabled,
  expected_version)``. ``days`` is an ``int`` or a string of ASCII digits (``parse_days``).
  ``expected_version`` is ``0`` when the status has no rule yet; saving then creates it (version
  1). An unknown status key raises ``StatusNotAllowed``; a bad ``days`` raises
  ``ReminderRuleInvalid`` (``app.domain.errors``; ``field`` ``"days"``, ``user_message`` the
  domain's ``DAYS_MESSAGE``); a stale version raises ``VersionConflict``.
- ``ApplicationStatuses.delete`` (existing) now also deletes the status's reminder rule in the
  same transaction: a rule is configuration, not use, so a custom status that only has a rule is
  still "unused". An application or a history row naming the status still blocks the delete.

``app.state.reminders`` is the ``ReminderService`` and ``app.state.reminder_rules`` the
``ReminderRules``, both built from the app's clock, ids and time zone (``create_app(tz=...)``).

Routes (launch token and origin; POST only; a form body of fields given once each; an unknown or
repeated field, a missing or non-numeric ``version`` and a bad ``choice`` are a plain-text 400):

- ``POST /reminders/{key}/done``, ``/reminders/{key}/dismiss`` (field ``version``) and
  ``/reminders/{key}/snooze`` (fields ``version`` and ``choice``). The key is a safe id; an
  unsafe or unknown one is a 404 with ``ReminderNotFound.message`` and the key is never echoed.
  A stale ``version`` is a 409 with the reload prompt. A success answers 200 for htmx (the body
  is not fixed: the notifications panel of M5.8 decides it) with an ``HX-Trigger`` header that
  holds the event ``reminders:changed`` and an ``announce`` message: "Reminder marked done.",
  "Reminder dismissed." or "Reminder snoozed until <Wed 21 Oct>." (``format_short_day`` of the
  date). A plain post answers 303 to ``/``.

Everything here is fictional.
"""

import importlib
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, tzinfo
from functools import cached_property
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from app.main import create_app
from tests.application_desk import Desk
from tests.board_pages import Seeder, stamp_after, ticker
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory
from tests.integration.test_setup_provider import make_wiring
from tests.integration.test_setup_redirect import BASE_URL, Running
from tests.integration.test_setup_reopen_banner import finished_guide, make_config

STAMP = "2026-10-01T09:00:00+00:00"
INVALID = "This request isn't valid."
RULE_KINDS = ("rule", "follow_up", "deadline", "offer_deadline", "thank_you")
# Words a scheduler, timer or background task for reminders would carry in its name.
SCHEDULING_WORDS = ("remind", "notif", "schedul", "timer", "snooze", "cron")


def instant(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    assert value.tzinfo is not None
    return value.astimezone(UTC)


def add_event(
    db: Any,
    event_id: str,
    application_id: str,
    kind: str,
    starts_at: str,
    ends_at: str | None = None,
    *,
    all_day: bool = False,
    thank_you: bool = False,
    status: str = "scheduled",
    zone: str = "UTC",
) -> str:
    """Insert one event row (instants as ISO text, UTC) and return its id."""
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO events (id, application_id, type, starts_at, ends_at, all_day, time_zone,"
            " thank_you_reminder, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                application_id,
                kind,
                starts_at,
                ends_at or starts_at,
                int(all_day),
                zone,
                int(thank_you),
                status,
                STAMP,
                STAMP,
            ),
        )
    return event_id


def all_tables(rows: Callable[[str], list[Any]]) -> dict[str, list[Any]]:
    """Every table as it is now: ``rows(sql)`` runs a query and returns its rows."""
    names = [
        row[0] if not isinstance(row, dict) else row["name"]
        for row in rows(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
    ]
    return {name: rows(f"SELECT * FROM {name} ORDER BY 1") for name in names}  # noqa: S608


def kinds(due: Any) -> list[str]:
    return [reminder.kind for reminder in due]


# --- a real database without the app ------------------------------------------------------------


class ReminderDesk(Desk):
    """The M5.3 desk (frozen clock at 2026-10-20T09:00Z, sequential ids) plus the reminder
    services in a chosen time zone."""

    def __init__(self, path: Path, zone: str = "UTC") -> None:
        super().__init__(path)
        self.zone: tzinfo = ZoneInfo(zone)
        self.reminder_ids = SequentialIdFactory("rem")

    @cached_property
    def reminders(self) -> Any:
        module = importlib.import_module("app.services.reminders")
        return module.ReminderService(lambda: self.db, self.clock, self.reminder_ids, self.zone)

    @cached_property
    def rules(self) -> Any:
        module = importlib.import_module("app.services.reminders")
        return module.ReminderRules(lambda: self.db, self.clock, self.reminder_ids)

    def applied(self, job_id: str, title: str = "Greenhouse Technician") -> Any:
        """A new job, applied for and moved to Applied now; the stored application."""
        self.add_job(job_id, title)
        application = self.apply(job_id).application
        return self.move(application, "applied").application

    def event(self, event_id: str, application: Any, kind: str, start: str, **more: Any) -> str:
        return add_event(self.db, event_id, application.id, kind, start, **more)

    def at(self, text: str) -> None:
        self.clock.set(instant(text))

    def tables(self) -> dict[str, list[Any]]:
        return all_tables(lambda sql: self.rows(sql))


# --- the running app, with a clock and a time zone of the test's choosing ----------------------


class Reminding:
    def __init__(self, run: Running, clock: FrozenClock, data_dir: Path) -> None:
        self.run = run
        self.clock = clock
        self.data_dir = data_dir

    @property
    def app(self) -> FastAPI:
        return self.run.app

    @property
    def client(self) -> TestClient:
        return self.run.client

    @property
    def db(self) -> Any:
        return self.app.state.workspace.db

    def get(self, path: str, *, htmx: bool = False, token: bool = True) -> Response:
        headers = self.run.htmx() if htmx else (self.run.with_token() if token else {})
        return self.client.get(path, headers=headers)

    def post(self, path: str, *, htmx: bool = True, **values: str) -> Response:
        headers = self.run.action(**({"HX-Request": "true"} if htmx else {}))
        return self.client.post(path, data=values, headers=headers)

    def post_data(self, path: str, data: Any, **headers: str) -> Response:
        return self.client.post(
            path, data=data, headers=self.run.action(**{"HX-Request": "true", **headers})
        )

    def rows(self, sql: str, params: tuple[object, ...] = ()) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            cursor = conn.execute(sql, params)
            names = [column[0] for column in cursor.description]
            return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        with self.db.transaction() as conn:
            conn.execute(sql, params)

    def tables(self) -> dict[str, list[Any]]:
        return all_tables(lambda sql: self.rows(sql))

    def seeder(self) -> Seeder:
        return Seeder.make(
            self.db,
            self.app.state.applications,
            self.app.state.application_statuses,
            ticker(self.clock),
            stamp_after(self.clock),
        )

    def applied(
        self, job_id: str, title: str = "Seed Buyer", company: str = "Larkspur Orchards"
    ) -> Any:
        """A job applied for and moved to Applied at the app's clock; the stored application."""
        return self.seeder().application(job_id, title, company, "applied")

    def event(self, event_id: str, application: Any, kind: str, start: str, **more: Any) -> str:
        return add_event(self.db, event_id, application.id, kind, start, **more)

    def at(self, text: str) -> None:
        self.clock.set(instant(text))

    def due(self) -> Any:
        return self.app.state.reminders.due()


@contextmanager
def reminding(
    tmp_path: Path,
    *,
    now: str = "2026-10-20T09:00:00+00:00",
    zone: str = "Europe/Lisbon",
    seed: bool = True,
) -> Iterator[Reminding]:
    """The app (lifespan included) on ``tmp_path`` with a finished setup, a frozen clock at
    ``now`` and ``zone`` as the computer's time zone. ``seed=False`` opens the folders as an
    earlier run left them, as a restart does."""
    if seed:
        finished_guide(tmp_path)
    clock = FrozenClock(instant(now))
    app = create_app(make_config(tmp_path), clock=clock, wiring=make_wiring(), tz=ZoneInfo(zone))
    with TestClient(
        app, base_url=BASE_URL, follow_redirects=False, raise_server_exceptions=False
    ) as client:
        yield Reminding(Running(app=app, client=client, hits=Counter()), clock, tmp_path / "data")


def one_day(days: float = 1) -> timedelta:
    return timedelta(days=days)
