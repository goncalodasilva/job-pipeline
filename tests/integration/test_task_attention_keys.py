"""The attention keys the server lists (M2.26 review): which tasks the top bar's "needs
attention" count can be about.

A key is ``<task id>:<attempt>``. It is listed for a task that ended **failed** or
**interrupted**, and for no other state (queued, running, succeeded and cancelled never), in
three places that must agree:

- ``GET /tasks/indicator``: ``data-attention`` of ``#tasks-count``, keys separated by spaces
  (the attribute is absent when there are none);
- the panel rows (``GET /tasks/panel``) and the single task (``GET /tasks/<id>``):
  ``data-attention`` of the task's ``.task-item``;
- a rendered page: ``#tasks-count`` in the top bar, the same as the indicator.

A retry that ends failed again is a new key (``:2``) and the old one is gone. Keys hold ids
and attempts only.

The practice task (M2.17) is the vehicle; the interrupted task is one a killed process left
running, recovered when the app starts. Real SQLite under ``tmp_path``; ``FrozenClock``; no
sleeps; every value is fictional.
"""

import re
from pathlib import Path

import pytest

from tests.integration.test_ac_m1_4_task_leaks import seed_running_task
from tests.integration.test_practice_task import SETTINGS, Practice, practising, started
from tests.integration.test_setup_redirect import Element, outline

pytestmark = pytest.mark.integration

PANEL = "/tasks/panel"
INDICATOR = "/tasks/indicator"
COUNT_ID = "tasks-count"
ITEM_CLASS = "task-item"
ATTRIBUTE = "data-attention"
KEY = re.compile(r"[A-Za-z0-9_-]+:[1-9][0-9]*")


def keys_of(element: Element) -> list[str]:
    assert element.attrs.get(ATTRIBUTE) != "", "no keys: the attribute is left out, not empty"
    keys = (element.attrs.get(ATTRIBUTE) or "").split()
    assert all(KEY.fullmatch(key) for key in keys), keys
    return keys


def count_keys(page: str) -> list[str]:
    (count,) = [e for e in outline(page) if e.attrs.get("id") == COUNT_ID]
    return keys_of(count)


def row_keys(fragment: str) -> dict[str, list[str]]:
    """Each task item's keys, by task id."""
    found = {}
    for element in outline(fragment):
        if ITEM_CLASS in (element.attrs.get("class") or "").split():
            found[(element.attrs["id"] or "").removeprefix("task-")] = keys_of(element)
    return found


def everywhere(practice: Practice) -> tuple[list[str], dict[str, list[str]]]:
    """The keys the indicator lists and each panel row's; the rendered page and every single
    task fragment must say the same."""
    client, htmx = practice.run.client, practice.run.htmx()
    indicator = client.get(INDICATOR, headers=htmx)
    panel = client.get(PANEL, headers=htmx)
    page = client.get(SETTINGS)
    assert (indicator.status_code, panel.status_code, page.status_code) == (200, 200, 200)
    listed, rows = count_keys(indicator.text), row_keys(panel.text)
    assert count_keys(page.text) == listed, "the rendered page lists what the indicator lists"
    for task_id, keys in rows.items():
        single = client.get(f"/tasks/{task_id}", headers=htmx)
        assert row_keys(single.text) == {task_id: keys}, "the single task says the same"
    assert sorted(listed) == sorted(key for keys in rows.values() for key in keys), (
        "the indicator lists exactly the keys the panel rows carry"
    )
    return listed, rows


def test_no_tasks_no_keys(tmp_path: Path) -> None:
    with practising(tmp_path) as practice:
        assert everywhere(practice) == ([], {})


def test_succeeded_queued_running_and_cancelled_tasks_never_have_a_key(tmp_path: Path) -> None:
    with practising(tmp_path) as practice:
        done = started(practice)
        assert practice.wait_terminal(done).status.value == "succeeded"
        assert everywhere(practice) == ([], {done: []})

        practice.gate.hold = True
        running = started(practice, duration="slow")
        practice.wait_entered(1)
        assert practice.status(running) == "running"
        assert everywhere(practice) == ([], {done: [], running: []})

        cancel = practice.run.client.post(f"/tasks/{running}/cancel", headers=practice.run.action())
        assert cancel.status_code == 200, cancel.status_code
        assert everywhere(practice)[0] == [], "cancelling"
        practice.gate.open()
        assert practice.wait_terminal(running).status.value == "cancelled"

        assert everywhere(practice) == ([], {done: [], running: []})


def test_a_failed_task_has_a_key_and_a_retry_that_fails_again_a_new_one(tmp_path: Path) -> None:
    with practising(tmp_path) as practice:
        failed = started(practice, outcome="network", duration="slow")  # a call the gate sees
        assert practice.wait_terminal(failed).status.value == "failed"
        assert everywhere(practice) == ([f"{failed}:1"], {failed: [f"{failed}:1"]})
        assert practice.gate.entered == 1

        # While the retry is queued or running nothing needs attention: the old key is gone.
        practice.gate.hold = True
        retried = practice.run.client.post(f"/tasks/{failed}/retry", headers=practice.run.action())
        assert retried.status_code == 200, retried.status_code
        practice.wait_entered(2)
        assert practice.row(failed).attempt == 2
        assert everywhere(practice) == ([], {failed: []})

        practice.gate.open()
        assert practice.wait_terminal(failed).status.value == "failed"
        assert everywhere(practice) == ([f"{failed}:2"], {failed: [f"{failed}:2"]})


def test_an_interrupted_task_has_a_key_and_the_keys_are_listed_together(tmp_path: Path) -> None:
    # A task a killed process left running: the app interrupts it when it starts.
    with practising(tmp_path):
        pass  # the folders exist, set up
    crashed = seed_running_task(tmp_path, "fictional-target", "fictional-input")

    with practising(tmp_path) as practice:
        assert practice.status(crashed) == "interrupted"
        assert everywhere(practice) == ([f"{crashed}:1"], {crashed: [f"{crashed}:1"]})

        failed = started(practice, outcome="invalid_output")
        assert practice.wait_terminal(failed).status.value == "failed"
        done = started(practice)
        practice.wait_terminal(done)

        listed, rows = everywhere(practice)
        assert sorted(listed) == sorted([f"{crashed}:1", f"{failed}:1"])
        assert rows == {crashed: [f"{crashed}:1"], failed: [f"{failed}:1"], done: []}
        assert "fictional-target" not in " ".join(listed), "a key is an id and an attempt"


def test_a_page_the_indicator_and_the_panel_each_read_the_tasks_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One snapshot per answer (M2.26 review): a rendered page and the indicator get the
    count and the keys from one read (``overview``), the panel its rows from one."""
    with practising(tmp_path) as practice:
        failed = started(practice, outcome="network")
        practice.wait_terminal(failed)
        service = practice.app.state.task_service
        reads: list[str] = []

        def counted(name: str) -> None:
            real = getattr(service, name)

            def read(*args: object, **kwargs: object) -> object:
                reads.append(name)
                return real(*args, **kwargs)

            monkeypatch.setattr(service, name, read)

        for name in ("overview", "recent", "active_counts", "get"):
            counted(name)
        client, htmx = practice.run.client, practice.run.htmx()

        page = client.get(SETTINGS)
        assert count_keys(page.text) == [f"{failed}:1"]
        assert reads == ["overview"], "a page reads the tasks once, for its top bar"

        reads.clear()
        assert count_keys(client.get(INDICATOR, headers=htmx).text) == [f"{failed}:1"]
        assert reads == ["overview"]

        reads.clear()
        assert row_keys(client.get(PANEL, headers=htmx).text) == {failed: [f"{failed}:1"]}
        assert reads == ["recent"]
