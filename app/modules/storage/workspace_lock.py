"""The data-folder lock (M2.4; ADR-072, ADR-076 item 9).

One copy of the app at a time may use a data folder: opening it takes an exclusive, non-blocking
``flock`` on ``<data>/.lock`` (mode 0600). The lock belongs to the open file, so it lasts until
``release()`` or until the process exits or is killed, when the OS drops it. POSIX only; on
other systems taking it does nothing (ADR-061).
"""

import os
import stat
import sys
from pathlib import Path

from app.domain.errors import DataFolderInUse, DataFolderUnavailable

if sys.platform != "win32":
    import fcntl

LOCK_FILE = ".lock"


class DataFolderLock:
    """A held data-folder lock; ``release()`` is idempotent."""

    def __init__(self, fd: int | None) -> None:
        self._fd = fd

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)  # closing the file releases the lock


def lock_data_folder(folder: Path) -> DataFolderLock:
    """Take the lock on ``folder`` (which must exist), creating ``.lock`` 0600 if needed.

    Raises ``DataFolderInUse`` when another open file holds it, and ``DataFolderUnavailable``
    when the lock file can't be opened or locked. Neither error names the path.
    """
    if sys.platform == "win32":  # pragma: no cover - POSIX only (ADR-061)
        return DataFolderLock(None)
    # No symlink is followed, a FIFO can't block the open, and the fd is never inherited.
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = os.open(folder / LOCK_FILE, flags, 0o600)
    except OSError as error:
        raise DataFolderUnavailable(detail=f"can't open lock ({type(error).__name__})") from None
    try:
        info = os.fstat(fd)
        if not _private_regular_file(info):
            os.close(fd)
            raise DataFolderUnavailable(detail="lock file not a private regular file")
        if stat.S_IMODE(info.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise DataFolderInUse() from None
    except OSError as error:
        os.close(fd)
        raise DataFolderUnavailable(detail=f"can't lock ({type(error).__name__})") from None
    return DataFolderLock(fd)


def _private_regular_file(info: os.stat_result) -> bool:
    """A regular file the user owns, with no other name (a hard link could be anyone's file)."""
    geteuid = getattr(os, "geteuid", None)
    owned = geteuid is None or info.st_uid == geteuid()
    return stat.S_ISREG(info.st_mode) and owned and info.st_nlink == 1
