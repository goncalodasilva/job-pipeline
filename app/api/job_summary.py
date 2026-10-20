"""Summary starts ask the budget gate; reads only refetch state (M4.4)."""

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.budget_gate import dialog_context, reason_context, refusal_message
from app.api.errors import INVALID_REQUEST
from app.api.forms import is_htmx, read_form
from app.api.job_fit import fit_context
from app.api.pages import AREAS, area
from app.api.templating import render
from app.domain.errors import JobNotFound, ProviderNotConfigured, message_for_kind
from app.onboarding.registry import Form
from app.services.budget_gate import BudgetGate, Start, confirmation_from_form
from app.services.cv_import import tier_from_form
from app.services.job_summary import PRACTICE_NOTICE, JobSummaryService


def register_job_summary_routes(app: FastAPI) -> None:
    app.add_api_route("/inbox/jobs/{job_id}/summary", show_summary, methods=["GET"])
    app.add_api_route("/inbox/jobs/{job_id}/summarize", start_summary, methods=["POST"])


def summary_context(
    request: Request, job_id: str, *, start: bool = False, form: Form | None = None
) -> dict[str, Any]:
    service: JobSummaryService = request.app.state.job_summary
    summary = service.view(job_id)
    message = dialog = reason = None
    blocked = False
    status = 200
    if start or not summary.active:
        try:
            tier = tier_from_form(form or {})
            plan = service.plan_start(job_id, tier=tier)
            gate: BudgetGate = request.app.state.budget_gate
            answer = gate.check(
                plan.service,
                tier=plan.tier,
                confirmation=confirmation_from_form(form or {}) if start else None,
            )
            if isinstance(answer, Start):
                if start:
                    service.start(job_id, tier=tier)
                    summary = service.view(job_id)
                    message = "Summary started."
            else:
                dialog = (
                    dialog_context(
                        answer,
                        dialog_id=f"job-summary-dialog-{job_id}",
                        action=f"/inbox/jobs/{job_id}/summarize",
                        target=f"#job-summary-{job_id}",
                        fields={"tier": tier} if tier else {},
                        return_focus=f"#job-summary-start-{job_id}",
                    )
                    if start and is_htmx(request)
                    else None
                )
                blocked = not answer.asks
                if dialog is None and (start or blocked):
                    reason = reason_context(
                        refusal_message(answer),
                        reason_id=f"job-summary-reason-{job_id}",
                        focus=start,
                    )
                status = 409 if start and dialog is None else 200
        except ProviderNotConfigured as error:
            blocked = True
            status = 409 if start else 200
            reason = reason_context(
                error.user_message, reason_id=f"job-summary-reason-{job_id}", focus=start
            )
            reason.update({"limits_url": "/settings", "link": "Open Settings"})
    return {
        "fit": fit_context(request, job_id),
        "view": summary,
        "message": message,
        "dialog": dialog,
        "budget_reason": reason,
        "blocked": blocked,
        "refresh_once": start and summary.active and dialog is None,
        "status": status,
        "error": message_for_kind(summary.error) if summary.error else "",
        "practice_notice": PRACTICE_NOTICE,
    }


def _answer(request: Request, shown: dict[str, Any]) -> Response:
    response = render(
        request,
        page="pages/job_summary.html",
        fragment="fragments/inbox/_summary.html",
        ctx={"summary": shown, "area": area("inbox"), "areas": AREAS},
    )
    response.status_code = shown["status"]
    if shown["message"]:
        response.headers["HX-Trigger"] = json.dumps({"announce": shown["message"]})
    return response


async def start_summary(request: Request, job_id: str) -> Response:
    form = await read_form(request)
    if form is None:
        return PlainTextResponse(INVALID_REQUEST, status_code=413)

    def start() -> Response:
        try:
            return _answer(request, summary_context(request, job_id, start=True, form=form))
        except JobNotFound as error:
            return PlainTextResponse(error.user_message, status_code=404)

    return await run_in_threadpool(start)


def show_summary(request: Request, job_id: str) -> Response:
    try:
        return _answer(request, summary_context(request, job_id))
    except JobNotFound as error:
        return PlainTextResponse(error.user_message, status_code=404)
