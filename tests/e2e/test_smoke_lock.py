"""Smoke: two copies of the real app on one data folder (M2.4; ADR-072, ADR-076 item 9).

The first copy holds ``<data>/.lock``. A second copy started on the same data and config
folders must exit with code 1 (ADR-040) after one plain ``jobpipeline:`` line on stderr that
names no folder and shows no traceback, while the first copy keeps serving. When the first
copy is killed with SIGKILL the OS releases the lock, so a restart on the same folders starts
healthy and holds the lock itself. POSIX only (ADR-061). Every value here is fictional and the
folders are temporary.
"""

import fcntl
import os
import re
from pathlib import Path

import pytest

from tests.running_app import AppDidNotStart, RunningApp, start_app

pytestmark = pytest.mark.smoke

LOCK_FILE = ".lock"
SECOND_OUTPUT = "second"
IN_USE_TEXT = "already open in another copy of the app"
EXITED = re.compile(r"the app exited with code (-?\d+) before /health answered")


def _assert_healthy(app: RunningApp) -> None:
    with app.client() as client:
        health = client.get("/health")
    assert health.status_code == 200, app.output_tail()
    assert health.json()["database"] == "ok"


def _lock_is_free(path: Path) -> bool:
    fd = os.open(path, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def test_second_instance_same_data_dir_exits_with_plain_message(tmp_path: Path) -> None:
    first = start_app(tmp_path)
    try:
        _assert_healthy(first)
        try:
            second = start_app(tmp_path, restart=True, output_name=SECOND_OUTPUT)
        except AppDidNotStart as error:
            refusal = str(error)
        else:
            second.stop()
            pytest.fail("a second copy on the same data folder must not start")

        _assert_healthy(first)  # the first copy keeps serving
        first_stopped = first.stop()
    finally:
        first.stop()

    exited = EXITED.search(refusal)
    assert exited is not None, refusal
    assert int(exited.group(1)) == 1, refusal  # start-up failure (ADR-040)
    stderr = (tmp_path / f"{SECOND_OUTPUT}-stderr.txt").read_text(encoding="utf-8")
    plain = [line for line in stderr.splitlines() if line.startswith("jobpipeline:")]
    assert len(plain) == 1, stderr
    assert IN_USE_TEXT in plain[0], stderr
    assert "Traceback" not in stderr
    for leak in (str(tmp_path), str(tmp_path.resolve()), str(first.data_dir)):
        assert leak not in stderr, "the console names no folder"
    assert first_stopped.returncode == 0, first.output_tail()


def test_lock_released_when_first_process_killed(tmp_path: Path) -> None:
    first = start_app(tmp_path)
    try:
        _assert_healthy(first)
        lock = first.data_dir / LOCK_FILE
        assert lock.is_file(), "the running app keeps <data>/.lock"
        assert not _lock_is_free(lock), "the running app holds the lock"
        first.kill()
    finally:
        first.stop()

    assert _lock_is_free(lock), "the OS releases the lock of a killed process"
    second = start_app(tmp_path, restart=True, output_name="after-kill")
    try:
        _assert_healthy(second)
        assert not _lock_is_free(lock), "the restarted app holds the lock"
    finally:
        stopped = second.stop()

    assert stopped.returncode == 0, second.output_tail()
