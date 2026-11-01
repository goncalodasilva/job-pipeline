"""Practice answers in the Prep tab: a read-only view, a metered answer start, and ticked claims
(M8.2 part A; AC-M8.4). Every route answers the ``#prep`` fragment with the practice view."""

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.budget_gate import dialog_context, reason_context, refusal_message
from app.api.errors import INVALID_REQUEST
from app.api.forms import is_htmx, posted_version, read_form, single_value_form
from app.api.templating import templates
from app.domain.answer_feedback import MAX_ANSWER_CHARS
from app.domain.errors import (
    ApplicationNotFound,
    ProviderNotConfigured,
    VersionConflict,
    message_for_kind,
)
from app.services.answer_practice import (
    AnswerPracticeService,
    UnknownClaim,
    clean_answer,
    normalize_answer,
)
from app.services.budget_gate import Start, confirmation_from_form
from app.services.cv_import import tier_from_form

FRAGMENT = "fragments/prep/_practice.html"
_ANSWER_FIELDS = {
    "version",
    "answer",
    "tier",
    "budget_chosen_tier",
    "budget_service",
    "budget_threshold",
    "budget_cycle_start",
}
_MAX_CLAIM_INDEX_CHARS = 4
CLAIM_CHOICE_ERROR = "Choose from the flagged claims."


def register_practice_routes(app: FastAPI) -> None:
    base = "/applications/{application_id}/prep/practice/{question_id}"
    app.add_api_route(base, show, methods=["GET"])
    app.add_api_route(base + "/answer", answer, methods=["POST"])
    app.add_api_route(base + "/claims/{attempt_id}", claims, methods=["POST"])


def _service(request: Request) -> AnswerPracticeService:
    service: AnswerPracticeService = request.app.state.answer_practice
    return service


def practice_context(
    request: Request,
    aid: str,
    qid: str,
    *,
    message: str = "",
    focus: str = "practice-heading",
    answer: str | None = None,
    answer_error: str = "",
) -> dict[str, Any]:
    """What the practice fragment needs. ``answer`` is the text for the box (already
    normalized); None means nothing was posted, so the box starts with the answer of the
    latest try when its feedback failed, which the person can send again."""
    service = _service(request)
    view = service.view(aid, qid)
    errors = {
        a.id: message_for_kind(a.error)
        for a in view.attempts
        if a.feedback is None and not a.active
    }
    failed = view.attempts[0] if view.attempts and view.attempts[0].id in errors else None
    reason = None
    try:
        plan = service.plan_start(aid, qid)
        block = request.app.state.budget_gate.blocked(plan.service)
        if block:
            reason = reason_context(block.message, reason_id="practice-reason", focus=False)
    except ProviderNotConfigured as error:
        reason = reason_context(error.user_message, reason_id="practice-reason", focus=False)
    shown = answer if answer is not None else (failed.answer if failed else "")
    return {
        "view": view,
        "base": f"/applications/{aid}/prep/practice/{qid}",
        "page_url": f"/applications/{aid}?tab=prep",
        "message": message,
        "focus": focus,
        "dialog": None,
        "conflict": False,
        "reason": reason,
        "answer": shown,
        "count": len(shown),
        "answer_error": answer_error,
        "claim_error": "",
        "retry": failed is not None,
        "max_chars": MAX_ANSWER_CHARS,
        "errors": errors,
    }


def _answer(request: Request, shown: dict[str, Any], *, status: int = 200) -> Response:
    response = templates.TemplateResponse(
        request, FRAGMENT, {"practice": shown}, status_code=status
    )
    if shown["message"]:
        response.headers["HX-Trigger"] = json.dumps({"announce": shown["message"]})
    return response


def _not_found() -> Response:
    return PlainTextResponse(ApplicationNotFound.message, status_code=404)


def _conflict(request: Request, aid: str, qid: str, *, answer: str | None = None) -> Response:
    shown = practice_context(request, aid, qid, focus="", answer=answer)
    shown["conflict"] = True
    return _answer(request, shown, status=409)


def show(request: Request, application_id: str, question_id: str) -> Response:
    try:
        refresh = request.query_params.get("refresh") == "1"
        shown = practice_context(
            request, application_id, question_id, focus="" if refresh else "practice-heading"
        )
        return _answer(request, shown)
    except ApplicationNotFound:
        return _not_found()


async def answer(request: Request, application_id: str, question_id: str) -> Response:
    posted = await single_value_form(request, _ANSWER_FIELDS)
    if isinstance(posted, Response):
        return posted
    version = posted_version(posted, minimum=0)
    if version is None or "answer" not in posted:
        return PlainTextResponse(INVALID_REQUEST, status_code=400)
    # What the box and the budget dialog hold again is what would be stored, never the raw post.
    echo = normalize_answer(posted["answer"])
    fields = {**posted, "answer": echo}

    def begin() -> Response:
        service = _service(request)
        try:
            shown = practice_context(request, application_id, question_id, answer=echo, focus="")
            try:
                clean_answer(echo)
            except ValueError as error:
                shown["answer_error"] = str(error)
                shown["focus"] = "practice-answer"
                return _answer(request, shown, status=422)
            if shown["view"].version != version:
                return _conflict(request, application_id, question_id, answer=echo)
            tier = tier_from_form({k: [v] for k, v in fields.items()})
            plan = service.plan_start(application_id, question_id, tier=tier)
            checked = request.app.state.budget_gate.check(
                plan.service,
                tier=plan.tier,
                confirmation=confirmation_from_form({k: [v] for k, v in fields.items()}),
            )
            if not isinstance(checked, Start):
                shown["dialog"] = (
                    dialog_context(
                        checked,
                        dialog_id="practice-budget-dialog",
                        action=shown["base"] + "/answer",
                        target="#prep",
                        fields=fields,
                        return_focus="#practice-submit",
                    )
                    if is_htmx(request)
                    else None
                )
                if shown["dialog"] is None:
                    shown["reason"] = reason_context(
                        refusal_message(checked), reason_id="practice-reason", focus=True
                    )
                return _answer(request, shown, status=200 if shown["dialog"] else 409)
            service.submit(application_id, question_id, echo, version=version, tier=tier)
            after = practice_context(
                request, application_id, question_id, message="Feedback started.", answer=""
            )
            return _answer(request, after)
        except VersionConflict:
            return _conflict(request, application_id, question_id, answer=echo)
        except ProviderNotConfigured as error:
            shown = practice_context(request, application_id, question_id, answer=echo)
            shown["reason"] = reason_context(
                error.user_message, reason_id="practice-reason", focus=True
            )
            return _answer(request, shown, status=409)
        except ApplicationNotFound:
            return _not_found()

    return await run_in_threadpool(begin)


async def claims(
    request: Request, application_id: str, question_id: str, attempt_id: str
) -> Response:
    form = await read_form(request)
    if form is None:
        return PlainTextResponse(INVALID_REQUEST, status_code=413)
    ticked = form.get("claim", ())
    versions = form.get("version", ())
    if (
        set(form) - {"version", "claim"}
        or len(versions) != 1
        or any(
            not (value.isascii() and value.isdigit()) or len(value) > _MAX_CLAIM_INDEX_CHARS
            for value in ticked
        )
    ):
        return PlainTextResponse(INVALID_REQUEST, status_code=400)
    version = posted_version({"version": versions[0]}, minimum=1)
    if version is None:
        return PlainTextResponse(INVALID_REQUEST, status_code=400)

    def add() -> Response:
        service = _service(request)
        try:
            # Any try of this question counts, however old, not only the ones the list shows.
            if not service.attempt_in(application_id, question_id, attempt_id):
                return _not_found()
            made = service.add_claims(attempt_id, [int(value) for value in ticked], version=version)
            message = (
                f"{made} draft{'' if made == 1 else 's'} added to your facts."
                if made
                else "No new drafts were added."
            )
            return _answer(
                request, practice_context(request, application_id, question_id, message=message)
            )
        except UnknownClaim:
            shown = practice_context(request, application_id, question_id, focus="")
            shown["claim_error"] = CLAIM_CHOICE_ERROR
            return _answer(request, shown, status=422)
        except VersionConflict:
            return _conflict(request, application_id, question_id)
        except ApplicationNotFound:
            return _not_found()

    return await run_in_threadpool(add)
