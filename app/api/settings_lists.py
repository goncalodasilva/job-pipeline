"""Settings > Lists: the controlled values and the skills' other spellings (M9.2 part D; ADR-131
items 6-7; ADR-125 item 5). Every route is a POST needing the launch token and the origin.

- ``POST /settings/lists/values`` adds ``value`` to ``field`` (industry, seniority or employment
  type; no version, since no existing row changes). Any other field is a plain-text 400.
- ``POST /settings/lists/values/{id}/aliases`` (``version``, ``aliases``: one per line) replaces
  the other spellings, and ``/delete`` (``version``) deletes a value no job holds.
- ``POST /settings/lists/skills/{id}/aliases`` (``version``, ``aliases``) replaces a skill's
  other spellings. Skills have no add and no delete here.

A form's fields each appear once; an unknown, repeated or missing field and a missing or
non-numeric version are a plain-text 400. An unknown or unsafe id (or the id of a work mode,
which is fixed) is a plain-text 404 that echoes nothing. The version is checked first: a stale one
is a 409 with the reload prompt and changes nothing, whatever the values are. A refused value is a
422 with the section (the whole page when plain), what was typed kept and the refused field tied
to its fixed message; a value some job holds cannot be deleted (409, naming the number of jobs). A
success answers 200 for htmx with the whole section, a line saying what was done, one focus
target and an ``announce`` message (303 to ``/settings/lists`` otherwise).
"""

import json

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.errors import INVALID_REQUEST
from app.api.forms import is_htmx, posted_version, single_value_form
from app.api.lists_forms import (
    ADD,
    ALIASES,
    AREA_PATH,
    DELETE,
    SKILLS_PATH,
    VALUES_PATH,
    ListsState,
    group_name,
)
from app.api.settings_areas import render_lists_settings
from app.domain.controlled_values import EDITABLE_FIELDS, skill_alias_lines
from app.domain.errors import (
    ControlledValueInUse,
    ControlledValueInvalid,
    ControlledValueNotFound,
    FactNotFound,
    SkillAliasTaken,
    VersionConflict,
)
from app.domain.ids import is_safe_id
from app.services.controlled_lists import ControlledLists
from app.services.skills import SkillService

SKILL_NOT_FOUND = "This skill wasn't found."
SKILL_NAME_TAKEN = "One of these spellings already means another skill."


def register_lists_routes(app: FastAPI) -> None:
    """Add the routes directly to the app; none is a page, so all need the token."""
    app.add_api_route(VALUES_PATH, add_value, methods=["POST"])
    app.add_api_route(f"{VALUES_PATH}/{{value_id}}/aliases", save_aliases, methods=["POST"])
    app.add_api_route(f"{VALUES_PATH}/{{value_id}}/delete", delete_value, methods=["POST"])
    app.add_api_route(f"{SKILLS_PATH}/{{skill_id}}/aliases", save_skill_aliases, methods=["POST"])


def _lists(request: Request) -> ControlledLists:
    service: ControlledLists = request.app.state.controlled_lists
    return service


def _invalid() -> Response:
    return PlainTextResponse(INVALID_REQUEST, status_code=400)


def _not_found(message: str = ControlledValueNotFound.message) -> Response:
    return PlainTextResponse(message, status_code=404)


def _fields_to_change(count: int) -> str:
    return "1 field needs a change." if count == 1 else f"{count} fields need a change."


def _answer(
    request: Request, state: ListsState, *, status_code: int = 200, announce: str = ""
) -> Response:
    """The section for htmx, the full page otherwise; a plain success goes back to the page."""
    if status_code == 200 and not is_htmx(request):
        return RedirectResponse(AREA_PATH, status_code=303)
    response = render_lists_settings(request, state)
    response.status_code = status_code
    if announce and is_htmx(request):
        response.headers["HX-Trigger"] = json.dumps({"announce": announce})
    return response


def _conflict(request: Request, group: str) -> Response:
    return _answer(request, ListsState(group=group, conflict=True), status_code=409)


def _refused(request: Request, state: ListsState, said: str, *, status_code: int = 422) -> Response:
    return _answer(
        request,
        state,
        status_code=status_code,
        announce=f"{said} {_fields_to_change(len(state.errors))}",
    )


def _clean(text: str) -> str:
    return text.replace("\x00", "")


# --- controlled values ---------------------------------------------------------------------


async def add_value(request: Request) -> Response:
    form = await single_value_form(request, {"field", "value"})
    if isinstance(form, Response):
        return form
    field = form.get("field", "")
    if set(form) != {"field", "value"} or field not in EDITABLE_FIELDS:
        return _invalid()
    typed = _clean(form["value"])

    def run() -> Response:
        try:
            added = _lists(request).add(field, typed)
        except ControlledValueInvalid as error:
            state = ListsState(group=field, scope=ADD, errors=error.fields, typed={"value": typed})
            return _refused(request, state, "Value not added.")
        message = f"Added {added.label} to {group_name(field)}."
        state = ListsState(group=field, target=added.id, message=message, focus="row")
        return _answer(request, state, announce=message)

    return await run_in_threadpool(run)


async def save_aliases(request: Request, value_id: str) -> Response:
    form = await single_value_form(request, {"version", "aliases"})
    if isinstance(form, Response):
        return form
    version = posted_version(form, minimum=1)
    if version is None or "aliases" not in form:
        return _invalid()
    typed = _clean(form["aliases"])

    def run() -> Response:
        current = _lists(request).get(value_id)
        if current is None:
            return _not_found()
        group = current.field
        try:
            saved = _lists(request).set_aliases(value_id, typed, expected_version=version)
        except VersionConflict:
            return _conflict(request, group)
        except ControlledValueNotFound:
            return _not_found()
        except ControlledValueInvalid as error:
            state = ListsState(
                group=group,
                scope=ALIASES,
                target=value_id,
                errors=error.fields,
                typed={"aliases": typed},
            )
            return _refused(request, state, "Other spellings not saved.")
        message = f"Saved the other spellings of {saved.label}."
        state = ListsState(group=group, target=value_id, message=message, focus="row")
        return _answer(request, state, announce=message)

    return await run_in_threadpool(run)


async def delete_value(request: Request, value_id: str) -> Response:
    form = await single_value_form(request, {"version"})
    if isinstance(form, Response):
        return form
    version = posted_version(form, minimum=1)
    if version is None:
        return _invalid()

    def run() -> Response:
        current = _lists(request).get(value_id)
        if current is None:
            return _not_found()
        group = current.field
        try:
            gone = _lists(request).delete(value_id, expected_version=version)
        except VersionConflict:
            return _conflict(request, group)
        except ControlledValueNotFound:
            return _not_found()
        except ControlledValueInUse as error:
            state = ListsState(
                group=group, scope=DELETE, target=value_id, errors={"delete": error.user_message}
            )
            return _answer(
                request,
                state,
                status_code=409,
                announce=f"{current.label} was not deleted. {error.user_message}",
            )
        message = f"Deleted {gone.label} from {group_name(group)}."
        state = ListsState(group=group, message=message, focus="summary")
        return _answer(request, state, announce=message)

    return await run_in_threadpool(run)


# --- skills --------------------------------------------------------------------------------


async def save_skill_aliases(request: Request, skill_id: str) -> Response:
    form = await single_value_form(request, {"version", "aliases"})
    if isinstance(form, Response):
        return form
    version = posted_version(form, minimum=1)
    if version is None or "aliases" not in form:
        return _invalid()
    typed = _clean(form["aliases"])

    def run() -> Response:
        skills: SkillService | None = request.app.state.skills
        known = None if skills is None or not is_safe_id(skill_id) else skills
        current = (
            None if known is None else next((s for s in known.list() if s.id == skill_id), None)
        )
        if known is None or current is None:
            return _not_found(SKILL_NOT_FOUND)
        if current.version != version:  # stale first, whatever was typed
            return _conflict(request, "skills")
        try:
            lines = skill_alias_lines(typed)
            saved = known.set_aliases(skill_id, lines, expected_version=version)
        except VersionConflict:
            return _conflict(request, "skills")
        except FactNotFound:
            return _not_found(SKILL_NOT_FOUND)
        except (ControlledValueInvalid, SkillAliasTaken) as error:
            errors = (
                error.fields
                if isinstance(error, ControlledValueInvalid)
                else {"aliases": SKILL_NAME_TAKEN}
            )
            state = ListsState(
                group="skills",
                scope=ALIASES,
                target=skill_id,
                errors=errors,
                typed={"aliases": typed},
            )
            return _refused(request, state, "Other spellings not saved.")
        message = f"Saved the other spellings of {saved.name}."
        state = ListsState(group="skills", target=skill_id, message=message, focus="row")
        return _answer(request, state, announce=message)

    return await run_in_threadpool(run)
