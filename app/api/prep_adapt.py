"""Adapting an earlier answer in the practice screen of a question (M8.2 part C; AC-M8.1 carrying
answers over; AC-M8.4). Every route answers the ``#prep`` fragment of the practice view. The page
is ``GET /applications/{id}/prep/practice/{question}`` (``app/api/prep_practice.py``); POSTs need
token and origin.

- ``POST {p}/adapt`` (``version``, ``tier`` and the budget dialog's fields): the adapted answer's
  current version as last seen ("" while it has none)
- ``POST {p}/adapted/{version}/units/{unit}/{edit|remove|confirm}`` (``text`` for an edit)
- ``POST {p}/adapted/{version}/approve``

The version of a change is in the path. A citation, a section or an origin is never posted. An
unknown application, question, version id, unit or action is the fixed 404, a version that is
not the current one the reload prompt (409), and a missing, extra or repeated field a 400.
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.budget_gate import dialog_context, reason_context, refusal_message
from app.api.errors import INVALID_REQUEST
from app.api.forms import is_htmx, single_value_form
from app.api.prep_practice import practice_context, practice_response
from app.domain.errors import ApplicationNotFound, ProviderNotConfigured, VersionConflict
from app.domain.ids import is_safe_id
from app.services.budget_gate import Start, confirmation_from_form
from app.services.cv_import import tier_from_form
from app.services.documents import (
    ApprovalBlocked,
    GenerateNotReady,
    NothingToApprove,
    UnitInvalid,
    UnitNotFound,
)
from app.services.prep_documents import PrepDocumentService

HEADING = "adapt-heading"
ANNOUNCE = {
    "adapt": "Adapting your earlier answer.",
    "edit": "Paragraph saved.",
    "edit_claim": "Paragraph saved. Save this as a fact?",
    "edit_approved": "Saved as a new draft. The approved version is unchanged.",
    "remove": "Paragraph removed.",
    "confirm": "Saved as a fact. The paragraph now cites it.",
    "approve": "Answer approved.",
}
NOT_STARTED = "Nothing was started."
NOT_SAVED = "Your change was not saved."
NOT_CHANGED = "Nothing was changed."
_ADAPT_FIELDS = {
    "version",
    "tier",
    "budget_chosen_tier",
    "budget_service",
    "budget_threshold",
    "budget_cycle_start",
}
_CHANGES = ("edit", "remove", "confirm")


def register_adapt_routes(app: FastAPI) -> None:
    base = "/applications/{application_id}/prep/practice/{question_id}"
    app.add_api_route(base + "/adapt", adapt, methods=["POST"])
    app.add_api_route(base + "/adapted/{version_id}/approve", approve, methods=["POST"])
    app.add_api_route(
        base + "/adapted/{version_id}/units/{unit_id}/{action}", unit_action, methods=["POST"]
    )


def _service(request: Request) -> PrepDocumentService:
    service: PrepDocumentService = request.app.state.prep_documents
    return service


def _not_found(message: str = ApplicationNotFound.message) -> Response:
    return PlainTextResponse(message, status_code=404)


def _typed(text: str) -> str:
    """Posted text as it may be shown again (no NUL)."""
    return text.replace("\x00", "")


def _shown(
    request: Request,
    aid: str,
    qid: str,
    *,
    message: str = "",
    error: str = "",
    status: int = 200,
    focus: str = HEADING,
    **changes: Any,
) -> Response:
    """The practice fragment after an adapt action: ``message`` is announced, ``error`` is shown
    as an alert (and not announced twice)."""
    shown = practice_context(request, aid, qid, message=message, focus=focus)
    shown.update(message_in="adapt", adapt_error=error, **changes)
    return practice_response(request, shown, status=status)


def _conflict(request: Request, aid: str, qid: str, detail: str, typed: str = "") -> Response:
    """A stale version: the reload prompt (inside the adapt section) saying what it means for
    the action (``detail``); typed paragraph text (an edit) is shown again in a read-only box so
    it can be copied."""
    return _shown(
        request,
        aid,
        qid,
        status=409,
        focus="",
        conflict=True,
        conflict_detail=detail,
        recovered_text=_typed(typed) if typed.strip() else "",
    )


def _posted_version(posted: dict[str, str]) -> str | None:
    """The posted ``version`` (a safe id, or "" while there is none); None when missing or
    malformed."""
    version = posted.get("version")
    if version is None or (version != "" and not is_safe_id(version)):
        return None
    return version


async def adapt(request: Request, application_id: str, question_id: str) -> Response:
    posted = await single_value_form(request, _ADAPT_FIELDS)
    if isinstance(posted, Response):
        return posted
    version = _posted_version(posted)
    if version is None:
        return PlainTextResponse(INVALID_REQUEST, status_code=400)

    def start() -> Response:
        service = _service(request)
        form = {k: [v] for k, v in posted.items()}
        try:
            try:
                tier = tier_from_form(form)
                plan = service.plan_adapt(application_id, question_id, tier=tier)
            except GenerateNotReady:
                raise  # a ValueError too: answered below with its own message
            except ValueError:
                return PlainTextResponse(INVALID_REQUEST, status_code=400)
            gate = request.app.state.budget_gate.check(
                plan.service, tier=plan.tier, confirmation=confirmation_from_form(form)
            )
            if isinstance(gate, Start):
                service.start_adapt(application_id, question_id, version=version or None, tier=tier)
                return _shown(request, application_id, question_id, message=ANNOUNCE["adapt"])
            shown = practice_context(request, application_id, question_id, focus="")
            dialog = (
                dialog_context(
                    gate,
                    dialog_id="adapt-budget-dialog",
                    action=f"/applications/{application_id}/prep/practice/{question_id}/adapt",
                    target="#adapt",
                    select="#adapt",
                    fields=posted,
                    return_focus="#adapt-submit",
                )
                if is_htmx(request)
                else None
            )
            shown["adapt_dialog"] = dialog
            if dialog is None:
                shown["adapt_reason"] = reason_context(
                    refusal_message(gate), reason_id="adapt-reason", focus=True
                )
            return practice_response(request, shown, status=200 if dialog else 409)
        except ApplicationNotFound:
            return _not_found()
        except VersionConflict:
            return _conflict(request, application_id, question_id, NOT_STARTED)
        except (GenerateNotReady, ProviderNotConfigured) as error:
            message = (
                error.user_message if isinstance(error, ProviderNotConfigured) else error.message
            )
            return _shown(request, application_id, question_id, error=message, status=409)

    return await run_in_threadpool(start)


async def approve(
    request: Request, application_id: str, question_id: str, version_id: str
) -> Response:
    if not is_safe_id(version_id):
        return _not_found()
    return await _mutate(request, application_id, question_id, version_id, "approve")


async def unit_action(
    request: Request,
    application_id: str,
    question_id: str,
    version_id: str,
    unit_id: str,
    action: str,
) -> Response:
    if action not in _CHANGES or not is_safe_id(unit_id) or not is_safe_id(version_id):
        return _not_found(UnitNotFound().message)
    return await _mutate(request, application_id, question_id, version_id, action, unit_id)


async def _mutate(
    request: Request, aid: str, qid: str, version_id: str, action: str, uid: str = ""
) -> Response:
    posted = await single_value_form(request, {"text"} if action == "edit" else set())
    if isinstance(posted, Response):
        return posted
    if action == "edit" and "text" not in posted:
        return PlainTextResponse(INVALID_REQUEST, status_code=400)

    def save() -> Response:
        service = _service(request)
        try:
            offer = None
            was_approved = False
            if action == "edit":
                edited = service.edit_adapted(aid, qid, version_id, uid, posted["text"])
                offer, was_approved = edited.offer, edited.was_approved
            elif action == "remove":
                was_approved = service.remove_adapted(aid, qid, version_id, uid).was_approved
            elif action == "confirm":
                was_approved = service.confirm_adapted(aid, qid, version_id, uid).was_approved
            else:
                service.approve_adapted(aid, qid, version_id)
            if offer:
                message = ANNOUNCE["edit_claim"]
            elif was_approved and action != "approve":
                message = ANNOUNCE["edit_approved"]
            else:
                message = ANNOUNCE[action]
            return _shown(request, aid, qid, message=message)
        except ApplicationNotFound:
            return _not_found()
        except UnitNotFound as error:
            return _not_found(error.message)
        except VersionConflict:
            if action == "edit":
                return _conflict(request, aid, qid, NOT_SAVED, posted.get("text", ""))
            return _conflict(request, aid, qid, NOT_CHANGED)
        except UnitInvalid as error:
            return _shown(
                request,
                aid,
                qid,
                error=error.message,
                status=422,
                focus="",  # the invalid textarea takes focus, not the heading
                invalid_unit=uid,
                posted_text=_typed(posted.get("text", "")),
            )
        except (ApprovalBlocked, NothingToApprove) as error:
            return _shown(request, aid, qid, error=error.message, status=409)

    return await run_in_threadpool(save)
