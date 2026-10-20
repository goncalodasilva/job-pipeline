"""A killed summary preserves the intake snapshot and is never retried automatically."""

import subprocess
import sys
from pathlib import Path

import pytest

from tests.cv_import_harness import importing
from tests.integration.failures.test_cv_import_failures import _read
from tests.integration.test_setup_reopen_banner import finished_guide
from tests.running_app import PROJECT_ROOT, isolated_env
from tests.waiting import wait_until

pytestmark = pytest.mark.integration


def test_kill_during_summarize_leaves_job_triageable(tmp_path: Path):
    finished_guide(tmp_path)
    ready = tmp_path / "SUMMARY_READY"
    with (tmp_path / "summary-child.txt").open("wb") as output:
        child = subprocess.Popen(  # noqa: S603 - this interpreter and a fixed test module
            [sys.executable, "-m", "tests.job_summary_child", str(tmp_path)],
            cwd=PROJECT_ROOT,
            env=isolated_env(tmp_path / "data", tmp_path / "cfg"),
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            wait_until(
                lambda: ready.exists() or child.poll() is not None,
                timeout=40,
                message="summary call in flight",
            )
            assert child.poll() is None and ready.exists()
            task_id = ready.read_text()
        finally:
            child.kill()
            child.wait(timeout=40)
    assert _read(tmp_path / "data", "SELECT triage_state FROM jobs") == [("new",)]
    assert _read(tmp_path / "data", "SELECT count(*) FROM job_summaries") == [(0,)]
    with importing(tmp_path, seed=False) as run:
        task = run.app.state.task_service.get(task_id)
        assert task.status.value == "interrupted" and task.attempt == 1
        assert run.spy.requests == []
        job_id = run.rows("SELECT id FROM jobs")[0]["id"]
        assert not run.app.state.job_summary.view(job_id).active
