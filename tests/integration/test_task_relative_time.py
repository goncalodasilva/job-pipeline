"""Each task row shows a relative time, with the exact time available (M2.26; ADR-077 update
of the M2.20 checkpoint).

Assumed contract, for every ``.task-item`` in ``GET /tasks/panel`` and for the single task
fragment ``GET /tasks/{id}``:

- one ``<time>`` element inside the item, with ``id="task-<id>-time"``;
- its ``datetime`` attribute is the exact time of the task's last change as an ISO 8601 time
  with a zone (``datetime.fromisoformat`` reads it);
- its text is the relative time, worked out on the server from the app's injected clock:
  "just now" for a change in the same minute, then "1 minute ago", "5 minutes ago",
  "1 hour ago", "2 hours ago", "1 day ago", "3 days ago";
- its ``title`` attribute holds the exact local date and time in words and digits (the year
  and an ``H:MM`` time), so a pointer and assistive technology can reach it;
- nothing stored with the task is printed there (no error text, target or input).

Which stored time is "the last change" (``updated_at`` or ``finished_at``) is left open: the
tests keep the clock still while a task runs, so both are the same instant.

Added with the M2.26 review:

- the exact time is local: with ``create_app(tz=ZoneInfo("Europe/Lisbon"))`` the ``title`` is
  exactly "6 April 2026, 11:00" for 10:00 UTC and ``datetime`` carries ``+01:00``; on the day
  the clocks go back the same wall-clock time appears with both offsets;
- the time sits in ``.task-time``; a running task's has the hidden words "started " before
  the ``<time>`` (whose own text keeps the pattern above), no other task's has;
- an idle panel asked again a minute later differs in the relative words only (ids, exact
  times and everything else are the same), which is what the open panel's 60 s refresh shows.

The practice task (M2.17) is the vehicle. Real SQLite under ``tmp_path``; ``FrozenClock``
advanced by the test, never a sleep; every value is fictional.
"""

import re
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.ai.providers.fake import FakeAIProvider
from tests.fakes.clock import FrozenClock
from tests.integration.test_practice_task import (
    START,
    Gate,
    Practice,
    PracticeWiring,
    practising,
    started,
)
from tests.integration.test_setup_provider import make_wiring
from tests.integration.test_setup_redirect import BASE_URL, Element, Running, outline
from tests.integration.test_setup_reopen_banner import finished_guide, make_config

pytestmark = pytest.mark.integration

PANEL = "/tasks/panel"
ITEM_CLASS = "task-item"
RELATIVE = re.compile(r"just now|1 (minute|hour|day) ago|([2-9]|[1-9]\d+) (minutes|hours|days) ago")
CLOCK_TIME = re.compile(r"\b\d{1,2}[:.]\d{2}\b")
AGES = [
    (timedelta(0), "just now"),
    (timedelta(seconds=20), "just now"),
    (timedelta(minutes=1), "1 minute ago"),
    (timedelta(minutes=5), "5 minutes ago"),
    (timedelta(minutes=59, seconds=59), "59 minutes ago"),
    (timedelta(hours=1), "1 hour ago"),
    (timedelta(hours=2, minutes=40), "2 hours ago"),
    (timedelta(days=1), "1 day ago"),
    (timedelta(days=3, hours=5), "3 days ago"),
]


def items(elements: list[Element]) -> list[Element]:
    return [e for e in elements if ITEM_CLASS in (e.attrs.get("class") or "").split()]


def times_in(elements: list[Element], item: Element) -> list[Element]:
    return [e for e in elements if e.tag == "time" and e.within(item)]


def panel_times(practice: Practice) -> list[tuple[Element, Element]]:
    """Every task item of the panel with its one ``<time>`` element."""
    response = practice.run.client.get(PANEL, headers=practice.run.htmx())
    assert response.status_code == 200, response.status_code
    elements = outline(response.text)
    found = []
    for item in items(elements):
        times = times_in(elements, item)
        assert len(times) == 1, f"one <time> in {item.attrs.get('id')}, found {len(times)}"
        found.append((item, times[0]))
    return found


def finished_task(practice: Practice, **changes: str) -> str:
    task_id = started(practice, **changes)
    practice.wait_terminal(task_id)
    return task_id


def assert_exact_time(time: Element, expected: datetime) -> None:
    stamp = time.attrs.get("datetime")
    assert stamp, "the exact time is in the datetime attribute"
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None, f"an ISO time with a zone: {stamp}"
    assert parsed == expected, (stamp, expected.isoformat())
    title = time.attrs.get("title") or ""
    assert str(expected.year) in title, f"the exact date is available as a title: {title!r}"
    assert CLOCK_TIME.search(title), f"the exact time of day is available as a title: {title!r}"
    assert "ago" not in title and "just now" not in title, "the title is the exact time"


def test_each_task_row_has_a_relative_time_with_the_exact_time(tmp_path: Path) -> None:
    with practising(tmp_path) as practice:
        done = finished_task(practice)
        failed = finished_task(practice, outcome="network")

        rows = panel_times(practice)
        assert {item.attrs.get("id") for item, _time in rows} == {f"task-{done}", f"task-{failed}"}
        for item, time in rows:
            assert time.attrs.get("id") == f"{item.attrs['id']}-time"
            assert time.text == "just now", time.text
            assert_exact_time(time, START)


@pytest.mark.parametrize(("age", "words"), AGES, ids=[words for _age, words in AGES])
def test_relative_time_follows_the_injected_clock(
    tmp_path: Path, age: timedelta, words: str
) -> None:
    """No waiting: the test moves the app's clock and asks again."""
    with practising(tmp_path) as practice:
        finished_task(practice)
        practice.clock.advance(age)

        ((_item, time),) = panel_times(practice)
        assert RELATIVE.fullmatch(time.text), time.text
        assert time.text == words
        assert_exact_time(time, START)  # the exact time never moves


def test_a_running_task_has_a_time_too(tmp_path: Path) -> None:
    with practising(tmp_path) as practice:
        practice.gate.hold = True
        started(practice, duration="slow")
        practice.wait_entered(1)

        ((item, time),) = panel_times(practice)
        assert RELATIVE.fullmatch(time.text), time.text
        assert time.attrs.get("datetime"), item.attrs


def test_single_task_fragment_has_the_same_time(tmp_path: Path) -> None:
    with practising(tmp_path) as practice:
        task_id = finished_task(practice)
        practice.clock.advance(timedelta(minutes=5))

        response = practice.run.client.get(f"/tasks/{task_id}", headers=practice.run.htmx())
        assert response.status_code == 200, response.status_code
        elements = outline(response.text)
        (item,) = items(elements)
        (time,) = times_in(elements, item)
        assert time.text == "5 minutes ago"
        assert_exact_time(time, START)


def test_a_time_far_in_the_future_reads_as_just_now(tmp_path: Path) -> None:
    """A clock set back (the stored time is later than now) never shows a negative age."""
    with practising(tmp_path) as practice:
        finished_task(practice)
        practice.clock.set(START - timedelta(hours=3))

        ((_item, time),) = panel_times(practice)
        assert time.text == "just now"


# --- added with the M2.26 review ---------------------------------------------------------------

LISBON = ZoneInfo("Europe/Lisbon")
TIME_CLASS = "task-time"
HIDDEN_CLASS = "visually-hidden"


@contextmanager
def practising_in(tmp_path: Path, tz: ZoneInfo, *, now: datetime = START) -> Iterator[Practice]:
    """``practising``, with the app built for the time zone ``tz`` and the clock at ``now``."""
    finished_guide(tmp_path)
    gate = Gate()
    fake = FakeAIProvider(sleep=gate.sleep)
    clock = FrozenClock(now)
    wiring = PracticeWiring(make_wiring(), fake)
    app = create_app(make_config(tmp_path), clock=clock, wiring=wiring, tz=tz)  # type: ignore[arg-type]
    try:
        with TestClient(app, base_url=BASE_URL, follow_redirects=False) as client:
            yield Practice(Running(app=app, client=client, hits=Counter()), fake, gate, clock)
    finally:
        gate.open()


def panel_elements(practice: Practice) -> list[Element]:
    response = practice.run.client.get(PANEL, headers=practice.run.htmx())
    assert response.status_code == 200, response.status_code
    return outline(response.text)


def time_wrappers(elements: list[Element]) -> list[Element]:
    return [e for e in elements if TIME_CLASS in (e.attrs.get("class") or "").split()]


def stamp(time: Element) -> tuple[str | None, str | None]:
    return time.attrs.get("datetime"), time.attrs.get("title")


def test_exact_time_is_local_with_its_offset(tmp_path: Path) -> None:
    """10:00 UTC on 6 April is 11:00 in Lisbon (summer time)."""
    assert datetime(2026, 4, 6, 10, 0, tzinfo=UTC) == START
    with practising_in(tmp_path, LISBON) as practice:
        finished_task(practice)
        practice.clock.advance(timedelta(minutes=5))

        ((_item, time),) = panel_times(practice)

        assert stamp(time) == ("2026-04-06T11:00:00+01:00", "6 April 2026, 11:00")
        assert time.text == "5 minutes ago"
        assert_exact_time(time, START)  # the same instant


def test_exact_time_on_the_day_the_clocks_go_back(tmp_path: Path) -> None:
    """Lisbon, 25 October 2026: 01:30 comes twice, an hour apart, with two offsets."""
    first = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    with practising_in(tmp_path, LISBON, now=first) as practice:
        before = finished_task(practice)
        practice.clock.advance(timedelta(hours=1))
        after = finished_task(practice)

        stamps = {
            item.attrs["id"]: (stamp(time), time.text) for item, time in panel_times(practice)
        }

    assert stamps == {
        f"task-{before}": (("2026-10-25T01:30:00+01:00", "25 October 2026, 01:30"), "1 hour ago"),
        f"task-{after}": (("2026-10-25T01:30:00+00:00", "25 October 2026, 01:30"), "just now"),
    }


def test_a_running_task_says_started_in_hidden_words(tmp_path: Path) -> None:
    with practising_in(tmp_path, ZoneInfo("UTC")) as practice:
        finished_task(practice)
        practice.gate.hold = True
        running = started(practice, duration="slow")
        practice.wait_entered(1)
        practice.clock.advance(timedelta(minutes=2))

        elements = panel_elements(practice)
        texts = {}
        for wrapper in time_wrappers(elements):
            (time,) = [e for e in elements if e.tag == "time" and e.within(wrapper)]
            hidden = [
                e.text
                for e in elements
                if e.within(wrapper) and HIDDEN_CLASS in (e.attrs.get("class") or "").split()
            ]
            assert RELATIVE.fullmatch(time.text), "the visible words keep their pattern"
            texts[time.attrs["id"]] = (wrapper.text, hidden)

    exact = "(6 April 2026, 10:00)"
    assert texts[f"task-{running}-time"] == (
        f"started 2 minutes ago {exact}",
        ["started", exact],
    )
    (other,) = [value for key, value in texts.items() if key != f"task-{running}-time"]
    assert other == (f"2 minutes ago {exact}", [exact]), "a finished task did not just start"


def test_an_idle_panel_a_minute_later_differs_in_the_relative_words_only(tmp_path: Path) -> None:
    """What the open panel's 60 s refresh gets: the same rows, ids and exact times, with the
    words moved on."""

    def panel_html(practice: Practice) -> str:
        response = practice.run.client.get(PANEL, headers=practice.run.htmx())
        assert response.status_code == 200, response.status_code
        return response.text

    def times(html: str) -> list[Element]:
        return [e for e in outline(html) if e.tag == "time"]

    with practising(tmp_path) as practice:
        finished_task(practice)
        finished_task(practice, outcome="network")
        first = panel_html(practice)
        practice.clock.advance(timedelta(seconds=60))
        second = panel_html(practice)

    assert [t.text for t in times(first)] == ["just now", "just now"]
    assert [t.text for t in times(second)] == ["1 minute ago", "1 minute ago"]
    assert [t.attrs for t in times(first)] == [t.attrs for t in times(second)]
    assert first.count(">just now</time>") == 2 and second.count(">1 minute ago</time>") == 2
    assert first.replace(">just now</time>", ">1 minute ago</time>") == second, (
        "nothing else in the panel changed"
    )
