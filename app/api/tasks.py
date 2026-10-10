"""Routes that start background tasks.

``POST /tasks/practice`` (launch token and origin; not a page; M2.17, ADR-076 item 2) starts
the practice task from Settings > Testing tools. While the task isn't offered it answers 404
with a fixed plain message, whatever was sent. ``duration``, ``outcome`` and ``tier`` must
each be a value from the fixed lists: anything else answers 422 and is never shown back. For
htmx the 422 is the section with a fixed message; for a plain request, which only a hand-made
form can send, it is that message as text, because the full page's other copy could happen
to contain what was sent.

A start answers 303 to ``/settings?practice=started#testing-tools`` (``already`` when a task
was already queued or running: it is kept and no second one starts), or for htmx 200 with the
Testing tools section, the accepted choices still selected, focus on its status line and the
same words in an "announce" trigger (as Settings > Providers does after a save).
"""

import json

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.forms import is_htmx, read_form
from app.api.settings import (
    PRACTICE_PATH,
    PRACTICE_STATUS,
    practice_started_url,
    testing_tools_response,
)
from app.domain.errors import PracticeNotOffered
from app.services.practice import PracticeService, choice_from_form

MSG_INVALID = "Choose one of the listed values for each field."
# Never the section's heading: a 404 must not look like the section.
MSG_NOT_OFFERED = (
    "The practice task is only available while the fake provider is active. Reload the page."
)


def register_task_routes(app: FastAPI) -> None:
    app.add_api_route(
        PRACTICE_PATH,
        start_practice,
        methods=["POST"],
        response_class=HTMLResponse,
        name="tasks_practice",
    )


async def start_practice(request: Request) -> Response:
    practice: PracticeService = request.app.state.practice
    form = await read_form(request)
    # Blocking (database): never on the event loop.
    if not await run_in_threadpool(practice.offered):
        return _not_offered()
    if form is None:
        return HTMLResponse("", status_code=413)
    choice = choice_from_form(form)
    if choice is None:
        if not is_htmx(request):
            return PlainTextResponse(MSG_INVALID, status_code=422)
        response = await run_in_threadpool(testing_tools_response, request, error=MSG_INVALID)
        response.status_code = 422
        return response
    try:
        ref = await run_in_threadpool(practice.start, choice)
    except PracticeNotOffered:  # the provider changed since the check above
        return _not_offered()
    flag = "started" if ref.created else "already"
    if not is_htmx(request):
        return RedirectResponse(practice_started_url(flag), status_code=303)
    message = PRACTICE_STATUS[flag]
    response = await run_in_threadpool(
        testing_tools_response, request, status=message, selected=choice.as_form()
    )
    response.headers["HX-Trigger"] = json.dumps({"announce": message})
    return response


def _not_offered() -> PlainTextResponse:
    return PlainTextResponse(MSG_NOT_OFFERED, status_code=404)
