"""Provider failures end a task as failed, with the mapped reason, and stay counted (M2.21;
spec 5 M2 reliability rules 1 and 7, spec 2.11; ADR-076 items 2 and 3).

The practice task (M2.17) is the vehicle, with its fake-provider outcomes: ``network`` fails
the first call, and ``invalid_output`` returns text that isn't the requested JSON for the first
call and for its one retry. The app runs in this process (TestClient, the app's own wiring
except that its fake provider is the test's recording one), on real SQLite under ``tmp_path``.
Nothing sleeps: every wait is a condition with a deadline. Every value is fictional.

What each test pins, beyond the M2.17 outcome mapping: the task ends ``failed`` with the error
kind (never its text) stored, the fragment and the panel show the spec 2.11 copy for that kind
with a Retry button, and the usage records show every provider call that was made: counted on
the ``fake`` service, finished as failed (none left reserved), linked to the task.
"""

from pathlib import Path
from typing import NamedTuple

import pytest

from app.domain.errors import InvalidModelOutput, NetworkUnavailable
from app.services.practice import PRACTICE
from tests.integration.test_practice_task import FAKE_SERVICE, Practice, practising, started
from tests.integration.test_task_routes import Item

pytestmark = pytest.mark.integration

TIER = "fast"


class UsageRow(NamedTuple):
    task_id: str | None
    service: str | None
    tier: str | None
    task_type: str
    success: int | None


def usage_rows(practice: Practice) -> list[UsageRow]:
    db = practice.app.state.workspace.db
    with db.read() as conn:
        rows = conn.execute(
            "SELECT task_id, service, tier, task_type, success FROM usage_records ORDER BY rowid"
        ).fetchall()
    return [UsageRow(*row) for row in rows]


def shown(practice: Practice, task_id: str) -> list[Item]:
    """The task as the user sees it: its own fragment and its item in the tasks panel."""
    headers = practice.run.with_token()
    fragment = practice.run.client.get(f"/tasks/{task_id}", headers=headers)
    panel = practice.run.client.get("/tasks/panel", headers=headers)
    assert fragment.status_code == 200 and panel.status_code == 200
    return [Item(fragment.text, task_id), Item(panel.text, task_id)]


def assert_failed_with(practice: Practice, task_id: str, kind: str, message: str) -> None:
    row = practice.row(task_id)
    assert str(row.status.value) == "failed"
    assert row.error == kind, "the error's kind is stored, never its text"
    assert row.result_ref is None, "a failed run saves no result"
    assert row.attempt == 1, "failing is not retrying"
    assert practice.app.state.task_service.active_counts() == (0, 0), "nothing is left active"
    for item in shown(practice, task_id):
        assert item.text("status") == "Failed"
        assert item.text("reason") == message, "the mapped copy for that kind"
        assert item.actions == {"retry"}, "the user can try again"
        assert item.part("progress") is None, "a failed task is not shown as running"
        assert item.part("result") is None
        assert kind not in item.html, "the stored kind is never shown"


def test_network_failure_ends_failed_with_mapped_reason_and_counted(tmp_path: Path) -> None:
    with practising(tmp_path) as practice:
        task_id = started(practice, outcome="network", tier=TIER)
        practice.wait_terminal(task_id)

        assert_failed_with(practice, task_id, NetworkUnavailable.kind, NetworkUnavailable.message)

        assert len(practice.fake.requests) == 1, "the first call failed and nothing went on"
        assert usage_rows(practice) == [UsageRow(task_id, FAKE_SERVICE, TIER, PRACTICE, 0)], (
            "the failed call is counted, finished as failed and linked to its task"
        )
        status = practice.app.state.usage.status(FAKE_SERVICE)
        assert (status.local_count, status.unfinished_count) == (1, 0)


def test_invalid_output_twice_ends_failed_counting_two_requests(tmp_path: Path) -> None:
    with practising(tmp_path) as practice:
        task_id = started(practice, outcome="invalid_output", tier=TIER)
        practice.wait_terminal(task_id)

        assert_failed_with(practice, task_id, InvalidModelOutput.kind, InvalidModelOutput.message)

        # The first call and its one retry, both answered with text that isn't the JSON asked
        # for; the run gave up there (the script holds a good answer for a third call, which
        # was never asked for).
        assert len(practice.fake.requests) == 2
        assert usage_rows(practice) == [
            UsageRow(task_id, FAKE_SERVICE, TIER, PRACTICE, 0),
            UsageRow(task_id, FAKE_SERVICE, TIER, PRACTICE, 0),
        ], "two requests counted, both finished as failed, none left reserved"
        status = practice.app.state.usage.status(FAKE_SERVICE)
        assert (status.local_count, status.unfinished_count) == (2, 0)
        breakdown = practice.app.state.usage.breakdown(FAKE_SERVICE)
        assert [(row.tier, row.task_type, row.requests, row.unfinished) for row in breakdown] == [
            (TIER, PRACTICE, 2, 0)
        ]
