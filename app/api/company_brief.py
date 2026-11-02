"""The company brief (M8.3 part B): a read-only fragment and an explicit, versioned, metered start.

- ``GET  /companies/{id}/brief`` (token): the ``#company-brief`` fragment. It writes nothing and
  asks the model for nothing.
- ``POST /companies/{id}/brief`` (token and origin): ``version`` (the company's, 1 or more) and
  the budget dialog's fields. 200 started (or the budget warning dialog, nothing started), 409
  for a stale version, a blocked budget or no search provider, 404 for an unknown company.

An unknown or unsafe company id is always the fixed plain-text 404, never echoed. The company
page itself stays token-free; this fragment is a separate protected route.
"""

import json
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.budget_gate import dialog_context, reason_context, refusal_message
from app.api.errors import INVALID_REQUEST
from app.api.forms import is_htmx, posted_version, single_value_form
from app.api.templating import templates
from app.domain.company_brief import LABELS
from app.domain.errors import ProviderNotConfigured, VersionConflict, format_day, message_for_kind
from app.services.budget_gate import Start, confirmation_from_form
from app.services.company_brief import BRIEF_LABEL, BriefService
from app.services.cv_import import tier_from_form
from app.services.task_views import failed_message

NOT_FOUND = "This company wasn't found."
FRAGMENT = "fragments/companies/_brief.html"
STARTED = "Company brief started."
REASON_ID = "company-brief-reason"
FAILED_INSTEAD = "Company brief failed. The reason is shown in the Company brief section."
_ACTIVE = {"queued", "running", "cancelling"}
_FIELDS = {
    "version",
    "tier",
    "budget_chosen_tier",
    "budget_service",
    "budget_threshold",
    "budget_cycle_start",
}


def register_company_brief_routes(app: FastAPI) -> None:
    app.add_api_route("/companies/{company_id}/brief", show, methods=["GET"])
    app.add_api_route("/companies/{company_id}/brief", start, methods=["POST"])


def _service(request: Request) -> BriefService:
    service: BriefService = request.app.state.company_brief
    return service


def _heading_level(request: Request) -> str:
    """The fragment sits under an h4 in the Prep tab and under the page title on a company page;
    the address of the page asking says which (only this one fact is read from it)."""
    try:
        path = urlsplit(request.headers.get("HX-Current-URL", "")).path
    except ValueError:
        return "h2"
    return "h5" if path.startswith("/applications/") else "h2"


def _label_level(level: str) -> str:
    """The section labels are headings one level under the block's own heading."""
    return "h6" if level == "h5" else "h3"


def brief_context(
    request: Request,
    company_id: str,
    *,
    level: str = "",
    focus: bool = False,
    conflict: bool = False,
    dialog: dict[str, Any] | None = None,
    reason: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """What ``fragments/companies/_brief.html`` shows. Raises ``LookupError`` for an unknown
    company. Reads only."""
    view = _service(request).view(company_id)
    usage = request.app.state.usage
    search_status = usage.reports(("web_search",))[0].status if usage else None
    shown_reason = reason
    if shown_reason is None and view.search_provider:
        gate = request.app.state.budget_gate
        message = ""
        if gate.blocked("web_search", lookahead=5) and search_status:
            message = (
                "Monthly search limit reached. Resets on "
                f"{format_day(search_status.cycle.reset_date)}."
            )
        else:
            try:
                plan = _service(request).plan_start(company_id)
            except ProviderNotConfigured:
                plan = None
            block = gate.blocked(plan.service) if plan else None
            message = block.message if block else ""
        if message:
            shown_reason = reason_context(message, reason_id=REASON_ID, focus=False)
    level = level or _heading_level(request)
    return {
        "view": view,
        "level": level,
        "label_level": _label_level(level),
        "labels": LABELS,
        "failed_said": failed_message(BRIEF_LABEL),
        "failed_instead": FAILED_INSTEAD,
        "focus": focus,
        "conflict": conflict,
        "dialog": dialog,
        "reason": shown_reason,
        "search_count": search_status.effective.value if search_status else 0,
        "search_allowance": search_status.budget.allowance
        if search_status and search_status.budget
        else None,
        "active": any(status in _ACTIVE for _, status, _ in view.tasks),
        "task_errors": [
            message_for_kind(error)
            for _, status, error in view.tasks
            if status == "failed" and error
        ],
    }


def _answer(
    request: Request, shown: dict[str, Any], *, status: int = 200, message: str = ""
) -> Response:
    response = templates.TemplateResponse(request, FRAGMENT, {"brief": shown}, status_code=status)
    if message:
        response.headers["HX-Trigger"] = json.dumps({"announce": message})
    return response


def _not_found() -> Response:
    return PlainTextResponse(NOT_FOUND, status_code=404)


def show(request: Request, company_id: str) -> Response:
    try:
        focus = request.query_params.get("focus") == "1"
        return _answer(request, brief_context(request, company_id, focus=focus))
    except LookupError:
        return _not_found()


async def start(request: Request, company_id: str) -> Response:
    posted = await single_value_form(request, _FIELDS)
    if isinstance(posted, Response):
        return posted
    version = posted_version(posted, minimum=1)
    if version is None:
        return PlainTextResponse(INVALID_REQUEST, status_code=400)
    return await run_in_threadpool(_begin, request, company_id, posted, version)


def _begin(request: Request, company_id: str, posted: dict[str, str], version: int) -> Response:
    service = _service(request)
    form = {k: [v] for k, v in posted.items()}
    try:
        shown = brief_context(request, company_id)
        if shown["view"].version != version:
            shown["conflict"] = True
            return _answer(request, shown, status=409)
        tier = tier_from_form(form)
        plan = service.plan_start(company_id, tier=tier)
        if plan is None:
            return _answer(request, shown, status=409)  # no search provider: the page says so
        endpoint = f"/companies/{company_id}/brief"
        for name, cap in ((plan.service, 1), ("web_search", 5)):
            answer = request.app.state.budget_gate.check(
                name,
                tier=plan.tier,
                lookahead=cap,
                confirmation=confirmation_from_form(form),
            )
            if isinstance(answer, Start):
                continue
            dialog = (
                dialog_context(
                    answer,
                    dialog_id="company-brief-budget-dialog",
                    action=endpoint,
                    target="#company-brief",
                    fields=posted,
                    return_focus="#company-brief-start",
                )
                if is_htmx(request)
                else None
            )
            if dialog is not None:
                shown["dialog"] = dialog
                return _answer(request, shown)
            reason = shown["reason"] or reason_context(
                refusal_message(answer), reason_id=REASON_ID, focus=True
            )
            shown["reason"] = {**reason, "focus": True}
            return _answer(request, shown, status=409)
        service.start(company_id, version=version, tier=tier)
        after = brief_context(request, company_id, focus=True)
        return _answer(request, after, message=STARTED)
    except VersionConflict:
        shown = brief_context(request, company_id)
        shown["conflict"] = True
        return _answer(request, shown, status=409)
    except ProviderNotConfigured:
        return _answer(request, brief_context(request, company_id), status=409)
    except LookupError:
        return _not_found()
