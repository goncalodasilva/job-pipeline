"""Routes of Refine with AI (M3.8, M3.9; AC-M3.4, AC-M3.5; spec 2.9 rules 11 and 12). None is
a page: each needs the launch token, each POST the origin as well, and takes a URL-encoded
form.

- ``GET /facts/{id}/refine``: the ``#fact-refine`` panel as it is now (M3.9). It changes
  nothing. The panel's own refetch, while a turn is on its way or after one failed, sends
  ``was`` (the phase it showed): while that is still the phase the answer is 204 (nothing is
  swapped, so focus stays where it is); when the turn has since ended, the answer says so in
  a line and in the live region ("The AI's suggestion is ready.").
- ``POST /facts/{id}/refine``: open a conversation about the fact (or continue its open one)
  and queue a turn, through the budget gate as the other start routes do (its dialog fields
  and ``tier`` are accepted).
- ``POST /facts/{id}/refine/answer`` (``answer``): add the user's answer and queue the next
  turn, through the budget gate; at most three answers.
- ``POST /facts/{id}/refine/accept`` (``version``: the fact's version as last seen; the
  wording: ``text`` or ``situation``, ``action`` and ``result``; ``acknowledged``, once per
  claim the user confirms is true): save the wording as a new version and close the
  conversation. The server checks the posted wording itself. The htmx answer also replaces,
  out of band, the editor's wording fields and version, its archive block and the history.
- ``POST /facts/{id}/refine/discard``: close the conversation; the fact is untouched.

Success answers 200 for htmx with the ``#fact-refine`` panel, or 303 back to the fact's
editor page. Refusals repeat nothing of the request: 404 an unknown fact or an id of another
shape (the fixed message); 409 a "never use" fact, a discarded or archived one, a budget
refusal, a turn still on its way, a start or an answer when no question waits, a fourth
answer, no open conversation, nothing to accept, a stale ``version`` or suggestion, or a fact
sent that became "never use"; 422 a blank answer, blank wording, or wording with claims
neither acknowledged nor edited out (each named, with a checkbox). An htmx refusal is the
panel with the reason, keeping what was typed and ticked; a plain request gets the reason as
text.
"""

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
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
from app.services.fact_editor import FactEditor
from app.services.fact_history import FactHistory
from app.services.fact_refine import (
    PHASE_ANSWERED,
    PHASE_FAILED,
    PHASE_WAITING,
    FactRefineService,
    RefinePanel,
    StartPlan,
    tier_from_form,
    wording_of,
)

FRAGMENT: Final = "fragments/facts/_refine_answer.html"
TARGET: Final = "#fact-refine"
DIALOG_ID: Final = "fact-refine-budget"
REASON_ID: Final = "fact-refine-budget-reason"
FOCUS: Final = "#fact-refine-heading"
MSG_STARTED: Final = "The AI is reading your fact."
MSG_ANSWERED: Final = "Your answer was sent."
MSG_ACCEPTED: Final = "The wording was accepted."
MSG_DISCARDED: Final = "The suggestion was discarded. Your fact is unchanged."
MSG_STOPPED: Final = "Stopped. Your fact is unchanged."
MSG_READY: Final = "The AI's suggestion is ready."
MSG_QUESTIONS_READY: Final = "The AI's questions are ready."
MSG_FAILED: Final = "The AI couldn't answer this time. Your fact is unchanged."
WAS_FIELD: Final = "was"  # the phase the refetching panel showed
_REFETCHED_FROM: Final = (PHASE_WAITING, PHASE_FAILED)
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
_TEXT_LABEL: Final = "Wording to accept (you can edit it)"
_STORY_PARTS: Final = (("situation", "Situation"), ("action", "Action"), ("result", "Result"))


@dataclass(frozen=True)
class WordingField:
    """One field of the Accept form: the suggestion's part, or what a refused Accept sent
    (then ``suggested`` is the suggestion's part, when it differs)."""

    name: str
    label: str
    value: str
    suggested: str | None


@dataclass(frozen=True)
class ClaimRow:
    """A claim that isn't in the fact bank, with its checkbox. ``refused_for``: the wording
    the server found it in when it refused an Accept; while the wording is that, editing
    can't have taken it out."""

    text: str
    edits_out: bool
    checked: bool
    refused_for: str | None


def refine_path(fact_id: str, action: str = "") -> str:
    return f"{FACTS_PATH}/{fact_id}/refine" + (f"/{action}" if action else "")


def register_fact_refine_routes(app: FastAPI) -> None:
    """Register before the fact pages' routes: their ``{id:path}/discard`` pattern would
    also match ``{id}/refine/discard``."""
    app.add_api_route(
        FACTS_PATH + "/{fact_id:path}/refine",
        show_refine,
        methods=["GET"],
        response_class=HTMLResponse,
        name="facts_refine_panel",
    )
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


# --- what the panel shows -----------------------------------------------------------------------


def ready_message(panel: RefinePanel) -> str | None:
    """What to say when a turn has ended: its suggestion, its questions or its failure."""
    if panel.phase == PHASE_FAILED:
        return MSG_FAILED
    if panel.phase != PHASE_ANSWERED or panel.view is None:
        return None
    return MSG_READY if panel.view.suggestion is not None else MSG_QUESTIONS_READY


def _line(panel: RefinePanel, said: str | None) -> str | None:
    """The panel's status line: what the last action did, the waiting line or a failure."""
    if panel.phase == PHASE_WAITING:
        return (f"{said} " if said and said != MSG_STARTED else "") + MSG_STARTED
    if said:
        return said
    return MSG_FAILED if panel.phase == PHASE_FAILED else None


def _fields(panel: RefinePanel, typed: FactContent | None) -> tuple[WordingField, ...]:
    suggestion = None if panel.view is None else panel.view.suggestion
    if suggestion is None:
        return ()
    story = suggestion.text is None and panel.story
    parts = _STORY_PARTS if story else (("text", _TEXT_LABEL),)
    fields = []
    for name, label in parts:
        offered = getattr(suggestion, name) or ""
        value = offered if typed is None else (getattr(typed, name) or "")
        differs = typed is not None and value.strip() != offered.strip()
        fields.append(WordingField(name, label, value, offered if differs else None))
    return tuple(fields)


def _claims(
    panel: RefinePanel,
    refused: Sequence[str],
    acknowledged: frozenset[str],
    typed: FactContent | None,
) -> tuple[ClaimRow, ...]:
    """The suggestion's flagged claims, then any other a refused Accept found."""
    refused_for = wording_of(typed) if typed is not None and refused else None
    rows = [
        ClaimRow(
            claim.text,
            claim.edits_out,
            claim.text in acknowledged,
            refused_for if claim.text in refused else None,
        )
        for claim in panel.claims
    ]
    shown = {row.text for row in rows}
    rows += [
        ClaimRow(text, True, text in acknowledged, refused_for)
        for text in dict.fromkeys(refused)
        if text not in shown
    ]
    return tuple(rows)


def panel_context(
    panel: RefinePanel,
    *,
    said: str | None = None,
    error: str | None = None,
    claims: Sequence[str] = (),
    typed: FactContent | None = None,
    acknowledged: Iterable[str] = (),
    version: int | None = None,
    answer: str = "",
    dialog: Any = None,
    budget_reason: Any = None,
    error_field: str | None = None,
    oob: bool = False,
) -> dict[str, Any]:
    """The template's ``refine``: ``said`` is what an action did (its line takes focus),
    ``error`` why nothing happened, tied to the field ``error_field`` names when the panel
    shows it; ``claims``, ``typed``, ``acknowledged`` and ``version`` what a refused Accept
    found and sent; ``answer`` a refused answer's text; ``oob``: sent out of band (with
    Archive and Restore)."""
    rows = _claims(panel, claims, frozenset(acknowledged), typed)
    fields = _fields(panel, typed)
    blocked_open = panel.blocked is not None and panel.view is not None
    return {
        "fact_id": panel.fact_id,
        "panel": panel,
        "view": panel.view,
        "line": said if blocked_open else _line(panel, said),
        "announces_turn": said
        in (
            MSG_READY,
            MSG_QUESTIONS_READY,
            MSG_FAILED,
            MSG_ACCEPTED,
            MSG_DISCARDED,
            MSG_STOPPED,
        ),
        "focus": said is not None,
        "error": error,
        "error_field": _tied_field(panel, fields, error_field, blocked_open=blocked_open),
        "dialog": dialog,
        "budget_reason": budget_reason,
        "fields": fields,
        "claims": rows,
        "accept_blocked": panel.stale or any(not row.checked for row in rows),
        "version": version if version else panel.version,
        "answer": answer,
        "edit_field": "editor-situation" if panel.story else "editor-text",
        "blocked_open": blocked_open,
        "oob": oob,
        "editor": None,
        "history": None,
    }


def _tied_field(
    panel: RefinePanel,
    fields: Sequence[WordingField],
    name: str | None,
    *,
    blocked_open: bool,
) -> str | None:
    """The field a refusal's message goes with, when the panel shows that field: the answer
    box, or a wording field (a story's wording refusal goes with its first part)."""
    view = panel.view
    if name is None or view is None or blocked_open or panel.phase == PHASE_WAITING:
        return None
    if name == "answer":
        return name if view.questions and view.answers_left > 0 else None
    names = [field.name for field in fields]
    if name in names:
        return name
    return names[0] if names and name in _WORDING else None


def _fragment(request: Request, fact_id: str, status_code: int = 200, **shown: Any) -> HTMLResponse:
    ctx = {"refine": panel_context(_service(request).panel(fact_id), **shown)}
    return templates.TemplateResponse(request, FRAGMENT, ctx, status_code=status_code)


def _announcing(response: Response, said: str, *, then: str | None = None) -> Response:
    """``said`` in the live region; ``then`` once the swap has settled (a turn that ended
    before its start's answer was written)."""
    response.headers["HX-Trigger"] = json.dumps({"announce": said})
    if then:
        response.headers["HX-Trigger-After-Settle"] = json.dumps({"announce": then})
    return response


def show_refine(request: Request, fact_id: str) -> Response:
    """The panel alone. Reads only (blocking: FastAPI runs it in a worker thread)."""
    if (unknown := no_such_id(fact_id)) is not None:
        return unknown
    try:
        panel = _service(request).panel(fact_id)
    except FactNotFound as error:
        return not_found(error)
    was = request.query_params.get(WAS_FIELD)
    if was in _REFETCHED_FROM and panel.phase == was:
        # Nothing changed: no swap, so focus and anything in the panel stay as they are.
        return Response(status_code=204)
    ended = was in _REFETCHED_FROM and panel.phase != PHASE_WAITING
    ready = ready_message(panel) if ended else None
    ctx = {"refine": panel_context(panel)}
    if ready is not None:
        ctx["refine"]["line"] = ready
        ctx["refine"]["announces_turn"] = True
    response = templates.TemplateResponse(request, FRAGMENT, ctx)
    return response if ready is None else _announcing(response, ready)


# --- the actions --------------------------------------------------------------------------------


def _done(request: Request, fact_id: str, said: str, *, queued: bool = False) -> Response:
    if not is_htmx(request):
        return RedirectResponse(f"{FACTS_PATH}/{fact_id}/edit", status_code=303)
    panel = _service(request).panel(fact_id)
    # A turn can end before this answer is written: then it is shown, and said, at once.
    ready = ready_message(panel) if queued and panel.phase != PHASE_WAITING else None
    extra: dict[str, Any] = {}
    if said == MSG_ACCEPTED:
        editor: FactEditor = request.app.state.fact_editor
        history: FactHistory = request.app.state.fact_history
        extra = {"editor": editor.edit(fact_id), "history": history.of(fact_id)}
    ctx = {"refine": {**panel_context(panel, said=ready or said), **extra}}
    response = templates.TemplateResponse(request, FRAGMENT, ctx)
    return _announcing(response, said, then=ready)


def _refused(request: Request, fact_id: str, error: AppError, **shown: Any) -> Response:
    """The reason nothing happened, in a fixed message; the panel keeps what was sent."""
    if isinstance(error, FactNotFound):
        return not_found(error)
    status = 409 if isinstance(error, _CONFLICTS) else 422
    if isinstance(error, RefineClaimsUnacknowledged):
        if not is_htmx(request):
            lines = [error.user_message, *(f"- {claim}" for claim in error.claims)]
            return PlainTextResponse("\n".join(lines), status_code=status)
        shown["claims"] = error.claims
    if isinstance(error, FactInputInvalid) and error.field:
        shown["error_field"] = error.field
    if not is_htmx(request):
        return PlainTextResponse(error.user_message, status_code=status)
    return _fragment(request, fact_id, status, error=error.user_message, **shown)


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
    typed = fields.get("answer", "")
    if dialog is not None:
        return _fragment(request, fact_id, dialog=dialog, answer=typed)
    reason = reason_context(refusal_message(answer), reason_id=REASON_ID, focus=True)
    return _fragment(request, fact_id, 409, budget_reason=reason, answer=typed)


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
    typed: str = "",
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
        return _refused(request, fact_id, error, answer=typed)
    return _done(request, fact_id, said, queued=True)


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
        typed=text,
    )


def _accept(request: Request, fact_id: str, form: Form) -> Response:
    wording = FactContent(*(form[name][0] if form.get(name) else None for name in _WORDING))
    version = form_version(form.get("version", ()))
    acknowledged = form.get("acknowledged", ())
    try:
        _service(request).accept(
            fact_id, wording, expected_version=version, acknowledged=acknowledged
        )
    except _ACCEPT_ERRORS as error:
        return _refused(
            request,
            fact_id,
            error,
            typed=wording,
            acknowledged=acknowledged,
            version=version,
        )
    return _done(request, fact_id, MSG_ACCEPTED)


def _discard(request: Request, fact_id: str, form: Form) -> Response:
    """Discard, or Cancel while a turn is on its way: with no suggestion yet it says the
    conversation stopped, as nothing was suggested."""
    service = _service(request)
    try:
        before = service.panel(fact_id)
        service.discard(fact_id)
    except (RefineNotOpen, FactNotFound, VersionConflict) as error:
        return _refused(request, fact_id, error)
    suggested = before.view is not None and before.view.suggestion is not None
    return _done(request, fact_id, MSG_DISCARDED if suggested else MSG_STOPPED)


async def start_refine(request: Request, fact_id: str) -> Response:
    return await _posted(request, fact_id, _start)


async def answer_refine(request: Request, fact_id: str) -> Response:
    return await _posted(request, fact_id, _answer)


async def accept_refine(request: Request, fact_id: str) -> Response:
    return await _posted(request, fact_id, _accept)


async def discard_refine(request: Request, fact_id: str) -> Response:
    return await _posted(request, fact_id, _discard)
