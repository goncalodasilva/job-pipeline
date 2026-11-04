"""Settings > Prompts: a page per AI task and its protected editor (M9.3 part B; ADR-055; ADR-131
item 7; spec 2.9 rules 2, 3, 12 and 13, 3.3 and 3.6). The list page is an area page
(``app/api/settings_areas.py``). The routes reach prompts only through the ``PromptSettings``
service (``app.state.prompt_settings``, over the prompt editor) and take only the tasks of
``DEFAULT_TIERS``; any other name is the same fixed plain-text 404 on every route, never echoed.

- ``GET`` and ``HEAD /settings/prompts/{task}`` is a ``PAGE_TAG`` route: a token-free shell with
  the summary and a placeholder that loads the editor. A prompt override is user-written text that
  may hold personal data, so the page holds none of it.
- ``GET /settings/prompts/{task}/editor`` is the protected, read-only fragment with the status, the
  default text, the compare view and the forms.
- ``POST /settings/prompts/{task}`` (``version``, ``system``, ``user``) saves the override and
  ``POST /settings/prompts/{task}/reset`` (``version``) removes it. Both need the launch token and
  the origin. Each field appears once and nothing else does, otherwise a plain-text 400; the
  version is ``none`` or the 64-character hex content version the editor gave. The version is
  checked first: a stale one is a 409 with the reload prompt and changes nothing, whatever the text
  is. A text the safety checks refuse is a 422 with the typed text kept (a fixed message per
  reason, never the text). A success answers 200 for htmx with the editor, a status line that
  takes focus and an ``announce`` message (303 to the task page otherwise).
"""

import json
from dataclasses import replace

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.errors import INVALID_REQUEST
from app.api.forms import MAX_PROMPT_FORM_BYTES, is_htmx, single_value_form
from app.api.prompt_forms import (
    AREA_PATH,
    NOT_DONE,
    NOT_FOUND,
    NOT_SAVED,
    RESET,
    SAVED,
    PromptState,
    editor_context,
    is_content_version,
    is_task,
    prompt_settings,
    refusal_message,
)
from app.api.security import PAGE_TAG
from app.api.settings_areas import PROMPT_EDITOR_FRAGMENT, render_prompt_task
from app.api.templating import templates
from app.services.prompt_settings import (
    PromptRefusal,
    PromptSettings,
    PromptStale,
    PromptText,
    PromptUnavailable,
)

SAVE_FIELDS = {"version", "system", "user"}


def register_prompt_routes(app: FastAPI) -> None:
    """Add the routes directly to the app. Only the task page is a page (token-free)."""
    task_path = f"{AREA_PATH}/{{task}}"
    app.add_api_route(
        task_path,
        show_task,
        methods=["GET", "HEAD"],
        tags=[PAGE_TAG],
        response_class=HTMLResponse,
        name="page_settings_prompt",
    )
    app.add_api_route(
        f"{task_path}/editor",
        show_editor,
        methods=["GET"],
        response_class=HTMLResponse,
        name="settings_prompt_editor",
    )
    app.add_api_route(task_path, save_prompt, methods=["POST"], name="settings_prompt_save")
    app.add_api_route(
        f"{task_path}/reset", reset_prompt, methods=["POST"], name="settings_prompt_reset"
    )


def _known(request: Request, task: str) -> PromptSettings | None:
    """The prompt service, when ``task`` is one the app lists."""
    service = prompt_settings(request)
    return service if service is not None and is_task(task) else None


def _not_found() -> Response:
    return PlainTextResponse(NOT_FOUND, status_code=404)


def _invalid() -> Response:
    return PlainTextResponse(INVALID_REQUEST, status_code=400)


def _failed() -> Response:
    return PlainTextResponse(NOT_DONE, status_code=500)


def _clean(text: str) -> str:
    return text.replace("\x00", "")


def _answer(
    request: Request,
    task: str,
    editor: PromptSettings,
    state: PromptState,
    *,
    status_code: int = 200,
    announce: str = "",
) -> Response:
    """The editor for htmx, the whole task page otherwise; a plain success goes back to it."""
    if status_code == 200 and not is_htmx(request):
        return RedirectResponse(f"{AREA_PATH}/{task}", status_code=303)
    state = replace(state, refresh_summary=is_htmx(request) and status_code == 200)
    response = render_prompt_task(request, task, editor, state=state)
    response.status_code = status_code
    if announce and is_htmx(request):
        response.headers["HX-Trigger"] = json.dumps({"announce": announce})
    return response


def _conflict(request: Request, task: str, editor: PromptSettings) -> Response:
    return _answer(request, task, editor, PromptState(conflict=True), status_code=409)


# --- reading -------------------------------------------------------------------------------


def show_task(request: Request, task: str) -> Response:
    """The token-free shell. A plain function: FastAPI runs it in its thread pool."""
    editor = _known(request, task)
    if editor is None:
        return _not_found()
    try:
        return render_prompt_task(request, task, editor)
    except PromptUnavailable:
        return _not_found()


def show_editor(request: Request, task: str) -> Response:
    """The protected editor fragment (read only; it writes nothing)."""
    editor = _known(request, task)
    if editor is None:
        return _not_found()
    try:
        context = editor_context(editor, task, editor.status(task), None)
    except PromptUnavailable:
        return _not_found()
    return templates.TemplateResponse(request, PROMPT_EDITOR_FRAGMENT, {"editor": context})


# --- saving and resetting --------------------------------------------------------------------


async def save_prompt(request: Request, task: str) -> Response:
    editor = _known(request, task)
    if editor is None:
        return _not_found()
    form = await single_value_form(request, SAVE_FIELDS, limit=MAX_PROMPT_FORM_BYTES)
    if isinstance(form, Response):
        return form
    if set(form) != SAVE_FIELDS or not is_content_version(form["version"]):
        return _invalid()
    version, typed = form["version"], PromptText(_clean(form["system"]), _clean(form["user"]))

    def run() -> Response:
        try:  # the service checks the version first, whatever was typed
            editor.save(task, typed.system, typed.user, version=version)
        except PromptStale:
            return _conflict(request, task, editor)
        except PromptRefusal as refused:
            state = PromptState(reason=refused.reason, typed=typed)
            said = f"{NOT_SAVED} {refusal_message(refused.reason)}"
            return _answer(request, task, editor, state, status_code=422, announce=said)
        except PromptUnavailable:
            return _failed()
        return _answer(request, task, editor, PromptState(message=SAVED), announce=SAVED)

    return await run_in_threadpool(run)


async def reset_prompt(request: Request, task: str) -> Response:
    editor = _known(request, task)
    if editor is None:
        return _not_found()
    form = await single_value_form(request, {"version"})
    if isinstance(form, Response):
        return form
    if set(form) != {"version"} or not is_content_version(form["version"]):
        return _invalid()
    version = form["version"]

    def run() -> Response:
        try:
            editor.reset(task, version=version)
        except PromptStale:
            return _conflict(request, task, editor)
        except PromptUnavailable:
            return _failed()
        return _answer(request, task, editor, PromptState(message=RESET), announce=RESET)

    return await run_in_threadpool(run)
