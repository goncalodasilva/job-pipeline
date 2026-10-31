"""Read-only session pages, protected panels and versioned actions (M7.6)."""

import json
import re
from datetime import date
from typing import Any, Literal, cast

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.budget_gate import dialog_context, reason_context, refusal_message
from app.api.errors import INVALID_REQUEST
from app.api.facts import form_version
from app.api.forms import is_htmx, read_form
from app.api.pages import AREAS, area
from app.api.security import PAGE_TAG
from app.api.templating import render, templates
from app.domain.enrichment import LaterChoice
from app.domain.errors import AppError, SessionAlreadyOpen, SessionNotFound, VersionConflict
from app.domain.ids import is_safe_id
from app.domain.settings import Tier
from app.onboarding.registry import Form
from app.services.budget_gate import confirmation_from_form
from app.services.session_backlog import FocusKind, SessionFocus
from app.services.session_screen import SessionScreens


def register_session_routes(app: FastAPI) -> None:
    app.add_api_route("/sessions/new", new_session, methods=["GET", "HEAD"], tags=[PAGE_TAG])
    app.add_api_route("/sessions/{sid}", session_page, methods=["GET", "HEAD"], tags=[PAGE_TAG])
    app.add_api_route("/sessions/{sid}/panel", session_panel, methods=["GET"])
    app.add_api_route("/sessions", start_session, methods=["POST"])
    app.add_api_route("/sessions/{sid}/{action}", session_action, methods=["POST"])
    app.add_api_route("/sessions/{sid}/drafts/{fid}/{action}", draft_action, methods=["POST"])


def _screens(request: Request) -> SessionScreens:
    return cast(SessionScreens, request.app.state.session_screens)


def _missing() -> Response:
    return PlainTextResponse(SessionNotFound.message, status_code=404)


def _new(
    request: Request, *, form: Form | None = None, status: int = 200, **extra: Any
) -> Response:
    form = form or {}
    ctx = {
        "areas": AREAS,
        "area": area("facts"),
        "new": _screens(request).new(
            _one(form, "focus") or request.query_params.get("focus", "most_useful"),
            _one(form, "length"),
        ),
        "error": None,
        "dialog": None,
        "budget_reason": None,
        **extra,
    }
    response = render(
        request, page="pages/session_new.html", fragment="fragments/sessions/_new.html", ctx=ctx
    )
    response.status_code = status
    return response


def new_session(request: Request) -> Response:
    return _new(request)


# The Coverage tab's Start session buttons post here and aim their answer at this element (htmx
# sends its id as ``HX-Target``). A refusal for such a request is the reason alone, not the whole
# new-session screen. The header only picks how the answer looks; every check is the same.
COVERAGE_SLOT = "coverage-start"
# Today's Suggested session block (M7.8) does the same with its Start form: a refusal for a
# request aimed at this element is the reason alone, with the form's own focus and length kept.
TODAY_SLOT = "today-session-refusal"
_TODAY_START = "#today-session-start button"  # gets focus back from the budget dialog
_CLUSTER_ID = re.compile(r"[0-9a-f]{12}")


def _from_coverage(request: Request) -> bool:
    return is_htmx(request) and request.headers.get("HX-Target") == COVERAGE_SLOT


def _from_today(request: Request) -> bool:
    return is_htmx(request) and request.headers.get("HX-Target") == TODAY_SLOT


def _coverage_button(form: Form) -> str:
    """The row's Start session button (focus goes back to it), else the tab's heading."""
    kind, _, ref = _one(form, "focus").partition(":")
    ok = kind == "cluster" and _CLUSTER_ID.fullmatch(ref)
    return f"#{COVERAGE_SLOT}-{ref}" if ok else "#coverage-heading"


def _coverage_refused(
    request: Request, *, status: int, error: str | None = None, open_id: str = "", **extra: Any
) -> Response:
    """Only why nothing started, in the slot's own element: the error, a way to continue an open
    session, the budget dialog or its reason. No heading and no focus or length form."""
    ctx = {
        "error": error,
        "open_id": open_id if is_safe_id(open_id) else "",
        "dialog": None,
        "budget_reason": None,
        **extra,
    }
    response = templates.TemplateResponse(request, "fragments/sessions/_coverage_refusal.html", ctx)
    response.status_code = status
    return response


def _today_refused(
    request: Request, *, status: int, error: str | None = None, open_id: str = "", **extra: Any
) -> Response:
    """Only why Today's Start did not start, in the block's own slot (the same parts as the
    Coverage answer): no heading and no focus or length form."""
    ctx = {
        "error": error,
        "open_id": open_id if is_safe_id(open_id) else "",
        "dialog": None,
        "budget_reason": None,
        **extra,
    }
    response = templates.TemplateResponse(request, "fragments/sessions/_today_refusal.html", ctx)
    response.status_code = status
    return response


def _start_screen(request: Request, form: Form, status: int, **extra: Any) -> Response:
    if _from_coverage(request):
        return _coverage_refused(request, status=status, **extra)
    if _from_today(request):
        return _today_refused(request, status=status, **extra)
    return _new(request, form=form, status=status, **extra)


def _shown(request: Request, sid: str, *, status: int = 200, **extra: Any) -> Response:
    try:
        panel = _screens(request).panel(sid)
    except SessionNotFound:
        return _missing()
    ctx = {
        "areas": AREAS,
        "area": area("facts"),
        "panel": panel,
        "message": None,
        "error": None,
        "field": None,
        "typed": None,
        "dialog": None,
        "budget_reason": None,
        "focus": False,
        "conflict": False,
        "editor_versions": None,
        "later_choice": None,
        "later_date": "",
        **extra,
    }
    response = render(
        request, page="pages/session.html", fragment="fragments/sessions/_panel.html", ctx=ctx
    )
    response.status_code = status
    if ctx["message"]:
        response.headers["HX-Trigger"] = json.dumps({"announce": ctx["message"]})
    return response


def session_page(request: Request, sid: str) -> Response:
    if not is_safe_id(sid):
        return _missing()
    try:
        return _shown(request, sid)
    except SessionNotFound:
        return _missing()


def session_panel(request: Request, sid: str) -> Response:
    if not is_safe_id(sid):
        return _missing()
    try:
        panel = _screens(request).panel(sid)
        was = request.query_params.get("was")
        if was == panel["signature"]:
            return Response(status_code=204)
        return _shown(request, sid, message="Session updated." if was else None, focus=False)
    except SessionNotFound:
        return _missing()


def _one(form: Form, name: str) -> str:
    values = form.get(name, ())
    return values[0] if len(values) == 1 else ""


def _retained(form: Form) -> dict[str, Any]:
    choice = _one(form, "later_choice")
    aid = _one(form, "answer_id")
    return {
        "later_choice": choice if choice in ("week", "month", "next_session", "date") else None,
        "later_date": _one(form, "later_date").replace("\x00", ""),
        "editor_versions": {
            "session": form_version(form.get("session_version", ())),
            "answer": form_version(form.get("version", ())),
            "id": aid if is_safe_id(aid) else "",
        },
    }


def _redirect(request: Request, sid: str) -> Response:
    url = "/sessions/" + sid
    return (
        Response(headers={"HX-Redirect": url})
        if is_htmx(request)
        else RedirectResponse(url, status_code=303)
    )


def _start(request: Request, form: Form) -> Response:
    try:
        selected = _one(form, "focus") or "most_useful"
        kind, _, ref = selected.partition(":")
        focus = SessionFocus(cast(FocusKind, kind), ref or None)
        tier = _tier(form)
        length = int(_one(form, "length"))
        result = _screens(request).sessions.start(
            focus, length, tier=tier, confirmation=confirmation_from_form(form)
        )
        if result.refusal:
            return _refusal(request, None, form, "/sessions", result.refusal)
        return _redirect(request, cast(str, result.session_id))
    except ValueError:
        return _start_screen(request, form, 422, error="Choose a focus and 3 or 5 questions.")
    except AppError as error:
        open_id = error.session_id if isinstance(error, SessionAlreadyOpen) else ""
        return _start_screen(request, form, 409, error=error.user_message, open_id=open_id)


async def start_session(request: Request) -> Response:
    return await _posted(request, lambda form: _start(request, form))


def _tier(form: Form) -> Tier:
    value = _one(form, "tier") or "fast"
    if value not in ("fast", "thorough"):
        raise ValueError
    return cast(Tier, value)


def _refusal(request: Request, sid: str | None, form: Form, action: str, answer: Any) -> Response:
    if not is_htmx(request):
        return PlainTextResponse(refusal_message(answer), status_code=409)
    fields = {k: v[0].replace("\x00", "") for k, v in form.items() if len(v) == 1}
    coverage = sid is None and _from_coverage(request)
    today = sid is None and _from_today(request)
    dialog = dialog_context(
        answer,
        dialog_id="session-budget",
        action=action,
        target=f"#{COVERAGE_SLOT}"
        if coverage
        else f"#{TODAY_SLOT}"
        if today
        else "#session-panel"
        if sid
        else "#session-new",
        fields=fields,
        return_focus=_coverage_button(form)
        if coverage
        else _TODAY_START
        if today
        else "#session-status"
        if sid
        else "#session-start",
    )
    extra = {
        "dialog": dialog,
        "budget_reason": None
        if dialog
        else reason_context(refusal_message(answer), reason_id="session-budget-reason", focus=True),
    }
    status = 200 if dialog else 409
    return (
        _shown(request, sid, status=status, typed=_one(form, "answer"), **_retained(form), **extra)
        if sid
        else _start_screen(request, form, status, **extra)
    )


def _act(request: Request, sid: str, action: str, form: Form) -> Response:
    service = _screens(request).sessions
    typed = _one(form, "answer").replace("\x00", "")
    try:
        version = form_version(form.get("session_version", ()))
        aid, av = _one(form, "answer_id"), form_version(form.get("version", ()))
        result = None
        if action == "answers":
            result = service.answer(
                sid,
                aid,
                _one(form, "answer"),
                expected_version=av,
                session_version=version,
                tier=_tier(form),
                confirmation=confirmation_from_form(form),
            )
            message = "Your answer was sent."
        elif action == "next":
            result = service.next(
                sid,
                expected_version=version,
                tier=_tier(form),
                confirmation=confirmation_from_form(form),
            )
            message = "Next question started."
        elif action == "end":
            service.end(sid, expected_version=version)
            message = "Session ended. Review your drafts."
        elif action in ("skip", "not-sure", "later"):
            choice: LaterChoice | None = None
            if action == "later":
                selected = _one(form, "later_choice")
                if selected == "date":
                    choice = date.fromisoformat(_one(form, "later_date"))
                elif selected in ("week", "month", "next_session"):
                    choice = cast(LaterChoice, selected)
                else:
                    raise ValueError
            service.outcome(
                sid,
                aid,
                cast(
                    Literal["skipped", "not_sure", "later"],
                    {"skip": "skipped", "not-sure": "not_sure", "later": "later"}[action],
                ),
                expected_version=av,
                session_version=version,
                later_choice=choice,
                zone=request.app.state.session_zone,
            )
            message = {
                "skip": "Question skipped.",
                "not-sure": "Not sure recorded.",
                "later": "Ask me later saved.",
            }[action]
        else:
            return _missing()
        if result and result.refusal:
            return _refusal(request, sid, form, f"/sessions/{sid}/{action}", result.refusal)
        if not is_htmx(request):
            return _redirect(request, sid)
        return _shown(request, sid, message=message, focus=True)
    except SessionNotFound:
        return _missing()
    except ValueError:
        field = "answer" if action == "answers" else "later_date"
        return _shown(
            request,
            sid,
            status=422,
            error="Write an answer of up to 4,000 characters."
            if action == "answers"
            else "Choose a date after today.",
            field=field,
            typed=typed,
            **_retained(form),
        )
    except AppError as error:
        return _shown(
            request,
            sid,
            status=409,
            error=error.user_message,
            conflict=isinstance(error, VersionConflict),
            typed=typed,
            focus=True,
            **_retained(form),
        )


async def _posted(request: Request, work: Any) -> Response:
    form = await read_form(request)
    if form is None:
        return PlainTextResponse(INVALID_REQUEST, status_code=413)
    allowed = {
        "focus",
        "length",
        "tier",
        "answer_id",
        "answer",
        "version",
        "session_version",
        "later_choice",
        "later_date",
        "budget_service",
        "budget_threshold",
        "budget_cycle_start",
        "budget_chosen_tier",
    }
    if set(form) - allowed or any(len(v) != 1 for v in form.values()):
        return PlainTextResponse(INVALID_REQUEST, status_code=400)
    return await run_in_threadpool(work, form)


async def session_action(request: Request, sid: str, action: str) -> Response:
    if not is_safe_id(sid) or action not in ("answers", "skip", "not-sure", "later", "next", "end"):
        return _missing()
    return await _posted(request, lambda form: _act(request, sid, action, form))


async def draft_action(request: Request, sid: str, fid: str, action: str) -> Response:
    if not all(is_safe_id(v) for v in (sid, fid)) or action not in ("confirm", "discard"):
        return _missing()

    def work(form: Form) -> Response:
        try:
            _screens(request).draft_action(sid, fid, action, form_version(form.get("version", ())))
            return (
                _shown(
                    request,
                    sid,
                    message="Draft confirmed." if action == "confirm" else "Draft discarded.",
                    focus=True,
                )
                if is_htmx(request)
                else _redirect(request, sid)
            )
        except SessionNotFound:
            return _missing()
        except AppError as error:
            return _shown(
                request,
                sid,
                status=409,
                error=error.user_message,
                conflict=isinstance(error, VersionConflict),
                focus=True,
            )

    return await _posted(request, work)
