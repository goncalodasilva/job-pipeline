"""Read-only Prep views and explicit, versioned question starts."""

import json
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.budget_gate import dialog_context, reason_context, refusal_message
from app.api.company_brief import brief_context
from app.api.errors import INVALID_REQUEST
from app.api.forms import is_htmx, posted_version, single_value_form
from app.api.templating import templates
from app.domain.errors import (
    ApplicationNotFound,
    ProviderNotConfigured,
    VersionConflict,
    format_day,
    message_for_kind,
)
from app.domain.question_bank import QUESTION_TYPES
from app.services.budget_gate import Start, confirmation_from_form
from app.services.cv_import import tier_from_form
from app.services.prep import WEB_QUESTIONS, PrepService

FRAGMENT = "fragments/prep/_prep.html"
NO_COMPANY = "This job needs a company name before web questions can be found."
NOTHING_NEW = "No new questions to add right now."
_ACTIVE = {"queued", "running", "cancelling"}
_FIELDS = {
    "version",
    "confirmed",
    "tier",
    "budget_chosen_tier",
    "budget_service",
    "budget_threshold",
    "budget_cycle_start",
}


def register_prep_routes(app: FastAPI) -> None:
    app.add_api_route("/applications/{application_id}/prep", show, methods=["GET"])
    app.add_api_route("/applications/{application_id}/prep/{action}", start, methods=["POST"])
    app.add_api_route(
        "/applications/{application_id}/prep/questions/{question_id}/ready", ready, methods=["POST"]
    )


def _service(request: Request) -> PrepService:
    service: PrepService = request.app.state.prep
    return service


def prep_context(
    request: Request, aid: str, *, message: str = "", focus: str = ""
) -> dict[str, Any]:
    raw_query = request.query_params.get("q", "")
    query = (
        raw_query.strip()
        if len(raw_query) <= 200 and not any(ord(c) < 32 for c in raw_query)
        else ""
    )
    kind = request.query_params.get("type", "")
    kind = kind if kind in QUESTION_TYPES else ""
    view = _service(request).view(aid, query=query, question_type=kind)
    params = {k: v for k, v in (("q", query), ("type", kind)) if v}
    suffix = "?" + urlencode(params) if params else ""
    usage = request.app.state.usage
    search_status = usage.reports(("web_search",))[0].status if usage else None
    gate = request.app.state.budget_gate
    search_block = gate.blocked("web_search", lookahead=5) if view.search_provider else None
    web_reason = None
    if view.search_provider and not view.company:
        web_reason = reason_context(NO_COMPANY, reason_id="prep-web-reason", focus=False)
    elif search_block and search_status:
        web_reason = reason_context(
            "Monthly search limit reached. Resets on "
            f"{format_day(search_status.cycle.reset_date)}.",
            reason_id="prep-web-reason",
            focus=False,
        )
    generate_reason = None
    try:
        plan = _service(request).plan_start(aid)
        block = gate.blocked(plan.service) if plan else None
        if block:
            generate_reason = reason_context(block.message, reason_id="prep-reason", focus=False)
    except ProviderNotConfigured as error:
        generate_reason = reason_context(error.user_message, reason_id="prep-reason", focus=False)
    return {
        "view": view,
        "query": query,
        "kind": kind,
        "suffix": suffix,
        "page_url": f"/applications/{aid}?" + urlencode({"tab": "prep", **params}),
        "message": message,
        "focus": focus,
        "dialog": None,
        "conflict": False,
        "web_reason": web_reason,
        "generate_reason": generate_reason,
        "search_count": search_status.effective.value if search_status else 0,
        "search_allowance": search_status.budget.allowance
        if search_status and search_status.budget
        else None,
        "active": any(status in _ACTIVE for _, status, _ in view.tasks),
        "active_web": any(
            kind == WEB_QUESTIONS and status in _ACTIVE for kind, status, _ in view.tasks
        ),
        "task_errors": [
            message_for_kind(error)
            for _, status, error in view.tasks
            if status == "failed" and error
        ],
        "brief": brief_context(request, view.company_id, level="h5") if view.company_id else None,
        "rows": {row.question.id: row for row in (*view.questions, *view.already_prepared)},
    }


def _count(shown: dict[str, Any]) -> int:
    count: int = shown["view"].item_count
    return count


def _answer(request: Request, shown: dict[str, Any], *, status: int = 200) -> Response:
    response = templates.TemplateResponse(request, FRAGMENT, {"prep": shown}, status_code=status)
    if shown["message"]:
        response.headers["HX-Trigger"] = json.dumps({"announce": shown["message"]})
    return response


def _filter_message(shown: dict[str, Any]) -> str:
    """What a filter or search left, said aloud once the list has changed."""
    count = len(shown["view"].bank)
    if count == 0:
        return "No bank questions match."
    return f"Showing {count} bank question{'' if count == 1 else 's'}."


def show(request: Request, application_id: str) -> Response:
    try:
        focused = request.query_params.get("focus") == "1"
        shown = prep_context(request, application_id, focus="prep-heading" if focused else "")
        if focused and (shown["query"] or shown["kind"]):
            shown["message"] = _filter_message(shown)
        response = _answer(request, shown)
        if request.query_params.get("push") == "1":
            response.headers["HX-Push-Url"] = shown["page_url"]
        return response
    except ApplicationNotFound:
        return PlainTextResponse(ApplicationNotFound.message, status_code=404)


def _conflict(request: Request, aid: str) -> Response:
    shown = prep_context(request, aid)
    shown["conflict"] = True
    return _answer(request, shown, status=409)


async def start(request: Request, application_id: str, action: str) -> Response:
    if action not in {"generate", "more", "regenerate", "web"}:
        return PlainTextResponse("Not found", status_code=404)
    posted = await single_value_form(request, _FIELDS)
    if isinstance(posted, Response):
        return posted
    version = posted_version(posted, minimum=1)
    if version is None or posted.get("confirmed", "") not in {"", "1"}:
        return PlainTextResponse(INVALID_REQUEST, status_code=400)

    def begin() -> Response:
        service = _service(request)
        try:
            shown = prep_context(request, application_id)
            if shown["view"].version != version:
                return _conflict(request, application_id)
            fields = dict(posted)
            endpoint = f"/applications/{application_id}/prep/{action}" + shown["suffix"]
            if action == "regenerate" and posted.get("confirmed") != "1":
                shown["dialog"] = {
                    "id": "prep-regenerate-dialog",
                    "heading": "Regenerate questions?",
                    "description": "This replaces generated questions you have not practiced. "
                    "It keeps practiced answers and ready marks. This uses one AI request.",
                    "action": endpoint,
                    "target": "#prep",
                    "fields": {**fields, "confirmed": "1"},
                    "confirm_label": "Regenerate",
                    "return_focus": "#prep-regenerate",
                    "limits_url": None,
                }
                return _answer(request, shown)
            if action == "web" and not shown["view"].company:
                shown["web_reason"] = reason_context(
                    NO_COMPANY, reason_id="prep-web-reason", focus=True
                )
                return _answer(request, shown, status=409)
            tier = tier_from_form({k: [v] for k, v in posted.items()})
            plan = service.plan_start(application_id, web=action == "web", action=action, tier=tier)
            if action == "web" and plan is None:
                raise ProviderNotConfigured()
            if plan is not None:
                checks = [(plan.service, 1)]
                if action == "web":
                    checks.append(("web_search", 5))
                for name, cap in checks:
                    answer = request.app.state.budget_gate.check(
                        name,
                        tier=plan.tier,
                        lookahead=cap,
                        confirmation=confirmation_from_form({k: [v] for k, v in posted.items()}),
                    )
                    if isinstance(answer, Start):
                        continue
                    shown["dialog"] = (
                        dialog_context(
                            answer,
                            dialog_id="prep-budget-dialog",
                            action=endpoint,
                            target="#prep",
                            fields=fields,
                            return_focus="#prep-web-start" if action == "web" else "#prep-generate",
                        )
                        if is_htmx(request)
                        else None
                    )
                    key = "web_reason" if action == "web" else "generate_reason"
                    if shown["dialog"] is None:
                        if not (name == "web_search" and shown["web_reason"]):
                            shown[key] = reason_context(
                                refusal_message(answer),
                                reason_id="prep-web-reason" if action == "web" else "prep-reason",
                                focus=True,
                            )
                        else:
                            shown[key]["focus"] = True
                    return _answer(request, shown, status=200 if shown["dialog"] else 409)
            ref = (
                service.start_web(application_id, version=version, tier=tier)
                if action == "web"
                else service.start_questions(
                    application_id,
                    version=version,
                    action=action,
                    confirmed=posted.get("confirmed") == "1",
                    tier=tier,
                )
            )
            after = prep_context(request, application_id, focus="prep-heading")
            if ref:
                after["message"] = (
                    "Web questions started." if action == "web" else "Question generation started."
                )
            elif _count(after) == _count(shown):
                after["message"] = NOTHING_NEW
            else:
                after["message"] = "Questions reused from your bank."
            return _answer(request, after)
        except VersionConflict:
            return _conflict(request, application_id)
        except ProviderNotConfigured as error:
            shown = prep_context(request, application_id)
            key = "web_reason" if action == "web" else "generate_reason"
            shown[key] = reason_context(
                error.user_message,
                reason_id="prep-web-reason" if action == "web" else "prep-reason",
                focus=True,
            )
            return _answer(request, shown, status=409)
        except ApplicationNotFound:
            return PlainTextResponse(ApplicationNotFound.message, status_code=404)

    return await run_in_threadpool(begin)


async def ready(request: Request, application_id: str, question_id: str) -> Response:
    posted = await single_value_form(request, {"version", "ready"})
    if isinstance(posted, Response):
        return posted
    version = posted_version(posted, minimum=0)
    if version is None or posted.get("ready") not in {"0", "1"}:
        return PlainTextResponse(INVALID_REQUEST, status_code=400)

    def save() -> Response:
        try:
            _service(request).mark_ready(
                application_id, question_id, ready=posted["ready"] == "1", version=version
            )
            message = (
                "Question marked ready." if posted["ready"] == "1" else "Question marked not ready."
            )
            return _answer(
                request,
                prep_context(
                    request, application_id, message=message, focus=f"ready:{question_id}"
                ),
            )
        except VersionConflict:
            return _conflict(request, application_id)
        except ApplicationNotFound:
            return PlainTextResponse(ApplicationNotFound.message, status_code=404)

    return await run_in_threadpool(save)
