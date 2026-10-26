"""Settings > Your details: contact details and the optional CV photo (M6.6; ADR-106 items 3 and
4; spec 2.9 rules 2, 3, 5 and 13; TD-280). None of these routes is a page, so every one needs the
launch token, and every POST also the origin. ``GET /settings`` carries only a lazy placeholder
(``_details_placeholder.html``) that fetches ``GET /settings/details``: the page itself holds no
personal value and no photo.

- ``GET /settings/details``: the ``#details-settings`` section with the saved values and the
  photo as an inline data URI. Changes nothing, takes no focus.
- ``POST /settings/details`` (``version``, ``name``, ``email``, ``phone``, ``location``,
  ``link_1`` to ``link_3``; each at most once): saves the text fields and keeps the photo.
- ``POST /settings/details/photo`` (multipart: ``file`` and ``version``) and
  ``POST /settings/details/photo/remove`` (``version``).

Every answer is the whole section. The details form swaps all of it; the photo forms use
``hx-select`` to swap only ``#photo-block`` (plus two out-of-band spots), so their outcome lines
are rendered inside the block (``DetailsState.scope``) and unsaved details text is never wiped.

An unknown or repeated field and a missing or non-numeric ``version`` are a plain-text 400. A
success answers 200 for htmx with the section, a line saying what was done, one focus target and
an ``announce`` message (303 to ``/settings`` otherwise). A refused value is a 422 with the
section: the typed values stay in the inputs (escaped), each refused control is tied to a fixed
message, and no message, header or log line holds an entered value or a file name. A stale
version is a 409 with the reload prompt; a failed write is a 422 with a general message and
nothing changed.
"""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.errors import INVALID_REQUEST
from app.api.forms import is_htmx, posted_version, single_value_form
from app.api.templating import templates
from app.api.uploads import read_upload
from app.domain.errors import (
    AppError,
    DetailsNotSaved,
    PhotoNotSaved,
    UploadNotAccepted,
    UploadTooLarge,
    VersionConflict,
)
from app.domain.images import PHOTO_MAX_BYTES
from app.services.personal_details import (
    FIELDS,
    FILE_FIELD,
    NOT_A_PICTURE,
    TOO_BIG,
    DetailsInvalid,
    PersonalDetailsService,
    PhotoInvalid,
)

_log = logging.getLogger(__name__)

SETTINGS_PATH: Final = "/settings"
DETAILS_PATH: Final = f"{SETTINGS_PATH}/details"
PHOTO_PATH: Final = f"{DETAILS_PATH}/photo"
PHOTO_REMOVE_PATH: Final = f"{PHOTO_PATH}/remove"
CONFLICT_DETAIL: Final = {
    "details": (
        "Your change was not saved. Your details changed in another tab and now show the "
        "latest. Check them and try again."
    ),
    "photo": (
        "Your photo was not saved. Your details changed in another tab. Reload to see the "
        "latest, then try again."
    ),
}
FRAGMENT: Final = "fragments/settings/_details.html"
_NOTHING: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True)
class DetailsState:
    """What just happened in the section; the default is a plain load."""

    message: str = ""  # done: the line shown in the section
    errors: Mapping[str, str] = field(default_factory=lambda: _NOTHING)  # message per control
    typed: Mapping[str, str] | None = None  # the refused form's text, shown back (escaped)
    version: int | None = None  # the version the user's form was based on (a refusal keeps it)
    problem: str = ""  # a failed write: a general message
    conflict: bool = False  # a stale version: the reload prompt shows and takes focus
    opened: bool = False  # the disclosure is open: every answer to a POST sets it
    scope: str = "details"  # which form was used: "photo" shows the outcome inside #photo-block
    after: str = ""  # a second line after the done message


def register_details_routes(app: FastAPI) -> None:
    """Add the routes directly to the app; none is a page, so all need the token."""
    app.add_api_route(DETAILS_PATH, show_details, methods=["GET"], response_class=HTMLResponse)
    app.add_api_route(DETAILS_PATH, save_details, methods=["POST"], response_class=HTMLResponse)
    app.add_api_route(PHOTO_PATH, save_photo, methods=["POST"], response_class=HTMLResponse)
    app.add_api_route(
        PHOTO_REMOVE_PATH, remove_photo, methods=["POST"], response_class=HTMLResponse
    )


def _service(request: Request) -> PersonalDetailsService:
    service: PersonalDetailsService = request.app.state.personal_details
    return service


def _invalid() -> Response:
    return PlainTextResponse(INVALID_REQUEST, status_code=400)


def _fields_to_change(count: int) -> str:
    return "1 field needs a change." if count == 1 else f"{count} fields need a change."


def _context(request: Request, state: DetailsState) -> dict[str, Any]:
    """The ``details`` template value. Blocking (it reads the setting and the photo file)."""
    view = _service(request).view()
    values = view.values()
    if state.typed is not None:
        values.update({name: state.typed.get(name, "") for name in FIELDS})
    first_error = next((name for name in (*FIELDS, FILE_FIELD) if name in state.errors), "")
    return {
        "version": view.version if state.version is None else state.version,
        "shown": values,
        "photo": view.photo,
        "errors": dict(state.errors),
        "first_error": first_error,
        "opened": state.opened,
        "message": state.message,
        "problem": state.problem,
        "conflict": state.conflict,
        "conflict_message": VersionConflict.message,
        "conflict_detail": CONFLICT_DETAIL[state.scope],
        "scope": state.scope,
        "after": state.after,
        "name_saved": bool(view.details.name),
        "settings_url": SETTINGS_PATH,
        "details_url": DETAILS_PATH,
        "photo_url": PHOTO_PATH,
        "photo_remove_url": PHOTO_REMOVE_PATH,
        "fields": FIELDS,
    }


def _answer(
    request: Request, state: DetailsState, *, status_code: int = 200, announce: str = ""
) -> Response:
    """The section for htmx; a plain success goes back to the page."""
    if status_code == 200 and state.message and not is_htmx(request):
        return RedirectResponse(SETTINGS_PATH, status_code=303)
    state = replace(state, opened=True)
    response = templates.TemplateResponse(
        request, FRAGMENT, {"details": _context(request, state)}, status_code=status_code
    )
    if announce:
        response.headers["HX-Trigger"] = json.dumps({"announce": announce})
    return response


def _conflict(request: Request, scope: str = "details") -> Response:
    return _answer(request, DetailsState(conflict=True, scope=scope), status_code=409)


def _failed(
    request: Request, error: AppError, *, version: int, subject: str, scope: str = "details"
) -> Response:
    _log.warning("%s not saved (%s)", subject, type(error).__name__)
    state = DetailsState(problem=error.user_message, version=version, scope=scope)
    return _answer(request, state, status_code=422, announce=error.user_message)


def show_details(request: Request) -> Response:
    """The section alone. A plain function: FastAPI runs it in its thread pool."""
    return templates.TemplateResponse(
        request, FRAGMENT, {"details": _context(request, DetailsState())}
    )


async def save_details(request: Request) -> Response:
    form = await single_value_form(request, {"version", *FIELDS})
    if isinstance(form, Response):
        return form
    version = posted_version(form, minimum=0)
    if version is None:
        return _invalid()
    typed = {name: form.get(name, "").replace("\x00", "") for name in FIELDS}

    def run() -> Response:
        try:
            _service(request).save_details(form, version=version)
        except DetailsInvalid as error:
            state = DetailsState(errors=error.fields, typed=typed, version=version)
            said = f"Details not saved. {_fields_to_change(len(error.fields))}"
            return _answer(request, state, status_code=422, announce=said)
        except VersionConflict:
            return _conflict(request)
        except DetailsNotSaved as error:
            return _failed(request, error, version=version, subject="Details")
        return _answer(request, DetailsState(message="Details saved."), announce="Details saved.")

    return await run_in_threadpool(run)


async def save_photo(request: Request) -> Response:
    """Takes only ``Request``: ``read_upload`` caps the body as it reads it. The client's file
    name and declared type are never used."""
    fields: dict[str, list[str]] = {}
    refusal = ""
    data = b""
    try:
        data = (await read_upload(request, fields=fields, max_bytes=PHOTO_MAX_BYTES)).data
    except UploadTooLarge:
        refusal = TOO_BIG
    except UploadNotAccepted:
        refusal = NOT_A_PICTURE
    version = _posted_photo_version(fields) if not refusal else None
    if not refusal and version is None:
        return _invalid()

    def run() -> Response:
        shown = 0 if version is None else version
        if refusal:
            return _photo_refused(request, refusal, None)
        try:
            _service(request).save_photo(data, version=shown)
        except PhotoInvalid as error:
            return _photo_refused(request, error.message, shown)
        except VersionConflict:
            return _conflict(request, "photo")
        except PhotoNotSaved as error:
            return _failed(request, error, version=shown, subject="Photo", scope="photo")
        state = DetailsState(message="Photo saved.", scope="photo")
        return _answer(request, state, announce="Photo saved.")

    return await run_in_threadpool(run)


def _posted_photo_version(fields: Mapping[str, list[str]]) -> int | None:
    """The one ``version`` field of the upload form, and nothing else beside the file."""
    if set(fields) != {"version"} or len(fields["version"]) != 1:
        return None
    return posted_version({"version": fields["version"][0]}, minimum=0)


def _photo_refused(request: Request, message: str, version: int | None) -> Response:
    state = DetailsState(errors={FILE_FIELD: message}, version=version, scope="photo")
    return _answer(
        request,
        state,
        status_code=422,
        announce=f"Photo not saved. {_fields_to_change(1)}",
    )


async def remove_photo(request: Request) -> Response:
    form = await single_value_form(request, {"version"})
    if isinstance(form, Response):
        return form
    version = posted_version(form, minimum=0)
    if version is None:
        return _invalid()

    def run() -> Response:
        try:
            _service(request).remove_photo(version=version)
        except VersionConflict:
            return _conflict(request, "photo")
        except PhotoNotSaved as error:
            return _failed(request, error, version=version, subject="Photo", scope="photo")
        state = DetailsState(
            message="Photo removed.", after="Upload one again any time.", scope="photo"
        )
        return _answer(request, state, announce="Photo removed.")

    return await run_in_threadpool(run)
