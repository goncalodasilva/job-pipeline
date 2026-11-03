"""Settings > Data and privacy: export, import and delete all data (M9.1 part D; AC-M9.3;
ADR-131 items 2-4; spec 2.9 rules 5, 9, 12 and 13; TD-033).

Only ``GET`` and ``HEAD /settings/data`` is a page (``PAGE_TAG``): token-free, read only, and the
data folder's path is never in it. Everything else needs the launch token, and every POST also
the origin:

- ``GET /settings/data/location`` (fragment, loaded by the page on ``load``): the folder's path.
- ``GET /settings/data/delete`` (fragment): the confirmation dialog, opened by app.js.
- ``POST /settings/data/export`` (optional ``snapshots=1``; any other field or value is the
  plain-text 400): writes the export file and answers the Export block with its generated name
  and a Download button. ``GET /settings/data/exports/{name}`` downloads it; only a generated
  name that is a real file of ours is served, every other request gets the same fixed 404.
- ``POST /settings/data/import`` (multipart, one file part named ``file``): the body is streamed
  to ``.restore-upload-*`` in the data folder (``restore_upload``) under ``MAX_IMPORT_BYTES``,
  ``READ_TIMEOUT_S`` (per wait) and ``UPLOAD_DEADLINE_S`` (whole upload), restored in a worker
  thread and deleted in every outcome. One import at a time: a second one is a 409 that reads
  nothing. A refusal answers the Import block with a fixed message that echoes nothing of the
  file: 409 (the workspace has content, or another import is running), 413 (too large), 422
  (unreadable file, stalled or too slow an upload, a disk that could not take the file, anything
  else; an ``OSError`` is logged as its class name only). A success refreshes what the app derived
  from the workspace (the setup decision, provider health).
- ``POST /settings/data/delete`` (``confirm``): erases in a worker thread. A wrong word is a 422
  and an erase that stopped or was refused a 409, both with the dialog again and the error's
  ``user_message``; success answers ``HX-Redirect: /`` (the app is at first run) and the
  ``data:erased`` event, which app.js turns into a one-visit note on the setup page (the setup
  redirect drops any query string, ADR-050).

The services block (files, SQLite, the keychain), so every call to them runs in a worker thread.
Both module constants are read at request time. ``app.state.import_active`` (one import at a time,
Settings and setup together) is set by ``create_app``.
"""

import json
import logging
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.errors import INVALID_REQUEST
from app.api.forms import single_value_form
from app.api.pages import AREAS, area
from app.api.restore_upload import (
    ENVELOPE_BYTES,
    DirectoryFor,
    UploadRefused,
    boundary_of,
    save_upload,
)
from app.api.security import PAGE_TAG
from app.api.settings_nav import AREA_FRAGMENT, AREA_PAGE, area_ctx
from app.api.templating import render, templates
from app.services.data_erasure import (
    CONFIRMATION_WORD,
    DataErasureService,
    ErasureIncomplete,
    ErasureRefused,
)
from app.services.data_portability import DataPortabilityService, PortabilityError

_log = logging.getLogger(__name__)

PAGE_PATH: Final = "/settings/data"
LOCATION_PATH: Final = f"{PAGE_PATH}/location"
EXPORT_PATH: Final = f"{PAGE_PATH}/export"
IMPORT_PATH: Final = f"{PAGE_PATH}/import"
DELETE_PATH: Final = f"{PAGE_PATH}/delete"
DOWNLOAD_PATH: Final = f"{PAGE_PATH}/exports/{{name}}"
EXPORT_FRAGMENT: Final = "fragments/settings/_data_export.html"
IMPORT_FRAGMENT: Final = "fragments/settings/_data_import.html"
LOCATION_FRAGMENT: Final = "fragments/settings/_data_location.html"
DELETE_FRAGMENT: Final = "fragments/settings/_data_delete_dialog.html"

# Module constants, read at request time (tests change them).
MAX_IMPORT_BYTES = 200 * 1024 * 1024
READ_TIMEOUT_S = 30.0
UPLOAD_DEADLINE_S = 15 * 60.0  # the whole upload, however steadily the bytes trickle in

UPLOAD_PREFIX: Final = ".restore-upload-"
LISTED_UNKNOWN: Final = 20  # entries the dialog names; the rest are counted

EXPORT_NOT_FOUND: Final = "That export wasn't found."
MSG_EXPORTED: Final = "Export ready."
MSG_RESTORED: Final = "Restored. Your data and files are back."
MSG_RESTORED_RESTART: Final = (
    "Restored. Restart the app to finish: it couldn't refresh its setup state by itself."
)
# The page's note says why a restore is blocked; this answer only says that nothing happened.
MSG_NOTHING_IMPORTED: Final = "Nothing was imported."
MSG_TOO_LARGE: Final = (
    "That file is too large. An export can be up to 200 MB. Nothing was imported."
)
MSG_CHOOSE: Final = "Choose one export file to import. Nothing was imported."
MSG_STALLED: Final = "The upload stopped before it finished. Nothing was imported. Try again."
MSG_TOO_SLOW: Final = "The upload took too long. Nothing was imported. Try again."
MSG_NOT_SAVED: Final = "The file could not be saved. Check that the disk has space, then try again."
MSG_BUSY: Final = "Another import is still running. Wait for it to finish, then try again."
MSG_UNREADABLE: Final = (
    "This file can't be restored. It may not be a Job Pipeline export, may be damaged, "
    "or may come from a newer version of the app. Nothing was imported."
)
MSG_NO_WORKSPACE: Final = "No workspace is open, so there is nothing to import into."

UPLOAD_REFUSALS: Final[dict[str, tuple[int, str]]] = {
    "form": (422, MSG_CHOOSE),
    "too_large": (413, MSG_TOO_LARGE),
    "stalled": (422, MSG_STALLED),
    "too_slow": (422, MSG_TOO_SLOW),
    "storage": (422, MSG_NOT_SAVED),
}
ERASED_EVENT: Final = "data:erased"


def register_data_routes(app: FastAPI) -> None:
    """Add the routes directly to the app; only the page is token-free."""
    app.add_api_route(
        PAGE_PATH,
        data_page,
        methods=["GET", "HEAD"],
        tags=[PAGE_TAG],
        response_class=HTMLResponse,
        name="page_settings_data",
    )
    app.add_api_route(LOCATION_PATH, location, methods=["GET"], response_class=HTMLResponse)
    app.add_api_route(DELETE_PATH, delete_dialog, methods=["GET"], response_class=HTMLResponse)
    app.add_api_route(DELETE_PATH, delete_data, methods=["POST"], response_class=HTMLResponse)
    app.add_api_route(EXPORT_PATH, export_data, methods=["POST"], response_class=HTMLResponse)
    app.add_api_route(IMPORT_PATH, import_data, methods=["POST"], response_class=HTMLResponse)
    app.add_api_route(DOWNLOAD_PATH, download_export, methods=["GET"])


def _portability(request: Request) -> DataPortabilityService:
    service: DataPortabilityService = request.app.state.data_portability
    return service


def _erasure(request: Request) -> DataErasureService:
    service: DataErasureService = request.app.state.data_erasure
    return service


def _announce(response: Response, words: str) -> None:
    response.headers["HX-Trigger"] = json.dumps({"announce": words})


def _fragment(
    request: Request, template: str, ctx: dict[str, Any], *, status: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(request, template, ctx, status_code=status)


# --- the page and its fragments ---------------------------------------------------------------


def export_context(*, name: str | None = None, error: str | None = None) -> dict[str, Any]:
    return {"name": name, "error": error}


def import_context(
    *, blocked: bool, done: str | None = None, error: str | None = None
) -> dict[str, Any]:
    return {"blocked": blocked, "done": done, "error": error}


def data_page(request: Request) -> HTMLResponse:
    """The page, or for htmx its main content. Reads only. A plain function, so it runs in
    FastAPI's thread pool: ``can_restore`` reads the database and the folder."""
    ctx = {
        "area": area("settings"),
        "areas": AREAS,
        **area_ctx("data"),
        "data_export": export_context(),
        "data_import": import_context(blocked=not _portability(request).can_restore()),
    }
    return render(request, page=AREA_PAGE, fragment=AREA_FRAGMENT, ctx=ctx)


def location(request: Request) -> HTMLResponse:
    """The data folder's path, for the page to load (spec 2.9 rule 13)."""
    try:
        folder: Path | None = _portability(request).folder()
    except PortabilityError:
        folder = None
    return _fragment(
        request, LOCATION_FRAGMENT, {"folder": None if folder is None else str(folder)}
    )


def delete_dialog(request: Request) -> HTMLResponse:
    """The confirmation dialog, with the entries the app will leave (reads only)."""
    plan = _erasure(request).plan()
    return _fragment(request, DELETE_FRAGMENT, _dialog_context(left=plan.unknown))


def _dialog_context(*, left: tuple[str, ...] = (), error: str | None = None) -> dict[str, Any]:
    return {
        "erase": {
            "word": CONFIRMATION_WORD,
            "error": error,
            "left": list(left[:LISTED_UNKNOWN]),
            "more_left": max(0, len(left) - LISTED_UNKNOWN),
        }
    }


# --- export and download ----------------------------------------------------------------------


async def export_data(request: Request) -> Response:
    form = await single_value_form(request, {"snapshots"})
    if isinstance(form, Response):
        return form
    if form.get("snapshots", "1") != "1":
        return PlainTextResponse(INVALID_REQUEST, status_code=400)
    snapshots = "snapshots" in form
    try:
        written = await run_in_threadpool(
            _portability(request).export, include_usage_snapshots=snapshots
        )
    except PortabilityError as error:
        words = str(error)
        response = _fragment(
            request, EXPORT_FRAGMENT, {"data_export": export_context(error=words)}, status=409
        )
        _announce(response, words)
        return response
    response = _fragment(
        request, EXPORT_FRAGMENT, {"data_export": export_context(name=written.name)}
    )
    _announce(response, f"{MSG_EXPORTED} {written.name}")
    return response


def download_export(request: Request, name: str) -> Response:
    """The stored bytes of one generated export file (a plain function: it reads a file)."""
    try:
        content = _portability(request).download(name)
    except PortabilityError:
        return PlainTextResponse(EXPORT_NOT_FOUND, status_code=404)
    return Response(
        content,
        media_type="application/json",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{name}"',
        },
    )


# --- import -----------------------------------------------------------------------------------


def _import_answer(
    request: Request, *, blocked: bool, status: int = 200, error: str | None = None
) -> HTMLResponse:
    ctx = {"data_import": import_context(blocked=blocked, error=error)}
    return _fragment(request, IMPORT_FRAGMENT, ctx, status=status)


async def _refuse_import(
    request: Request, status: int, words: str, *, blocked: bool | None = None
) -> Response:
    if blocked is None:
        blocked = not await run_in_threadpool(_portability(request).can_restore)
    return await run_in_threadpool(
        _import_answer, request, blocked=blocked, status=status, error=words
    )


async def import_data(request: Request) -> Response:
    service = _portability(request)
    if boundary_of(request.headers.get("content-type", "")) is None:
        return await _refuse_import(request, *UPLOAD_REFUSALS["form"])
    limit = MAX_IMPORT_BYTES  # read now: tests and later settings change it
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit + ENVELOPE_BYTES:
        return await _refuse_import(request, *UPLOAD_REFUSALS["too_large"])
    state = request.app.state
    if state.import_active:  # no await between this check and the next line: one at a time
        return await _refuse_import(request, 409, MSG_BUSY)
    state.import_active = True
    try:
        return await _import_upload(request, service, limit)
    finally:
        state.import_active = False


async def _import_upload(request: Request, service: DataPortabilityService, limit: int) -> Response:
    if not await run_in_threadpool(service.can_restore):
        return await _refuse_import(request, 409, MSG_NOTHING_IMPORTED, blocked=True)
    try:
        folder = await run_in_threadpool(make_upload_folder, service.folder())
    except PortabilityError:
        return await _refuse_import(request, 409, MSG_NO_WORKSPACE, blocked=True)
    except OSError as error:  # no room for the upload folder
        log_storage_failure(error)
        return await _refuse_import(request, *UPLOAD_REFUSALS["storage"])
    try:
        refused = await save_and_restore(request, service, folder, limit=limit)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if refused is not None:
        return await _refuse_import(request, refused.status, refused.words, blocked=refused.blocked)
    return await _restored(request)


@dataclass(frozen=True)
class ImportRefused:
    """Why a restore did not happen: the status, the fixed words (nothing of the file or of the
    error) and whether the workspace is known to have content (None: ask the service)."""

    status: int
    words: str
    blocked: bool | None = None
    fields: Mapping[str, str] = field(default_factory=dict)  # setup: the text parts read so far


async def save_and_restore(
    request: Request,
    service: DataPortabilityService,
    directory: Path | DirectoryFor,
    *,
    limit: int,
    text_fields: frozenset[str] = frozenset(),
) -> ImportRefused | None:
    """Stream the request's file into ``directory`` (see ``save_upload``) and restore it, under
    the module's read timeout and upload deadline. None when the workspace is restored. Shared
    with setup's restore; the caller removes the upload folder and answers. Anything the
    directory function raises passes through."""
    try:
        saved = await save_upload(
            request,
            directory,
            max_bytes=limit,
            read_timeout=READ_TIMEOUT_S,
            total_timeout=UPLOAD_DEADLINE_S,
            text_fields=text_fields,
        )
        await run_in_threadpool(service.restore, saved)
    except UploadRefused as refusal:
        if refusal.reason == "storage":
            log_storage_failure(refusal.__cause__)
        status, words = UPLOAD_REFUSALS[refusal.reason]
        return ImportRefused(status, words, fields=refusal.fields)
    except PortabilityError:
        # Nothing of the file or of the error is shown: only what the workspace says now.
        if not await run_in_threadpool(service.can_restore):
            return ImportRefused(409, MSG_NOTHING_IMPORTED, blocked=True)
        return ImportRefused(422, MSG_UNREADABLE, blocked=False)
    except OSError as error:  # the restore could not write (a full disk)
        log_storage_failure(error)
        return ImportRefused(*UPLOAD_REFUSALS["storage"])
    return None


def log_storage_failure(error: BaseException | None) -> None:
    """The class name only: an ``OSError``'s message and file name hold paths."""
    kind = type(error).__name__ if error is not None else "OSError"
    _log.error("Saving or restoring an import failed (%s)", kind)


def make_upload_folder(root: Path) -> Path:
    return Path(tempfile.mkdtemp(prefix=UPLOAD_PREFIX, dir=root))  # mode 0700


async def _restored(request: Request) -> Response:
    """The data is in: take in what the app derives from it, then say so."""
    words = MSG_RESTORED
    try:
        await run_in_threadpool(request.app.state.workspace_restored)
    except Exception as error:  # the data is restored either way; type only (no paths)
        _log.error("Refreshing after a restore failed (%s)", type(error).__name__)
        words = MSG_RESTORED_RESTART
    ctx = {"data_import": import_context(blocked=True, done=words)}
    response = _fragment(request, IMPORT_FRAGMENT, ctx)
    _announce(response, words)
    return response


# --- delete all data --------------------------------------------------------------------------


async def delete_data(request: Request) -> Response:
    form = await single_value_form(request, {"confirm"})
    if isinstance(form, Response):
        return form
    confirm = form.get("confirm", "")
    try:
        await run_in_threadpool(_erasure(request).erase, confirm)
    except ErasureRefused as refusal:
        wrong_word = confirm.strip() != CONFIRMATION_WORD
        return _dialog_answer(request, refusal.user_message, 422 if wrong_word else 409)
    except ErasureIncomplete as incomplete:
        return _dialog_answer(request, incomplete.user_message, 409)
    return Response(
        status_code=200,
        headers={"HX-Redirect": "/", "HX-Trigger": json.dumps({ERASED_EVENT: True})},
    )


def _dialog_answer(request: Request, words: str, status: int) -> HTMLResponse:
    """The dialog again with the reason in it; the typed word is not shown back."""
    return _fragment(request, DELETE_FRAGMENT, _dialog_context(error=words), status=status)
