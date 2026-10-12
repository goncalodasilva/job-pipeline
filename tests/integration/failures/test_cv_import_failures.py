"""Failure injection for the CV import task (M3.4; AC-M3.7 "no partial records"; spec 5 M2
reliability rules 3 and 4; background-task skill section 8).

- A process kill while the provider call is in flight (following ``test_process_kill.py``):
  the app runs in a child process (``tests/cv_import_child.py``) whose provider call never
  returns, and the test SIGKILLs it. What the crash left is read from the data folder through
  a read-only connection; the app is then started again on the same folders, in this process.
- A failure between the result write and the success update: a test-created trigger aborts
  the running -> succeeded update, after the drafts were written in the same transaction
  (as in ``test_atomic_result.py``; no production flag).

Folders under ``tmp_path``, the fake provider, no network; every wait has a deadline.
"""

import logging
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from app.config import DATABASE_FILE
from tests.cv_fixtures import fixture_path
from tests.cv_import_child import FAILED, READY
from tests.cv_import_harness import ACCEPTED, EXTRACT_CV, FACT_TABLES, dedupe_target, importing
from tests.integration.test_setup_reopen_banner import finished_guide
from tests.running_app import PROJECT_ROOT, isolated_env
from tests.waiting import wait_until

CV_TEXT = fixture_path("cv.txt").read_text(encoding="utf-8")
READY_DEADLINE_S = 40.0  # a new interpreter, the app's start and one parse in a child process
READ_TIMEOUT_S = 5.0


def _read(folder: Path, sql: str) -> list[tuple[object, ...]]:
    """One read through a read-only connection: it can't change the folder it inspects."""
    uri = f"{(folder / DATABASE_FILE).as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=READ_TIMEOUT_S)) as conn:
        return list(conn.execute(sql).fetchall())


def _fact_counts(folder: Path) -> dict[str, int]:
    return {
        table: int(_read(folder, f"SELECT count(*) FROM {table}")[0][0])  # type: ignore[call-overload]  # noqa: S608
        for table in FACT_TABLES
    }


def _crash_during_import(tmp_path: Path) -> str:
    """Start an import in a child process, kill the process while the provider call is in
    flight and return the task's id."""
    finished_guide(tmp_path)
    output = tmp_path / "child-output.txt"
    with output.open("wb") as stream:
        child = subprocess.Popen(  # noqa: S603 - this interpreter, a fixed module, a temp folder
            [sys.executable, "-m", "tests.cv_import_child", str(tmp_path)],
            cwd=PROJECT_ROOT,
            env=isolated_env(tmp_path / "data", tmp_path / "cfg"),
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
        try:

            def ready() -> bool:
                return (tmp_path / READY).exists() or child.poll() is not None

            wait_until(ready, timeout=READY_DEADLINE_S, message="the import's call is in flight")
            failed = tmp_path / FAILED
            reason = failed.read_text(encoding="utf-8") if failed.exists() else ""
            assert child.poll() is None and (tmp_path / READY).exists(), (
                f"the import never got to its provider call: {reason} "
                f"{output.read_text(encoding='utf-8', errors='replace')[-2000:]}"
            )
            task_id = (tmp_path / READY).read_text(encoding="utf-8")
            folder = tmp_path / "data"
            running = _read(folder, "SELECT id, type, status FROM tasks")
            assert running == [(task_id, EXTRACT_CV, "running")], "the kill finds it mid-flight"
        finally:
            child.kill()
            child.wait(timeout=READY_DEADLINE_S)
    return task_id


def test_kill_during_import_leaves_no_partial_drafts(tmp_path: Path) -> None:
    task_id = _crash_during_import(tmp_path)
    folder = tmp_path / "data"

    # What the kill left, with no app running: the task still says running, the upload and
    # its file are there, and no fact, role or skill was written.
    assert _read(folder, "SELECT status, result_ref FROM tasks") == [("running", None)]
    ((import_id, state, stored_name),) = _read(
        folder, "SELECT id, state, stored_name FROM cv_imports"
    )
    assert state == "uploaded"
    assert (folder / "imports" / str(stored_name)).read_bytes() == CV_TEXT.encode("utf-8")
    assert _fact_counts(folder) == dict.fromkeys(FACT_TABLES, 0)

    with importing(tmp_path, seed=False) as run:
        # Recovery: interrupted, never retried by itself, and still nothing written.
        row = run.app.state.task_service.get(task_id)
        assert str(row.status.value) == "interrupted"
        assert (row.attempt, row.result_ref, row.error) == (1, None, None)
        run.assert_no_fact_rows()
        run.assert_upload_kept(str(import_id))
        assert run.spy.requests == [], "no provider call after the restart"

        # The user's Retry makes the drafts, once.
        again = run.client.post(f"/tasks/{task_id}/retry", headers=run.run.action())
        assert again.status_code == 200, again.status_code
        done = run.wait_terminal(task_id)
        assert str(done.status.value) == "succeeded", done.error
        assert done.attempt == 2
        assert len(run.facts()) == 4 and len(run.roles()) == 2
        assert {draft["status"] for draft in run.facts()} == {"draft"}
        stored = run.only_import()
        assert (stored["state"], stored["task_id"]) == ("done", task_id)
        assert len(run.spy.requests) == 1


def test_failure_between_result_write_and_success_leaves_no_drafts(tmp_path: Path) -> None:
    with importing(tmp_path) as run:
        run.execute(
            "CREATE TRIGGER fictional_fail_success BEFORE UPDATE OF status ON tasks "
            "WHEN NEW.status = 'succeeded' "
            "BEGIN SELECT RAISE(ABORT, 'fictional injected failure'); END"
        )

        response = run.paste(CV_TEXT)

        assert response.status_code in ACCEPTED, response.status_code
        task = run.wait_terminal(run.only_task().id)
        assert str(task.status.value) == "failed"
        assert task.error == "TaskFailed", "a kind, never the error's text"
        assert task.result_ref is None
        assert len(run.spy.requests) == 1, "the call was made: the failure came after it"
        run.assert_no_fact_rows()  # the drafts written in that transaction went with it
        import_id = run.assert_upload_kept(run.only_import()["id"])["id"]

        # With the fault gone, the same task's retry makes the drafts once.
        run.execute("DROP TRIGGER fictional_fail_success")
        again = run.client.post(f"/tasks/{task.id}/retry", headers=run.run.action())
        assert again.status_code == 200, again.status_code
        done = run.wait_terminal(task.id)
        assert str(done.status.value) == "succeeded", done.error
        assert done.dedupe_key == f"{EXTRACT_CV}:{dedupe_target(import_id)}"
        assert len(run.facts()) == 4 and len(run.roles()) == 2
        assert run.only_import()["state"] == "done"


def test_orphan_file_of_a_kill_before_the_row_insert_is_swept_at_restart(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A kill between the file write and the row insert leaves a stored CV no row names
    (simulated: the file is put there while no app runs). The next start removes it and
    nothing else."""
    folder = tmp_path / "data"
    imports = folder / "imports"
    with importing(tmp_path) as run:
        assert not imports.exists(), "a start with no imports folder sweeps nothing, makes none"
        run.spy.fail("network")
        run.finished(run.paste(CV_TEXT), status="failed")
        kept_name = str(run.only_import()["stored_name"])

    outside = tmp_path / "fictional-outside.txt"
    outside.write_text("fictional outside", encoding="utf-8")
    orphans = [imports / "fictional-orphan-1.txt", imports / "0a1b2c3d_orphan.pdf"]
    bystanders = [
        imports / "fictional notes.txt",  # not a generated name
        imports / ".fictional-hidden.txt",
        imports / "fictional-orphan.txt.tmp",
        imports / "fictional-orphan.md",
        folder / "fictional-orphan-2.txt",  # a generated name, outside the folder
    ]
    for path in (*orphans, *bystanders):
        path.write_text("fictional", encoding="utf-8")
    nested = imports / "fictional-folder.txt"  # a folder with a generated name
    nested.mkdir()
    (nested / "fictional-inner-1.txt").write_text("fictional", encoding="utf-8")
    link = imports / "fictional-link-1.txt"
    link.symlink_to(outside)

    caplog.set_level(logging.DEBUG)
    with importing(tmp_path, seed=False) as run:
        for path in orphans:
            assert not path.exists(), f"{path.name} is named by no row"
        for path in bystanders:
            assert path.read_text(encoding="utf-8") == "fictional", path.name
        assert (nested / "fictional-inner-1.txt").is_file()
        assert link.is_symlink() and outside.read_text(encoding="utf-8") == "fictional outside"
        # The upload with a row is as it was, and can still be started.
        stored = run.only_import()
        assert stored["stored_name"] == kept_name
        assert (imports / kept_name).read_bytes() == CV_TEXT.encode("utf-8")
        assert run.start(str(stored["id"])).status_code in ACCEPTED

    logged = "\n".join(f"{record.getMessage()} {record.args!r}" for record in caplog.records)
    assert "2 stored CV file(s) no import names" in logged, "the count is logged"
    assert "orphan" not in logged and kept_name not in logged, "never a file name"


def test_restart_with_nothing_orphaned_or_a_linked_imports_folder_removes_nothing(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "data"
    with importing(tmp_path) as run:
        run.spy.fail("network")
        run.finished(run.paste(CV_TEXT), status="failed")
        kept_name = str(run.only_import()["stored_name"])
    with importing(tmp_path, seed=False) as run:
        assert [path.name for path in run.stored_files()] == [kept_name]

    # The imports folder replaced by a link to another folder: nothing in it is touched.
    elsewhere = tmp_path / "fictional-elsewhere"
    (folder / "imports").rename(elsewhere)
    (elsewhere / "fictional-orphan-1.txt").write_text("fictional", encoding="utf-8")
    (folder / "imports").symlink_to(elsewhere, target_is_directory=True)
    with importing(tmp_path, seed=False):
        assert sorted(path.name for path in elsewhere.iterdir()) == sorted(
            [kept_name, "fictional-orphan-1.txt"]
        )
