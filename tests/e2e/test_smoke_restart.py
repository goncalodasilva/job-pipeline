"""Smoke: kill the real app with SIGKILL, then restart it on the same folders (M2.3).

Groundwork for the crash-recovery and data-folder-lock tests (M2.4, M2.9): ``RunningApp.kill()``
sends SIGKILL (POSIX only; Windows work is suspended, ADR-061) and records the result as
``stop()`` does; ``start_app(..., restart=True)`` starts the app again on the same data and
config folders without writing anything to them first, so nothing is re-seeded.

Every value here is fictional and the folders are temporary.
"""

import signal
import sqlite3
from pathlib import Path

import pytest

from tests.running_app import RunningApp, start_app

pytestmark = pytest.mark.smoke

SENTINEL_NAME = "fictional-sentinel.txt"
SENTINEL_TEXT = "written before the kill"


def _settings_rows(data_dir: Path) -> list[tuple[str, str, int]]:
    # Read-only, so this check never changes the folder it inspects.
    uri = f"{(data_dir / 'app.db').as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        rows = conn.execute("SELECT key, updated_at, version FROM settings ORDER BY key")
        return [(str(key), str(updated), int(version)) for key, updated, version in rows]


def _assert_healthy(app: RunningApp) -> None:
    with app.client() as client:
        health = client.get("/health")
        page = client.get("/")
    assert health.status_code == 200, app.output_tail()
    assert page.status_code == 200, "a completed setup must still open the app's pages"


def test_running_app_kill_then_restart_same_folders_healthy(tmp_path: Path) -> None:
    first = start_app(tmp_path)
    try:
        _assert_healthy(first)
        (first.data_dir / SENTINEL_NAME).write_text(SENTINEL_TEXT, encoding="utf-8")
        killed = first.kill()
    finally:
        first.stop()  # never leave the app running; after kill() it only returns the result

    assert killed.returncode == -signal.SIGKILL, first.output_tail()
    assert killed.killed is True
    assert first.process.poll() == -signal.SIGKILL
    assert first.kill() == killed, "kill() is safe to call twice"
    assert first.stop() == killed, "stop() after kill() returns the recorded result"

    database = first.data_dir / "app.db"
    assert database.is_file()
    settings_before = _settings_rows(first.data_dir)
    assert settings_before, "the completed setup saved settings"

    second = start_app(tmp_path, restart=True, output_name="after-kill")
    try:
        assert second.data_dir == first.data_dir
        assert second.config_dir == first.config_dir
        assert second.port != 0
        _assert_healthy(second)
        assert (second.data_dir / SENTINEL_NAME).read_text(encoding="utf-8") == SENTINEL_TEXT
        assert _settings_rows(second.data_dir) == settings_before, "restart must not re-seed"
    finally:
        stopped = second.stop()

    assert stopped.killed is False, second.output_tail()
    assert stopped.returncode == 0, second.output_tail()
    assert first.stdout_path != second.stdout_path
    assert first.stdout_path.exists(), "the first run's output is kept"


def test_restart_refuses_folders_that_do_not_exist(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="restart"):
        start_app(tmp_path, restart=True)
