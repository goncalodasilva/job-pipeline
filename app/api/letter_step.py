"""Protected cover letter fragments and version-checked actions (M6.9; step 4 of the Tailoring
tab). The letter works as the CV step does (``app/api/cv_step.py``): the same rules for tokens,
origin, ``version``, ids and errors; what differs is below.

- ``GET  /applications/{id}/tailoring/4/frame``: the code-built greeting, opening line, closing
  and name (the name is personal data, so the token-free page only holds a placeholder that
  loads this).
- ``GET  .../4/version/{vid}``: an older version, read-only.
- ``POST .../4/generate`` (``tone``, ``length``, ``note``, ``replace_all``, ``confirm_replace``
  and the budget dialog's fields), ``.../4/unit/{uid}/{edit|remove|confirm}``, ``.../4/unit/add``
  and ``.../4/approve``. A citation, a section or a role is never posted.
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.budget_gate import dialog_context, reason_context, refusal_message
from app.api.cv_step import VERSION_NOT_FOUND, not_found, tailoring_answer
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
from app.services.document_screen import letter_screen_data, view_state
from app.services.documents import (
    ApprovalBlocked,
    GenerateInProgress,
    GenerateNotReady,
    NothingToApprove,
    ReplaceNotConfirmed,
    UnitInvalid,
    UnitNotFound,
)
from app.services.letters import BAD_LENGTH, BAD_TONE, LENGTHS, TONES, LetterService

STEP = 4
HEADING = "letter-heading"
ANNOUNCE = {
    "started": "Letter drafting started.",
    "edit": "Paragraph saved.",
    "offer": "Paragraph saved. Save this as a fact?",
    "add": "Paragraph added.",
    "remove": "Paragraph removed.",
    "confirm": "Saved as a fact. The paragraph now cites it.",
    "approve": "Letter approved.",
    "new_draft": "Saved as a new draft. The approved version is unchanged.",
}
GENERATE_FIELDS = {
    "tone",
    "length",
    "note",
    "replace_all",
    "confirm_replace",
    "tier",
    "budget_threshold",
    "budget_cycle_start",
    "budget_service",
    "budget_chosen_tier",
}


def service(request: Request) -> LetterService:
    result: LetterService = request.app.state.letters
    return result


def letter_context(request: Request, aid: str, *, version: str | None = None) -> dict[str, Any]:
    current = service(request).view(aid)
    wanted = version
    if wanted is None and not is_htmx(request):
        wanted = request.query_params.get("version")
        if wanted not in {v.id for v in current.versions}:
            wanted = None
    view = service(request).view(aid, wanted) if wanted else current
    old = bool(view.version and current.version and view.version.id != current.version.id)
    found: dict[str, Any] = {
        "view": view,
        **letter_screen_data(view),
        "state": view_state(view),
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
        "tones": TONES,
        "lengths": LENGTHS,
        "chosen_tone": view.tone or "warm",
        "chosen_length": view.length or "medium",
    }
    try:
        plan = service(request).plan_start(aid)
        block = request.app.state.budget_gate.blocked(plan.service)
        if block:
            found.update(
                blocked=True,
                reason=reason_context(block.message, reason_id="letter-budget", focus=False),
            )
    except GenerateNotReady as error:
        found["not_ready"] = error
    except ProviderNotConfigured as error:
        found.update(
            blocked=True,
            reason=reason_context(error.user_message, reason_id="letter-budget", focus=False),
        )
    return found


def register_letter_routes(app: FastAPI) -> None:
    base = "/applications/{application_id}/tailoring/4"
    app.add_api_route(base + "/frame", frame, methods=["GET"])
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
    return tailoring_answer(
        request,
        aid,
        STEP,
        "letter",
        letter_context,
        status=status,
        message=message,
        version=version,
        **values,
    )


def version_view(request: Request, application_id: str, version_id: str) -> Response:
    try:
        view = service(request).view(application_id)
        if not is_safe_id(version_id) or version_id not in {v.id for v in view.versions}:
            return PlainTextResponse(VERSION_NOT_FOUND, status_code=404)
        return answer(request, application_id, version=version_id, focus=HEADING)
    except ApplicationNotFound:
        return not_found()


def frame(request: Request, application_id: str) -> Response:
    try:
        view = service(request).view(application_id)
    except ApplicationNotFound:
        return not_found()
    return templates.TemplateResponse(
        request,
        "fragments/tailoring/_letter_frame.html",
        {"letter": {"view": view, "private": True}},
    )


async def generate(request: Request, application_id: str) -> Response:
    posted = await single_value_form(request, GENERATE_FIELDS)
    if isinstance(posted, Response):
        return posted
    if any(posted.get(key, "1") != "1" for key in ("replace_all", "confirm_replace")):
        return PlainTextResponse(INVALID_REQUEST, status_code=400)

    def start() -> Response:
        try:
            form = {k: [v] for k, v in posted.items()}
            tier = tier_from_form(form)
            plan = service(request).plan_start(application_id, tier=tier)
            gate = request.app.state.budget_gate.check(
                plan.service, tier=plan.tier, confirmation=confirmation_from_form(form)
            )
            if isinstance(gate, Start):
                service(request).start_generate(
                    application_id,
                    tone=posted.get("tone"),
                    length=posted.get("length"),
                    note=posted.get("note"),
                    replace_all=posted.get("replace_all") == "1",
                    confirm_replace=posted.get("confirm_replace") == "1",
                    tier=tier,
                )
                return answer(request, application_id, message=ANNOUNCE["started"], focus=HEADING)
            dialog = (
                dialog_context(
                    gate,
                    dialog_id="tailoring-dialog-4",
                    action=f"/applications/{application_id}/tailoring/4/generate",
                    target="#tailoring",
                    fields=posted,
                    return_focus="#letter-generate-start",
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
                else reason_context(refusal_message(gate), reason_id="letter-budget", focus=True),
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
                focus=HEADING,
                posted=posted,
            )
        except (ReplaceNotConfirmed, ValueError) as error:
            message = str(error)
            invalid = (
                "confirm_replace"
                if isinstance(error, ReplaceNotConfirmed)
                else "tone"
                if message == BAD_TONE
                else "length"
                if message == BAD_LENGTH
                else "note"
            )
            return answer(
                request,
                application_id,
                status=422,
                message=message,
                posted=posted,
                invalid=invalid,
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
    allowed = {"version"} | ({"text"} if action in ("edit", "add") else set())
    posted = await single_value_form(request, allowed)
    if isinstance(posted, Response):
        return posted
    version = posted.get("version", "")
    if not is_safe_id(version):
        return PlainTextResponse(INVALID_REQUEST, status_code=400)

    def save() -> Response:
        try:
            letters = service(request)
            before = letters.view(aid)
            outcome = None
            saved_version = version
            if action == "edit":
                outcome = letters.edit_unit(aid, version, uid, posted.get("text", ""))
            elif action == "add":
                outcome = letters.add_unit(aid, version, posted.get("text", ""))
            elif action == "remove":
                saved_version = letters.remove_unit(aid, version, uid)
            elif action == "confirm":
                outcome = letters.resolve_flag_confirm(aid, version, uid)
            else:
                letters.approve(aid, version)
            message = ANNOUNCE[action]
            offer = outcome.offer if outcome and action == "edit" else None
            if (
                before.version
                and before.version.status in ("approved", "sent")
                and action != "approve"
            ):
                message = ANNOUNCE["new_draft"]
            if offer:
                message = ANNOUNCE["offer"]
            return answer(
                request,
                aid,
                message=message,
                version=outcome.version_id if outcome else saved_version,
                focus="" if offer else f"line-{outcome.unit_id}" if outcome else HEADING,
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
            return answer(
                request,
                aid,
                status=422,
                message=error.message,
                posted=posted,
                invalid="text",
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
                focus=HEADING,
            )

    return await run_in_threadpool(save)
