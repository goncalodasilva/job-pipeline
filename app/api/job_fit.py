"""Explicit fit starts use the same budget gate as the automatic handoff (M4.5)."""

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.budget_gate import dialog_context, reason_context, refusal_message
from app.api.errors import INVALID_REQUEST
from app.api.forms import is_htmx, read_form
from app.api.pages import AREAS, area
from app.api.templating import render
from app.domain.errors import JobNotFound, ProviderNotConfigured, message_for_kind
from app.onboarding.registry import Form
from app.services.budget_gate import Start, confirmation_from_form
from app.services.cv_import import tier_from_form
from app.services.job_fit import PRACTICE_NOTICE, JobFitService


def register_job_fit_routes(app: FastAPI) -> None:
    app.add_api_route("/inbox/jobs/{job_id}/fit", show_fit, methods=["GET"])
    app.add_api_route("/inbox/jobs/{job_id}/assess-fit", start_fit, methods=["POST"])


def fit_context(
    request: Request, job_id: str, *, start: bool = False, form: Form | None = None
) -> dict[str, Any]:
    service: JobFitService = request.app.state.job_fit
    view = service.view(job_id)
    message = dialog = reason = None
    blocked, status = False, 200
    if start or not view.active:
        try:
            tier = tier_from_form(form or {})
            plan = service.plan_start(job_id, tier=tier)
            if plan is None:
                status = 409 if start else 200
            else:
                answer = request.app.state.budget_gate.check(
                    plan.service,
                    tier=plan.tier,
                    confirmation=confirmation_from_form(form or {}) if start else None,
                )
                if isinstance(answer, Start):
                    if start:
                        ref = service.start(job_id, tier=tier)
                        view = service.view(job_id)
                        message = "Fit assessment started." if ref else None
                        status = 200 if ref else 409
                else:
                    dialog = (
                        dialog_context(
                            answer,
                            dialog_id=f"job-fit-dialog-{job_id}",
                            action=f"/inbox/jobs/{job_id}/assess-fit",
                            target=f"#job-fit-{job_id}",
                            fields={"tier": tier} if tier else {},
                            return_focus=f"#job-fit-start-{job_id}",
                        )
                        if start and is_htmx(request)
                        else None
                    )
                    blocked = not answer.asks
                    if dialog is None and (start or blocked):
                        reason = reason_context(
                            refusal_message(answer),
                            reason_id=f"job-fit-reason-{job_id}",
                            focus=start,
                        )
                    status = 409 if start and dialog is None else 200
        except ProviderNotConfigured as error:
            blocked, status = True, 409 if start else 200
            reason = reason_context(
                error.user_message, reason_id=f"job-fit-reason-{job_id}", focus=start
            )
            reason.update({"limits_url": "/settings", "link": "Open Settings"})
    return {
        "matches": request.app.state.job_detail.matches(job_id),
        "view": view,
        "message": message,
        "dialog": dialog,
        "budget_reason": reason,
        "blocked": blocked,
        "status": status,
        "refresh_once": start and view.active and dialog is None,
        "error": message_for_kind(view.error) if view.error else "",
        "practice_notice": PRACTICE_NOTICE,
    }


def _answer(request: Request, shown: dict[str, Any]) -> Response:
    response = render(
        request,
        page="pages/job_fit.html",
        fragment="fragments/inbox/_fit.html",
        ctx={"fit": shown, "area": area("inbox"), "areas": AREAS},
    )
    response.status_code = shown["status"]
    if shown["message"]:
        response.headers["HX-Trigger"] = json.dumps({"announce": shown["message"]})
    return response


async def start_fit(request: Request, job_id: str) -> Response:
    form = await read_form(request)
    if form is None:
        return PlainTextResponse(INVALID_REQUEST, status_code=413)

    def start() -> Response:
        try:
            return _answer(request, fit_context(request, job_id, start=True, form=form))
        except JobNotFound as error:
            return PlainTextResponse(error.user_message, status_code=404)

    return await run_in_threadpool(start)


def show_fit(request: Request, job_id: str) -> Response:
    try:
        return _answer(request, fit_context(request, job_id))
    except JobNotFound as error:
        return PlainTextResponse(error.user_message, status_code=404)
