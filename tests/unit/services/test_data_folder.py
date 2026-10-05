"""Inspecting a chosen data folder before step 1 opens it (M1.16; ADR-049 route requirement 3).

``inspect_data_folder`` only reads the file system: a folder is ``missing``, ``empty``, a
``workspace`` (an ``app.db`` holding this app's ``schema_migrations``), ``not_empty`` (anything
else) or a ``file``. ``is_workspace_database`` opens the file read-only and never creates or
changes it, so a foreign SQLite file is never migrated. Every path is under ``tmp_path``.
"""

import sqlite3
from pathlib import Path

import pytest

from app.modules.storage import is_workspace_database
from app.services.data_folder import inspect_data_folder
from tests.fakes.clock import FrozenClock
from tests.setup_harness import HARNESS_START, seed_workspace


def _foreign_sqlite(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE fictional_recipes (id INTEGER PRIMARY KEY, name TEXT)")
        connection.execute("INSERT INTO fictional_recipes (name) VALUES ('Imaginary soup')")
        connection.commit()
    finally:
        connection.close()
    return path


def _workspace(folder: Path) -> Path:
    seed_workspace(folder, clock=FrozenClock(HARNESS_START))
    return folder


# --- is_workspace_database -----------------------------------------------------------------


def test_workspace_database_is_recognised(tmp_path: Path) -> None:
    folder = _workspace(tmp_path / "workspace")
    assert is_workspace_database(folder / "app.db") is True


def test_missing_file_is_not_a_workspace_and_is_not_created(tmp_path: Path) -> None:
    path = tmp_path / "nowhere" / "app.db"
    assert is_workspace_database(path) is False
    assert not path.exists()
    assert not path.parent.exists()


def test_foreign_sqlite_file_is_not_a_workspace_and_is_unchanged(tmp_path: Path) -> None:
    path = _foreign_sqlite(tmp_path / "other" / "app.db")
    before = path.read_bytes()
    entries = sorted(path.parent.iterdir())

    assert is_workspace_database(path) is False
    assert path.read_bytes() == before, "a foreign database is never modified"
    assert sorted(path.parent.iterdir()) == entries


@pytest.mark.parametrize(
    "content",
    [b"", b"fictional plain text, not a database\n", b"SQLite format 3\x00" + b"\xff" * 200],
    ids=["empty", "text", "corrupt"],
)
def test_non_sqlite_file_is_not_a_workspace(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "app.db"
    path.write_bytes(content)

    assert is_workspace_database(path) is False
    assert path.read_bytes() == content


def test_directory_is_not_a_workspace_database(tmp_path: Path) -> None:
    folder = tmp_path / "app.db"
    folder.mkdir()
    assert is_workspace_database(folder) is False
    assert list(folder.iterdir()) == []


# --- inspect_data_folder -------------------------------------------------------------------


def test_missing_folder(tmp_path: Path) -> None:
    folder = tmp_path / "not-yet"
    assert inspect_data_folder(folder) == "missing"
    assert not folder.exists()


def test_missing_folder_under_a_file_is_missing(tmp_path: Path) -> None:
    blocker = tmp_path / "a-file.txt"
    blocker.write_text("fictional\n", encoding="utf-8")
    assert inspect_data_folder(blocker / "inside") == "missing"


def test_file_is_reported_as_file(tmp_path: Path) -> None:
    path = tmp_path / "a-file.txt"
    path.write_text("fictional\n", encoding="utf-8")
    assert inspect_data_folder(path) == "file"
    assert path.read_text(encoding="utf-8") == "fictional\n"


def test_empty_folder(tmp_path: Path) -> None:
    folder = tmp_path / "empty"
    folder.mkdir()
    assert inspect_data_folder(folder) == "empty"
    assert list(folder.iterdir()) == []


def test_workspace_folder(tmp_path: Path) -> None:
    folder = _workspace(tmp_path / "workspace")
    entries = sorted(folder.rglob("*"))
    assert inspect_data_folder(folder) == "workspace"
    assert sorted(folder.rglob("*")) == entries


@pytest.mark.parametrize(
    "fill",
    ["a_file", "a_subfolder", "a_hidden_file", "foreign_app_db", "text_app_db", "app_db_folder"],
)
def test_folder_with_other_content_is_not_empty(tmp_path: Path, fill: str) -> None:
    folder = tmp_path / "chosen"
    folder.mkdir()
    if fill == "a_file":
        (folder / "notes.txt").write_text("fictional\n", encoding="utf-8")
    elif fill == "a_subfolder":
        (folder / "photos").mkdir()
    elif fill == "a_hidden_file":
        (folder / ".hidden").write_text("fictional\n", encoding="utf-8")
    elif fill == "foreign_app_db":
        _foreign_sqlite(folder / "app.db")
    elif fill == "text_app_db":
        (folder / "app.db").write_text("fictional, not a database\n", encoding="utf-8")
    else:
        (folder / "app.db").mkdir()
    entries = sorted(folder.rglob("*"))
    contents = {p: p.read_bytes() for p in entries if p.is_file()}

    assert inspect_data_folder(folder) == "not_empty"
    assert sorted(folder.rglob("*")) == entries
    assert {p: p.read_bytes() for p in entries if p.is_file()} == contents


def test_workspace_needs_app_db_at_top_level(tmp_path: Path) -> None:
    folder = tmp_path / "outer"
    _workspace(folder / "nested")
    assert inspect_data_folder(folder) == "not_empty"


@pytest.mark.parametrize("name", [".DS_Store", "Thumbs.db", "desktop.ini"])
def test_system_files_alone_leave_a_folder_empty(tmp_path: Path, name: str) -> None:
    folder = tmp_path / "made-in-finder"
    folder.mkdir()
    (folder / name).write_bytes(b"fictional")
    assert inspect_data_folder(folder) == "empty"


def test_ignored_names_do_not_count(tmp_path: Path) -> None:
    folder = tmp_path / "shared"
    folder.mkdir()
    (folder / "config.toml").write_text("fictional\n", encoding="utf-8")
    assert inspect_data_folder(folder) == "not_empty"
    assert inspect_data_folder(folder, ignored=frozenset({"config.toml"})) == "empty"


def test_probe_with_wal_but_no_shm_creates_nothing(tmp_path: Path) -> None:
    path = _foreign_sqlite(tmp_path / "other" / "app.db")
    (path.parent / "app.db-wal").write_bytes(b"")
    before = sorted(path.parent.iterdir())
    assert is_workspace_database(path) is False
    assert sorted(path.parent.iterdir()) == before
