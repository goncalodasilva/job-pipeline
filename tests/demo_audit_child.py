"""AC-M9.4 probe, run in a subprocess by ``tests/integration/test_demo_seed.py``.

It runs ``jobpipeline --demo --no-browser`` through the real command-line entry point
(``app.cli.main``), with the server swapped for an in-process client, and checks every file
access of the process:

- Before the app is imported, the ``os`` functions that name a path (``stat``, ``lstat``,
  ``access``, ``readlink``, ``open``, ``listdir``, ``scandir``, ``mkdir``, ``rmdir``, ``unlink``,
  ``remove``, ``rename``, ``replace``, ``chmod``, ``utime``, ``link``, ``symlink``, ``truncate``)
  are wrapped, so metadata reads (``Path.exists``) are seen too; a ``dir_fd`` or a file
  descriptor is turned back into its folder's path. An audit hook covers what runs in C: the
  built-in ``open``, ``sqlite3.connect`` and ``shutil``.
- Any access to the real data folder or the real settings folder is refused and recorded.
- Any write outside the test's temporary folder (``JOBPIPELINE_AUDIT_ROOT``) is refused and
  recorded: the home folders and ``TMPDIR`` point inside it.

The probe first puts a real-looking workspace (``app.db`` and ``config.toml`` naming it) in the
real folders under the temporary home, so a read would find something. Then the demo is used:
every page, the palette, the Data and privacy area, an export and Delete all data.

It prints one JSON line: the refused accesses and what the demo showed.
"""

import json
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir, user_data_dir

AUDITED = frozenset(
    {"open", "sqlite3.connect", "shutil.copyfile", "shutil.copytree", "shutil.move"}
    | {"shutil.rmtree", "shutil.copymode", "shutil.copystat", "glob.glob"}
)
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
PAGES = (
    "/",
    "/inbox",
    "/board",
    "/facts",
    "/companies",
    "/sessions/new",
    "/jobs/demo-job-01",
    "/companies/demo-company-brightwater",
    "/settings",
    "/settings/data",
    "/settings/keyboard",
    "/settings/your-details",
    "/settings/prompts",
    "/setup/welcome",
)
FRAGMENTS = (
    "/today/content",
    "/inbox/queue",
    "/palette/search?q=Irrigation",
    "/palette/recent?path=/jobs/demo-job-01",
    "/settings/data/location",
    "/settings/data/delete",
)
ORIGINAL: dict[str, Callable[..., Any]] = {}


def _variants(path: Path) -> tuple[str, ...]:
    """The path as given and with links resolved (/var and /private/var on macOS)."""
    return tuple({os.path.abspath(path), os.path.realpath(path)})


def _fd_path(fd: int) -> str | None:
    """The path a file descriptor names, without going through the wrapped functions."""
    try:
        import fcntl

        if hasattr(fcntl, "F_GETPATH"):  # macOS
            raw = fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024)
            return os.fsdecode(raw.split(b"\0", 1)[0])
    except OSError:
        return None
    try:
        return str(ORIGINAL["readlink"](f"/proc/self/fd/{fd}"))  # Linux
    except OSError:
        return None


class Guard:
    def __init__(self, forbidden: tuple[str, ...], write_roots: tuple[str, ...]) -> None:
        self.forbidden = forbidden
        self.write_roots = write_roots
        self.refused: list[str] = []

    @staticmethod
    def _under(path: str, roots: tuple[str, ...]) -> bool:
        return any(path == root or path.startswith(root + os.sep) for root in roots)

    def path_of(self, value: object, dir_fd: object = None) -> str | None:
        if isinstance(value, int) and not isinstance(value, bool):
            return _fd_path(value)
        if isinstance(value, os.PathLike):
            value = os.fspath(value)
        if isinstance(value, bytes):
            value = os.fsdecode(value)
        if not isinstance(value, str) or not value or "\x00" in value:
            return None
        if value.startswith("file:"):  # an SQLite URI
            value = value[len("file:") :].split("?", 1)[0]
        if not os.path.isabs(value) and isinstance(dir_fd, int):
            base = _fd_path(dir_fd)
            if base is not None:
                value = os.path.join(base, value)
        return os.path.abspath(value)

    def check(self, event: str, value: object, *, write: bool, dir_fd: object = None) -> None:
        path = self.path_of(value, dir_fd)
        if path is None:
            return
        if self._under(path, self.forbidden):
            self.refused.append(f"{event} of a real workspace path")
            raise PermissionError("the demo touched a real workspace folder")
        if write and not self._under(path, self.write_roots) and not path.startswith("/dev/"):
            self.refused.append(f"{event} outside the test folder: {path}")
            raise PermissionError("the demo wrote outside the test folder")


def _wrap(guard: Guard) -> None:
    def reading(name: str) -> None:
        real = getattr(os, name)
        ORIGINAL[name] = real

        def wrapper(path: Any = ".", *args: Any, **kwargs: Any) -> Any:
            guard.check(name, path, write=False, dir_fd=kwargs.get("dir_fd"))
            return real(path, *args, **kwargs)

        setattr(os, name, wrapper)

    def writing(name: str) -> None:
        real = getattr(os, name)
        ORIGINAL[name] = real

        def wrapper(path: Any, *args: Any, **kwargs: Any) -> Any:
            guard.check(name, path, write=True, dir_fd=kwargs.get("dir_fd"))
            return real(path, *args, **kwargs)

        setattr(os, name, wrapper)

    def two_paths(name: str) -> None:
        real = getattr(os, name)
        ORIGINAL[name] = real

        def wrapper(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
            guard.check(name, src, write=True, dir_fd=kwargs.get("src_dir_fd"))
            guard.check(name, dst, write=True, dir_fd=kwargs.get("dst_dir_fd"))
            return real(src, dst, *args, **kwargs)

        setattr(os, name, wrapper)

    for name in ("stat", "lstat", "access", "listdir", "scandir"):
        reading(name)
    ORIGINAL["readlink"] = os.readlink
    real_readlink = os.readlink

    def readlink(path: Any, *args: Any, **kwargs: Any) -> Any:
        guard.check("readlink", path, write=False, dir_fd=kwargs.get("dir_fd"))
        return real_readlink(path, *args, **kwargs)

    os.readlink = readlink  # type: ignore[assignment]
    for name in ("mkdir", "rmdir", "unlink", "remove", "chmod", "utime", "truncate"):
        writing(name)
    for name in ("rename", "replace", "link", "symlink"):
        two_paths(name)
    real_open = os.open
    inside = threading.local()  # os.open raises its own "open" event, without the dir_fd

    def os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        guard.check("open", path, write=bool(flags & WRITE_FLAGS), dir_fd=kwargs.get("dir_fd"))
        inside.active = True
        try:
            return real_open(path, flags, *args, **kwargs)
        finally:
            inside.active = False

    os.open = os_open  # type: ignore[assignment]

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if event not in AUDITED or not args:
            return
        if event == "open" and getattr(inside, "active", False):
            return  # checked by the os.open wrapper, with its dir_fd
        if event == "open":
            mode, flags = (args[1] if len(args) > 1 else None), (args[2] if len(args) > 2 else 0)
            write = (isinstance(mode, str) and any(c in mode for c in "wax+")) or bool(
                isinstance(flags, int) and flags & WRITE_FLAGS
            )
            guard.check(event, args[0], write=write)
        elif event.startswith(("shutil.copy", "shutil.move")) and len(args) > 1:
            guard.check(event, args[0], write=False)
            guard.check(event, args[1], write=True)
        elif event == "shutil.rmtree":
            guard.check(event, args[0], write=True, dir_fd=args[1] if len(args) > 1 else None)
        else:
            guard.check(event, args[0], write=False)

    sys.addaudithook(audit)


def _plant_real_workspace(real_data: Path, real_config: Path, root: Path) -> None:
    for folder in (real_data, real_config):
        if not folder.resolve().is_relative_to(root):
            raise SystemExit(f"refusing to write outside the test folder: {folder}")
    real_data.mkdir(parents=True, exist_ok=True)
    real_config.mkdir(parents=True, exist_ok=True)
    (real_data / "app.db").write_bytes(b"a fictional real workspace, never to be opened")
    (real_config / "config.toml").write_text(
        f'data_dir = "{real_data.as_posix()}"\nversion = "0.0.0"\n', encoding="utf-8"
    )


def main() -> None:
    root = Path(os.environ["JOBPIPELINE_AUDIT_ROOT"]).resolve()
    real_data = Path(user_data_dir("jobpipeline"))
    real_config = Path(user_config_dir("jobpipeline"))
    _plant_real_workspace(real_data, real_config, root)
    guard = Guard(_variants(real_data) + _variants(real_config), _variants(root))
    _wrap(guard)

    from fastapi.testclient import TestClient

    from app import cli
    from app.api.security import TOKEN_HEADER

    shown: dict[str, Any] = {}
    base = "http://127.0.0.1:8000"

    def serve(config: Any) -> bool:
        app = config.app
        with TestClient(app, base_url=base, follow_redirects=False) as client:
            token = {TOKEN_HEADER: app.state.launch_token}
            today = client.get("/")
            shown["today"] = today.status_code == 200 and "Today" in today.text
            shown["demo_note"] = "Demo workspace" in today.text
            shown["pages"] = all(client.get(page).status_code == 200 for page in PAGES)
            shown["fragments"] = all(
                client.get(path, headers=token).status_code == 200 for path in FRAGMENTS
            )
            found = client.get("/palette/search", params={"q": "Irrigation"}, headers=token)
            shown["palette"] = found.status_code == 200 and "Irrigation" in found.text
            workspace = app.state.workspace
            shown["demo_folder"] = workspace.data_dir.name
            shown["jobs"] = workspace.db.connection.execute("SELECT count(*) FROM jobs").fetchone()[
                0
            ]
            post = {**token, "Origin": base}
            exported = client.post("/settings/data/export", headers=post)
            shown["exported"] = exported.status_code == 200
            erased = client.post("/settings/data/delete", data={"confirm": "delete"}, headers=post)
            shown["erased"] = erased.status_code == 200 and erased.headers.get("HX-Redirect") == "/"
            after = client.get("/")
            shown["after_erase"] = after.status_code in (200, 303, 307)
            reopened = app.state.workspace
            shown["jobs_after_erase"] = (
                reopened.db.connection.execute("SELECT count(*) FROM jobs").fetchone()[0]
                if reopened is not None
                else None
            )
        return True

    launcher = cli.Launcher(
        port_free=lambda port: True,
        serve=serve,
        health_ok=lambda url: False,
        open_browser=lambda url: None,
        sleep=lambda seconds: None,
    )
    cli.main(["--demo", "--no-browser"], env=os.environ, launcher=launcher)
    print(json.dumps({"refused": guard.refused, **shown}))


if __name__ == "__main__":
    main()
