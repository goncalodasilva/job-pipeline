"""``TaskViews`` over a stub task service (M2.26 review): the time a task row shows, the
attention keys and how many reads a page or the indicator costs.

- A stored time is read only when it is an ISO 8601 time WITH a zone. Anything else (a time
  without a zone, text that is no time, nothing) gives no ``TimeView``, so the row shows no
  ``<time>`` rather than a wrong one.
- The exact time is written in the zone the views were built with, daylight saving included.
- A running task's time is when it started (``started``); once a cancel was asked for, the
  time is when that happened, so it is not "started".
- ``attention`` holds ``<id>:<attempt>`` for failed and interrupted tasks only.
- ``top_bar()`` (a page being rendered) and ``indicator()`` each read the task service once,
  through ``overview``: the list and the counts come from one snapshot.

No database: the rows are built here. ``FrozenClock``; every value is fictional.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.domain.tasks import TaskStatus
from app.services.task_views import PANEL_LIMIT, TaskViews
from app.services.tasks import TaskRow
from tests.fakes.clock import FrozenClock

NOW = datetime(2026, 4, 6, 10, 0, 0, tzinfo=UTC)
LISBON = ZoneInfo("Europe/Lisbon")
LABEL = "Fictional task"
TASK_ID = "task-fictional-0001"
ENDED = (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.INTERRUPTED)


def row(
    status: TaskStatus = TaskStatus.SUCCEEDED,
    *,
    task_id: str = TASK_ID,
    attempt: int = 1,
    updated_at: object = NOW.isoformat(),
    finished_at: object = "same",
    cancel_requested: bool = False,
) -> TaskRow:
    """A task row; an ended task finished at ``updated_at`` unless ``finished_at`` says."""
    if finished_at == "same":
        finished_at = updated_at if status in ENDED else None
    return TaskRow(
        id=task_id,
        type="fictional",
        status=status,
        dedupe_key=f"fictional:{task_id}",
        attempt=attempt,
        progress=0,
        heartbeat_at=None,
        cancel_requested=cancel_requested,
        input_ref=None,
        result_ref=None,
        error="NetworkUnavailable" if status == TaskStatus.FAILED else None,
        created_at=NOW.isoformat(),
        updated_at=updated_at,  # type: ignore[arg-type]
        finished_at=finished_at,  # type: ignore[arg-type]
        version=1,
    )


class StubTasks:
    """The reads ``TaskViews`` makes, counted."""

    def __init__(self, *rows: TaskRow, running: int = 0, queued: int = 0) -> None:
        self.rows = list(rows)
        self.counts = (running, queued)
        self.calls: list[str] = []

    def recent(self, limit: int) -> list[TaskRow]:
        self.calls.append("recent")
        return self.rows[:limit]

    def active_counts(self) -> tuple[int, int]:
        self.calls.append("active_counts")
        return self.counts

    def overview(self, limit: int) -> tuple[list[TaskRow], tuple[int, int]]:
        self.calls.append("overview")
        return self.rows[:limit], self.counts

    def get(self, task_id: str) -> TaskRow | None:
        self.calls.append("get")
        return next((found for found in self.rows if found.id == task_id), None)


def views(stub: StubTasks, *, now: datetime = NOW, tz: ZoneInfo | None = None) -> TaskViews:
    return TaskViews(
        tasks=lambda: stub,  # type: ignore[arg-type,return-value]
        label_for=lambda _type: LABEL,
        clock=FrozenClock(now),
        tz=tz if tz is not None else ZoneInfo("UTC"),
    )


# --- a stored time that can't be read shows no time --------------------------------------------


@pytest.mark.parametrize(
    "stored",
    [
        "2026-04-06T10:00:00",  # no zone: whose 10 o'clock?
        "2026-04-06 10:00:00",
        "2026-04-06",
        "not a time",
        "",
        "12:00",
        None,
        1775469600,
    ],
    ids=["naive", "naive-space", "date-only", "garbage", "empty", "time-only", "none", "number"],
)
def test_a_stored_time_without_a_zone_or_that_is_no_time_shows_none(stored: object) -> None:
    stub = StubTasks(row(TaskStatus.QUEUED, updated_at=stored))

    (view,) = views(stub, tz=LISBON).panel()

    assert view.time is None, f"{stored!r} must not be shown as a time"
    assert view.status == "Queued", "the row itself is still shown"


def test_an_unreadable_finish_time_is_not_replaced_by_another_time() -> None:
    """A finished task's time is when it finished: an unreadable one shows nothing, not the
    last update dressed up as the finish."""
    stub = StubTasks(row(TaskStatus.FAILED, finished_at="2026-04-06T10:00:00"))

    (view,) = views(stub).panel()

    assert view.time is None


def test_a_time_with_a_zone_is_shown() -> None:
    stub = StubTasks(row(updated_at="2026-04-06T09:55:00+00:00"))

    (view,) = views(stub).panel()

    assert view.time is not None
    assert (view.time.iso, view.time.exact, view.time.ago) == (
        "2026-04-06T09:55:00+00:00",
        "6 April 2026, 09:55",
        "5 minutes ago",
    )


# --- the exact time is local, daylight saving included -----------------------------------------


def test_exact_time_is_written_in_the_views_zone() -> None:
    """Lisbon is an hour ahead of UTC in April; a stored time in another zone is the same
    instant."""
    stub = StubTasks(row(updated_at="2026-04-06T11:30:00+02:00"))

    (view,) = views(stub, tz=LISBON).panel()

    assert view.time is not None
    assert view.time.iso == "2026-04-06T10:30:00+01:00"
    assert view.time.exact == "6 April 2026, 10:30"
    assert view.time.ago == "30 minutes ago"


@pytest.mark.parametrize(
    ("stored", "iso", "exact"),
    [
        # Clocks go back in Lisbon on 25 October 2026 at 01:00 UTC: 01:30 happens twice.
        ("2026-10-25T00:30:00+00:00", "2026-10-25T01:30:00+01:00", "25 October 2026, 01:30"),
        ("2026-10-25T01:30:00+00:00", "2026-10-25T01:30:00+00:00", "25 October 2026, 01:30"),
        # And forward on 29 March 2026 at 01:00 UTC: 01:30 local never happens.
        ("2026-03-29T00:59:00+00:00", "2026-03-29T00:59:00+00:00", "29 March 2026, 00:59"),
        ("2026-03-29T01:00:00+00:00", "2026-03-29T02:00:00+01:00", "29 March 2026, 02:00"),
    ],
    ids=["before-clocks-go-back", "after-clocks-go-back", "before-forward", "after-forward"],
)
def test_exact_time_on_a_daylight_saving_day(stored: str, iso: str, exact: str) -> None:
    now = datetime.fromisoformat(stored) + timedelta(hours=2)
    stub = StubTasks(row(updated_at=stored))

    (view,) = views(stub, now=now, tz=LISBON).panel()

    assert view.time is not None
    assert (view.time.iso, view.time.exact) == (iso, exact)
    assert view.time.ago == "2 hours ago", "the age counts real time, not wall-clock time"


# --- "started" ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "cancel_requested", "started"),
    [
        (TaskStatus.RUNNING, False, True),
        (TaskStatus.RUNNING, True, False),  # the time is when the cancel was asked for
        (TaskStatus.QUEUED, False, False),
        (TaskStatus.SUCCEEDED, False, False),
        (TaskStatus.FAILED, False, False),
        (TaskStatus.CANCELLED, False, False),
        (TaskStatus.INTERRUPTED, False, False),
    ],
)
def test_only_a_running_task_time_is_when_it_started(
    status: TaskStatus, cancel_requested: bool, started: bool
) -> None:
    stub = StubTasks(row(status, cancel_requested=cancel_requested))

    (view,) = views(stub).panel()

    assert view.time is not None
    assert view.time.started is started


# --- attention keys ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "needs"),
    [
        (TaskStatus.FAILED, True),
        (TaskStatus.INTERRUPTED, True),
        (TaskStatus.SUCCEEDED, False),
        (TaskStatus.CANCELLED, False),
        (TaskStatus.QUEUED, False),
        (TaskStatus.RUNNING, False),
    ],
)
def test_attention_key_is_id_and_attempt_for_failed_and_interrupted_only(
    status: TaskStatus, needs: bool
) -> None:
    stub = StubTasks(row(status, attempt=3))
    expected = f"{TASK_ID}:3" if needs else None

    (view,) = views(stub).panel()

    assert view.attention == expected
    assert views(stub).top_bar().attention == ((expected,) if expected else ())
    assert views(stub).indicator().attention == ((expected,) if expected else ())


# --- one read per answer -----------------------------------------------------------------------


def test_a_page_reads_the_tasks_once_and_builds_no_announcements() -> None:
    stub = StubTasks(
        row(TaskStatus.FAILED), row(task_id="task-fictional-0002"), running=1, queued=2
    )

    top_bar = views(stub).top_bar()

    assert stub.calls == ["overview"], "one snapshot: the list and the counts together"
    assert (top_bar.count, top_bar.attention) == ("1 running, 2 queued", (f"{TASK_ID}:1",))
    assert not hasattr(top_bar, "finished"), "a page announces nothing, so none is built"


def test_the_indicator_reads_the_tasks_once() -> None:
    stub = StubTasks(row(TaskStatus.FAILED), running=1)

    indicator = views(stub).indicator()

    assert stub.calls == ["overview"]
    assert indicator.count == "1 running"
    assert [finished.key for finished in indicator.finished] == [f"{TASK_ID}:1"]
    assert indicator.attention == (f"{TASK_ID}:1",)


def test_the_top_bar_and_the_indicator_agree() -> None:
    rows = [
        row(TaskStatus.FAILED, task_id="task-fictional-0001"),
        row(TaskStatus.CANCELLED, task_id="task-fictional-0002"),
        row(TaskStatus.INTERRUPTED, task_id="task-fictional-0003", attempt=2),
        row(TaskStatus.FAILED, task_id="not a safe id"),  # never shown, never counted
    ]
    stub = StubTasks(*rows, queued=1)

    top_bar, indicator = views(stub).top_bar(), views(stub).indicator()

    assert top_bar.count == indicator.count == "1 queued"
    assert top_bar.attention == indicator.attention
    assert top_bar.attention == ("task-fictional-0001:1", "task-fictional-0003:2")


def test_the_overview_asks_for_as_many_tasks_as_the_panel_lists() -> None:
    asked: list[int] = []

    class Recording(StubTasks):
        def overview(self, limit: int) -> tuple[list[TaskRow], tuple[int, int]]:
            asked.append(limit)
            return super().overview(limit)

    stub = Recording()
    views(stub).top_bar()
    views(stub).indicator()

    assert asked == [PANEL_LIMIT, PANEL_LIMIT]


def test_without_a_workspace_nothing_is_read() -> None:
    none = TaskViews(
        tasks=lambda: None, label_for=lambda _type: None, clock=FrozenClock(NOW), tz=LISBON
    )

    assert (none.top_bar().count, none.top_bar().attention) == ("No tasks running", ())
    assert none.indicator().finished == ()
