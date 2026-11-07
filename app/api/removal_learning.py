"""Protected inbox and Settings fragments for removal learning."""

import json
import re
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.budget_gate import dialog_context, reason_context, refusal_message
from app.api.errors import INVALID_REQUEST
from app.api.forms import is_htmx, posted_version, single_value_form
from app.api.inbox import removal_rule_saved
from app.api.templating import templates
from app.domain.errors import JobNotFound, ProviderNotConfigured, VersionConflict, message_for_kind
from app.services.budget_gate import Start, confirmation_from_form
from app.services.cv_import import tier_from_form
from app.services.removal_grouping import RemovalGrouping
from app.services.removal_learning import RemovalLearning

SUGGESTIONS = "/inbox/removal-suggestions"
RULES = "/settings/preferences/removal-rules"
GROUPING = SUGGESTIONS + "/group"
GROUP_REASON = "removal-group-budget-reason"


def register_removal_learning(app: FastAPI) -> None:
    app.add_api_route(SUGGESTIONS, show_suggestions, methods=["GET"])
    app.add_api_route(SUGGESTIONS, decide, methods=["POST"])
    app.add_api_route(GROUPING, start_grouping, methods=["POST"])
    app.add_api_route(RULES, show_rules, methods=["GET"])
    app.add_api_route(RULES + "/threshold", change_threshold, methods=["POST"])
    app.add_api_route(RULES + "/{rule_id}", change_rule, methods=["POST"])


def _service(request: Request) -> RemovalLearning:
    service: RemovalLearning = request.app.state.removal_learning
    return service


def _grouping(request: Request) -> RemovalGrouping:
    service: RemovalGrouping = request.app.state.removal_grouping
    return service


def _suggestions(
    request: Request,
    message: str = "",
    *,
    status: int = 200,
    dialog: dict[str, Any] | None = None,
    reason: dict[str, Any] | None = None,
) -> Response:
    grouping = _grouping(request)
    view = grouping.view()
    if reason is None and view.available and not view.active:
        try:
            plan = grouping.plan()
        except ProviderNotConfigured:
            reason = reason_context(
                "Choose an AI provider in Settings to group notes.",
                reason_id=GROUP_REASON,
                focus=False,
            )
            reason.update(limits_url="/settings", link="Open Providers and AI")
        else:
            blocked = request.app.state.budget_gate.blocked(plan.service)
            if blocked is not None:
                reason = reason_context(blocked.message, reason_id=GROUP_REASON, focus=False)
    response = templates.TemplateResponse(
        request,
        "fragments/inbox/_removal_suggestions.html",
        {
            "previews": _service(request).previews(),
            "message": message,
            "conflict": status == 409,
            "grouping": view,
            "dialog": dialog,
            "budget_blocked": reason,
            "grouping_error": message_for_kind(view.error) if view.error else "",
        },
        status_code=status,
    )
    if message:
        response.headers["HX-Trigger-After-Settle"] = json.dumps({"announce": message})
    return response


def show_suggestions(request: Request) -> Response:
    return _suggestions(request)


def _start_grouping(request: Request, fields: dict[str, str]) -> Response:
    service = _grouping(request)
    view = service.view()
    if not view.available:
        return _suggestions(request, "Add at least two removal notes to group them.", status=409)
    input_version = fields.get("input_version", view.input_version)
    if input_version != view.input_version:
        return _suggestions(
            request, "The removal notes changed. Review the inbox again.", status=409
        )
    form = {name: [value] for name, value in fields.items()}
    try:
        plan = service.plan(tier_from_form(form))
    except ProviderNotConfigured:
        return _suggestions(request, "Choose an AI provider in Settings.", status=409)
    answer = request.app.state.budget_gate.check(
        plan.service, tier=plan.tier, confirmation=confirmation_from_form(form)
    )
    if not isinstance(answer, Start):
        dialog = (
            dialog_context(
                answer,
                dialog_id="removal-group-budget",
                action=GROUPING,
                target="#removal-suggestions",
                fields={**fields, "input_version": input_version},
                return_focus="#removal-group-start",
            )
            if is_htmx(request)
            else None
        )
        if dialog:
            return _suggestions(request, dialog=dialog)
        reason = reason_context(refusal_message(answer), reason_id=GROUP_REASON, focus=True)
        return _suggestions(request, status=409, reason=reason)
    try:
        service.start(plan, input_version)
    except VersionConflict:
        return _suggestions(
            request, "The removal notes changed. Review the inbox again.", status=409
        )
    except JobNotFound:
        return PlainTextResponse(JobNotFound.message, status_code=404)
    return _suggestions(request, "Grouping started." + (f" {answer.note}" if answer.note else ""))


async def start_grouping(request: Request) -> Response:
    values = await single_value_form(
        request,
        {
            "input_version",
            "tier",
            "budget_chosen_tier",
            "budget_service",
            "budget_threshold",
            "budget_cycle_start",
        },
    )
    if isinstance(values, Response):
        return values
    return await run_in_threadpool(_start_grouping, request, values)


def _decide(request: Request, signature: str, version: str, action: str) -> Response:
    try:
        _service(request).decide(signature, version, action)
    except VersionConflict:
        return _suggestions(request, "This suggestion changed. Review it again.", status=409)
    except JobNotFound:
        return PlainTextResponse(JobNotFound.message, status_code=404)
    message = {
        "accept": "Rule saved. Matching jobs will be flagged.",
        "hide": "Rule saved. Matching jobs will be in Hidden by your rules.",
        "dismiss": "Suggestion dismissed.",
    }[action]
    return (
        _suggestions(request, message)
        if action == "dismiss"
        else removal_rule_saved(request, message)
    )


async def decide(request: Request) -> Response:
    values = await single_value_form(request, {"signature", "preview_version", "action"})
    if isinstance(values, Response):
        return values
    signature, action = values.get("signature", ""), values.get("action", "")
    version = values.get("preview_version", "")
    if (
        not re.fullmatch(r"[0-9a-f]{64}", signature)
        or not re.fullmatch(r"[0-9a-f]{64}", version)
        or action not in {"accept", "hide", "dismiss"}
    ):
        return PlainTextResponse(INVALID_REQUEST, status_code=400)
    return await run_in_threadpool(_decide, request, signature, version, action)


def _rules(
    request: Request,
    message: str = "",
    *,
    status: int = 200,
    target_rule_id: str = "",
    target_threshold: bool = False,
    submitted_threshold: str = "",
) -> Response:
    response = templates.TemplateResponse(
        request,
        "fragments/settings/_removal_rules.html",
        {
            "settings": _service(request).settings(),
            "message": message,
            "conflict": status == 409,
            "target_rule_id": target_rule_id,
            "target_threshold": target_threshold,
            "threshold_error": target_threshold and status == 422,
            "submitted_threshold": submitted_threshold,
        },
        status_code=status,
    )
    if message:
        response.headers["HX-Trigger"] = json.dumps({"announce": message})
    return response


def show_rules(request: Request) -> Response:
    return _rules(request)


def _change_rule(
    request: Request, rule_id: str, version: int, action: str, mode: str, active: bool
) -> Response:
    try:
        _service(request).change_rule(
            rule_id, version, delete=action == "delete", mode=mode, active=active
        )
    except VersionConflict:
        return _rules(request, "This rule changed.", status=409, target_rule_id=rule_id)
    except JobNotFound:
        return PlainTextResponse(JobNotFound.message, status_code=404)
    return _rules(
        request, "Rule deleted." if action == "delete" else "Rule saved.", target_rule_id=rule_id
    )


async def change_rule(request: Request, rule_id: str) -> Response:
    values = await single_value_form(request, {"version", "action", "mode", "active"})
    if isinstance(values, Response):
        return values
    version = posted_version(values, minimum=1)
    action, mode = values.get("action", ""), values.get("mode", "flag")
    if version is None or action not in {"save", "delete"} or mode not in {"flag", "hide"}:
        return PlainTextResponse(INVALID_REQUEST, status_code=400)
    return await run_in_threadpool(
        _change_rule, request, rule_id, version, action, mode, values.get("active") == "1"
    )


def _change_threshold(request: Request, version: int, threshold: int, submitted: str) -> Response:
    try:
        _service(request).change_threshold(threshold, version)
    except ValueError:
        return _rules(
            request,
            "Choose a threshold from 1 to 100.",
            status=422,
            target_threshold=True,
            submitted_threshold=submitted,
        )
    except VersionConflict:
        return _rules(request, "The threshold changed.", status=409, target_threshold=True)
    except JobNotFound:
        return PlainTextResponse(JobNotFound.message, status_code=404)
    return _rules(request, "Threshold saved.", target_threshold=True)


async def change_threshold(request: Request) -> Response:
    values = await single_value_form(request, {"version", "threshold"})
    if isinstance(values, Response):
        return values
    version = posted_version(values, minimum=0)
    raw = values.get("threshold", "")
    threshold = posted_version({"version": raw}, minimum=1)
    if version is None:
        return PlainTextResponse(INVALID_REQUEST, status_code=400)
    return await run_in_threadpool(_change_threshold, request, version, threshold or 0, raw[:32])
