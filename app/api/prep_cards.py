"""Narrative cards in the Prep tab (M8.2 part B): a read-only view, a metered draft start, the
edit, remove, confirm, approve and reuse actions, and the salary card built in code. Every route
answers the ``#prep`` fragment with the cards view. GET needs the token; POSTs need token,
origin and the card's ``version`` as last seen ("" while it has none).

- ``GET  /applications/{id}/prep/cards``
- ``POST .../cards/{kind}/generate`` (``version``, ``tier`` and the budget dialog's fields)
- ``POST .../cards/{kind}/units/{unit}/{edit|remove|confirm}`` (``version``, and ``text``)
- ``POST .../cards/{kind}/approve``, ``.../cards/{kind}/use`` (``version``, ``source``)
- ``POST .../cards/salary`` (``version``)

A citation, a section or an origin is never posted. An unknown application, kind, unit or
version is the fixed 404 or 409.
"""

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.budget_gate import dialog_context, reason_context, refusal_message
from app.api.errors import INVALID_REQUEST
from app.api.forms import is_htmx, single_value_form
from app.api.templating import templates
from app.domain.errors import (
    ApplicationNotFound,
    ProviderNotConfigured,
    VersionConflict,
    message_for_kind,
)
from app.domain.ids import is_safe_id
from app.domain.narrative import CARD_KINDS
from app.services.budget_gate import Start, confirmation_from_form
from app.services.cv_import import tier_from_form
from app.services.documents import (
    ApprovalBlocked,
    GenerateNotReady,
    NothingToApprove,
    UnitInvalid,
    UnitNotFound,
)
from app.services.prep_documents import SALARY, PrepDocumentService, ReuseRefused

FRAGMENT = "fragments/prep/_cards.html"
ANNOUNCE = {
    "generate": "Card drafting started.",
    "edit": "Paragraph saved.",
    "offer": "Paragraph saved. Save this as a fact?",
    "new_draft": "Saved as a new draft. The approved version is unchanged.",
    "remove": "Paragraph removed.",
    "confirm": "Saved as a fact. The paragraph now cites it.",
    "approve": "Card approved.",
    "use": "Card copied as a new draft. Review it before approving.",
    "salary": "Salary card built and approved.",
    "salary_removed": "Line removed. The salary card stays approved.",
}
_GENERATE_FIELDS = {
    "version",
    "tier",
    "budget_chosen_tier",
    "budget_service",
    "budget_threshold",
    "budget_cycle_start",
}
_MAX_SOURCE_CHARS = 200


def register_card_routes(app: FastAPI) -> None:
    base = "/applications/{application_id}/prep/cards"
    app.add_api_route(base, show, methods=["GET"])
    app.add_api_route(base + "/salary", salary, methods=["POST"])
    app.add_api_route(base + "/{kind}/generate", generate, methods=["POST"])
    app.add_api_route(base + "/{kind}/approve", approve, methods=["POST"])
    app.add_api_route(base + "/{kind}/use", use, methods=["POST"])
    app.add_api_route(base + "/{kind}/units/{unit_id}/{action}", unit_action, methods=["POST"])


def _service(request: Request) -> PrepDocumentService:
    service: PrepDocumentService = request.app.state.prep_documents
    return service


def cards_context(
    request: Request, aid: str, *, message: str = "", focus: str = "cards-heading", card: str = ""
) -> dict[str, Any]:
    """What the cards fragment needs. ``focus`` is the id of the element that takes focus;
    ``card`` is the kind the ``message`` belongs to (shown inside that card), "" for a message
    about the whole page."""
    service = _service(request)
    view = service.view(aid)
    reason = None
    try:
        plan = service.plan_start(aid, CARD_KINDS[0])
        block = request.app.state.budget_gate.blocked(plan.service)
        if block:
            reason = reason_context(block.message, reason_id="cards-reason", focus=False)
    except GenerateNotReady:
        pass  # each card says what it needs
    except ProviderNotConfigured as error:
        reason = reason_context(error.user_message, reason_id="cards-reason", focus=False)
    return {
        "view": view,
        "base": f"/applications/{aid}/prep/cards",
        "prep_url": f"/applications/{aid}/prep?focus=1",
        "page_url": f"/applications/{aid}?tab=prep",
        "message": message,
        "card": card,
        "error": False,
        "focus": focus,
        "reason": reason,
        "dialog": None,
        "conflict": False,
        "invalid_unit": "",
        "posted_text": "",
        "recovered_kind": "",
        "active": any(card.task.active for card in view.cards),
        "errors": {
            card.kind: message_for_kind(card.task.error)
            for card in view.cards
            if card.task.attention and card.task.error
        },
    }


def _answer(request: Request, shown: dict[str, Any], *, status: int = 200) -> Response:
    response = templates.TemplateResponse(request, FRAGMENT, {"cards": shown}, status_code=status)
    if shown["message"] and not shown["error"]:  # an error is a role="alert": it says itself
        response.headers["HX-Trigger"] = json.dumps({"announce": shown["message"]})
    return response


def _not_found() -> Response:
    return PlainTextResponse(ApplicationNotFound.message, status_code=404)


def _heading(kind: str) -> str:
    return f"card-{kind}-heading"


def _failed(
    request: Request, aid: str, kind: str, message: str, *, status: int, **changes: Any
) -> Response:
    shown = cards_context(request, aid, message=message, focus=_heading(kind), card=kind)
    shown.update(error=True, **changes)
    return _answer(request, shown, status=status)


def _typed(text: str) -> str:
    """Posted text as it may be shown again (no NUL)."""
    return text.replace("\x00", "")


def _conflict(request: Request, aid: str, kind: str, typed: str = "") -> Response:
    """A stale ``version``: the page-level message and reload prompt; typed paragraph text (an
    edit) is shown again in a read-only box inside its card so it can be copied."""
    return _failed(
        request,
        aid,
        kind,
        VersionConflict.message,
        status=409,
        conflict=True,
        card="",
        recovered_kind=kind if typed.strip() else "",
        posted_text=_typed(typed),
    )


def _posted_version(posted: dict[str, str]) -> str | None:
    """The posted ``version`` (a safe id, or "" while the card has none); None when missing or
    malformed."""
    version = posted.get("version")
    if version is None or (version != "" and not is_safe_id(version)):
        return None
    return version


def show(request: Request, application_id: str) -> Response:
    try:
        refresh = request.query_params.get("refresh") == "1"
        return _answer(
            request,
            cards_context(request, application_id, focus="" if refresh else "cards-heading"),
        )
    except ApplicationNotFound:
        return _not_found()


async def generate(request: Request, application_id: str, kind: str) -> Response:
    if kind not in CARD_KINDS:
        return _not_found()
    posted = await single_value_form(request, _GENERATE_FIELDS)
    if isinstance(posted, Response):
        return posted
    version = _posted_version(posted)
    if version is None:
        return PlainTextResponse(INVALID_REQUEST, status_code=400)

    def start() -> Response:
        service = _service(request)
        try:
            form = {k: [v] for k, v in posted.items()}
            tier = tier_from_form(form)
            plan = service.plan_start(application_id, kind, tier=tier)
            gate = request.app.state.budget_gate.check(
                plan.service, tier=plan.tier, confirmation=confirmation_from_form(form)
            )
            if isinstance(gate, Start):
                service.start_card(application_id, kind, version=version or None, tier=tier)
                shown = cards_context(
                    request,
                    application_id,
                    message=ANNOUNCE["generate"],
                    focus=_heading(kind),
                    card=kind,
                )
                return _answer(request, shown)
            shown = cards_context(request, application_id, focus="")
            dialog = (
                dialog_context(
                    gate,
                    dialog_id="cards-budget-dialog",
                    action=f"{shown['base']}/{kind}/generate",
                    target="#prep",
                    fields=posted,
                    return_focus=f"#card-{kind}-generate",
                )
                if is_htmx(request)
                else None
            )
            shown["dialog"] = dialog
            if dialog is None:
                shown["reason"] = reason_context(
                    refusal_message(gate), reason_id="cards-reason", focus=True
                )
            return _answer(request, shown, status=200 if dialog else 409)
        except ApplicationNotFound:
            return _not_found()
        except VersionConflict:
            return _conflict(request, application_id, kind)
        except (GenerateNotReady, ProviderNotConfigured) as error:
            message = (
                error.user_message if isinstance(error, ProviderNotConfigured) else error.message
            )
            return _failed(request, application_id, kind, message, status=409)
        except ValueError:
            return PlainTextResponse(INVALID_REQUEST, status_code=400)

    return await run_in_threadpool(start)


async def salary(request: Request, application_id: str) -> Response:
    posted = await single_value_form(request, {"version"})
    if isinstance(posted, Response):
        return posted
    version = _posted_version(posted)
    if version is None:
        return PlainTextResponse(INVALID_REQUEST, status_code=400)

    def build() -> Response:
        try:
            _service(request).build_salary(application_id, version=version or None)
            shown = cards_context(
                request,
                application_id,
                message=ANNOUNCE["salary"],
                focus=_heading(SALARY),
                card=SALARY,
            )
            return _answer(request, shown)
        except ApplicationNotFound:
            return _not_found()
        except VersionConflict:
            return _conflict(request, application_id, SALARY)
        except GenerateNotReady as error:
            return _failed(request, application_id, SALARY, error.message, status=409)

    return await run_in_threadpool(build)


async def approve(request: Request, application_id: str, kind: str) -> Response:
    if kind not in CARD_KINDS:
        return _not_found()
    return await _mutate(request, application_id, kind, "approve")


async def use(request: Request, application_id: str, kind: str) -> Response:
    if kind not in CARD_KINDS:
        return _not_found()
    posted = await single_value_form(request, {"version", "source"})
    if isinstance(posted, Response):
        return posted
    version = _posted_version(posted)
    source = posted.get("source")
    if version is None or source is None or len(source) > _MAX_SOURCE_CHARS:
        return PlainTextResponse(INVALID_REQUEST, status_code=400)

    def copy() -> Response:
        try:
            _service(request).use_card(application_id, kind, source, version=version or None)
            shown = cards_context(
                request, application_id, message=ANNOUNCE["use"], focus=_heading(kind), card=kind
            )
            return _answer(request, shown)
        except ApplicationNotFound:
            return _not_found()
        except VersionConflict:
            return _conflict(request, application_id, kind)
        except ReuseRefused as error:
            return _failed(request, application_id, kind, error.message, status=422)

    return await run_in_threadpool(copy)


async def unit_action(
    request: Request, application_id: str, kind: str, unit_id: str, action: str
) -> Response:
    if (
        action not in ("edit", "remove", "confirm")
        or kind not in (*CARD_KINDS, SALARY)
        or (kind == SALARY and action != "remove")
        or not is_safe_id(unit_id)
    ):
        return PlainTextResponse(UnitNotFound().message, status_code=404)
    return await _mutate(request, application_id, kind, action, unit_id)


async def _mutate(request: Request, aid: str, kind: str, action: str, uid: str = "") -> Response:
    allowed = {"version"} | ({"text"} if action == "edit" else set())
    posted = await single_value_form(request, allowed)
    if isinstance(posted, Response):
        return posted
    version = posted.get("version", "")
    if not is_safe_id(version) or (action == "edit" and "text" not in posted):
        return PlainTextResponse(INVALID_REQUEST, status_code=400)

    def save() -> Response:
        service = _service(request)
        try:
            offer = None
            was_approved = False
            if action == "edit":
                edited = service.edit_unit(aid, kind, version, uid, posted["text"])
                offer, was_approved = edited.offer, edited.was_approved
            elif action == "remove":
                was_approved = service.remove_unit_outcome(aid, kind, version, uid).was_approved
            elif action == "confirm":
                was_approved = service.resolve_flag_confirm(aid, kind, version, uid).was_approved
            else:
                service.approve(aid, kind, version)
            if offer:
                message = ANNOUNCE["offer"]
            elif kind == SALARY:
                message = ANNOUNCE["salary_removed"]  # built by code: it stays approved
            elif was_approved and action != "approve":
                message = ANNOUNCE["new_draft"]
            else:
                message = ANNOUNCE[action]
            return _answer(
                request,
                cards_context(request, aid, message=message, focus=_heading(kind), card=kind),
            )
        except ApplicationNotFound:
            return _not_found()
        except UnitNotFound as error:
            return PlainTextResponse(error.message, status_code=404)
        except VersionConflict:
            return _conflict(request, aid, kind, posted.get("text", "") if action == "edit" else "")
        except UnitInvalid as error:
            return _failed(
                request,
                aid,
                kind,
                error.message,
                status=422,
                invalid_unit=uid,
                posted_text=_typed(posted.get("text", "")),
            )
        except (ApprovalBlocked, NothingToApprove) as error:
            return _failed(request, aid, kind, error.message, status=409)

    return await run_in_threadpool(save)
