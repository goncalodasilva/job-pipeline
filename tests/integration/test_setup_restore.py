"""M9.1 part D2: setup step 1 "Restore from an export" (AC-M9.3; ADR-131 items 3-4; spec 2.9 rules
2, 3, 5, 9 and 12; TD-033). Every value is fictional; no network.

ASSUMED INTERFACE (the implementer follows it; where the task text was silent the choice is marked
"CHOICE"). The helpers shared with part D1 are in ``tests/data_pages.py``.

Route (``app/api/setup.py``, registered before the generic ``POST /setup/{step_id}``)
- ``POST /setup/welcome/restore``: not a page (no ``PAGE_TAG``), so the launch token and the origin
  check apply. POST only (any other verb: 404 or 405). ``multipart/form-data`` with a text part
  ``folder`` and ONE file part ``file``; CHOICE: ``folder`` comes first (the folder must be known
  before the file is streamed into it), and is absent when the workspace is already open.
- It opens the folder exactly like the Welcome step's "open" action (same folder messages, folder
  fixed or explicit rules, ``config.toml`` written when none exists and never in demo mode), but
  WITHOUT marking ``welcome`` done, then restores the file into the freshly opened, empty workspace
  by the path of ``POST /settings/data/import`` (``app.api.restore_upload.save_upload`` into
  ``.restore-upload-*`` inside the data folder, removed in every outcome, capped by the module
  constants ``app.api.settings_data.MAX_IMPORT_BYTES`` and ``READ_TIMEOUT_S`` read at request time,
  ``DataPortabilityService.restore`` in a worker thread).
- Reachable only while the guide shows step 1 (the cached start-up decision is ``guide`` at
  ``welcome``, or ``locate_or_fresh``). Otherwise 409 with a fixed refusal, nothing opened, written
  or restored.
- If the workspace is already open and empty (``--data-dir``, the environment, demo, or a folder
  opened by an earlier refused attempt) the ``folder`` part is ignored and only the file is needed.
- A folder error answers 422 with the guide fragment exactly as the Welcome step does (error on
  ``#folder``: ``aria-invalid``, ``aria-describedby`` naming ``folder-error``). A folder that
  already holds data from the app or other files is refused with the Welcome step's existing
  messages (CHOICE: restore accepts only a missing or empty folder, like "Start fresh").
- A refused restore answers the guide fragment (htmx) with ``input#restore-file`` marked
  ``aria-invalid="true"`` and ``aria-describedby`` naming ``p#restore-error`` (``role="alert"``,
  a fixed message that echoes nothing of the file, its name, the error or a path), exactly one
  ``data-autofocus`` element (``#restore-file``), the workspace stays open and empty, ``welcome``
  not done, and the form works again with only the file. CHOICE: 422 for an unreadable file, a
  failed checksum, a newer schema, a missing file and a stalled upload (408 also allowed); 413
  over the cap; 409 when the open workspace has content (or files).
- Success: 200, ``HX-Redirect: /`` and ``HX-Trigger: {"announce": "Restored..."}``. The setup
  decision is recomputed from the restored ``setup_state`` (``app.state.workspace_restored``).

Markup on step 1 (``GET /setup/welcome``, full page and htmx fragment)
- ``section#setup-restore`` (``aria-labelledby="restore-heading"``) with ``h3#restore-heading``
  "Restore from an export" and ``form#restore-form`` (``action="/setup/welcome/restore"``, method
  post, ``enctype="multipart/form-data"``, ``hx-post`` the same path, ``hx-target="#setup-guide"``),
  ``input#restore-file`` (type file, name ``file``, labelled) and ``button#restore-submit``
  (text starts "Restore", ``type=submit``, no ``name="action"`` so the Welcome step still has the
  one ``open`` button). The form sends the Data folder field's value as ``folder`` (CHOICE: an input
  named ``folder`` in the form, or ``form="restore-form"``, or ``hx-include`` naming ``#folder``).
  Steps other than 1 never show the section.
"""

import asyncio
import hashlib
import html
import json
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from httpx import Response

from app import __version__
from app.api.security import PAGE_TAG
from app.config import RuntimeConfig, load_bootstrap, write_bootstrap
from app.domain.redaction import Redactor
from app.main import create_app
from app.modules.storage import Database
from app.onboarding.detection import StepStatus, first_unfinished_required
from app.onboarding.progress import SetupProgress
from app.onboarding.registry import m1_registry
from app.onboarding.steps.welcome import (
    MSG_ALREADY_WORKSPACE_RESTORE,
    MSG_EMPTY,
    MSG_FILE,
    MSG_HAS_DATA_RESTORE,
    MSG_RELATIVE,
    MSG_RESERVED,
)
from app.services.setup_state import SetupStateService
from app.wiring import build_wiring
from tests.asgi_stream import app_lifespan
from tests.board_pages import by_id, elements
from tests.data_pages import (
    EXPORT,
    FACT_TEXT,
    FILES,
    MARKER,
    MIB,
    PAGE,
    add_fact,
    announced,
    drive,
    error_tied_to,
    focus_targets,
    folder_tree,
    generated_name,
    get_token,
    inside,
    label_for,
    one,
    page_text,
    plant,
    post,
    source_export,
    upload_folders,
    workspace_state,
)
from tests.data_pages import running as finished_app
from tests.fakes.clock import FrozenClock
from tests.fakes.keyring_backends import MemoryKeyring
from tests.integration.test_data_portability import service
from tests.integration.test_setup_redirect import BASE_URL, ORIGIN, PORT, START, Running
from tests.setup_harness import HARNESS_PROVIDER, seed_workspace
from tests.upload_bodies import (
    BOUNDARY,
    CONTENT_TYPE,
    FieldPart,
    FilePart,
    file_part_head,
    multipart_body,
)

pytestmark = pytest.mark.integration

WELCOME = "/setup/welcome"
RESTORE = "/setup/welcome/restore"
PRIVATE_NAME = "kestrel-private-name-3377.json"
STEP_IDS = [step.id for step in m1_registry().steps]
FOLDER_MESSAGES = {
    "relative": MSG_RELATIVE,
    "empty": MSG_EMPTY,
    "file": MSG_FILE,
    "reserved": MSG_RESERVED,
}
NON_EMPTY_MESSAGES = {MSG_HAS_DATA_RESTORE, MSG_ALREADY_WORKSPACE_RESTORE}
FOLDER_AGAIN = "Choose the file again after changing the folder."
# Setup says "restore", never "import" (the Settings screen's words); the fixed copy is pinned.
SETUP_WORDS = {
    "form": "Choose one export file to restore. Nothing was restored.",
    "too_large": "That file is too large. An export can be up to 200 MB. Nothing was restored.",
    "stalled": "The upload stopped before it finished. Nothing was restored. Try again.",
    "unreadable": (
        "This file can't be restored. It may not be a Job Pipeline export, may be damaged, "
        "or may come from a newer version of the app. Nothing was restored."
    ),
    "busy": "Another restore or import is still running. Wait for it to finish, then try again.",
    "has_content": (
        "This data folder already has content, so nothing was restored. "
        "Restore only works into a new or empty folder."
    ),
    "storage": "The file could not be saved. Check that the disk has space, then try again.",
    "not_saved": (
        "Restored. The app couldn't save where your data folder is, so choose it again "
        "the next time you start the app."
    ),
    "restart": (
        "Restored. Restart the app to finish: it couldn't refresh its setup state by itself."
    ),
}
OPENED_HINT = (
    "This folder is open now. Choose the export file again, or use it as it is and continue."
)
USING_HINT = (
    "The app is already using this folder. To use another one, start the app with --data-dir."
)
NOT_HERE = "Restoring an export is only offered on the first step of setup."


# --- the app ---------------------------------------------------------------------------------


def make_config(root: Path, source: str, data_dir: Path | None = None) -> RuntimeConfig:
    return RuntimeConfig(
        data_dir=data_dir if data_dir is not None else root / "data",
        config_dir=root / "cfg",
        port=PORT,
        log_level="INFO",
        demo=source == "demo",
        setup=False,
        no_browser=False,
        data_dir_source=source,  # type: ignore[arg-type]
    )


def make_app(root: Path, source: str, data_dir: Path | None = None) -> Any:
    wiring = build_wiring(
        environ={"JOBPIPELINE_ALLOW_FAKE_PROVIDERS": "1"},
        demo=source == "demo",
        redactor=Redactor(),
        keyring_backend=MemoryKeyring(),
        clock=FrozenClock(START),
        demo_workspace=False,
    )
    # A demo starts with no content, as delete all data leaves it: restoring at step 1 needs an
    # empty workspace, and M9.5 part C seeds a new demo folder (ADR-133).
    return create_app(
        make_config(root, source, data_dir),
        clock=FrozenClock(START),
        wiring=wiring,
        demo_dataset=None,
    )


@contextmanager
def started(root: Path, source: str = "default", data_dir: Path | None = None) -> Iterator[Running]:
    """The app before step 1 is done: ``default`` is a true first run (nothing exists, no
    workspace); ``cli`` and ``demo`` open their folder at start-up (new, empty, no step done)."""
    from collections import Counter

    app = make_app(root, source, data_dir)
    with TestClient(app, base_url=BASE_URL, follow_redirects=False) as client:
        yield Running(app=app, client=client, hits=Counter())


# --- requests and exports ---------------------------------------------------------------------


def restore(
    run: Running,
    data: bytes | None,
    folder: str | None = None,
    *,
    token: bool = True,
    origin: str | bool = True,
    filename: str = PRIVATE_NAME,
) -> Response:
    parts: list[FieldPart | FilePart] = []
    if folder is not None:
        parts.append(FieldPart(folder, name="folder"))
    if data is not None:
        parts.append(FilePart(filename, data, name="file"))
    return post(
        run,
        RESTORE,
        token=token,
        origin=origin,
        content=multipart_body(*parts),
        content_type=CONTENT_TYPE,
    )  # type: ignore[no-any-return]


def finished_export(root: Path) -> tuple[bytes, dict[str, Any]]:
    """An export of a finished workspace (every step done, a fact, the app's files) and the rows
    of its source."""
    with finished_app(root, empty=False, files=True) as run:
        add_fact(run)
        name = generated_name(post(run, EXPORT, {}).text)
        raw = get_token(run, f"{PAGE}/exports/{name}").content
        return raw, workspace_state(run.app)


def export_with_steps(root: Path, done: tuple[str, ...]) -> bytes:
    """An export of a workspace whose guide has exactly ``done`` finished (the fake provider is
    saved once the provider step is)."""
    data = root / "data"
    seed_workspace(
        data,
        done=done,
        clock=FrozenClock(START),
        provider=HARNESS_PROVIDER if "provider" in done else None,
    )
    plant(data, {"imports/fictional-cv.txt": b"Fictional CV text for the restore"})
    with Database.open(data / "app.db") as db:
        return service(db).export().read_bytes()


@pytest.fixture(scope="module")
def finished(tmp_path_factory: pytest.TempPathFactory) -> tuple[bytes, dict[str, Any]]:
    return finished_export(tmp_path_factory.mktemp("finished-source"))


@pytest.fixture(scope="module")
def valid(finished: tuple[bytes, dict[str, Any]]) -> bytes:
    return finished[0]


# --- reading the workspace and the answers ----------------------------------------------------


def statuses(run: Running) -> dict[str, str]:
    db: Database = run.app.state.workspace.db
    progress = SetupProgress(
        run.app.state.setup_registry, SetupStateService(db, FrozenClock(START))
    )
    return dict(progress.statuses())


def mark_done(run: Running, *step_ids: str) -> None:
    """Finish steps in the open workspace and refresh the app's setup decision."""
    db: Database = run.app.state.workspace.db
    progress = SetupProgress(
        run.app.state.setup_registry, SetupStateService(db, FrozenClock(START))
    )
    for step_id in step_ids:
        progress.mark_done(step_id)
    run.app.state.onboarding.refresh(progress, bootstrap_exists=True)


def bootstrap_file(root: Path) -> Path:
    return root / "cfg" / "config.toml"


def bootstrap_folder(root: Path) -> Path | None:
    loaded = load_bootstrap(root / "cfg")
    return loaded.data_dir if loaded is not None else None


def listing(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


def digest(folder: Path) -> dict[str, str]:
    return {
        p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


def leftovers(folder: Path) -> list[str]:
    """``.restore-upload-*`` entries of ``folder`` (none when it does not exist)."""
    return upload_folders(folder) if folder.is_dir() else []


def can_restore(run: Running) -> bool:
    return bool(run.app.state.data_portability.can_restore())


def assert_restored(answer: Response) -> None:
    assert answer.status_code == 200, (answer.status_code, answer.text[:300])
    assert answer.headers["HX-Redirect"] == "/"
    assert "Restored" in announced(answer)


def restore_refused(answer: Response, status: int, *, typed: str | None = None) -> str:
    """The restore field carries the error; the folder does not; one place takes focus. ``typed``:
    the folder field shows what was typed (a refusal that came before the folder opened)."""
    assert answer.status_code == status, (answer.status_code, answer.text[:300])
    assert "HX-Redirect" not in answer.headers
    message = error_tied_to(answer.text, "restore-file", "restore-error")
    targets = focus_targets(answer.text)
    assert [t.attrs.get("id") for t in targets] == ["restore-file"], "the error takes focus"
    folder = one(answer.text, "folder")
    assert folder.attrs.get("aria-invalid") != "true", "the folder is not the problem"
    if typed is not None:
        assert folder.attrs.get("value") == typed, "the typed folder is shown back"
        assert "readonly" not in folder.attrs, "nothing was opened, so the folder can still change"
    assert by_id_absent(answer.text, "restore-folder-note")
    text = answer.text + " ".join(f"{k}: {v}" for k, v in answer.headers.items())
    assert MARKER not in text and PRIVATE_NAME not in text, "nothing of the upload is echoed"
    assert "outside.txt" not in text
    return message


def folder_refused(answer: Response, expected: set[str], submitted: str | None) -> None:
    assert answer.status_code == 422, (answer.status_code, answer.text[:300])
    assert "HX-Redirect" not in answer.headers
    field = one(answer.text, "folder")
    assert field.attrs.get("aria-invalid") == "true"
    assert "folder-error" in (field.attrs.get("aria-describedby") or "").split()
    message = html.unescape(one(answer.text, "folder-error").text)
    assert message in expected, message
    if submitted is not None:
        assert field.attrs.get("value") == submitted, "the typed folder is shown back"
    assert by_id_absent(answer.text, "restore-error"), "no restore error: the file was not read"
    assert one(answer.text, "restore-folder-note").text == FOLDER_AGAIN
    assert (
        "restore-folder-note"
        in (one(answer.text, "restore-file").attrs.get("aria-describedby") or "").split()
    )


def by_id_absent(markup: str, element_id: str) -> bool:
    return by_id(markup, element_id) is None


def guide_step(run: Running) -> str | None:
    step: str | None = run.app.state.onboarding.guide_step()
    return step


def foreign_folder(root: Path) -> Path:
    folder = root / "holiday-photos"
    folder.mkdir()
    (folder / "notes.txt").write_text("fictional notes\n", encoding="utf-8")
    return folder


# --- AC-M9.3: the route -----------------------------------------------------------------------


def test_setup_restore_route_token_origin_and_get_writes_nothing(
    tmp_path: Path, valid: bytes
) -> None:
    """Only POST, with the token and the origin (spec 2.9 rules 2, 3, 12): a refused request
    opens no folder, writes no config and restores nothing."""
    with started(tmp_path, "default") as run:
        routes = [r for r in run.app.routes if isinstance(r, APIRoute) and r.path == RESTORE]
        assert routes and all("POST" in r.methods for r in routes)
        assert not any(PAGE_TAG in r.tags for r in routes), "the restore is not a page"
        chosen = str(tmp_path / "chosen")

        for token, origin in (
            (False, True),
            (True, False),
            (True, "http://evil.example"),
            (False, False),
        ):
            answer = restore(run, valid, chosen, token=token, origin=origin)
            assert answer.status_code == 403, (token, origin)
        assert run.app.state.workspace is None
        assert listing(tmp_path) == [], "a refused request creates nothing"

        headers = run.with_token(origin=ORIGIN)
        for method in ("GET", "HEAD", "PUT", "PATCH", "DELETE"):
            answer = run.client.request(method, RESTORE, headers=headers)
            assert answer.status_code in (404, 405), method
            plain = run.client.request(method, RESTORE, headers={"HX-Request": "true"})
            assert plain.status_code in (403, 404, 405), method
        assert run.client.get(RESTORE, params={"folder": chosen}).status_code in (403, 404, 405)
        assert run.app.state.workspace is None
        assert listing(tmp_path) == [], "a GET (or another verb) writes nothing"
        assert run.client.get(WELCOME).status_code == 200  # the page itself is still token-free


def test_setup_restore_opens_folder_then_restores_rows_and_files(
    tmp_path: Path, finished: tuple[bytes, dict[str, Any]]
) -> None:
    raw, source = finished
    chosen = tmp_path / "my-job-data"
    with started(tmp_path, "default") as run:
        assert run.app.state.workspace is None

        answer = restore(run, raw, str(chosen))

        assert_restored(answer)
        assert run.app.state.workspace.data_dir == chosen
        assert (chosen / "app.db").is_file()
        for relative, content in FILES.items():
            assert (chosen / relative).read_bytes() == content, relative
        assert upload_folders(chosen) == []
        restored = workspace_state(run.app)
        assert restored == source, "every row is back"
        assert FACT_TEXT in json.dumps(restored["fact_versions"])
        assert statuses(run) == {step: "done" for step in STEP_IDS}, "the export's setup state"
        assert guide_step(run) is None
        assert run.client.get("/").status_code == 200
        assert not (tmp_path / "data").exists(), "only the chosen folder is created"
        assert not can_restore(run), "now it has content"
    assert bootstrap_folder(tmp_path) == chosen


def test_setup_restore_writes_bootstrap_unless_demo(tmp_path: Path, valid: bytes) -> None:
    # A first run: config.toml names the folder the restore opened (an empty folder is accepted).
    first = tmp_path / "first"
    chosen = first / "empty-folder"
    chosen.mkdir(parents=True)
    with started(first, "default") as run:
        assert_restored(restore(run, valid, str(chosen)))
    assert bootstrap_folder(first) == chosen

    # --data-dir without a config.toml: written, naming the explicit folder.
    explicit = tmp_path / "second" / "explicit"
    with started(tmp_path / "second", "cli", explicit) as run:
        assert not bootstrap_file(tmp_path / "second").exists()
        assert_restored(restore(run, valid))
    assert bootstrap_folder(tmp_path / "second") == explicit

    # --data-dir with a config.toml already there: never overwritten (ADR-050).
    third = tmp_path / "third"
    other = third / "original"
    write_bootstrap(third / "cfg", data_dir=other, version=__version__)
    original = bootstrap_file(third).read_bytes()
    with started(third, "cli", third / "explicit") as run:
        assert_restored(restore(run, valid))
        assert FILES and (third / "explicit" / next(iter(FILES))).is_file()
    assert bootstrap_file(third).read_bytes() == original
    assert not other.exists()

    # Demo: restored into the demo folder, no config.toml, ever.
    fourth = tmp_path / "fourth"
    demo_dir = fourth / "demo-data"
    with started(fourth, "demo", demo_dir) as run:
        assert_restored(restore(run, valid, str(fourth / "elsewhere")))
        assert run.app.state.workspace.data_dir == demo_dir
        for relative, content in FILES.items():
            assert (demo_dir / relative).read_bytes() == content, relative
    assert not bootstrap_file(fourth).exists()
    assert not (fourth / "cfg").exists() or not list((fourth / "cfg").iterdir())
    assert not (fourth / "elsewhere").exists(), "a read-only folder ignores what was sent"


def test_setup_restore_refused_keeps_empty_workspace_open_and_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, valid: bytes
) -> None:
    """A refused file leaves the opened workspace open and empty, says so in a fixed plain
    message tied to the file field, and the same form works again with only the file."""
    from app.api import settings_data

    cases = {
        "bad_json": (422, source_export(tmp_path / "s1", rows=True, change="bad_json")),
        "newer_schema": (422, source_export(tmp_path / "s2", rows=True, change="newer_schema")),
        "sha_mismatch": (422, source_export(tmp_path / "s3", rows=True, change="sha_mismatch")),
        "oversize": (413, valid),
    }
    chosen = tmp_path / "chosen"
    with started(tmp_path / "ws", "default") as run:
        workspace = None
        content_messages: set[str] = set()
        for number, (case, (status, data)) in enumerate(cases.items()):
            if case == "oversize":
                monkeypatch.setattr(settings_data, "MAX_IMPORT_BYTES", len(data) - 1)
            answer = restore(run, data, str(chosen) if number == 0 else None)

            message = restore_refused(answer, status)
            if status == 422:
                content_messages.add(message)
                assert message == SETUP_WORDS["unreadable"], "setup says restore, not import"
            else:
                assert message == SETUP_WORDS["too_large"]
            # This attempt opened the folder: the hint must not send the person to --data-dir.
            hint = html.unescape(one(answer.text, "folder-fixed").text)
            assert hint == OPENED_HINT, hint
            assert "--data-dir" not in hint
            monkeypatch.setattr(settings_data, "MAX_IMPORT_BYTES", 200 * MIB)

            assert run.app.state.workspace is not None, case
            workspace = workspace or run.app.state.workspace
            assert run.app.state.workspace is workspace, "the same workspace stays open"
            assert workspace.data_dir == chosen
            assert (chosen / "app.db").is_file()
            assert upload_folders(chosen) == [], f"{case}: the upload was deleted"
            assert can_restore(run), f"{case}: the workspace is still empty"
            assert not any((chosen / name).exists() for name in FILES), case
            assert statuses(run).get("welcome") != "done", f"{case}: step 1 is not done"
            assert guide_step(run) == "welcome"

            page = run.client.get(WELCOME)
            field = one(page.text, "folder")
            assert "readonly" in field.attrs and field.attrs.get("value") == str(chosen)
            assert html.unescape(one(page.text, "folder-fixed").text) == OPENED_HINT
            assert one(page.text, "restore-form").tag == "form", "the form can be tried again"
        assert len(content_messages) == 1, f"one fixed message for a bad file: {content_messages}"

        retry = restore(run, valid)  # only the file
        assert_restored(retry)
        assert FACT_TEXT in json.dumps(workspace_state(run.app)["fact_versions"])
        assert upload_folders(chosen) == []


def test_setup_restore_refuses_folder_with_other_files_or_existing_workspace(
    tmp_path: Path, valid: bytes
) -> None:
    """Restore never goes into a non-empty folder: the Welcome step's messages, nothing opened,
    nothing written, the other folder untouched. An open workspace with content is refused too."""
    file_path = tmp_path / "a-file.txt"
    file_path.write_text("fictional\n", encoding="utf-8")
    foreign_sqlite = tmp_path / "other-app"
    foreign_sqlite.mkdir()
    import sqlite3

    connection = sqlite3.connect(foreign_sqlite / "app.db")
    try:
        connection.execute("CREATE TABLE fictional_notes (id INTEGER PRIMARY KEY, body TEXT)")
        connection.commit()
    finally:
        connection.close()
    existing = tmp_path / "existing-workspace"
    seed_workspace(existing, clock=FrozenClock(START))
    foreign = foreign_folder(tmp_path)
    reserved = tmp_path / "cfg"  # the config folder of the app below
    cases: dict[str, tuple[str, set[str]]] = {
        "relative": ("relative/job-data", {FOLDER_MESSAGES["relative"]}),
        "empty": ("", {FOLDER_MESSAGES["empty"]}),
        "file": (str(file_path), {FOLDER_MESSAGES["file"]}),
        "reserved": (str(reserved), {FOLDER_MESSAGES["reserved"]}),
        "other_files": (str(foreign), NON_EMPTY_MESSAGES),
        "foreign_database": (str(foreign_sqlite), NON_EMPTY_MESSAGES),
        "existing_workspace": (str(existing), NON_EMPTY_MESSAGES),
    }
    before = {name: digest(Path(p)) for name, (p, _) in cases.items() if Path(p).is_dir()}
    tree = listing(tmp_path)
    with started(tmp_path, "default") as run:
        tree = listing(tmp_path)
        for name, (folder, expected) in cases.items():
            answer = restore(run, valid, folder)
            folder_refused(answer, expected, folder if name not in ("empty",) else None)
            assert run.app.state.workspace is None, name
            assert listing(tmp_path) == tree, f"{name}: a refused folder creates nothing"
        missing = restore(run, valid, None)
        folder_refused(missing, {MSG_EMPTY}, None)
        assert {name: digest(Path(cases[name][0])) for name in before} == before
        assert not bootstrap_file(tmp_path).exists()
        assert run.decision.step == "welcome"

    # A workspace that is already open and holds content (or files) is never restored into.
    for what in ("content", "files"):
        root = tmp_path / f"open-{what}"
        with started(root, "cli", root / "explicit") as run:
            if what == "content":
                add_fact(run)
            else:
                plant(root / "explicit", {"imports/fictional-cv.txt": b"fictional existing file"})
            tree_before = (folder_tree(root / "explicit"), workspace_state(run.app))
            answer = restore(run, valid)

            restore_refused(answer, 409)
            assert (folder_tree(root / "explicit"), workspace_state(run.app)) == tree_before
            assert upload_folders(root / "explicit") == []
            assert statuses(run).get("welcome") != "done"


def test_setup_restore_only_while_guide_is_on_step_1(
    tmp_path: Path, finished: tuple[bytes, dict[str, Any]]
) -> None:
    raw, _ = finished
    elsewhere = tmp_path / "elsewhere"

    # Step 1 finished: the guide is at the provider step.
    with started(tmp_path / "a", "cli", tmp_path / "a" / "explicit") as run:
        mark_done(run, "welcome")
        assert guide_step(run) == "provider"
        before = (folder_tree(tmp_path / "a" / "explicit"), workspace_state(run.app))
        answer = restore(run, raw, str(elsewhere))
        assert answer.status_code == 409, answer.text[:300]
        assert "HX-Redirect" not in answer.headers
        assert MARKER not in answer.text and PRIVATE_NAME not in answer.text
        # base.html swaps a 409 into #setup-guide: the answer is the current step's guide, not
        # plain text that would wipe the guide from a stale tab.
        stale_guide_survives(run, answer, shows="provider")
        assert (folder_tree(tmp_path / "a" / "explicit"), workspace_state(run.app)) == before
        assert guide_step(run) == "provider", "the decision is unchanged"
        assert upload_folders(tmp_path / "a" / "explicit") == []
        provider_page = run.client.get("/setup/provider")
        assert provider_page.status_code == 200
        assert by_id(provider_page.text, "restore-form") is None
        assert by_id(provider_page.text, "setup-restore") is None

    # No guide at all: setup is finished.
    with finished_app(tmp_path / "b", empty=True) as run:
        data = tmp_path / "b" / "data"
        assert guide_step(run) is None
        before = (folder_tree(data), workspace_state(run.app))
        answer = restore(run, raw, str(elsewhere))
        assert answer.status_code == 409, answer.text[:300]
        assert "HX-Redirect" not in answer.headers
        assert by_id(answer.text, "setup-guide") is not None, "the guide is not wiped"
        assert by_id(answer.text, "restore-form") is None
        assert (folder_tree(data), workspace_state(run.app)) == before
        assert upload_folders(data) == []

    assert not elsewhere.exists(), "a refused request opens no folder"


def stale_guide_survives(run: Running, answer: Response, *, shows: str) -> None:
    """A refusal that htmx swaps into ``#setup-guide`` (409 is swapped) is the guide of the step
    the app is on now, with the reason announced; the restore form is not offered again."""
    guide = by_id(answer.text, "setup-guide")
    assert guide is not None, "the answer is the guide fragment"
    assert answer.headers["content-type"].startswith("text/html")
    current = [e for e in elements(answer.text) if e.attrs.get("aria-current") == "step"]
    title = run.app.state.setup_registry.get(shows).title
    assert [e.text.split(" · ")[0] for e in current] == [title]
    assert one(answer.text, "step-heading").within(guide)
    assert (
        by_id(answer.text, "restore-form") is None and by_id(answer.text, "setup-restore") is None
    )
    assert announced(answer) == NOT_HERE
    assert answer.headers["HX-Push-Url"] == f"/setup/{shows}"


def test_setup_restore_with_fixed_data_dir_needs_only_the_file(
    tmp_path: Path, valid: bytes
) -> None:
    explicit = tmp_path / "explicit"
    elsewhere = tmp_path / "elsewhere"
    with started(tmp_path, "cli", explicit) as run:
        workspace = run.app.state.workspace
        page = run.client.get(WELCOME)
        assert "readonly" in one(page.text, "folder").attrs
        assert html.unescape(one(page.text, "folder-fixed").text).startswith(
            "This folder was set when the app started"
        )
        assert one(page.text, "restore-form").tag == "form"

        # The file is what is missing: a message on the file field, nothing changes.
        for answer in (
            restore(run, None),
            restore(run, None, str(elsewhere)),
            post(run, RESTORE, {"folder": str(elsewhere)}),
        ):
            restore_refused(answer, 422)
            assert can_restore(run)
        assert not elsewhere.exists()

        # The folder part is ignored when the workspace is open; the file is restored into it.
        answer = restore(run, valid, str(elsewhere))

        assert_restored(answer)
        assert run.app.state.workspace is workspace, "an open workspace is never reopened"
        for relative, content in FILES.items():
            assert (explicit / relative).read_bytes() == content, relative
        assert upload_folders(explicit) == []
    assert not elsewhere.exists()
    assert bootstrap_folder(tmp_path) == explicit


@pytest.mark.parametrize(
    ("done", "literal"),
    [((), "welcome"), (("welcome", "provider"), "preferences"), (tuple(STEP_IDS), None)],
    ids=["nothing_done", "welcome_and_provider", "all_done"],
)
def test_setup_restore_refreshes_setup_decision_from_restored_state(
    tmp_path: Path, done: tuple[str, ...], literal: str | None
) -> None:
    """The restored ``setup_state`` decides which step shows (AC-M1.2): the restore itself does
    not mark step 1 done, and the answer is ``HX-Redirect: /`` whatever follows."""
    raw = export_with_steps(tmp_path / "source", done)
    expected = expected_guide_step(done)
    assert expected == literal, "the oracle agrees with the step written out by hand"
    with started(tmp_path / "target", "default") as run:
        assert guide_step(run) == "welcome"

        answer = restore(run, raw, str(tmp_path / "target" / "chosen"))

        assert_restored(answer)
        assert guide_step(run) == expected
        assert {k for k, v in statuses(run).items() if v == "done"} == set(done)
        home = run.client.get("/")
        if expected is None:
            assert home.status_code == 200
        else:
            assert home.status_code == 303 and home.headers["location"] == f"/setup/{expected}"
        assert run.client.get("/setup").headers["location"] == (f"/setup/{expected or STEP_IDS[0]}")


def expected_guide_step(done: tuple[str, ...]) -> str | None:
    """The step the guide shows for these finished steps (AC-M1.2, detection rule 3): the first
    REQUIRED step not done (skipping or leaving an optional step open never holds the guide
    back), else the Done step until it is finished; None once setup is finished."""
    statuses: dict[str, StepStatus] = {step: "done" for step in done}
    registry = m1_registry()
    unfinished = first_unfinished_required(registry.infos(), statuses)
    if unfinished is not None:
        return unfinished
    return None if registry.last.id in done else registry.last.id


def test_setup_restore_upload_removed_in_every_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, valid: bytes
) -> None:
    """The streamed upload lives in ``.restore-upload-*`` inside the data folder and is removed
    after a success, an unreadable file, a workspace with content and an oversize file."""
    from app.api import settings_data

    bad = source_export(tmp_path / "s", rows=True, change="bad_json")
    with started(tmp_path / "a", "default") as run:
        chosen = tmp_path / "a" / "chosen"
        restore_refused(restore(run, bad, str(chosen)), 422)
        assert upload_folders(chosen) == []
        monkeypatch.setattr(settings_data, "MAX_IMPORT_BYTES", len(valid) - 1)
        restore_refused(restore(run, valid), 413)
        assert upload_folders(chosen) == []
        monkeypatch.setattr(settings_data, "MAX_IMPORT_BYTES", 200 * MIB)
        restore_refused(restore(run, None), 422)
        assert upload_folders(chosen) == []
        assert_restored(restore(run, valid))
        assert upload_folders(chosen) == []
        assert [p for p in chosen.rglob("*") if ".restore-upload" in p.name] == []

    with started(tmp_path / "b", "cli", tmp_path / "b" / "explicit") as run:
        add_fact(run)
        restore_refused(restore(run, valid), 409)
        assert upload_folders(tmp_path / "b" / "explicit") == []


def folder_part(folder: str) -> bytes:
    head = f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="folder"\r\n\r\n'
    return f"{head}{folder}\r\n".encode()


def restore_stream(
    payload: Callable[[], AsyncIterator[bytes]], folder: str | None
) -> Callable[[Callable[[int], None]], AsyncIterator[bytes]]:
    async def source(count: Callable[[int], None]) -> AsyncIterator[bytes]:
        if folder is not None:
            first = folder_part(folder)
            count(len(first))
            yield first
        head = file_part_head(FilePart(PRIVATE_NAME, b"", name="file"))
        count(len(head))
        yield head
        async for chunk in payload():
            count(len(chunk))
            yield chunk
        tail = f"\r\n--{BOUNDARY}--\r\n".encode()
        count(len(tail))
        yield tail

    return source


def test_setup_restore_upload_streamed_capped_and_removed_when_stalled_or_over_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, valid: bytes
) -> None:
    """TD-033, spec 2.9 rule 5, through the setup route: the file part is written to disk as it
    arrives, a body over the cap is refused 413 after about the cap, a stalled stream is answered
    and the folder the first request opened stays open and empty for the next try."""
    from app.api import settings_data

    monkeypatch.setattr(settings_data, "MAX_IMPORT_BYTES", MIB)
    monkeypatch.setattr(settings_data, "READ_TIMEOUT_S", 0.3)
    root = tmp_path / "ws"
    chosen = root / "chosen"
    app = make_app(root, "default")
    chunk = 64 * 1024
    assert len(valid) < MIB

    async def zeros() -> AsyncIterator[bytes]:
        for _ in range(12 * MIB // chunk):
            yield b"0" * chunk

    async def stalls() -> AsyncIterator[bytes]:
        yield b'{"format": 1, "files": ['
        await asyncio.Event().wait()
        yield b""

    async def pieces() -> AsyncIterator[bytes]:
        for start in range(0, len(valid), 977):
            yield valid[start : start + 977]

    def on_disk(asked: int) -> list[str]:
        return leftovers(chosen) if asked == 8 and chosen.is_dir() else []

    async def scenario() -> dict[str, Any]:
        found: dict[str, Any] = {}
        async with app_lifespan(app):
            token = app.state.launch_token
            found["stalled"] = await drive(
                app, RESTORE, restore_stream(stalls, str(chosen)), token=token, deadline=10.0
            )
            found["after_stalled"] = (
                app.state.workspace.data_dir if app.state.workspace else None,
                leftovers(chosen),
                app.state.data_portability.can_restore(),
            )
            found["over"] = await drive(
                app, RESTORE, restore_stream(zeros, None), midway=on_disk, token=token
            )
            found["after_over"] = (
                leftovers(chosen),
                app.state.data_portability.can_restore(),
            )
            monkeypatch.setattr(settings_data, "MAX_IMPORT_BYTES", 200 * MIB)
            monkeypatch.setattr(settings_data, "READ_TIMEOUT_S", 5.0)
            found["valid"] = await drive(app, RESTORE, restore_stream(pieces, None), token=token)
            found["restored"] = {
                name: (chosen / name).read_bytes() for name in FILES if (chosen / name).is_file()
            }
            found["leftovers"] = leftovers(chosen)
        return found

    found = asyncio.run(scenario())

    stalled = found["stalled"]
    assert stalled.status in (408, 422), stalled.status
    assert (
        error_tied_to(stalled.body.decode(), "restore-file", "restore-error")
        == (SETUP_WORDS["stalled"])
    )
    assert found["after_stalled"] == (chosen, [], True), "the folder is open and empty"

    over = found["over"]
    assert over.status == 413, over.body[:200]
    assert over.delivered <= MIB + 3 * chunk + chunk, f"read {over.delivered} bytes of a 1 MiB cap"
    assert any(folders for folders in over.midway), "the upload is written under .restore-upload-*"
    assert (
        error_tied_to(over.body.decode(), "restore-file", "restore-error")
        == (SETUP_WORDS["too_large"])
    )
    assert found["after_over"] == ([], True)

    done = found["valid"]
    assert done.status == 200, done.body[:300]
    assert done.headers["hx-redirect"] == ["/"]
    assert found["restored"] == dict(FILES)
    assert found["leftovers"] == []


# --- the markup of step 1 ---------------------------------------------------------------------


def carries_folder(markup: str, form: Any) -> bool:
    """The restore form sends the Data folder field as ``folder``."""
    named = [e for e in inside(markup, form) if e.attrs.get("name") == "folder"]
    outside = [
        e
        for e in elements(markup)
        if e.attrs.get("name") == "folder" and e.attrs.get("form") == "restore-form"
    ]
    included = "#folder" in (form.attrs.get("hx-include") or "")
    return bool(named or outside or included)


def test_setup_welcome_page_lists_restore_section(tmp_path: Path) -> None:
    with started(tmp_path / "first", "default") as run:
        before = listing(tmp_path)
        full = run.client.get(WELCOME)
        fragment = run.client.get(WELCOME, headers=run.htmx())
        assert listing(tmp_path) == before, "reading the step writes nothing"
    with started(tmp_path / "fixed", "cli", tmp_path / "fixed" / "explicit") as run:
        fixed = run.client.get(WELCOME)
    assert full.status_code == fragment.status_code == fixed.status_code == 200

    # The intro asks for a folder above only when there is one to type; a fixed folder is "used".
    for answer in (full, fragment):
        intro = page_text(one(answer.text, "setup-restore").text)
        assert "Enter a new or empty data folder above" in intro
        assert "This data folder will be used" not in intro
    fixed_intro = page_text(one(fixed.text, "setup-restore").text)
    assert "This data folder will be used" in fixed_intro
    assert "Enter a new or empty data folder above" not in fixed_intro
    for answer in (full, fragment, fixed):
        intro = page_text(one(answer.text, "setup-restore").text)
        assert "You don't need to finish this step first; restoring takes its place." in intro

    for label, answer in {"full": full, "fragment": fragment, "fixed folder": fixed}.items():
        markup = answer.text
        section = one(markup, "setup-restore")
        assert section.tag == "section", label
        assert section.attrs.get("aria-labelledby") == "restore-heading"
        heading = one(markup, "restore-heading")
        assert heading.tag == "h3" and heading.text == "Restore from an export"
        assert heading.within(section)

        form = one(markup, "restore-form")
        assert form.within(section) and form.tag == "form"
        assert form.attrs.get("action") == RESTORE
        assert form.attrs.get("method", "").lower() == "post"
        assert form.attrs.get("enctype") == "multipart/form-data"
        assert form.attrs.get("hx-post") == RESTORE
        assert form.attrs.get("hx-target") == "#setup-guide"
        assert carries_folder(markup, form), "the folder typed above is sent with the file"

        field = one(markup, "restore-file")
        assert field.tag == "input" and field.within(form)
        assert field.attrs.get("type") == "file" and field.attrs.get("name") == "file"
        assert label_for(markup, field)
        button = one(markup, "restore-submit")
        assert button.tag == "button" and button.within(form)
        assert button.text.startswith("Restore")
        assert (button.attrs.get("type") or "submit") == "submit"
        assert "name" not in button.attrs or button.attrs.get("name") != "action"
        assert "disabled" not in button.attrs

        # The Welcome step itself is unchanged: one folder field and the one "open" button.
        actions = [
            e for e in elements(markup) if e.tag == "button" and e.attrs.get("name") == "action"
        ]
        assert [a.attrs.get("value") for a in actions] == ["open"], label
        assert len([e for e in elements(markup) if e.attrs.get("id") == "folder"]) == 1
        assert len([e for e in elements(markup) if e.attrs.get("id") == "step-heading"]) == 1


# --- refusals that come before the folder opens ------------------------------------------------


def refused_markup(markup: str, message: str, *, typed: str) -> None:
    """A refusal answered as the step: the message on the file field and the typed folder still in
    the folder field (still editable: nothing was opened)."""
    assert error_tied_to(markup, "restore-file", "restore-error") == message
    folder = one(markup, "folder")
    assert folder.attrs.get("value") == typed, "the typed folder is shown back"
    assert "readonly" not in folder.attrs
    assert by_id(markup, "restore-folder-note") is None
    assert [t.attrs.get("id") for t in focus_targets(markup)] == ["restore-file"]


def test_setup_restore_refusal_before_the_folder_opens_keeps_the_typed_folder(
    tmp_path: Path,
) -> None:
    """A file refused before it began (none chosen, an empty file field) answers with the folder
    the person typed, not the default; no folder was opened or made."""
    typed = str(tmp_path / "ws" / "typed-by-hand")
    with started(tmp_path / "ws", "default") as run:
        tree = listing(tmp_path)

        no_file = restore(run, None, typed)
        assert restore_refused(no_file, 422, typed=typed) == SETUP_WORDS["form"]

        nothing_chosen = restore(run, b"", typed, filename="")  # a browser's empty file field
        assert restore_refused(nothing_chosen, 422, typed=typed) == SETUP_WORDS["form"]

        assert run.app.state.workspace is None and not Path(typed).exists()
        assert listing(tmp_path) == tree


def test_setup_restore_declared_length_over_the_cap_is_refused_with_the_typed_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Content-Length past the cap is refused 413 as soon as the folder part and the file's
    headers have been read (a few bytes, not the file): the folder is not opened and the typed
    folder is shown back."""
    from app.api import settings_data

    monkeypatch.setattr(settings_data, "MAX_IMPORT_BYTES", MIB)
    root = tmp_path / "ws"
    typed = str(root / "typed-by-hand")
    app = make_app(root, "default")

    async def small() -> AsyncIterator[bytes]:
        yield b"{}"

    async def scenario() -> tuple[Any, Any]:
        async with app_lifespan(app):
            driven = await drive(
                app,
                RESTORE,
                restore_stream(small, typed),
                token=app.state.launch_token,
                headers={"content-length": str(50 * MIB)},
            )
            return driven, app.state.workspace

    answered, workspace = asyncio.run(scenario())

    assert answered.status == 413, answered.body[:200]
    assert answered.delivered <= 8 * 1024, "only the start of the form was read"
    refused_markup(answered.body.decode(), SETUP_WORDS["too_large"], typed=typed)
    assert workspace is None and not Path(typed).exists()


def test_setup_restore_allows_one_restore_at_a_time_and_releases_the_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, valid: bytes
) -> None:
    """A second restore while one is being received (here: the first has sent its folder and
    stalls) is answered 409 with the typed folder and opens nothing; the slot is free again when
    the first one ends, and the same app then restores."""
    from app.api import settings_data

    monkeypatch.setattr(settings_data, "READ_TIMEOUT_S", 1.0)
    root = tmp_path / "ws"
    first_folder = str(root / "first")
    typed = str(root / "second-typed")
    app = make_app(root, "default")

    async def scenario() -> dict[str, Any]:
        found: dict[str, Any] = {}
        started = asyncio.Event()

        async def holds(count: Callable[[int], None]) -> AsyncIterator[bytes]:
            part = folder_part(first_folder)
            count(len(part))
            yield part
            started.set()  # the app asked for more, so it holds the slot
            await asyncio.Event().wait()
            yield b""

        async def pieces() -> AsyncIterator[bytes]:
            for start in range(0, len(valid), 4096):
                yield valid[start : start + 4096]

        async with app_lifespan(app):
            token = app.state.launch_token
            first = asyncio.ensure_future(drive(app, RESTORE, holds, token=token, deadline=10.0))
            await asyncio.wait_for(started.wait(), 10.0)
            found["held"] = app.state.import_active
            found["second"] = await drive(app, RESTORE, restore_stream(pieces, typed), token=token)
            found["workspace_during"] = app.state.workspace
            found["first"] = await first  # stalls out and ends
            found["held_after"] = app.state.import_active
            found["third"] = await drive(
                app, RESTORE, restore_stream(pieces, first_folder), token=token
            )
            found["restored"] = (Path(first_folder) / next(iter(FILES))).is_file()
        return found

    found = asyncio.run(scenario())

    assert found["held"] is True
    second = found["second"]
    assert second.status == 409, second.body[:200]
    refused_markup(second.body.decode(), SETUP_WORDS["busy"], typed=typed)
    assert found["workspace_during"] is None and not Path(typed).exists()
    assert found["first"].status == 422
    assert found["held_after"] is False, "the slot is released when the first one ends"
    assert found["third"].status == 200, found["third"].body[:300]
    assert found["restored"]


def test_setup_restore_shares_its_slot_with_the_settings_import(
    tmp_path: Path, valid: bytes
) -> None:
    """The slot is the app's (``app.state.import_active``), so a restore while an import runs is
    refused with the setup words; it is initialised before any route is used."""
    with started(tmp_path, "default") as run:
        assert run.app.state.import_active is False
        run.app.state.import_active = True
        typed = str(tmp_path / "typed")
        answer = restore(run, valid, typed)

        assert restore_refused(answer, 409, typed=typed) == SETUP_WORDS["busy"]
        assert run.app.state.workspace is None and run.app.state.import_active is True
        run.app.state.import_active = False
        assert_restored(restore(run, valid, typed))


# --- what is said when the restore succeeded but a later step failed -------------------------


def test_setup_restore_config_write_failure_still_redirects_and_says_so(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    valid: bytes,
) -> None:
    caplog.set_level(logging.DEBUG)
    chosen = tmp_path / "chosen"
    with started(tmp_path, "default") as run:

        def broken(folder: Path) -> None:
            raise OSError(f"disk is read-only at {folder}")

        monkeypatch.setattr(run.app.state.setup_handlers["welcome"], "_write_bootstrap", broken)

        answer = restore(run, valid, str(chosen))

        assert answer.status_code == 200 and answer.headers["HX-Redirect"] == "/"
        assert announced(answer) == SETUP_WORDS["not_saved"]
        assert not bootstrap_file(tmp_path).exists()
        for relative, content in FILES.items():
            assert (chosen / relative).read_bytes() == content, "the data is restored"
        ours = " ".join(r.getMessage() for r in caplog.records if r.name.startswith("app"))
        assert "OSError" in ours and str(chosen) not in ours


def test_setup_restore_refresh_failure_says_to_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    valid: bytes,
) -> None:
    caplog.set_level(logging.DEBUG)
    chosen = tmp_path / "chosen"
    with started(tmp_path, "default") as run:

        def broken() -> None:
            raise RuntimeError(f"cannot refresh at {chosen}")

        monkeypatch.setattr(run.app.state, "workspace_restored", broken)

        answer = restore(run, valid, str(chosen))

        assert answer.status_code == 200 and answer.headers["HX-Redirect"] == "/"
        assert announced(answer) == SETUP_WORDS["restart"]
        for relative, content in FILES.items():
            assert (chosen / relative).read_bytes() == content, "the data is restored"
        assert bootstrap_folder(tmp_path) == chosen, "the folder was still named"
        ours = " ".join(r.getMessage() for r in caplog.records if r.name.startswith("app"))
        assert "RuntimeError" in ours and str(chosen) not in ours
        assert all(r.exc_info is None for r in caplog.records if r.name.startswith("app"))


def test_setup_restore_upload_folder_failure_is_the_storage_message_and_keeps_the_folder_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    valid: bytes,
) -> None:
    from app.api import settings_data

    caplog.set_level(logging.DEBUG)
    chosen = tmp_path / "chosen"
    with started(tmp_path, "default") as run:

        def no_room(root: Path) -> Path:
            raise OSError(28, f"No space left on device at {root}")

        with monkeypatch.context() as patched:
            patched.setattr(settings_data, "make_upload_folder", no_room)
            answer = restore(run, valid, str(chosen))

        assert restore_refused(answer, 422) == SETUP_WORDS["storage"]
        assert run.app.state.workspace is not None and can_restore(run)
        assert upload_folders(chosen) == []
        ours = " ".join(r.getMessage() for r in caplog.records if r.name.startswith("app"))
        assert "OSError" in ours and "No space left" not in ours and str(chosen) not in ours

        assert_restored(restore(run, valid))  # the same form works again


# --- the refusal words are setup's own ----------------------------------------------------------


def test_setup_restore_words_have_content_unreadable_and_stalled(
    tmp_path: Path, valid: bytes
) -> None:
    with started(tmp_path / "a", "cli", tmp_path / "a" / "explicit") as run:
        add_fact(run)
        has_content = restore_refused(restore(run, valid), 409)
        assert has_content == SETUP_WORDS["has_content"]

    bad = source_export(tmp_path / "s", rows=True, change="bad_json")
    with started(tmp_path / "b", "default") as run:
        assert (
            restore_refused(restore(run, bad, str(tmp_path / "b" / "c")), 422)
            == (SETUP_WORDS["unreadable"])
        )
    for key, words in SETUP_WORDS.items():
        if key != "busy":  # the slot is shared with Settings' import, which the busy words name
            assert "import" not in words.lower(), key


# --- an oversized part header is refused (TD-033) ----------------------------------------------


@pytest.mark.parametrize("where", ["name", "value"])
def test_setup_restore_refuses_an_oversized_part_header(
    tmp_path: Path, valid: bytes, where: str
) -> None:
    pad = "a" * 20_000
    header = f"X-{pad}: x" if where == "name" else f"X-Pad: {pad}"
    head = (
        f"--{BOUNDARY}\r\n"
        'Content-Disposition: form-data; name="file"; filename="export.json"\r\n'
        f"{header}\r\n\r\n"
    ).encode()
    body = head + valid + f"\r\n--{BOUNDARY}--\r\n".encode()
    with started(tmp_path, "cli", tmp_path / "explicit") as run:
        before = folder_tree(tmp_path / "explicit")

        answer = post(run, RESTORE, content=body, content_type=CONTENT_TYPE)

        assert restore_refused(answer, 422) == SETUP_WORDS["form"]
        assert can_restore(run) and folder_tree(tmp_path / "explicit") == before
        assert upload_folders(tmp_path / "explicit") == []
