"""Protected CV review fragments and version-checked actions (M6.8)."""

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
from app.services.budget_gate import Start, confirmation_from_form
from app.services.cv_import import tier_from_form
from app.services.document_screen import screen_data
from app.services.documents import (
    ApprovalBlocked,
    DocumentService,
    GenerateInProgress,
    GenerateNotReady,
    NothingToApprove,
    ReplaceNotConfirmed,
    UnitInvalid,
    UnitNotFound,
)

VERSION_NOT_FOUND = "That version wasn't found."


def service(request: Request) -> DocumentService:
    result: DocumentService = request.app.state.documents
    return result


def cv_context(request: Request, aid: str, *, version: str | None = None) -> dict[str, Any]:
    current = service(request).view(aid)
    wanted = version
    if wanted is None and not is_htmx(request):
        wanted = request.query_params.get("version")
        if wanted not in {v.id for v in current.versions}:
            wanted = None
    view = service(request).view(aid, wanted) if wanted else current
    old = bool(view.version and current.version and view.version.id != current.version.id)
    state = (
        "running"
        if view.task.active
        else "failed"
        if view.task.attention
        else (
            "approved"
            if view.version and view.version.status in ("approved", "sent")
            else "draft"
            if view.version
            else "none"
        )
    )
    found: dict[str, Any] = {
        "view": view,
        **screen_data(view),
        "state": state,
        "readonly": old or view.task.active,
        "old": old,
        "private": is_htmx(request),
        "not_ready": None,
        "blocked": False,
        "reason": None,
        "dialog": None,
        "error": message_for_kind(view.task.error) if view.task.attention else "",
        "posted": {},
        "invalid": "",
        "invalid_unit": "",
        "field_error": "",
        "focus": "",
        "offer": None,
        "conflict": False,
        "kept": None,
    }
    try:
        plan = service(request).plan_start(aid)
        block = request.app.state.budget_gate.blocked(plan.service)
        if block:
            found.update(
                blocked=True,
                reason=reason_context(block.message, reason_id="cv-budget", focus=False),
            )
    except GenerateNotReady as error:
        found["not_ready"] = error
    except ProviderNotConfigured as error:
        found.update(
            blocked=True,
            reason=reason_context(error.user_message, reason_id="cv-budget", focus=False),
        )
    return found


def register_cv_routes(app: FastAPI) -> None:
    base = "/applications/{application_id}/tailoring/3"
    app.add_api_route(base + "/contact", contact, methods=["GET"])
    app.add_api_route(base + "/version/{version_id}", version_view, methods=["GET"])
    app.add_api_route(base + "/generate", generate, methods=["POST"])
    app.add_api_route(base + "/approve", approve, methods=["POST"])
    app.add_api_route(base + "/unit/add", add, methods=["POST"])
    app.add_api_route(base + "/unit/{unit_id}/{action}", unit_action, methods=["POST"])


def answer(
    request: Request,
    aid: str,
    *,
    status: int = 200,
    message: str = "",
    version: str | None = None,
    **values: Any,
) -> Response:
    # Imported here because the shared workspace context also builds this step's context.
    from app.api.tailoring import tailoring_context

    context = tailoring_context(request, aid, 3)
    if version:
        context["cv"] = cv_context(request, aid, version=version)
    context["cv"].update(values)
    context["autofocus"] = False
    response = templates.TemplateResponse(
        request, "fragments/tailoring/_tailoring.html", {"tailoring": context}, status_code=status
    )
    if message:
        response.headers["HX-Trigger"] = json.dumps({"announce": message})
    return response


def not_found() -> Response:
    return PlainTextResponse(ApplicationNotFound.message, status_code=404)


def version_view(request: Request, application_id: str, version_id: str) -> Response:
    try:
        view = service(request).view(application_id)
        if not is_safe_id(version_id) or version_id not in {v.id for v in view.versions}:
            return PlainTextResponse(VERSION_NOT_FOUND, status_code=404)
        return answer(request, application_id, version=version_id, focus="cv-heading")
    except ApplicationNotFound:
        return not_found()


def contact(request: Request, application_id: str) -> Response:
    try:
        view = service(request).view(application_id)
        wanted = request.query_params.get("version")
        if wanted is not None:
            if not is_safe_id(wanted) or wanted not in {v.id for v in view.versions}:
                return PlainTextResponse(VERSION_NOT_FOUND, status_code=404)
            view = service(request).view(application_id, wanted)
        return templates.TemplateResponse(
            request,
            "fragments/tailoring/_cv_contact.html",
            {"cv": {"view": view, "private": True}},
        )
    except ApplicationNotFound:
        return not_found()


async def generate(request: Request, application_id: str) -> Response:
    posted = await single_value_form(
        request,
        {
            "note",
            "replace_all",
            "confirm_replace",
            "tier",
            "budget_threshold",
            "budget_cycle_start",
            "budget_service",
            "budget_chosen_tier",
        },
    )
    if isinstance(posted, Response):
        return posted
    if any(posted.get(key, "1") != "1" for key in ("replace_all", "confirm_replace")):
        return PlainTextResponse(INVALID_REQUEST, status_code=400)

    def start() -> Response:
        try:
            tier = tier_from_form({k: [v] for k, v in posted.items()})
            plan = service(request).plan_start(application_id, tier=tier)
            gate = request.app.state.budget_gate.check(
                plan.service,
                tier=plan.tier,
                confirmation=confirmation_from_form({k: [v] for k, v in posted.items()}),
            )
            if isinstance(gate, Start):
                service(request).start_generate(
                    application_id,
                    note=posted.get("note"),
                    replace_all=posted.get("replace_all") == "1",
                    confirm_replace=posted.get("confirm_replace") == "1",
                    tier=tier,
                )
                return answer(
                    request, application_id, message="CV generation started.", focus="cv-heading"
                )
            dialog = (
                dialog_context(
                    gate,
                    dialog_id="tailoring-dialog-3",
                    action=f"/applications/{application_id}/tailoring/3/generate",
                    target="#tailoring",
                    fields=posted,
                    return_focus="#cv-generate-start",
                )
                if is_htmx(request)
                else None
            )
            return answer(
                request,
                application_id,
                status=200 if dialog else 409,
                posted=posted,
                dialog=dialog,
                reason=None
                if dialog
                else reason_context(refusal_message(gate), reason_id="cv-budget", focus=True),
                blocked=not gate.asks,
            )
        except ApplicationNotFound:
            return not_found()
        except (GenerateNotReady, GenerateInProgress, ProviderNotConfigured) as error:
            message = (
                error.user_message if isinstance(error, ProviderNotConfigured) else error.message
            )
            return answer(
                request,
                application_id,
                status=409,
                message=message,
                error=message,
                focus="cv-heading",
                posted=posted,
            )
        except (ReplaceNotConfirmed, ValueError) as error:
            message = str(error)
            return answer(
                request,
                application_id,
                status=422,
                message=message,
                posted=posted,
                invalid="confirm_replace" if isinstance(error, ReplaceNotConfirmed) else "note",
                field_error=message,
            )

    return await run_in_threadpool(start)


async def approve(request: Request, application_id: str) -> Response:
    return await mutate(request, application_id, "approve")


async def add(request: Request, application_id: str) -> Response:
    return await mutate(request, application_id, "add")


async def unit_action(request: Request, application_id: str, unit_id: str, action: str) -> Response:
    if action not in ("edit", "remove", "confirm") or not is_safe_id(unit_id):
        return PlainTextResponse(UnitNotFound().message, status_code=404)
    return await mutate(request, application_id, action, unit_id)


async def mutate(request: Request, aid: str, action: str, uid: str = "") -> Response:
    allowed = {"version"} | (
        {"text"} if action == "edit" else {"section", "text", "role"} if action == "add" else set()
    )
    posted = await single_value_form(request, allowed)
    if isinstance(posted, Response):
        return posted
    version = posted.get("version", "")
    if not is_safe_id(version):
        return PlainTextResponse(INVALID_REQUEST, status_code=400)

    def save() -> Response:
        try:
            before = service(request).view(aid)
            outcome = None
            saved_version = version
            if action == "edit":
                outcome = service(request).edit_unit(aid, version, uid, posted.get("text", ""))
            elif action == "add":
                outcome = service(request).add_unit(
                    aid,
                    version,
                    posted.get("section", ""),
                    posted.get("text", ""),
                    role=posted.get("role") or None,
                )
            elif action == "remove":
                saved_version = service(request).remove_unit(aid, version, uid)
            elif action == "confirm":
                outcome = service(request).resolve_flag_confirm(aid, version, uid)
            else:
                service(request).approve(aid, version)
            message = {
                "edit": "Line saved.",
                "add": "Line added.",
                "remove": "Line removed.",
                "confirm": "Saved as a fact. The line now cites it.",
                "approve": "CV approved.",
            }[action]
            offer = outcome.offer if outcome and action == "edit" else None
            if (
                before.version
                and before.version.status in ("approved", "sent")
                and action != "approve"
            ):
                message = "Saved as a new draft. The approved version is unchanged."
            if offer:
                message = "Line saved. Save this as a fact?"
            return answer(
                request,
                aid,
                message=message,
                version=outcome.version_id if outcome else saved_version,
                focus="" if offer else f"line-{outcome.unit_id}" if outcome else "cv-heading",
                offer=offer,
            )
        except ApplicationNotFound:
            return not_found()
        except UnitNotFound as error:
            return PlainTextResponse(error.message, status_code=404)
        except VersionConflict:
            return answer(
                request,
                aid,
                status=409,
                message=VersionConflict.message,
                conflict=True,
                kept=posted.get("text"),
            )
        except UnitInvalid as error:
            field = (
                "section"
                if error.message == "Choose one of the CV's sections."
                else "role"
                if error.message == "Choose one of your roles, or none."
                else "text"
            )
            return answer(
                request,
                aid,
                status=422,
                message=error.message,
                posted=posted,
                invalid=field,
                invalid_unit=uid or "add",
                field_error=error.message,
            )
        except (ApprovalBlocked, NothingToApprove, GenerateInProgress) as error:
            return answer(
                request,
                aid,
                status=409,
                message=error.message,
                error=error.message,
                focus="cv-heading",
            )

    return await run_in_threadpool(save)
