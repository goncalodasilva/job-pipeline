"""Setup step 1: "Restore from an export" (M9.1 part D; AC-M9.3; ADR-050, ADR-131 items 3-4;
spec 2.9 rules 2, 3, 5, 9 and 12; TD-033).

``POST /setup/welcome/restore`` is not page-tagged and POST only, so the launch token and the
origin check apply, like the other setup POSTs; the setup page itself stays token-free and
read-only. The body is multipart: a text part ``folder`` (the Data folder field) before one file
part ``file``. It is served only while the guide shows step 1. Any other time it is a 409 that
opens, writes and restores nothing and answers with the guide of the step the app is on, because
htmx swaps a 409 into ``#setup-guide`` (a stale tab keeps its guide).

The folder is opened the way the Welcome step's "Start fresh" opens it (``open_for_restore``:
only a missing or empty folder, the same messages), as soon as the file part's headers have
arrived and before any of its bytes are written; a workspace that is already open (``--data-dir``,
the environment, demo, or a folder an earlier refused attempt opened) is used as it is and the
folder part is ignored. A folder that cannot be used answers 422 with the step, the error on the
folder field and nothing else touched. The upload then goes the way Settings > Data and privacy
takes one: ``save_and_restore`` (streamed to ``.restore-upload-*`` under ``MAX_IMPORT_BYTES``,
``READ_TIMEOUT_S`` and ``UPLOAD_DEADLINE_S``, read at request time from
``app.api.settings_data``; one import at a time through ``app.state.import_active``), with its
fixed messages that echo nothing of the file, worded for setup ("restore", not "import"). A
refusal answers 413, 409 or 422 with the step, the message on the file field and focus there; the
opened workspace stays open and empty, step 1 stays unfinished and the same form works again with
only the file. A refusal that comes before the folder opens (no file, a declared length over the
cap, another import running) still reads the form up to the file part's headers, so the folder
the person typed is shown back.

A success writes ``config.toml`` as finishing the step would (``finish_restore``: never in demo
mode, never over an existing one unless this step opened the folder), takes in what the app
derives from the restored rows (``app.state.workspace_restored``: the setup decision, provider
health, the task runner) and answers ``HX-Redirect: /`` with an announcement; the setup redirect
then sends the user to whichever step the restored ``setup_state`` leaves, or to the app. The
query string is never carried over (ADR-050).
"""

import json
import logging
import shutil
from collections.abc import Mapping
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from app.api import settings_data
from app.api.restore_upload import ENVELOPE_BYTES, UploadRefused, boundary_of, save_upload
from app.api.setup import render_step
from app.api.setup_redirect import setup_step_path
from app.onboarding.registry import WELCOME, Form, StepRegistry
from app.onboarding.startup import OnboardingState
from app.onboarding.steps.welcome import WelcomeStep
from app.services.data_portability import DataPortabilityService, PortabilityError

_log = logging.getLogger(__name__)

RESTORE_PATH = f"{setup_step_path(WELCOME.id)}/restore"
FOLDER_FIELD = "folder"
RESTORE_ERROR = "restore"  # the error key the step's template shows on the file field
FOLDER_NOTE = "restore_folder"  # shown with a folder refusal: the file must be chosen again
MSG_NOT_HERE = "Restoring an export is only offered on the first step of setup."
MSG_HAS_CONTENT = (
    "This data folder already has content, so nothing was restored. "
    "Restore only works into a new or empty folder."
)
MSG_RESTORED_NOT_SAVED = (
    "Restored. The app couldn't save where your data folder is, so choose it again "
    "the next time you start the app."
)
MSG_CHOOSE_AGAIN = "Choose the file again after changing the folder."

# Settings' refusal words say "import"; setup's own say "restore" (the same fixed meaning).
SETUP_WORDS: dict[str, str] = {
    settings_data.MSG_CHOOSE: "Choose one export file to restore. Nothing was restored.",
    settings_data.MSG_TOO_LARGE: (
        "That file is too large. An export can be up to 200 MB. Nothing was restored."
    ),
    settings_data.MSG_STALLED: (
        "The upload stopped before it finished. Nothing was restored. Try again."
    ),
    settings_data.MSG_TOO_SLOW: "The upload took too long. Nothing was restored. Try again.",
    settings_data.MSG_UNREADABLE: (
        "This file can't be restored. It may not be a Job Pipeline export, may be damaged, "
        "or may come from a newer version of the app. Nothing was restored."
    ),
    settings_data.MSG_BUSY: (
        "Another restore or import is still running. Wait for it to finish, then try again."
    ),
}


class FolderRefused(Exception):
    """The folder named by the form could not be opened: ``errors`` are the step's, ``typed``
    what was sent (shown back)."""

    def __init__(self, errors: Mapping[str, str], typed: str | None) -> None:
        super().__init__("folder")
        self.errors = dict(errors)
        self.typed = typed


def register_setup_restore(app: FastAPI) -> None:
    """Add the route directly to the app (not through an included router)."""
    app.add_api_route(
        RESTORE_PATH,
        restore_export,
        methods=["POST"],
        response_class=Response,
        name="setup_welcome_restore",
    )


async def restore_export(request: Request) -> Response:
    state = request.app.state
    registry: StepRegistry = state.setup_registry
    onboarding: OnboardingState = state.onboarding
    if registry.first.id != WELCOME.id or onboarding.guide_step() != WELCOME.id:
        return await _not_here(request)
    if boundary_of(request.headers.get("content-type", "")) is None:
        return await _refuse_file(request, *settings_data.UPLOAD_REFUSALS["form"])
    limit = settings_data.MAX_IMPORT_BYTES  # read now: tests and later settings change it
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit + ENVELOPE_BYTES:
        typed = await _typed_folder(request, limit)
        return await _refuse_file(request, *settings_data.UPLOAD_REFUSALS["too_large"], typed=typed)
    if state.import_active:  # no await between this check and the next line: one at a time
        typed = await _typed_folder(request, limit)
        return await _refuse_file(request, 409, settings_data.MSG_BUSY, typed=typed)
    state.import_active = True
    try:
        return await _restore(request, limit)
    finally:
        state.import_active = False


async def _typed_folder(request: Request, limit: int) -> str | None:
    """The folder part of a form that is refused before its file is looked at: read the form up
    to the file part's headers (a few bytes) and stop there; never opens or writes anything."""

    async def stop(fields: Mapping[str, str]) -> Path:
        raise UploadRefused("form")

    try:
        await save_upload(
            request,
            stop,
            max_bytes=limit,
            read_timeout=settings_data.READ_TIMEOUT_S,
            total_timeout=settings_data.UPLOAD_DEADLINE_S,
            text_fields=frozenset({FOLDER_FIELD}),
        )
    except UploadRefused as refusal:
        return refusal.fields.get(FOLDER_FIELD)
    return None  # pragma: no cover - ``stop`` always refuses


async def _restore(request: Request, limit: int) -> Response:
    state = request.app.state
    service: DataPortabilityService = state.data_portability
    welcome: WelcomeStep = state.setup_handlers[WELCOME.id]
    if state.workspace is not None and not await run_in_threadpool(service.can_restore):
        return await _refuse_file(request, 409, MSG_HAS_CONTENT)

    made: list[Path] = []

    async def upload_folder(fields: Mapping[str, str]) -> Path:
        """Open the workspace the form names (unless one is open), then make the upload folder
        in it; called when the file part begins."""
        typed = fields.get(FOLDER_FIELD)
        opened = await run_in_threadpool(welcome.open_for_restore, typed)
        if not opened.ok:
            raise FolderRefused(opened.errors, typed)
        try:
            folder = await run_in_threadpool(_make_folder, service)
        except (PortabilityError, OSError) as error:  # no room, or the folder went away
            raise UploadRefused("storage") from error
        made.append(folder)
        return folder

    try:
        refused = await settings_data.save_and_restore(
            request,
            service,
            upload_folder,
            limit=limit,
            text_fields=frozenset({FOLDER_FIELD}),
        )
    except FolderRefused as error:
        return await _folder_refused(request, error)
    finally:
        for folder in made:
            shutil.rmtree(folder, ignore_errors=True)
    if refused is not None:
        words = MSG_HAS_CONTENT if refused.blocked else refused.words
        typed = refused.fields.get(FOLDER_FIELD)
        return await _refuse_file(request, refused.status, words, typed=typed)
    return await _restored(request, welcome)


def _make_folder(service: DataPortabilityService) -> Path:
    return settings_data.make_upload_folder(service.folder())


async def _restored(request: Request, welcome: WelcomeStep) -> Response:
    """The data is in: name the folder in config.toml, take in what the app derives from the
    restored rows, then send the user on. Neither can undo the restore, so a failure only
    changes what is said."""
    words = settings_data.MSG_RESTORED
    if not await run_in_threadpool(welcome.finish_restore):
        words = MSG_RESTORED_NOT_SAVED
    try:
        await run_in_threadpool(request.app.state.workspace_restored)
    except Exception as error:  # the data is restored either way; type only (no paths)
        _log.error("Refreshing after a restore failed (%s)", type(error).__name__)
        words = settings_data.MSG_RESTORED_RESTART
    return Response(
        status_code=200,
        headers={"HX-Redirect": "/", "HX-Trigger": json.dumps({"announce": words})},
    )


async def _not_here(request: Request) -> Response:
    """Restoring is not offered now (step 1 was finished, or setup is). The 409 is swapped into
    ``#setup-guide``, so it is the guide of the step the app is on, with the reason announced."""
    registry: StepRegistry = request.app.state.setup_registry
    onboarding: OnboardingState = request.app.state.onboarding
    step = registry.get(onboarding.guide_step() or registry.last.id)
    response = await run_in_threadpool(render_step, request, step)
    response.status_code = 409
    response.headers["HX-Push-Url"] = setup_step_path(step.id)
    response.headers["HX-Trigger"] = json.dumps({"announce": MSG_NOT_HERE})
    return response


async def _refuse_file(
    request: Request, status: int, words: str, *, typed: str | None = None
) -> Response:
    """The step again with the reason on the file field (fixed words; nothing of the upload).
    ``typed``: the folder the person typed, shown back when the folder is still theirs to change."""
    return await _show(request, status, {RESTORE_ERROR: SETUP_WORDS.get(words, words)}, typed=typed)


async def _folder_refused(request: Request, refusal: FolderRefused) -> Response:
    errors = dict(refusal.errors)
    if "folder" in errors:  # the file input is empty after the swap: it must be chosen again
        errors[FOLDER_NOTE] = MSG_CHOOSE_AGAIN
    return await _show(request, 422, errors, typed=refusal.typed)


async def _show(
    request: Request, status: int, errors: Mapping[str, str], *, typed: str | None = None
) -> Response:
    registry: StepRegistry = request.app.state.setup_registry
    submitted: Form | None = {FOLDER_FIELD: [typed]} if typed is not None else None
    response = await run_in_threadpool(
        render_step, request, registry.first, errors=errors, submitted=submitted
    )
    response.status_code = status
    return response
