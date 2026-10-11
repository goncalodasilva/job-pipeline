"""Hardening of the data-folder lock file (M2.4 security review; ADR-072; POSIX only, ADR-061).

``lock_data_folder`` only locks a private regular file: ``<data>/.lock`` must be a regular
file the user owns with a single link. A symlink, a directory, a hard link or a FIFO is
refused with ``DataFolderUnavailable`` naming no path, without following, blocking or changing
anything. A leftover ``.lock`` with a wider mode becomes 0600, and the lock's file descriptor
is never inherited by child processes. Every folder lives under ``tmp_path``.
"""

import errno
import os
import stat
from pathlib import Path

import pytest

from app.domain.errors import DataFolderUnavailable
from app.modules.storage import LOCK_FILE, lock_data_folder

FOLDER_NAME = "fictional-heron-workspace-2291"


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    path = tmp_path / FOLDER_NAME
    path.mkdir(mode=0o700)
    return path


def _assert_no_path(error: BaseException, folder: Path) -> None:
    for text in (str(error), repr(error), *(str(arg) for arg in error.args)):
        for leak in (str(folder), folder.name, folder.parent.name):
            assert leak not in text, f"the error names the folder: {text!r}"


def _assert_refused(folder: Path) -> DataFolderUnavailable:
    with pytest.raises(DataFolderUnavailable) as refused:
        lock_data_folder(folder)
    _assert_no_path(refused.value, folder)
    return refused.value


def test_symlinked_lock_file_refused_and_target_untouched(folder: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.txt"
    target.write_text("fictional\n", encoding="utf-8")
    target.chmod(0o644)
    (folder / LOCK_FILE).symlink_to(target)

    _assert_refused(folder)

    assert target.read_text(encoding="utf-8") == "fictional\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_lock_file_that_is_a_directory_refused(folder: Path) -> None:
    (folder / LOCK_FILE).mkdir()
    _assert_refused(folder)


def test_hard_linked_lock_file_refused(folder: Path, tmp_path: Path) -> None:
    other = tmp_path / "other-name"
    other.write_bytes(b"")
    other.chmod(0o600)
    os.link(other, folder / LOCK_FILE)
    assert (folder / LOCK_FILE).stat().st_nlink == 2

    error = _assert_refused(folder)

    assert "not a private regular file" in str(error)
    assert stat.S_IMODE(other.stat().st_mode) == 0o600


def test_fifo_lock_file_refused_without_blocking(folder: Path) -> None:
    os.mkfifo(folder / LOCK_FILE, 0o600)
    # Opening a FIFO for reading and writing without O_NONBLOCK can hang on some systems;
    # the call must return at once (a hang shows up as the test timing out).
    _assert_refused(folder)


def test_leftover_wide_lock_file_becomes_0600(folder: Path) -> None:
    path = folder / LOCK_FILE
    path.write_bytes(b"")
    path.chmod(0o644)

    lock = lock_data_folder(folder)
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        lock.release()


def test_flock_os_error_is_unavailable_without_path(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_locks(_fd: int, _operation: int) -> None:
        raise OSError(errno.ENOLCK, "fictional: no locks available", str(folder))

    monkeypatch.setattr("app.modules.storage.workspace_lock.fcntl.flock", no_locks)

    error = _assert_refused(folder)

    assert "OSError" in str(error)


def test_lock_fd_is_not_inheritable(folder: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[int] = []
    real_open = os.open

    def recording_open(path: object, flags: int, mode: int = 0o777) -> int:
        fd = real_open(path, flags, mode)  # type: ignore[arg-type]
        opened.append(fd)
        return fd

    monkeypatch.setattr("app.modules.storage.workspace_lock.os.open", recording_open)
    lock = lock_data_folder(folder)
    try:
        assert len(opened) == 1
        assert os.get_inheritable(opened[0]) is False
    finally:
        lock.release()


def test_release_is_idempotent(folder: Path) -> None:
    lock = lock_data_folder(folder)
    assert lock.held
    lock.release()
    lock.release()
    assert not lock.held
