"""Settings > Board: the status list and the reminder rules (M5.7; AC-M5.1; ADR-102 items 5
and 10, ADR-104). Every route is a POST needing the launch token and the origin.

- ``POST /settings/board/statuses`` adds a status (``label`` and ``phase``; no version, since no
  existing row changes).
- ``POST /settings/board/statuses/{key}/rename`` (``version``, ``label``), ``/move``
  (``version``, ``direction`` = ``up`` or ``down``, inside the status's phase) and ``/delete``
  (``version``; custom statuses nothing uses only).
- ``POST /settings/board/reminders/{key}`` saves the status's rule: ``version`` (``0`` while it
  has none), ``days`` and ``enabled`` (``1`` when on, absent when off).

A form's fields each appear once; an unknown or repeated field, a missing or non-numeric version
and a bad ``direction`` or ``enabled`` are a plain-text 400. An unknown or unsafe status key is
the fixed 404 and is never echoed. A success answers 200 for htmx with the whole section, a line
saying what was done, one focus target and an ``announce`` message (303 to ``/settings``
otherwise). A refused value is a 422 with the section (the full page when plain), what was typed
kept and each refused field tied to its error; a stale version is a 409 with the reload prompt and
changes nothing. Every message is fixed copy with at most a status name and a count.
"""

import json

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.board_settings_forms import (
    ADD,
    DELETE,
    REMINDERS_PATH,
    RENAME,
    RULE,
    SETTINGS_PATH,
    STATUSES_PATH,
    BoardSettingsState,
)
from app.api.errors import INVALID_REQUEST
from app.api.forms import is_htmx, posted_version, single_value_form
from app.api.settings import render_board_settings
from app.domain.errors import ReminderRuleInvalid, StatusNotAllowed, VersionConflict
from app.domain.ids import is_safe_id
from app.services.applications import ApplicationStatuses
from app.services.reminders import ReminderRules

DIRECTIONS = ("up", "down")


def register_board_settings_routes(app: FastAPI) -> None:
    """Add the routes directly to the app; none is a page, so all need the token."""
    app.add_api_route(STATUSES_PATH, add_status, methods=["POST"])
    app.add_api_route(f"{STATUSES_PATH}/{{key}}/rename", rename_status, methods=["POST"])
    app.add_api_route(f"{STATUSES_PATH}/{{key}}/move", move_status, methods=["POST"])
    app.add_api_route(f"{STATUSES_PATH}/{{key}}/delete", delete_status, methods=["POST"])
    app.add_api_route(f"{REMINDERS_PATH}/{{key}}", save_rule, methods=["POST"])


def _statuses(request: Request) -> ApplicationStatuses:
    service: ApplicationStatuses = request.app.state.application_statuses
    return service


def _rules(request: Request) -> ReminderRules:
    service: ReminderRules = request.app.state.reminder_rules
    return service


def _invalid() -> Response:
    return PlainTextResponse(INVALID_REQUEST, status_code=400)


def _not_found() -> Response:
    return PlainTextResponse(StatusNotAllowed.message, status_code=404)


def _label_of(request: Request, key: str) -> str | None:
    """The status's name, or None when no status has this key (an unsafe key has none)."""
    if not is_safe_id(key):
        return None
    return next((s.label for s in _statuses(request).listing() if s.key == key), None)


def _at_edge(request: Request, key: str, direction: str) -> bool:
    """Whether a Move in ``direction`` has nowhere to go: first of its phase going up, or last
    of it going down."""
    listed = _statuses(request).listing()
    current = next((s for s in listed if s.key == key), None)
    if current is None:
        return False
    same = [s.key for s in listed if s.phase == current.phase]
    return same[0 if direction == "up" else -1] == key


def _other(direction: str) -> str:
    return "down" if direction == "up" else "up"


def _fields_to_change(count: int) -> str:
    return "1 field needs a change." if count == 1 else f"{count} fields need a change."


def _answer(
    request: Request,
    state: BoardSettingsState,
    *,
    status_code: int = 200,
    announce: str = "",
) -> Response:
    """The section for htmx, the full page otherwise; a plain success goes back to the page."""
    if status_code == 200 and not is_htmx(request):
        return RedirectResponse(SETTINGS_PATH, status_code=303)
    response = render_board_settings(request, state)
    response.status_code = status_code
    if announce and is_htmx(request):
        response.headers["HX-Trigger"] = json.dumps({"announce": announce})
    return response


def _conflict(request: Request) -> Response:
    return _answer(request, BoardSettingsState(conflict=True), status_code=409)


def _refused(
    request: Request,
    state: BoardSettingsState,
    said: str,
) -> Response:
    announce = f"{said} {_fields_to_change(len(state.errors))}" if said else ""
    return _answer(request, state, status_code=422, announce=announce)


def _typed(form: dict[str, str], *names: str) -> dict[str, str]:
    return {name: form.get(name, "").replace("\x00", "") for name in names}


# --- statuses ------------------------------------------------------------------------------


async def add_status(request: Request) -> Response:
    form = await single_value_form(request, {"label", "phase"})
    if isinstance(form, Response):
        return form
    if set(form) != {"label", "phase"}:
        return _invalid()

    def run() -> Response:
        try:
            saved = _statuses(request).add(form["label"], form["phase"])
        except StatusNotAllowed as error:
            errors = error.fields or {"label": error.user_message}
            state = BoardSettingsState(
                scope=ADD, errors=errors, typed=_typed(form, "label", "phase")
            )
            return _refused(request, state, "Status not added.")
        message = f"Added the status {saved.label}."
        state = BoardSettingsState(message=message, key=saved.key, focus="status")
        return _answer(request, state, announce=message)

    return await run_in_threadpool(run)


async def rename_status(request: Request, key: str) -> Response:
    form = await single_value_form(request, {"version", "label"})
    if isinstance(form, Response):
        return form
    version = posted_version(form, minimum=1)
    if version is None or "label" not in form:
        return _invalid()

    def run() -> Response:
        if _label_of(request, key) is None:
            return _not_found()
        try:
            saved = _statuses(request).rename(key, form["label"], expected_version=version)
        except VersionConflict:
            return _conflict(request)
        except StatusNotAllowed as error:
            errors = error.fields or {"label": error.user_message}
            state = BoardSettingsState(
                scope=RENAME, key=key, errors=errors, typed=_typed(form, "label")
            )
            return _refused(request, state, "Status not renamed.")
        message = f"Renamed to {saved.label}."
        state = BoardSettingsState(message=message, key=key, focus="name")
        return _answer(request, state, announce=message)

    return await run_in_threadpool(run)


async def move_status(request: Request, key: str) -> Response:
    form = await single_value_form(request, {"version", "direction"})
    if isinstance(form, Response):
        return form
    version = posted_version(form, minimum=1)
    direction = form.get("direction", "")
    if version is None or direction not in DIRECTIONS:
        return _invalid()

    def run() -> Response:
        current = next((s for s in _statuses(request).listing() if s.key == key), None)
        if current is None:
            return _not_found()
        if current.version == version and _at_edge(request, key, direction):
            # Nothing would change: say so, write nothing and leave focus on the other button.
            word = "first" if direction == "up" else "last"
            message = f"{current.label} is already {word} in {current.phase.capitalize()}."
            state = BoardSettingsState(message=message, key=key, focus=_other(direction))
            return _answer(request, state, announce=message)
        try:
            saved = _statuses(request).reorder(key, direction, expected_version=version)
        except VersionConflict:
            return _conflict(request)
        except StatusNotAllowed:
            return _not_found()
        message = f"Moved {saved.label} {direction}."
        # Focus stays on the button just used, unless it has reached the end and is now idle.
        focus = _other(direction) if _at_edge(request, key, direction) else direction
        state = BoardSettingsState(message=message, key=key, focus=focus)
        return _answer(request, state, announce=message)

    return await run_in_threadpool(run)


async def delete_status(request: Request, key: str) -> Response:
    form = await single_value_form(request, {"version"})
    if isinstance(form, Response):
        return form
    version = posted_version(form, minimum=1)
    if version is None:
        return _invalid()

    def run() -> Response:
        label = _label_of(request, key)
        if label is None:
            return _not_found()
        try:
            _statuses(request).delete(key, expected_version=version)
        except VersionConflict:
            return _conflict(request)
        except StatusNotAllowed as error:
            state = BoardSettingsState(scope=DELETE, key=key, errors={"delete": error.user_message})
            return _answer(request, state, status_code=422, announce=error.user_message)
        message = f"Deleted the status {label}."
        return _answer(
            request, BoardSettingsState(message=message, focus="statuses"), announce=message
        )

    return await run_in_threadpool(run)


# --- reminder rules --------------------------------------------------------------------------


async def save_rule(request: Request, key: str) -> Response:
    form = await single_value_form(request, {"version", "days", "enabled"})
    if isinstance(form, Response):
        return form
    version = posted_version(form, minimum=0)
    if version is None or "days" not in form or form.get("enabled", "1") != "1":
        return _invalid()
    enabled = "enabled" in form

    def run() -> Response:
        label = _label_of(request, key)
        if label is None:
            return _not_found()
        try:
            _rules(request).save(key, days=form["days"], enabled=enabled, expected_version=version)
        except VersionConflict:
            return _conflict(request)
        except StatusNotAllowed:
            return _not_found()
        except ReminderRuleInvalid as error:
            typed = {**_typed(form, "days"), **({"enabled": "1"} if enabled else {})}
            state = BoardSettingsState(
                scope=RULE, key=key, errors={error.field: error.user_message}, typed=typed
            )
            return _refused(request, state, f"Reminder for {label} not saved.")
        message = f"Reminder for {label} saved." if enabled else f"Reminder for {label} turned off."
        state = BoardSettingsState(message=message, key=key, focus="rule")
        return _answer(request, state, announce=message)

    return await run_in_threadpool(run)
