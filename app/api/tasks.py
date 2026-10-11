"""Routes that start background tasks.

``POST /tasks/practice`` (launch token and origin; not a page; M2.17, ADR-076 item 2) starts
the practice task from Settings > Testing tools. While the task isn't offered it answers 404
with a fixed plain message, whatever was sent. ``duration``, ``outcome`` and ``tier`` must
each be a value from the fixed lists: anything else answers 422 and is never shown back. For
htmx the 422 is the section with a fixed message; for a plain request, which only a hand-made
form can send, it is that message as text, because the full page's other copy could happen
to contain what was sent.

Before it queues anything, a start asks the budget gate (M2.24; ``BudgetGate`` over
``UsageService.check_start``) about the fake service and the chosen tier. A warning not yet
confirmed this cycle answers 200 (htmx) with the section and the warning dialog, whose
"Continue" posts the same values with ``budget_service``, ``budget_threshold`` and
``budget_cycle_start``; those count only when they name the warning that is due right now
for the fake service. A Thorough cap with Fast free answers the dialog with "Run once on
Fast" (the same start with ``tier=fast`` and, in ``budget_chosen_tier``, the tier the form
showed, which it keeps showing). The blocking point, a cap without that offer and unreadable
budgets answer 409 with the reason. A plain request gets 409 with the reason as text.
Nothing is queued in any of these cases.

A start answers 303 to ``/settings?practice=started#testing-tools`` (``already`` when a task
was already queued or running: it is kept and no second one starts), or for htmx 200 with the
Testing tools section, the accepted choices still selected, focus on its status line and the
same words in an "announce" trigger (as Settings > Providers does after a save). A start past
a confirmed warning also shows a note with the count, added to the announcement.

Task status (M2.19; fragments only, the same with or without ``HX-Request``):
``GET /tasks/panel`` and ``GET /tasks/{id}`` need the launch token and change nothing
(spec 2.9 rule 12). ``POST /tasks/{id}/cancel`` and ``POST /tasks/{id}/retry`` need the token
and origin and answer the task's fragment with focus on its status and the outcome in an
"announce" trigger: for a retry, the task now queued or running (an active duplicate when
there is one), or 409 with the task as it is and the reason when it can't be retried. Those
last two also trigger ``tasks:resync``, so whatever lists tasks refetches.

``GET /tasks/indicator`` (M2.20; token; changes nothing) answers the top bar's count of
queued and running tasks in words. Its ``HX-Trigger`` always fires ``tasks:finished`` with
the recently finished tasks (``{"tasks": [{"key", "message", "age"}]}``: id and attempt, a
fixed sentence from the type's label and the outcome, and the whole seconds since it
finished), which the page announces once each. The count element also carries, in
``data-attention``, the ``<id>:<attempt>`` keys of the recent tasks that ended failed or
interrupted (M2.26): the page counts those it hasn't shown in the open panel yet.

Only an id of the safe shape (``is_task_id``) is looked up. Anything else, and any id that
isn't a task, answers 404 with one fixed fragment that repeats nothing from the request. The
id is a ``path`` parameter so that a GET, or a POST ending in ``/cancel`` or ``/retry``, with
slashes in the id gets that same answer. Other shapes get the framework's own fixed answers:
405 for a POST to any other path under ``/tasks/``, 404 for an id holding a line break.
"""

import json

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.budget_gate import FAST, TIER_FIELD, chosen_tier, refusal_message
from app.api.forms import is_htmx, read_form
from app.api.settings import (
    PRACTICE_PATH,
    PRACTICE_STATUS,
    practice_started_url,
    testing_tools_response,
)
from app.api.templating import templates
from app.domain.errors import PracticeNotOffered
from app.onboarding.registry import Form
from app.services.budget_gate import BudgetGate, Refusal, Start, confirmation_from_form
from app.services.practice import (
    PRACTICE_SERVICE,
    TIERS,
    PracticeChoice,
    PracticeService,
    choice_from_form,
)
from app.services.task_views import ActionView, TaskView, TaskViews

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
    # After the fixed paths above, which the id parameter would otherwise take.
    routes = (
        ("/tasks/panel", tasks_panel, "GET", "tasks_panel"),
        (INDICATOR_PATH, tasks_indicator, "GET", "tasks_indicator"),
        ("/tasks/{task_id:path}/cancel", cancel_task, "POST", "tasks_cancel"),
        ("/tasks/{task_id:path}/retry", retry_task, "POST", "tasks_retry"),
        ("/tasks/{task_id:path}", show_task, "GET", "tasks_show"),
    )
    for path, endpoint, method, name in routes:
        app.add_api_route(path, endpoint, methods=[method], response_class=HTMLResponse, name=name)


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
    # The budget first (M2.24): only ``Start`` lets the task be queued. A confirmation that
    # names the warning due right now is stored by this check; any other changes nothing.
    gate: BudgetGate = request.app.state.budget_gate
    answer = await run_in_threadpool(
        gate.check,
        PRACTICE_SERVICE,
        tier=choice.tier,
        confirmation=confirmation_from_form(form),
    )
    if not isinstance(answer, Start):
        return await run_in_threadpool(_not_started, request, choice, answer)
    try:
        ref = await run_in_threadpool(practice.start, choice)
    except PracticeNotOffered:  # the provider changed since the check above
        return _not_offered()
    flag = "started" if ref.created else "already"
    if not is_htmx(request):
        return RedirectResponse(practice_started_url(flag), status_code=303)
    message = PRACTICE_STATUS[flag]
    note = answer.note if ref.created else None
    response = await run_in_threadpool(
        testing_tools_response,
        request,
        status=message,
        selected=_shown_choice(form, choice),
        note=note,
    )
    announced = message if note is None else f"{message} {note}"
    response.headers["HX-Trigger"] = json.dumps({"announce": announced})
    return response


def _shown_choice(form: Form, choice: PracticeChoice) -> dict[str, str]:
    """What the form shows after a start: the accepted values, except that a run moved to
    Fast for once ("Run once on Fast") keeps the tier the user chose."""
    shown = dict(choice.as_form())
    kept = chosen_tier(form, TIERS)
    if kept is not None and choice.tier == FAST:
        shown[TIER_FIELD] = kept
    return shown


def _not_started(request: Request, choice: PracticeChoice, answer: Refusal) -> Response:
    """The budget gate's answer to a start it didn't let through. For htmx the section: 200
    with the dialog when there is something to confirm, else 409 with the reason (the
    blocked button, or a line with a link to the budgets). A plain request, which only a
    hand-made form can send, gets the reason as text. Blocking."""
    if not is_htmx(request):
        return PlainTextResponse(refusal_message(answer), status_code=409)
    response = testing_tools_response(request, selected=choice.as_form(), refusal=answer)
    if not answer.asks:
        response.status_code = 409
    return response


def _not_offered() -> PlainTextResponse:
    return PlainTextResponse(MSG_NOT_OFFERED, status_code=404)


# The routes below are plain functions: FastAPI runs them in its thread pool, so their
# database reads never block the event loop.

RESYNC_EVENT = "tasks:resync"
INDICATOR_PATH = "/tasks/indicator"
FINISHED_EVENT = "tasks:finished"


def _views(request: Request) -> TaskViews:
    views: TaskViews = request.app.state.task_views
    return views


def _not_found(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "fragments/tasks/_not_found.html", {}, status_code=404
    )


def _task(request: Request, task: TaskView, *, focus: bool = False) -> HTMLResponse:
    """``focus``: the status takes focus after the swap (an answer to a button only; a
    refresh must never move focus)."""
    ctx = {"task": task, "refresh": True, "focus": focus}
    return templates.TemplateResponse(request, "fragments/tasks/_task.html", ctx)


def _acted(request: Request, action: ActionView | None) -> HTMLResponse:
    """The task after a cancel or retry: focus on its status, the outcome for the page's
    live region and, when the page may show stale tasks, a resync for whatever lists them."""
    if action is None:
        return _not_found(request)
    response = _task(request, action.task, focus=True)
    if action.refused:
        response.status_code = 409
    events: dict[str, str] = {}
    if action.announce is not None:
        events["announce"] = action.announce
    if action.resync:
        events[RESYNC_EVENT] = ""
    if events:
        response.headers["HX-Trigger"] = json.dumps(events)
    return response


def tasks_panel(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "fragments/tasks/_panel.html", {"tasks": _views(request).panel()}
    )


def tasks_indicator(request: Request) -> HTMLResponse:
    indicator = _views(request).indicator()
    response = templates.TemplateResponse(
        request,
        "fragments/tasks/_indicator.html",
        {"count": indicator.count, "attention": indicator.attention},
    )
    finished = [
        {"key": task.key, "message": task.message, "age": task.age} for task in indicator.finished
    ]
    response.headers["HX-Trigger"] = json.dumps({FINISHED_EVENT: {"tasks": finished}})
    return response


def show_task(request: Request, task_id: str) -> HTMLResponse:
    task = _views(request).get(task_id)
    return _not_found(request) if task is None else _task(request, task)


def cancel_task(request: Request, task_id: str) -> HTMLResponse:
    return _acted(request, _views(request).cancel(task_id))


def retry_task(request: Request, task_id: str) -> HTMLResponse:
    return _acted(request, _views(request).retry(task_id))
