"""Private export files, opened relative to real directory descriptors, never through links."""

import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Final

from app.domain.ids import is_safe_id
from app.modules.storage import Database, exports_repo

GENERATED_NAME: Final = re.compile(r"(?:cv|cover-letter)-v[1-9][0-9]*(?:-[a-z0-9]+)?\.(?:pdf|docx)")
_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@contextmanager
def directory(parent: int, name: str, *, create: bool = False) -> Iterator[int]:
    if create:
        with suppress(FileExistsError):
            os.mkdir(name, 0o700, dir_fd=parent)
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def application_folder(root: Path, aid: str, *, create: bool = False) -> Iterator[int]:
    if not is_safe_id(aid):
        raise OSError("invalid application folder")
    fd = os.open(root, _DIR_FLAGS)
    try:
        with (
            directory(fd, "documents", create=create) as documents,
            directory(documents, aid, create=create) as folder,
        ):
            yield folder
    finally:
        os.close(fd)


def read(folder: int, name: str, size: int) -> bytes:
    if not GENERATED_NAME.fullmatch(name) or not 0 <= size <= 64 * 1024 * 1024:
        raise OSError("invalid export")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            raise OSError("invalid export")
        result = stream.read(size + 1)
    if len(result) != size:
        raise OSError("invalid export")
    return result


def write(folder: int, name: str, content: bytes, created: list[str]) -> None:
    if not GENERATED_NAME.fullmatch(name):
        raise OSError("invalid export name")
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
    created.append(name)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def sweep_orphan_exports(root: Path, db: Database) -> None:
    """At startup only: remove generated regular files with no row, leaving other files alone."""
    try:
        fd = os.open(root, _DIR_FLAGS)
        try:
            with directory(fd, "documents") as documents:
                for aid in os.listdir(documents):
                    if not is_safe_id(aid):
                        continue
                    try:
                        with directory(documents, aid) as folder:
                            with db.read() as conn:
                                kept = {row.stored_name for row in exports_repo.listing(conn, aid)}
                            for name in os.listdir(folder):
                                if name in kept or not GENERATED_NAME.fullmatch(name):
                                    continue
                                info = os.stat(name, dir_fd=folder, follow_symlinks=False)
                                if stat.S_ISREG(info.st_mode):
                                    os.unlink(name, dir_fd=folder)
                    except OSError:
                        continue
        finally:
            os.close(fd)
    except OSError:
        return
