"""The debrief after an interview (M8.3 part A; AC-M8.5): a token-free page, a refreshable
fragment and four metered or versioned actions.

``GET /applications/{id}/debriefs/{event_id}`` is a page route (``PAGE_TAG``): without the
token it answers only a shell (heading, time, privacy line) whose placeholder loads the
``#debrief`` fragment on load; the saved texts are personal data and so only the fragment
(``HX-Request: true``, token needed) holds them. While the draft facts run, the fragment asks
again on a task event or every few seconds with ``?active=1``; that answers 204 (nothing is
swapped) until the task has ended. The POSTs (``save``,
``dismiss``, ``drafts``, ``status``) take the launch token and origin, each field once, and a
whole-number ``version`` (the debrief's, or the application's for ``status``): stale or
refused is a 409 with the reload prompt and nothing changes. An unknown or unsafe id, an event of
another application and an event that is not an interview are one fixed plain-text 404.
Nothing the person typed is written to a log or echoed anywhere but the escaped form.
"""

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.budget_gate import dialog_context, reason_context, refusal_message
from app.api.errors import INVALID_REQUEST
from app.api.forms import is_htmx, posted_version, single_value_form
from app.api.pages import AREAS, area
from app.api.security import PAGE_TAG
from app.api.templating import render, renders_fragment, templates
from app.api.today import content_response
from app.domain.errors import (
    ApplicationNotFound,
    ProviderNotConfigured,
    StatusNotAllowed,
    VersionConflict,
    message_for_kind,
)
from app.services.board_views import zone_names
from app.services.budget_gate import Start, confirmation_from_form
from app.services.cv_import import tier_from_form
from app.services.debrief import DebriefEntry, DebriefInvalid, DebriefService

FRAGMENT = "fragments/debrief/_debrief.html"
PAGE_TEMPLATE = "pages/debrief.html"
NOT_FOUND = "This interview wasn't found."
SAVED = "Debrief saved."
DISMISSED = "Debrief marked not needed."
DRAFTS_STARTED = "Draft facts started."
_SAVE_FIELDS = {
    "version",
    "questions",
    "how_it_went",
    "experiences",
    "company_only",
    "next_step",
    "next_date",
    "next_time",
    "next_zone",
}
# The form's fields in the order they appear: name, element id, words for the error summary.
_FORM_FIELDS = (
    ("questions", "debrief-questions", "Questions you were asked"),
    ("how_it_went", "debrief-how", "How it went"),
    ("experiences", "debrief-experiences", "Experiences you mentioned"),
    ("next_step", "debrief-next-step", "What happens next"),
    ("next_date", "debrief-next-date", "Next round date"),
    ("next_time", "debrief-next-time", "Next round time"),
    ("next_zone", "debrief-next-zone", "Next round time zone"),
)
_DRAFT_FIELDS = {
    "version",
    "tier",
    "budget_chosen_tier",
    "budget_service",
    "budget_threshold",
    "budget_cycle_start",
}


def register_debrief_routes(app: FastAPI) -> None:
    base = "/applications/{application_id}/debriefs/{event_id}"
    app.add_api_route(
        base,
        show,
        methods=["GET", "HEAD"],
        tags=[PAGE_TAG],
        response_class=HTMLResponse,
        name="page_debrief",
    )
    app.add_api_route(base + "/save", save, methods=["POST"])
    app.add_api_route(base + "/dismiss", dismiss, methods=["POST"])
    app.add_api_route(base + "/drafts", drafts, methods=["POST"])
    app.add_api_route(base + "/status", status, methods=["POST"])


def _service(request: Request) -> DebriefService:
    service: DebriefService = request.app.state.debrief
    return service


def _not_found() -> Response:
    return PlainTextResponse(NOT_FOUND, status_code=404)


def _address(aid: str, event_id: str) -> str:
    return f"/applications/{aid}/debriefs/{event_id}"


def debrief_context(
    request: Request,
    aid: str,
    event_id: str,
    *,
    message: str = "",
    focus: str = "",
    entry: DebriefEntry | None = None,
    errors: dict[str, str] | None = None,
    dialog: dict[str, Any] | None = None,
    reason: dict[str, Any] | None = None,
    conflict: bool = False,
) -> dict[str, Any]:
    """What the fragment needs. ``entry`` is the text for the form (already normalized);
    ``reason`` says why the draft facts cannot start (the budget or the provider)."""
    service = _service(request)
    view = service.view(aid, event_id)
    if reason is None and view.can_draft:
        try:
            plan = service.plan_start(aid, event_id)
            block = request.app.state.budget_gate.blocked(plan.service, lookahead=plan.requests)
            if block:
                reason = reason_context(block.message, reason_id="debrief-reason", focus=False)
        except ProviderNotConfigured as error:
            reason = reason_context(error.user_message, reason_id="debrief-reason", focus=False)
        except VersionConflict:
            pass
    # The zone first offered: the computer's, else the interview's own. A refused Save shows
    # what was typed, even an empty zone.
    zone = getattr(request.app.state, "system_time_zone", None) or view.zone
    shown_errors = errors or {}
    return {
        "view": view,
        "base": _address(aid, event_id),
        "application_url": f"/applications/{aid}",
        "message": message,
        "focus": focus,
        "entry": entry or DebriefEntry(next_zone=zone),
        "errors": shown_errors,
        "error_items": [
            {"id": field_id, "label": label, "message": shown_errors[name]}
            for name, field_id, label in _FORM_FIELDS
            if name in shown_errors
        ],
        "first_error": next((name for name, _, _ in _FORM_FIELDS if name in shown_errors), ""),
        "dialog": dialog,
        "reason": reason,
        "conflict": conflict,
        "task_error": message_for_kind(view.task_error) if view.failed else "",
        "zone_names": zone_names() if view.state == "open" else (),
    }


def shell_context(aid: str, event_id: str, view: Any) -> dict[str, Any]:
    """What the token-free page needs: the interview's heading and time, never a saved text."""
    return {
        "view": view,
        "base": _address(aid, event_id),
        "application_url": f"/applications/{aid}",
    }


def debrief_response(request: Request, shown: dict[str, Any], *, status: int = 200) -> Response:
    response = templates.TemplateResponse(request, FRAGMENT, {"debrief": shown}, status_code=status)
    if shown["message"]:
        response.headers["HX-Trigger"] = json.dumps({"announce": shown["message"]})
    return response


def _conflict(
    request: Request, aid: str, event_id: str, entry: DebriefEntry | None = None
) -> Response:
    shown = debrief_context(request, aid, event_id, entry=entry, conflict=True)
    return debrief_response(request, shown, status=409)


def _done(request: Request, aid: str, event_id: str, message: str) -> Response:
    """A success: the fragment with the announcement, or the page again for a plain post."""
    if not is_htmx(request):
        return RedirectResponse(_address(aid, event_id), status_code=303)
    shown = debrief_context(request, aid, event_id, message=message, focus="debrief-heading")
    return debrief_response(request, shown)


def show(request: Request, application_id: str, event_id: str) -> Response:
    try:
        if renders_fragment(request):
            if request.query_params.get("active") == "1" and (
                _service(request).view(application_id, event_id).active
            ):
                return Response(status_code=204)  # still running: nothing to swap, no focus lost
            shown = debrief_context(request, application_id, event_id)
        else:
            shown = shell_context(
                application_id, event_id, _service(request).view(application_id, event_id)
            )
    except ApplicationNotFound:
        return _not_found()
    ctx = {"debrief": shown, "area": area("board"), "areas": AREAS}
    return render(request, page=PAGE_TEMPLATE, fragment=FRAGMENT, ctx=ctx)


async def save(request: Request, application_id: str, event_id: str) -> Response:
    posted = await single_value_form(request, _SAVE_FIELDS)
    if isinstance(posted, Response):
        return posted
    version = posted_version(posted, minimum=0)
    if version is None or posted.get("company_only", "1") != "1":
        return PlainTextResponse(INVALID_REQUEST, status_code=400)
    entry = DebriefEntry(
        posted.get("questions", ""),
        posted.get("how_it_went", ""),
        posted.get("experiences", ""),
        posted.get("company_only") == "1",
        posted.get("next_step", ""),
        posted.get("next_date", ""),
        posted.get("next_time", ""),
        posted.get("next_zone", ""),
    ).normalized()

    def keep() -> Response:
        try:
            _service(request).save(application_id, event_id, entry, version=version)
            return _done(request, application_id, event_id, SAVED)
        except DebriefInvalid as error:
            shown = debrief_context(
                request,
                application_id,
                event_id,
                focus="debrief-field",
                entry=entry,
                errors=error.errors,
            )
            return debrief_response(request, shown, status=422)
        except VersionConflict:
            return _conflict(request, application_id, event_id, entry)
        except ApplicationNotFound:
            return _not_found()

    return await run_in_threadpool(keep)


async def dismiss(request: Request, application_id: str, event_id: str) -> Response:
    posted = await single_value_form(request, {"version", "return"})
    if isinstance(posted, Response):
        return posted
    version = posted_version(posted, minimum=0)
    if version is None or posted.get("return", "today") != "today":
        return PlainTextResponse(INVALID_REQUEST, status_code=400)
    to_today = "return" in posted

    def mark() -> Response:
        try:
            _service(request).dismiss(application_id, event_id, version=version)
        except VersionConflict:
            if to_today and is_htmx(request):
                return content_response(request, focus=True)
            return _conflict(request, application_id, event_id)
        except ApplicationNotFound:
            return _not_found()
        if not to_today:
            return _done(request, application_id, event_id, DISMISSED)
        if not is_htmx(request):
            return RedirectResponse("/", status_code=303)
        response = content_response(request, focus=True)
        response.headers["HX-Trigger"] = json.dumps({"announce": DISMISSED})
        return response

    return await run_in_threadpool(mark)


async def drafts(request: Request, application_id: str, event_id: str) -> Response:
    posted = await single_value_form(request, _DRAFT_FIELDS)
    if isinstance(posted, Response):
        return posted
    version = posted_version(posted, minimum=0)
    if version is None:
        return PlainTextResponse(INVALID_REQUEST, status_code=400)

    def begin() -> Response:
        service = _service(request)
        form = {name: [value] for name, value in posted.items()}
        try:
            tier = tier_from_form(form)
            plan = service.plan_start(application_id, event_id, version=version, tier=tier)
            checked = request.app.state.budget_gate.check(
                plan.service,
                tier=plan.tier,
                confirmation=confirmation_from_form(form),
                lookahead=plan.requests,
            )
            if not isinstance(checked, Start):
                dialog = (
                    dialog_context(
                        checked,
                        dialog_id="debrief-budget-dialog",
                        action=_address(application_id, event_id) + "/drafts",
                        target="#debrief",
                        fields=posted,
                        return_focus="#debrief-drafts-start",
                    )
                    if is_htmx(request)
                    else None
                )
                reason = None
                if dialog is None:
                    reason = reason_context(
                        refusal_message(checked), reason_id="debrief-reason", focus=True
                    )
                shown = debrief_context(
                    request, application_id, event_id, dialog=dialog, reason=reason
                )
                return debrief_response(request, shown, status=200 if dialog else 409)
            service.start_drafts(application_id, event_id, version=version, tier=tier)
            return _done(request, application_id, event_id, DRAFTS_STARTED)
        except VersionConflict:
            return _conflict(request, application_id, event_id)
        except ProviderNotConfigured as error:
            reason = reason_context(error.user_message, reason_id="debrief-reason", focus=True)
            shown = debrief_context(request, application_id, event_id, reason=reason)
            return debrief_response(request, shown, status=409)
        except ApplicationNotFound:
            return _not_found()

    return await run_in_threadpool(begin)


async def status(request: Request, application_id: str, event_id: str) -> Response:
    posted = await single_value_form(request, {"version"})
    if isinstance(posted, Response):
        return posted
    version = posted_version(posted, minimum=0)
    if version is None:
        return PlainTextResponse(INVALID_REQUEST, status_code=400)

    def move() -> Response:
        try:
            label = _service(request).confirm_status(application_id, event_id, version=version)
            return _done(request, application_id, event_id, f"Moved to {label}.")
        except (VersionConflict, StatusNotAllowed):
            return _conflict(request, application_id, event_id)
        except ApplicationNotFound:
            return _not_found()

    return await run_in_threadpool(move)
