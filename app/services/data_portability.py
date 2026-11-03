"""Private JSON workspace exports and staged restores (ADR-131)."""

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC
from pathlib import Path
from typing import Any, Final

from app import __version__
from app.domain.clock import Clock
from app.modules.storage import MIGRATIONS_DIR, Database, portability
from app.services.data_folder import SYSTEM_FILES

PortabilityError = portability.PortabilityError
MAX_EXPORT_BYTES: Final = 200 * 1024 * 1024
MAX_FILES: Final = 10000
_DIR: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_PART: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")
_EXPORT: Final = re.compile(r"jobpipeline-export-[0-9]{8}T[0-9]{12}Z(?:-[1-9][0-9]*)?\.json")
_FOLDERS: Final = ("documents", "assets", "imports", "prompts")
# The task runner writes heartbeats while an export reads; these never decide coherence.
_VOLATILE_TABLES: Final = frozenset({"tasks", "usage_records"})
_STAGING_PREFIX: Final = ".restore-"


def _parts(name: object) -> tuple[str, ...]:
    if not isinstance(name, str) or len(name) > 1024:
        raise PortabilityError("The export has an unsafe file name.")
    parts = tuple(name.split("/"))
    if not all(_PART.fullmatch(p) for p in parts):
        raise PortabilityError("The export has an unsafe file name.")
    if not (
        (len(parts) == 3 and parts[0] == "documents")
        or (len(parts) == 2 and parts[0] in ("assets", "imports"))
        or (len(parts) == 4 and parts[:2] == ("prompts", "overrides"))
    ):
        raise PortabilityError("The export has an unsafe file name.")
    return parts


@contextmanager
def _directory(
    root: int, parts: tuple[str, ...], *, created: list[tuple[str, ...]] | None = None
) -> Iterator[int]:
    with ExitStack() as stack:
        current = root
        for index, part in enumerate(parts):
            made = False
            if created is not None:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current)
                    created.append(parts[: index + 1])
                    made = True
                except FileExistsError:
                    pass
            child = os.open(part, _DIR, dir_fd=current)
            stack.callback(os.close, child)
            if made:
                os.fchmod(child, 0o700)
            current = child
        yield current


def _read(folder: int, name: str, cap: int) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > cap:
            raise PortabilityError("A workspace file cannot be read safely or is too large.")
        result = stream.read(info.st_size + 1)
    if len(result) != info.st_size:
        raise PortabilityError("A workspace file changed during export. Try again.")
    return result


def _write(folder: int, name: str, data: bytes) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        os.unlink(name, dir_fd=folder)
        raise


def _files(root: int) -> list[tuple[str, ...]]:
    found: list[tuple[str, ...]] = []

    def walk(folder: int, prefix: tuple[str, ...]) -> None:
        for name in sorted(os.listdir(folder)):
            info = os.stat(name, dir_fd=folder, follow_symlinks=False)
            if name in SYSTEM_FILES and not stat.S_ISDIR(info.st_mode):
                continue  # Finder/Explorer noise: never exported, never content
            if not _PART.fullmatch(name):
                raise PortabilityError("A workspace file has an unsupported name.")
            parts = (*prefix, name)
            if stat.S_ISDIR(info.st_mode):
                if len(parts) >= 4:
                    raise PortabilityError("A workspace folder has an unsupported layout.")
                with _directory(folder, (name,)) as child:
                    walk(child, parts)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                _parts("/".join(parts))
                found.append(parts)
                if len(found) > MAX_FILES:
                    raise PortabilityError("An export can hold at most 10,000 files.")
            else:
                raise PortabilityError("Links and special files cannot be exported or restored.")

    for name in _FOLDERS:
        try:
            with _directory(root, (name,)) as folder:
                if name == "prompts":
                    try:
                        with _directory(folder, ("overrides",)) as overrides:
                            walk(overrides, (name, "overrides"))
                    except FileNotFoundError:
                        pass
                else:
                    walk(folder, (name,))
        except FileNotFoundError:
            continue
    return found


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise PortabilityError("The export has duplicate JSON fields.")
        result[name] = value
    return result


def _payload(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_EXPORT_BYTES:
        raise PortabilityError("The export is larger than 200 MB.")
    try:
        value = json.loads(raw, object_pairs_hook=_json_object)
        if (
            not isinstance(value, dict)
            or set(value)
            != {"format", "app_version", "schema_version", "tables", "excluded", "files"}
            or type(value["format"]) is not int
            or value["format"] != 1
            or not isinstance(value["app_version"], str)
            or not value["app_version"]
            or not isinstance(value["files"], list)
        ):
            raise PortabilityError("This is not a supported Job Pipeline export.")
        return value
    except (ValueError, TypeError, RecursionError, UnicodeError) as error:
        raise PortabilityError("This is not a valid Job Pipeline JSON export.") from error


def _decoded_files(value: list[Any]) -> list[tuple[tuple[str, ...], bytes]]:
    if len(value) > MAX_FILES:
        raise PortabilityError("An export can hold at most 10,000 files.")
    result = []
    names: set[str] = set()
    total = 0
    for item in value:
        if not isinstance(item, dict) or set(item) != {"name", "size", "sha256", "data"}:
            raise PortabilityError("The export has an invalid file manifest.")
        parts = _parts(item["name"])
        name = "/".join(parts).casefold()
        if (
            name in names
            or type(item["size"]) is not int
            or not 0 <= item["size"] <= MAX_EXPORT_BYTES
        ):
            raise PortabilityError("The export has duplicate or invalid file entries.")
        names.add(name)
        try:
            data = base64.b64decode(item["data"], validate=True)
        except (ValueError, TypeError, binascii.Error) as error:
            raise PortabilityError("The export has invalid file data.") from error
        total += len(data)
        if (
            total > MAX_EXPORT_BYTES
            or len(data) != item["size"]
            or hashlib.sha256(data).hexdigest() != item["sha256"]
        ):
            raise PortabilityError("The export's file sizes or checksums do not match.")
        result.append((parts, data))
    return result


def _rollback_files(
    root: int, files: list[tuple[str, ...]], directories: list[tuple[str, ...]]
) -> None:
    """Best effort per entry: one stubborn entry must not stop the rest or hide the error."""
    for parts in reversed(files):
        try:
            with _directory(root, parts[:-1]) as folder:
                os.unlink(parts[-1], dir_fd=folder)
        except OSError:
            continue
    for parts in reversed(directories):
        try:
            with _directory(root, parts[:-1]) as parent:
                os.rmdir(parts[-1], dir_fd=parent)
        except OSError:
            continue


def _stable(snapshot: dict[str, Any]) -> dict[str, Any]:
    tables = {k: v for k, v in snapshot["tables"].items() if k not in _VOLATILE_TABLES}
    return {**snapshot, "tables": tables}


def sweep_restore_staging(root: Path) -> None:
    """At startup only: remove ``.restore-*`` staging directories a killed restore left behind.
    Real directories only (links are never followed or removed); best effort."""
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    for entry in entries:
        try:
            if entry.name.startswith(_STAGING_PREFIX) and entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path)
        except OSError:
            continue


class DataPortabilityService:
    def __init__(
        self,
        database: Callable[[], Database | None],
        data_dir: Callable[[], Path | None],
        clock: Clock,
        *,
        migrations_dir: Path = MIGRATIONS_DIR,
    ) -> None:
        self._database, self._data_dir, self._clock = database, data_dir, clock
        self._migrations_dir = migrations_dir
        self._lock = threading.Lock()

    def folder(self) -> Path:
        """The open workspace's data folder (checked against its database). Raises
        ``PortabilityError`` when none is open."""
        return self._workspace()[1]

    def can_restore(self) -> bool:
        try:
            db, root = self._workspace()
            if not portability.is_empty(db, migrations_dir=self._migrations_dir):
                return False
            fd = os.open(root, _DIR)
            try:
                return not _files(fd)
            finally:
                os.close(fd)
        except (OSError, PortabilityError, sqlite3.Error):
            return False

    def export(self, *, include_usage_snapshots: bool = False) -> Path:
        db, root = self._workspace()
        with self._lock:
            try:
                fd = os.open(root, _DIR)
            except OSError as error:
                raise PortabilityError("The workspace folder cannot be opened safely.") from error
            try:
                snapshot = portability.export_tables(
                    db, include_usage_snapshots=include_usage_snapshots
                )
                contents = []
                remaining = MAX_EXPORT_BYTES
                names = _files(fd)
                for parts in names:
                    with _directory(fd, parts[:-1]) as folder:
                        data = _read(folder, parts[-1], remaining)
                    remaining -= len(data)
                    contents.append(data)
                # Refuse before base64 inflates a result that can't fit anyway.
                tables_size = len(json.dumps(snapshot, ensure_ascii=True, separators=(",", ":")))
                if sum(map(len, contents)) * 4 // 3 + tables_size > MAX_EXPORT_BYTES:
                    raise PortabilityError("The export is larger than 200 MB.")
                files = [
                    {
                        "name": "/".join(parts),
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "data": base64.b64encode(data).decode("ascii"),
                    }
                    for parts, data in zip(names, contents, strict=True)
                ]
                # Refuse an interleaved writer rather than pair old rows with new files.
                # App routes also drain competing requests before this operation.
                if names != _files(fd) or _stable(snapshot) != _stable(
                    portability.export_tables(db, include_usage_snapshots=include_usage_snapshots)
                ):
                    raise PortabilityError("The workspace changed during export. Try again.")
                for item, parts in zip(files, names, strict=True):
                    with _directory(fd, parts[:-1]) as folder:
                        if (
                            hashlib.sha256(_read(folder, parts[-1], MAX_EXPORT_BYTES)).hexdigest()
                            != item["sha256"]
                        ):
                            raise PortabilityError(
                                "A workspace file changed during export. Try again."
                            )
                raw = json.dumps(
                    {"format": 1, "app_version": __version__, **snapshot, "files": files},
                    ensure_ascii=True,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                if len(raw) > MAX_EXPORT_BYTES:
                    raise PortabilityError("The export is larger than 200 MB.")
                with _directory(fd, ("exports",), created=[]) as folder:
                    os.fchmod(folder, 0o700)
                    stamp = self._clock.now().astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
                    suffix = 0
                    while True:
                        name = (
                            f"jobpipeline-export-{stamp}{'-' + str(suffix) if suffix else ''}.json"
                        )
                        try:
                            _write(folder, name, raw)
                            break
                        except FileExistsError:
                            info = os.stat(name, dir_fd=folder, follow_symlinks=False)
                            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                                raise PortabilityError(
                                    "Links cannot be used for an export."
                                ) from None
                            suffix += 1
                            if suffix > 1000:
                                raise PortabilityError(
                                    "An export could not be created. Try again."
                                ) from None
                return root / "exports" / name
            except (OSError, ValueError, TypeError, sqlite3.Error) as error:
                if isinstance(error, PortabilityError):
                    raise
                # No chaining: an OS or database message can carry a path.
                raise PortabilityError("The workspace could not be exported safely.") from None
            finally:
                os.close(fd)

    def download(self, name: str) -> bytes:
        if not _EXPORT.fullmatch(name):
            raise PortabilityError("This export was not found.")
        _, root = self._workspace()
        try:
            fd = os.open(root, _DIR)
            try:
                with _directory(fd, ("exports",)) as folder:
                    return _read(folder, name, MAX_EXPORT_BYTES)
            finally:
                os.close(fd)
        except OSError as error:
            raise PortabilityError("This export was not found.") from error

    def restore(self, path: Path) -> None:
        """``path`` must be an app-generated file under the data folder (an upload the app
        stored itself), never a path taken from a request."""
        db, root = self._workspace()
        try:
            parent = os.open(path.parent, _DIR)
            try:
                payload = _payload(_read(parent, path.name, MAX_EXPORT_BYTES))
            finally:
                os.close(parent)
            files = _decoded_files(payload["files"])
            with self._lock:
                fd = os.open(root, _DIR)
                try:
                    if not portability.is_empty(db, migrations_dir=self._migrations_dir) or _files(
                        fd
                    ):
                        raise PortabilityError("Restore needs a workspace with no content.")
                    with tempfile.TemporaryDirectory(prefix=".restore-", dir=root) as staging:
                        with portability.database_from_snapshot(
                            Path(staging) / "app.db",
                            payload,
                            self._clock,
                            migrations_dir=self._migrations_dir,
                        ) as stage:
                            upgraded = portability.export_tables(
                                stage, include_usage_snapshots=True
                            )
                        created_files: list[tuple[str, ...]] = []
                        created_dirs: list[tuple[str, ...]] = []
                        if _files(fd):
                            raise PortabilityError(
                                "The workspace changed during restore. Nothing was imported."
                            )
                        try:
                            for parts, content in files:
                                with _directory(fd, parts[:-1], created=created_dirs) as folder:
                                    _write(folder, parts[-1], content)
                                created_files.append(parts)
                            if set(_files(fd)) != set(created_files):
                                raise PortabilityError(
                                    "The workspace changed during restore. Nothing was imported."
                                )
                            portability.import_tables(
                                db, upgraded, migrations_dir=self._migrations_dir
                            )
                        except BaseException:
                            _rollback_files(fd, created_files, created_dirs)
                            raise
                finally:
                    os.close(fd)
        except (OSError, ValueError, TypeError, RecursionError, sqlite3.Error) as error:
            if isinstance(error, PortabilityError):
                raise
            raise PortabilityError(
                "The export could not be restored. Nothing was imported."
            ) from None

    def _workspace(self) -> tuple[Database, Path]:
        db, root = self._database(), self._data_dir()
        if db is None or root is None:
            raise PortabilityError("Choose an empty data folder first.")
        try:
            same = db.path.parent.resolve() == root.resolve()
        except (OSError, RuntimeError):
            same = False
        if not same:
            raise PortabilityError("The workspace folder does not match its database.")
        return db, root
