"""Shared helpers for the M9.1 part D1 tests of Settings > Data and privacy (export, import and
delete all data; AC-M9.3; ADR-131 items 2-4; spec 2.9 rules 5, 9, 12, 13 and 3.8; TD-033). Every
value is fictional.

The module builds throwaway apps in temporary folders (``prepare``, ``make_app``, ``running``),
sends requests the way the page does (``post``, ``upload``, ``get_token``), reads what the
workspace and the data folder hold (``workspace_state``, ``folder_tree``, ``source_export``),
reads answers as markup (``one``, ``inside``, ``focus_targets``, ``error_tied_to``,
``import_is_blocked``) and drives the app with a raw, slow or endless request body
(``drive``, ``multipart_stream``). The routes under test are in ``app/api/settings_data.py`` and
``app/api/restore_upload.py``; the tests that use these helpers are
``tests/integration/test_settings_data_routes.py`` and ``tests/e2e/test_settings_data.py``. No
D1 module is imported at module level, so a missing route or template fails the test that needs
it, not the collection.

What the tests rely on (the contract; the routes and templates implement it)

- Only ``GET`` and ``HEAD /settings/data`` is a page; it is token-free and read only, and the data
  folder's path is never in it (``GET /settings/data/location`` is a protected fragment loaded on
  ``load``). The delete dialog is a protected fragment (``GET /settings/data/delete``). Export,
  import and delete are POSTs that need the token and the origin; a download needs the token and
  serves only a generated export name.
- Sections ``#data-export``, ``#data-import`` and ``#data-delete`` are swapped whole by their
  answers, each with exactly one ``data-autofocus`` element and, where the outcome is a result,
  an ``HX-Trigger`` ``announce``.
- Import is a blocked action while the workspace has content: ``#data-import-submit`` is
  ``aria-disabled`` (not ``disabled``), ``#data-import-note`` explains it, and the file field is
  not shown. A refusal answers the section again with ``#data-import-error`` (``role="alert"``)
  that echoes nothing of the upload; with the file field shown, the field is ``aria-invalid`` and
  names the error.
- Delete success answers ``HX-Redirect: /``; a refusal answers the dialog again with
  ``#data-delete-error``.
"""

import asyncio
import html
import json
import re
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import __version__
from app.api.security import TOKEN_HEADER
from app.config import RuntimeConfig, write_bootstrap
from app.domain.redaction import Redactor
from app.main import create_app
from app.modules.storage.portability import export_tables
from app.onboarding.registry import m1_registry
from app.wiring import build_wiring
from tests.asgi_stream import http_scope
from tests.board_pages import by_id, elements
from tests.fakes.clock import FrozenClock
from tests.fakes.keyring_backends import MemoryKeyring
from tests.integration.test_app_shell import Element
from tests.integration.test_setup_redirect import BASE_URL, ORIGIN, PORT, START, Running
from tests.setup_harness import complete_setup, seed_workspace
from tests.upload_bodies import CONTENT_TYPE, FilePart, closing, file_part_head, multipart_body

PAGE = "/settings/data"
LOCATION = f"{PAGE}/location"
EXPORT = f"{PAGE}/export"
IMPORT = f"{PAGE}/import"
DELETE = f"{PAGE}/delete"
INVALID = "This request isn't valid."
EXPORT_NAME = re.compile(r"jobpipeline-export-[0-9]{8}T[0-9]{12}Z(?:-[1-9][0-9]*)?\.json")
VOLATILE = frozenset({"tasks", "usage_records"})  # the runner writes these while a test reads
SQLITE_FILES = ("app.db", "app.db-wal", "app.db-shm", ".lock")
UPLOAD_FOLDER = ".restore-upload-"
MIB = 1024 * 1024
# The words of the page (spec 3.10: the copy belongs to the screen; tests pin it).
IMPORT_BLOCKED_NOTE = "Import only works in a workspace with no content. This one already has some."
NOTHING_IMPORTED = "Nothing was imported."
IMPORT_BUSY = "Another import is still running. Wait for it to finish, then try again."
FILE_NOT_SAVED = "The file could not be saved. Check that the disk has space, then try again."
UPLOAD_TOO_SLOW = "The upload took too long. Nothing was imported. Try again."
ERASED_NOTICE = "All data was deleted. You're at the start of setup again."
ERASED_EVENT = "data:erased"

# Fictional markers: planted in a fact, searched for in every place the text must never reach.
MARKER = "fictional-kestrel-harbour-survey-5521"
FACT_TEXT = f"Coordinated the {MARKER} with 12 volunteers."
# Names and contents of files the app keeps (valid names: documents/<id>/<file>, assets/,
# imports/, prompts/overrides/<task>/<file>).
FILES = {
    "documents/app-example/export-0001.pdf": b"Fictional PDF bytes\x00\xff",
    "assets/photo-fictional.png": b"Fictional photo bytes\x01",
    "imports/fictional-cv.txt": b"Fictional CV text for the restore",
    "prompts/overrides/refine_fact/prompt.md": b"Fictional override bytes",
}
# Entries the app owns but an export never holds: erased, never restored.
OTHER_ENTRIES = {
    "backups/app-20260101T000000Z.db": b"fictional backup copy",
    "exports/jobpipeline-export-20260103T090000000000Z.json": b'{"fictional": true}',
    "logs/app.log": b"fictional log line",
}


# --- the app ---------------------------------------------------------------------------------


def prepare(root: Path, *, empty: bool) -> Path:
    """``root/data`` and ``root/cfg`` with a finished setup. ``empty`` keeps the workspace free of
    content (no saved provider), so a restore is allowed; otherwise the fake provider is saved."""
    data = root / "data"
    if empty:
        write_bootstrap(root / "cfg", data_dir=data, version=__version__)
        seed_workspace(
            data, done=[step.id for step in m1_registry().steps], clock=FrozenClock(START)
        )
    else:
        complete_setup(data, root / "cfg", clock=FrozenClock(START))
    return data


def make_app(root: Path, *, backend: MemoryKeyring | None = None) -> FastAPI:
    config = RuntimeConfig(
        data_dir=root / "data",
        config_dir=root / "cfg",
        port=PORT,
        log_level="INFO",
        demo=False,
        setup=False,
        no_browser=False,
        data_dir_source="bootstrap",
    )
    wiring = build_wiring(
        environ={"JOBPIPELINE_ALLOW_FAKE_PROVIDERS": "1"},
        demo=False,
        redactor=Redactor(),
        keyring_backend=backend if backend is not None else MemoryKeyring(),
        clock=FrozenClock(START),
        demo_workspace=False,
    )
    return create_app(config, clock=FrozenClock(START), wiring=wiring)


@contextmanager
def running(
    root: Path, *, empty: bool, backend: MemoryKeyring | None = None, files: bool = False
) -> Iterator[Running]:
    """The app on a finished setup under ``root``. ``files`` plants the app's files and the
    entries an export leaves out (backups, exports, logs), once the app is open."""
    data = prepare(root, empty=empty)
    app = make_app(root, backend=backend)
    with TestClient(app, base_url=BASE_URL, follow_redirects=False) as client:
        if files:  # after the open: the app sweeps orphan imports and photos when it opens
            plant(data, {**FILES, **OTHER_ENTRIES})
        yield Running(app=app, client=client, hits=Counter())


def plant(data: Path, files: dict[str, bytes]) -> None:
    for relative, content in files.items():
        path = data / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def add_fact(run: Running, text: str = FACT_TEXT) -> str:
    from app.services.facts import FactInput

    return str(run.app.state.facts.create(FactInput(text=text)).id)


# --- requests --------------------------------------------------------------------------------


def post(
    run: Running,
    path: str,
    data: dict[str, Any] | None = None,
    *,
    token: bool = True,
    origin: str | bool = True,
    htmx: bool = True,
    content: bytes | None = None,
    content_type: str | None = None,
) -> Any:
    headers: dict[str, str] = {}
    if token:
        headers[TOKEN_HEADER] = run.token
    if origin is True:
        headers["origin"] = ORIGIN
    elif isinstance(origin, str):
        headers["origin"] = origin
    if htmx:
        headers["HX-Request"] = "true"
    if content_type is not None:
        headers["Content-Type"] = content_type
    if content is not None:
        return run.client.post(path, content=content, headers=headers)
    return run.client.post(path, data=data, headers=headers)


def upload(
    run: Running,
    data: bytes,
    *,
    filename: str = "export.json",
    token: bool = True,
    origin: str | bool = True,
) -> Any:
    body = multipart_body(FilePart(filename, data, name="file"))
    return post(run, IMPORT, token=token, origin=origin, content=body, content_type=CONTENT_TYPE)


def get_token(run: Running, path: str, *, htmx: bool = False) -> Any:
    return run.client.get(path, headers=run.htmx() if htmx else run.with_token())


# --- what the workspace holds ------------------------------------------------------------------


def folder_tree(data: Path) -> dict[str, int | None]:
    """Every entry under the data folder (directories as None, files by size), without the
    database's own files."""
    found: dict[str, int | None] = {}
    for path in sorted(data.rglob("*")):
        relative = path.relative_to(data).as_posix()
        if relative in SQLITE_FILES:
            continue
        found[relative] = None if path.is_dir() else path.lstat().st_size
    return found


def workspace_state(app: FastAPI) -> dict[str, Any]:
    snapshot = export_tables(app.state.workspace.db, include_usage_snapshots=True)
    return {name: table for name, table in snapshot["tables"].items() if name not in VOLATILE}


def state_of(run: Running, data: Path) -> tuple[dict[str, int | None], dict[str, Any]]:
    return folder_tree(data), workspace_state(run.app)


def upload_folders(data: Path) -> list[str]:
    return sorted(p.name for p in data.iterdir() if p.name.startswith(UPLOAD_FOLDER))


# --- export files for the import tests -----------------------------------------------------------


def source_export(root: Path, *, rows: bool, change: str | None = None) -> bytes:
    """A valid export of a fictional source workspace (``rows``: a seeded database; otherwise
    only the app's files), changed one way for a refusal test. Content never holds ``MARKER``
    except through ``change`` (the refusal tests look for it in the answers)."""
    from tests.integration.test_data_portability import service
    from tests.integration.test_portability import fresh, seed

    with fresh(root) as source:
        if rows:
            seed(source)
        plant(source.path.parent, {**FILES, **OTHER_ENTRIES})
        archive = service(source).export()
    raw = archive.read_bytes()
    if change is None:
        return raw
    if change == "bad_json":
        return b'{"note": "' + MARKER.encode() + b'", ' + raw[1:60]
    payload = json.loads(raw)
    if change == "not_an_export":
        payload = {"hello": MARKER}
    elif change == "unknown_top_level":
        payload["note"] = MARKER
    elif change == "unknown_table":
        payload["tables"]["unexpected"] = {"columns": [], "rows": []}
    elif change == "sha_mismatch":
        payload["files"][0]["sha256"] = "0" * 64
    elif change == "unsafe_file_name":
        payload["files"][0]["name"] = "../outside.txt"
    elif change == "newer_schema":
        payload["schema_version"] = 10**6
    else:
        raise AssertionError(f"unknown change {change}")
    return json.dumps(payload).encode()


# --- reading answers ------------------------------------------------------------


def page_text(markup: str) -> str:
    """The visible words of ``markup`` (tags dropped, entities decoded, spaces collapsed)."""
    plain = re.sub(r"<(script|style)\b.*?</\1>", " ", markup, flags=re.DOTALL | re.IGNORECASE)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", plain)).split())


def section(markup: str, section_id: str) -> Element:
    found = by_id(markup, section_id)
    assert found is not None, f"no #{section_id} in the answer"
    return found


def inside(markup: str, container: Element) -> list[Element]:
    return [e for e in elements(markup) if e.within(container)]


def one(markup: str, element_id: str) -> Element:
    return section(markup, element_id)


def focus_targets(markup: str) -> list[Element]:
    return [e for e in elements(markup) if "data-autofocus" in e.attrs]


def announced(response: Any) -> str:
    raw = response.headers.get("HX-Trigger")
    assert raw, "the answer has no HX-Trigger announcement"
    value = json.loads(raw)
    assert isinstance(value, dict) and isinstance(value.get("announce"), str) and value["announce"]
    return str(value["announce"])


def download_button(markup: str) -> Element | None:
    found = [e for e in elements(markup) if "data-file-download" in e.attrs]
    assert len(found) <= 1, "more than one download button"
    return found[0] if found else None


def generated_name(markup: str) -> str:
    names = EXPORT_NAME.findall(page_text(markup))
    full = [m.group(0) for m in EXPORT_NAME.finditer(page_text(markup))]
    assert len(set(full)) == 1, f"expected one generated file name in the answer, got {names}"
    return full[0]


def label_for(markup: str, control: Element) -> str:
    control_id = control.attrs.get("id")
    assert control_id, "the control has no id for its label"
    labels = [e for e in elements(markup) if e.tag == "label" and e.attrs.get("for") == control_id]
    assert len(labels) == 1, f"expected one label for #{control_id}"
    return labels[0].text


def error_tied_to(markup: str, control_id: str, error_id: str) -> str:
    """The text of ``#error_id`` after checking the control is marked invalid and names it."""
    control = section(markup, control_id)
    assert control.attrs.get("aria-invalid") == "true", f"#{control_id} is not aria-invalid"
    described = (control.attrs.get("aria-describedby") or "").split()
    assert error_id in described, f"#{control_id} does not name #{error_id}"
    error = section(markup, error_id)
    assert error.attrs.get("role") == "alert", f"#{error_id} is not role=alert"
    assert error.text, f"#{error_id} is empty"
    return error.text


def import_is_blocked(markup: str) -> bool:
    """The Import button is a blocked action (aria-disabled, still focusable, explained) and,
    while it is blocked, there is no file field to fill in."""
    button = section(markup, "data-import-submit")
    blocked = button.attrs.get("aria-disabled") == "true"
    if blocked:
        assert "disabled" not in button.attrs, "a blocked action must stay focusable"
        assert button.attrs.get("aria-describedby") == "data-import-note"
        note = section(markup, "data-import-note")
        assert re.search(r"no content|empty", note.text, re.IGNORECASE), note.text
        assert by_id(markup, "data-import-file") is None, "a blocked Import shows no file field"
    else:
        assert "disabled" not in button.attrs
        assert by_id(markup, "data-import-note") is None
        assert section(markup, "data-import-file").attrs.get("type") == "file"
    return blocked


# --- driving the app with a raw, slow or endless request body ---------------------------------


@dataclass
class Driven:
    status: int = 0
    headers: dict[str, list[str]] = field(default_factory=dict)
    body: bytes = b""
    offered: int = 0  # bytes the test's body source produced
    delivered: int = 0  # bytes the app took with receive()
    midway: list[Any] = field(default_factory=list)


async def drive(
    app: FastAPI,
    path: str,
    source: Callable[[Callable[[int], None]], AsyncIterator[bytes]],
    *,
    token: str,
    deadline: float = 20.0,
    midway: Callable[[int], Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Driven:
    """POST a multipart body to the running app (its lifespan already started) as a server
    would: the body comes from ``source`` one chunk per ``receive()``, with no
    ``Content-Length`` (a chunked upload). ``source`` may stall for ever: the app is
    expected to answer on its own (a read timeout). ``midway(n)`` is called when the app asks
    for chunk number ``n``, to look at the data folder while the upload is on its way.
    ``headers`` adds or replaces request headers (for example a declared ``Content-Length``)."""
    result = Driven()
    scope = http_scope(
        path,
        port=PORT,
        method="POST",
        headers={
            TOKEN_HEADER: token,
            "origin": ORIGIN,
            "hx-request": "true",
            "content-type": CONTENT_TYPE,
            **(headers or {}),
        },
    )
    finished = asyncio.Event()
    body = bytearray()
    asked = 0

    def count(size: int) -> None:
        result.offered += size

    chunks = source(count).__aiter__()
    ended = False

    async def receive() -> dict[str, Any]:
        nonlocal asked, ended
        if finished.is_set():
            return {"type": "http.disconnect"}
        if ended:
            await finished.wait()
            return {"type": "http.disconnect"}
        asked += 1
        if midway is not None:
            result.midway.append(midway(asked))
        pending = asyncio.ensure_future(chunks.__anext__())
        stop = asyncio.ensure_future(finished.wait())
        try:
            done, _ = await asyncio.wait({pending, stop}, return_when=asyncio.FIRST_COMPLETED)
            if pending not in done:
                return {"type": "http.disconnect"}
            try:
                chunk = pending.result()
            except StopAsyncIteration:
                ended = True
                return {"type": "http.request", "body": b"", "more_body": False}
            result.delivered += len(chunk)
            return {"type": "http.request", "body": chunk, "more_body": True}
        finally:
            for future in (pending, stop):
                if not future.done():
                    future.cancel()

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            result.status = int(message["status"])
            for name, value in message.get("headers", []):
                result.headers.setdefault(bytes(name).decode("latin-1").lower(), []).append(
                    bytes(value).decode("latin-1")
                )
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                finished.set()

    task = asyncio.ensure_future(app(scope, receive, send))
    done, _ = await asyncio.wait({task}, timeout=deadline)
    if not done:
        task.cancel()
        await asyncio.wait({task}, timeout=2)
        raise AssertionError(f"POST {path}: the app did not answer within {deadline} s")
    task.result()
    result.body = bytes(body)
    return result


def multipart_stream(
    payload: Callable[[], AsyncIterator[bytes]], *, filename: str = "export.json"
) -> Callable[[Callable[[int], None]], AsyncIterator[bytes]]:
    """A body source: the file part's head, the chunks of ``payload``, the closing boundary."""

    async def source(count: Callable[[int], None]) -> AsyncIterator[bytes]:
        head = file_part_head(FilePart(filename, b"", name="file"))
        count(len(head))
        yield head
        async for chunk in payload():
            count(len(chunk))
            yield chunk
        tail = closing()
        count(len(tail))
        yield tail

    return source
