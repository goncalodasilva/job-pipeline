"""Killed assessment calls keep the paid summary and recover without an automatic retry."""

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


def test_kill_during_fit_preserves_summary_and_does_not_retry(tmp_path: Path):
    finished_guide(tmp_path)
    ready = tmp_path / "FIT_READY"
    with (tmp_path / "fit-child.txt").open("wb") as output:
        child = subprocess.Popen(  # noqa: S603 - fixed test module, this interpreter
            [sys.executable, "-m", "tests.job_fit_child", str(tmp_path)],
            cwd=PROJECT_ROOT,
            env=isolated_env(tmp_path / "data", tmp_path / "cfg"),
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            wait_until(
                lambda: ready.exists() or child.poll() is not None,
                timeout=40,
                message="fit call in flight",
            )
            assert child.poll() is None and ready.exists()
            task_id = ready.read_text()
        finally:
            child.kill()
            child.wait(timeout=40)
    assert _read(tmp_path / "data", "SELECT count(*) FROM job_summaries") == [(1,)]
    assert _read(tmp_path / "data", "SELECT count(*) FROM job_assessments") == [(0,)]
    with importing(tmp_path, seed=False) as run:
        task = run.app.state.task_service.get(task_id)
        assert task.status.value == "interrupted" and task.attempt == 1
        assert run.spy.requests == []
