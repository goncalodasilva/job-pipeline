"""Refine-specific rollback, cancellation, watchdog and real-kill recovery checks (M3.8)."""

import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from app.main import REFINE_TIMEOUT
from tests.integration.failures.test_cv_import_failures import _read
from tests.integration.test_setup_reopen_banner import finished_guide
from tests.refine_harness import ACCEPTED, refining
from tests.running_app import PROJECT_ROOT, isolated_env
from tests.waiting import wait_until

pytestmark = pytest.mark.integration


def test_result_write_and_success_are_one_transaction(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        before = r.fact_tables()
        r.answer_with("ok")
        r.run.execute(
            "CREATE TRIGGER fictional_refine_fail BEFORE UPDATE OF status ON tasks "
            "WHEN NEW.type = 'refine_fact' AND NEW.status = 'succeeded' "
            "BEGIN SELECT RAISE(ABORT, 'fictional failure'); END"
        )

        task = r.started(fact.id, status="failed")

        assert task.error == "TaskFailed" and task.result_ref is None
        session = r.open_session(fact.id)
        assert session["turns"] == "[]" and session["suggestion"] is None
        assert session["provider"] is None and session["task_id"] is None
        assert r.fact_tables() == before and len(r.requests()) == 1
        r.run.execute("DROP TRIGGER fictional_refine_fail")
        retried = r.post(f"/tasks/{task.id}/retry")
        assert retried.status_code == 200
        done = r.run.wait_terminal(task.id)
        assert done.status.value == "succeeded" and done.attempt == 2
        assert r.open_session(fact.id)["task_id"] == task.id
        assert r.fact_tables() == before


@pytest.mark.parametrize("outcome", ["cancelled", "interrupted"])
def test_held_refine_call_leaves_no_suggestion_after_cancel_or_watchdog(
    tmp_path: Path, outcome: str
) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        before = r.fact_tables()
        r.answer_with("ok")
        r.run.hold.hold = True
        assert r.start(fact.id).status_code in ACCEPTED
        r.run.wait_entered(1)
        task = r.tasks()[0]
        if outcome == "cancelled":
            assert r.post(f"/tasks/{task.id}/cancel").status_code == 200
        else:
            r.run.clock.advance(REFINE_TIMEOUT + timedelta(seconds=1))
            assert r.client.portal.call(r.run.app.state.runner.check_watchdog_once) == [task.id]
        r.run.hold.open()

        done = r.run.wait_terminal(task.id)

        assert done.status.value == outcome and done.result_ref is None
        assert r.open_session(fact.id)["suggestion"] is None
        assert r.fact_tables() == before


def test_killed_refine_recovers_interrupted_and_waits_for_explicit_retry(tmp_path: Path) -> None:
    finished_guide(tmp_path)
    ready = tmp_path / "REFINE_READY"
    with (tmp_path / "refine-child.txt").open("wb") as output:
        child = subprocess.Popen(  # noqa: S603 - this interpreter and a fixed test module
            [sys.executable, "-m", "tests.refine_child", str(tmp_path)],
            cwd=PROJECT_ROOT,
            env=isolated_env(tmp_path / "data", tmp_path / "cfg"),
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            wait_until(
                lambda: ready.exists() or child.poll() is not None,
                timeout=40,
                message="the refine provider call is in flight",
            )
            assert child.poll() is None and ready.exists()
            task_id = ready.read_text(encoding="utf-8")
            assert _read(tmp_path / "data", "SELECT status FROM tasks") == [("running",)]
        finally:
            child.kill()
            child.wait(timeout=40)

    assert _read(tmp_path / "data", "SELECT suggestion FROM refine_sessions") == [(None,)]
    with refining(tmp_path, seed=False) as r:
        task = r.run.app.state.task_service.get(task_id)
        assert task.status.value == "interrupted" and task.attempt == 1
        assert r.requests() == []
        (fact,) = r.run.rows("SELECT id FROM facts")
        before = r.fact_tables()
        r.answer_with("ok")
        assert r.post(f"/tasks/{task_id}/retry").status_code == 200
        done = r.run.wait_terminal(task_id)
        assert done.status.value == "succeeded" and done.attempt == 2
        assert r.open_session(fact["id"])["suggestion"] is not None
        assert r.fact_tables() == before
