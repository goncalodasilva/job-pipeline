"""Routes of Refine with AI (M3.8; AC-M3.4, AC-M3.5; spec 2.9 rules 11 and 12). None is a
page: each POST needs the launch token and the origin, and takes a URL-encoded form.

- ``POST /facts/{id}/refine``: open a conversation about the fact (or continue its open one)
  and queue a turn, through the budget gate as the other start routes do (its dialog fields
  and ``tier`` are accepted).
- ``POST /facts/{id}/refine/answer`` (``answer``): add the user's answer and queue the next
  turn, through the budget gate; at most three answers.
- ``POST /facts/{id}/refine/accept`` (``version``: the fact's version as last seen; the
  wording: ``text`` or ``situation``, ``action`` and ``result``; ``acknowledged``, once per
  claim the user confirms is true): save the wording as a new version and close the
  conversation. The server checks the posted wording itself.
- ``POST /facts/{id}/refine/discard``: close the conversation; the fact is untouched.

Success answers 200 for htmx with the ``#fact-refine`` fragment (M3.9 builds the panel on
it), or 303 back to the fact's editor page. Refusals repeat nothing of the request: 404 an
unknown fact or an id of another shape (the fixed message); 409 a "never use" fact, a
discarded or archived one, a budget refusal, a turn still on its way, a start or an answer
when no question waits, a fourth answer, no open conversation, nothing to accept, a stale
``version`` or suggestion, or a fact sent that became "never use"; 422 a blank answer, blank
wording, or wording with claims neither acknowledged nor edited out (each named). A plain
request gets the reason as text.
"""

import json
from collections.abc import Callable
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.budget_gate import dialog_context, reason_context, refusal_message
from app.api.errors import INVALID_REQUEST
from app.api.facts import FACTS_PATH, form_version, no_such_id, not_found
from app.api.forms import is_htmx, read_form
from app.api.templating import templates
from app.domain.errors import (
    AppError,
    FactInputInvalid,
    FactNotFound,
    FactTransitionRefused,
    ProviderNotConfigured,
    RefineAnswersUsed,
    RefineBusy,
    RefineClaimsUnacknowledged,
    RefineContextChanged,
    RefineNeverUse,
    RefineNoSuggestion,
    RefineNotAvailable,
    RefineNotOpen,
    VersionConflict,
)
from app.domain.facts import FactContent
from app.onboarding.registry import Form
from app.services.budget_gate import BudgetGate, Refusal, Start, confirmation_from_form
from app.services.fact_refine import FactRefineService, StartPlan, tier_from_form

FRAGMENT: Final = "fragments/facts/_refine.html"
TARGET: Final = "#fact-refine"
DIALOG_ID: Final = "fact-refine-budget"
REASON_ID: Final = "fact-refine-budget-reason"
FOCUS: Final = "#fact-refine-heading"
MSG_STARTED: Final = "The AI is reading your fact."
MSG_ANSWERED: Final = "Your answer was sent."
MSG_ACCEPTED: Final = "The wording was accepted."
MSG_DISCARDED: Final = "The suggestion was discarded. Your fact is unchanged."
_CONFLICTS: Final[tuple[type[AppError], ...]] = (
    RefineNeverUse,
    RefineContextChanged,
    RefineNotAvailable,
    RefineNotOpen,
    RefineBusy,
    RefineAnswersUsed,
    RefineNoSuggestion,
    VersionConflict,
    FactTransitionRefused,
    ProviderNotConfigured,
)
_START_ERRORS: Final[tuple[type[AppError], ...]] = (*_CONFLICTS, FactNotFound, FactInputInvalid)
_ACCEPT_ERRORS: Final[tuple[type[AppError], ...]] = (*_START_ERRORS, RefineClaimsUnacknowledged)
_WORDING: Final = ("text", "situation", "action", "result")


def refine_path(fact_id: str, action: str = "") -> str:
    return f"{FACTS_PATH}/{fact_id}/refine" + (f"/{action}" if action else "")


def register_fact_refine_routes(app: FastAPI) -> None:
    """Register before the fact pages' routes: their ``{id:path}/discard`` pattern would
    also match ``{id}/refine/discard``."""
    routes = (
        ("", start_refine, "facts_refine"),
        ("/answer", answer_refine, "facts_refine_answer"),
        ("/accept", accept_refine, "facts_refine_accept"),
        ("/discard", discard_refine, "facts_refine_discard"),
    )
    for suffix, endpoint, name in routes:
        path = FACTS_PATH + "/{fact_id:path}/refine" + suffix
        app.add_api_route(path, endpoint, methods=["POST"], response_class=HTMLResponse, name=name)


def _service(request: Request) -> FactRefineService:
    service: FactRefineService = request.app.state.fact_refine
    return service


def _fragment(request: Request, fact_id: str, status_code: int = 200, **shown: Any) -> HTMLResponse:
    shown_now = {"status": None, "error": None, "claims": (), "dialog": None, "budget_reason": None}
    view = _service(request).view(fact_id)
    ctx = {"refine": {"fact_id": fact_id, "view": view, **shown_now, **shown}}
    return templates.TemplateResponse(request, FRAGMENT, ctx, status_code=status_code)


def _done(request: Request, fact_id: str, said: str) -> Response:
    if not is_htmx(request):
        return RedirectResponse(f"{FACTS_PATH}/{fact_id}/edit", status_code=303)
    response = _fragment(request, fact_id, status=said)
    response.headers["HX-Trigger"] = json.dumps({"announce": said})
    return response


def _refused(request: Request, fact_id: str, error: AppError) -> Response:
    """The reason nothing happened, in a fixed message."""
    if isinstance(error, FactNotFound):
        return not_found(error)
    status = 409 if isinstance(error, _CONFLICTS) else 422
    if isinstance(error, RefineClaimsUnacknowledged):
        if not is_htmx(request):
            lines = [error.user_message, *(f"- {claim}" for claim in error.claims)]
            return PlainTextResponse("\n".join(lines), status_code=status)
        return _fragment(request, fact_id, status, error=error.user_message, claims=error.claims)
    if not is_htmx(request):
        return PlainTextResponse(error.user_message, status_code=status)
    return _fragment(request, fact_id, status, error=error.user_message)


def _not_started(
    request: Request, fact_id: str, action: str, form: Form, answer: Refusal
) -> Response:
    """The budget gate's answer to a turn it didn't let through: the dialog (200) when there
    is something to confirm, else 409 with the reason; a plain request gets the reason as
    409 text. Nothing is written."""
    if not is_htmx(request):
        return PlainTextResponse(refusal_message(answer), status_code=409)
    fields = {"answer": form["answer"][0]} if form.get("answer") else {}
    dialog = dialog_context(
        answer,
        dialog_id=DIALOG_ID,
        action=refine_path(fact_id, action),
        target=TARGET,
        fields=fields,
        return_focus=FOCUS,
    )
    if dialog is not None:
        return _fragment(request, fact_id, dialog=dialog)
    reason = reason_context(refusal_message(answer), reason_id=REASON_ID, focus=True)
    return _fragment(request, fact_id, 409, budget_reason=reason)


async def _posted(
    request: Request, fact_id: str, work: Callable[[Request, str, Form], Response]
) -> Response:
    """``work(request, fact_id, form)`` in a worker thread. An id of another shape is the
    fixed 404 before anything is read."""
    if (unknown := no_such_id(fact_id)) is not None:
        return unknown
    form = await read_form(request)
    if form is None:  # no form of these routes is that large
        return PlainTextResponse(INVALID_REQUEST, status_code=413)
    return await run_in_threadpool(work, request, fact_id, form)


def _gated(
    request: Request,
    fact_id: str,
    form: Form,
    *,
    action: str,
    plan: Callable[[], StartPlan],
    queue: Callable[[], object],
    said: str,
) -> Response:
    """Ask the budget gate about the turn ``plan`` describes; only a ``Start`` queues it."""
    gate: BudgetGate = request.app.state.budget_gate
    try:
        planned = plan()
        answer = gate.check(
            planned.service, tier=planned.tier, confirmation=confirmation_from_form(form)
        )
        if not isinstance(answer, Start):
            return _not_started(request, fact_id, action, form, answer)
        queue()
    except _START_ERRORS as error:
        return _refused(request, fact_id, error)
    return _done(request, fact_id, said)


def _start(request: Request, fact_id: str, form: Form) -> Response:
    service, tier = _service(request), tier_from_form(form)
    return _gated(
        request,
        fact_id,
        form,
        action="",
        plan=lambda: service.plan_start(fact_id, tier=tier),
        queue=lambda: service.start(fact_id, tier=tier),
        said=MSG_STARTED,
    )


def _answer(request: Request, fact_id: str, form: Form) -> Response:
    service, tier = _service(request), tier_from_form(form)
    given = form.get("answer", ())
    text = given[0] if len(given) == 1 else ""
    return _gated(
        request,
        fact_id,
        form,
        action="answer",
        plan=lambda: service.plan_answer(fact_id, text, tier=tier),
        queue=lambda: service.answer(fact_id, text, tier=tier),
        said=MSG_ANSWERED,
    )


def _accept(request: Request, fact_id: str, form: Form) -> Response:
    wording = FactContent(*(form[name][0] if form.get(name) else None for name in _WORDING))
    try:
        _service(request).accept(
            fact_id,
            wording,
            expected_version=form_version(form.get("version", ())),
            acknowledged=form.get("acknowledged", ()),
        )
    except _ACCEPT_ERRORS as error:
        return _refused(request, fact_id, error)
    return _done(request, fact_id, MSG_ACCEPTED)


def _discard(request: Request, fact_id: str, form: Form) -> Response:
    try:
        _service(request).discard(fact_id)
    except (RefineNotOpen, FactNotFound, VersionConflict) as error:
        return _refused(request, fact_id, error)
    return _done(request, fact_id, MSG_DISCARDED)


async def start_refine(request: Request, fact_id: str) -> Response:
    return await _posted(request, fact_id, _start)


async def answer_refine(request: Request, fact_id: str) -> Response:
    return await _posted(request, fact_id, _answer)


async def accept_refine(request: Request, fact_id: str) -> Response:
    return await _posted(request, fact_id, _accept)


async def discard_refine(request: Request, fact_id: str) -> Response:
    return await _posted(request, fact_id, _discard)
