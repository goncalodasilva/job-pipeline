"""The workspace's Tailoring tab, steps 1 and 2 (M6.5; detailed design "Application workspace").

The tab itself is the existing token-free page ``GET /applications/{id}?tab=tailoring[&step=N]``
(read-only; ``application_detail.page`` calls ``tailoring_context``). These routes need the
launch token; POSTs also the app's origin:

- ``GET  /applications/{id}/tailoring/{step}``: the ``#tailoring`` fragment (404 outside 1-5).
- ``POST .../tailoring/1/check`` and ``.../2/propose``: start the gap check or the angle
  proposals through the budget gate (the dialog, a 409 with the reason, or the running task).
- ``POST .../tailoring/1/facts`` (``text``, optional ``requirement_id``): a fact typed at a gap,
  confirmed at once (422 when blank or over 10,000 characters; a text a confirmed fact already
  has is not stored twice); nothing is asked of the AI.
- ``POST .../tailoring/country`` (``version``, ``cv_country``): the CV country of this
  application, stored on the workspace (created by the first save). The select is on every step;
  it shows the workspace's country, else the preference's. 422 for a code not in the list
  (tied to the select), 409 for a stale version, 400 for any other form shape.
- ``POST .../tailoring/2/angle`` (``name``, ``description``, ``version``, optional
  ``reuse_angle_id``, and ``slot``, the form it came from): saves the angle on the workspace
  (400 without a version, 409 stale, 422 refused). A refused or stale answer shows the same
  form again with what was typed.

Answers announce what happened with ``HX-Trigger``; after a success the step heading takes
focus, after a refusal the invalid field (or the reload prompt). The step refreshing itself
(``HX-Trigger: tailoring``) announces once when its task has finished.
"""

import json
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.budget_gate import dialog_context, reason_context, refusal_message
from app.api.errors import INVALID_REQUEST
from app.api.forms import is_htmx, posted_version, read_form, single_value_form
from app.api.settings_templates import STEP_LEVEL as STEP_UNIT_LEVEL
from app.api.settings_templates import unit_context
from app.api.templating import templates
from app.domain.errors import (
    ApplicationNotFound,
    FactInputInvalid,
    ProviderNotConfigured,
    VersionConflict,
    message_for_kind,
)
from app.domain.gap_check import NOT_IN_FACT_BANK
from app.domain.locales import COUNTRIES
from app.onboarding.registry import Form
from app.services.budget_gate import Start, confirmation_from_form
from app.services.cv_import import tier_from_form
from app.services.facts import FactInput
from app.services.job_fit import PRACTICE_NOTICE
from app.services.tailoring import (
    STEP_TITLES,
    AngleInvalid,
    CountryInvalid,
    TailoringService,
    TailoringView,
)

FRAGMENT: Final = "fragments/tailoring/_tailoring.html"
STEP_LINK_PREFIX: Final = "tailoring-step-"
FACT_NEEDED: Final = "Write the fact first."
MAX_FACT_TEXT: Final = 10_000
FACT_TOO_LONG: Final = f"Keep the fact to {MAX_FACT_TEXT:,} characters or fewer."
ALREADY_RUNNING: Final = "Already running."
COMPLETED: Final = {1: "Gap check finished.", 2: "Angle proposals ready."}
POLL_TRIGGER: Final = "tailoring"  # the id htmx sends for the section's own refresh
STARTS: Final = {
    1: ("gap_check", "Gap check started.", "gap-check-start"),
    2: ("propose_angles", "Angle proposals started.", "angle-propose"),
}
LATER: Final = {
    3: "Tailoring the CV comes in a later step. Nothing to do here yet.",
    4: "The cover letter comes in a later step. Nothing to do here yet.",
    5: "Submitting comes in a later step. You always submit applications yourself.",
}


def register_tailoring_routes(app: FastAPI) -> None:
    base = "/applications/{application_id}/tailoring"
    app.add_api_route(base + "/{step}", show_step, methods=["GET"])
    app.add_api_route(base + "/1/check", start_check, methods=["POST"])
    app.add_api_route(base + "/1/facts", add_fact, methods=["POST"])
    app.add_api_route(base + "/2/propose", start_propose, methods=["POST"])
    app.add_api_route(base + "/2/angle", save_angle, methods=["POST"])
    app.add_api_route(base + "/country", save_country, methods=["POST"])


def _service(request: Request) -> TailoringService:
    service: TailoringService = request.app.state.tailoring
    return service


def step_from_query(value: str | None) -> int | None:
    """The ``step`` of a page address: 1 to 5, else None (the derived resume step)."""
    return int(value) if value is not None and value in ("1", "2", "3", "4", "5") else None


def _starts(
    request: Request, view: TailoringView, aid: str, *, start: bool, form: Form
) -> dict[str, Any]:
    """The budget gate's answer for the open step; with ``start`` also the start itself."""
    found: dict[str, Any] = {
        "view": view,
        "message": None,
        "dialog": None,
        "reason": None,
        "blocked": False,
        "status": 200,
        "button_id": "",
        "already": False,
    }
    if view.step not in STARTS:
        return found
    task_type, started, button = STARTS[view.step]
    found["button_id"] = button
    task = view.gap_task if view.step == 1 else view.angle_task
    if task.active and not start:
        return found
    service = _service(request)
    reason_id = f"tailoring-reason-{view.step}"
    try:
        tier = tier_from_form(form)
        plan = service.plan_start(aid, task_type, tier=tier)
        if plan is None:
            found["status"] = 409 if start else 200
            return found
        gate = request.app.state.budget_gate
        if not start:  # a read: ask only whether the button is blocked, which writes nothing
            block = gate.blocked(plan.service)
            if block is not None:
                found["blocked"] = True
                found["reason"] = reason_context(block.message, reason_id=reason_id, focus=False)
            return found
        answer = gate.check(plan.service, tier=plan.tier, confirmation=confirmation_from_form(form))
        if isinstance(answer, Start):
            begin = service.start_gap_check if view.step == 1 else service.start_angles
            ref = begin(aid, tier=tier)
            found["view"] = service.view(aid, view.step)
            found["message"] = started if ref else ALREADY_RUNNING
            found["already"] = ref is None
            found["status"] = 200 if ref else 409
            return found
        dialog = (
            dialog_context(
                answer,
                dialog_id=f"tailoring-dialog-{view.step}",
                action=f"/applications/{aid}/tailoring/{view.step}/"
                + ("check" if view.step == 1 else "propose"),
                target="#tailoring",
                fields={"tier": tier} if tier else {},
                return_focus=f"#{button}",
            )
            if start and is_htmx(request)
            else None
        )
        found["dialog"] = dialog
        found["blocked"] = not answer.asks
        if dialog is None and (start or found["blocked"]):
            found["reason"] = reason_context(
                refusal_message(answer), reason_id=reason_id, focus=start
            )
        found["status"] = 409 if start and dialog is None else 200
    except ProviderNotConfigured as error:
        found["blocked"] = True
        found["status"] = 409 if start else 200
        found["reason"] = reason_context(error.user_message, reason_id=reason_id, focus=start)
        found["reason"].update({"limits_url": "/settings", "link": "Open Settings"})
    return found


def tailoring_context(
    request: Request,
    aid: str,
    step: int | None,
    *,
    start: bool = False,
    form: Form | None = None,
    message: str | None = None,
    announce: str | None = None,
    autofocus: bool = False,
    fact_form: dict[str, str] | None = None,
    fact_added: str | None = None,
    angle_form: dict[str, Any] | None = None,
    country_form: dict[str, Any] | None = None,
    polled: bool = False,
) -> dict[str, Any]:
    """Everything ``fragments/tailoring/_tailoring.html`` shows for one step.

    ``message`` is announced and takes the heading's focus; ``announce`` is announced only.
    ``fact_form`` and ``angle_form`` carry what a refused form had (its values and error),
    ``fact_added`` the requirement a fact was just added for, ``country_form`` what a refused
    or stale country save had (``error``, ``conflict``), ``polled`` that the step
    refreshed itself while its task ran."""
    service = _service(request)
    view = service.view(aid, step)
    found = _starts(request, view, aid, start=start, form=form or {})
    view = found["view"]
    error = view.gap_task if view.step == 1 else view.angle_task
    active = error.active
    completed = polled and view.step in STARTS and not active and error.status == "succeeded"
    asked = fact_form or {}
    gaps = {row.id for row in view.requirements if row.is_gap}
    found.update(
        {
            "polling": active,
            "refresh_once": start and active and found["dialog"] is None,
            "error": message_for_kind(error.error) if error.attention else "",
            "practice_notice": PRACTICE_NOTICE,
            "not_in_fact_bank": NOT_IN_FACT_BANK,
            "later": LATER.get(view.step, ""),
            "fact_form": asked,
            "fact_orphan": bool(asked) and asked.get("requirement_id") not in gaps,
            "fact_added": fact_added,
            "angle_form": _angle_form(angle_form, len(view.angles)),
            "conflict_message": VersionConflict.message,
            "countries": COUNTRIES,
            "country_form": country_form or {},
            "titles": STEP_TITLES,
        }
    )
    # Step 3 holds the CV country's template unit (M6.6): a read, which writes nothing.
    found["template_unit"] = (
        unit_context(request, view.cv_country, level=STEP_UNIT_LEVEL)
        if view.step == 3 and view.cv_country
        else None
    )
    if message:
        found["message"] = message
    finished = ""
    if completed:  # said, not focused: the user may be anywhere on the page by now
        warning = view.gap_message if view.step == 1 else None
        finished = " ".join(filter(None, (COMPLETED[view.step], warning)))
    found["announce"] = found["message"] or announce or finished
    found["autofocus"] = (autofocus or bool(found["message"])) and found["dialog"] is None
    return found


def _angle_form(posted: dict[str, Any] | None, cards: int) -> dict[str, Any]:
    """The refused angle form to show again; ``slot`` is the card it came from, else "own"."""
    if not posted:
        return {}
    slot = str(posted.get("slot", ""))
    kept = slot if slot in {str(n) for n in range(1, cards + 1)} else "own"
    return {**posted, "slot": kept}


def _answer(request: Request, context: dict[str, Any], *, status: int | None = None) -> Response:
    response = templates.TemplateResponse(
        request,
        FRAGMENT,
        {"tailoring": context},
        status_code=status if status is not None else context["status"],
    )
    if context["announce"]:
        response.headers["HX-Trigger"] = json.dumps({"announce": context["announce"]})
    return response


def _not_found() -> Response:
    return PlainTextResponse(ApplicationNotFound.message, status_code=404)


def show_step(request: Request, application_id: str, step: str) -> Response:
    number = step_from_query(step)
    if number is None:
        return PlainTextResponse("Not found", status_code=404)
    trigger = request.headers.get("HX-Trigger", "")
    opened = trigger.startswith(STEP_LINK_PREFIX)
    try:
        context = tailoring_context(
            request,
            application_id,
            number,
            message=f"Step {number}: {STEP_TITLES[number]}." if opened else None,
            autofocus=opened,
            polled=trigger == POLL_TRIGGER,
        )
        return _answer(request, context)
    except ApplicationNotFound:
        return _not_found()


async def _start(request: Request, application_id: str, step: int) -> Response:
    form = await read_form(request)
    if form is None:
        return PlainTextResponse(INVALID_REQUEST, status_code=413)

    def start() -> Response:
        try:
            return _answer(
                request, tailoring_context(request, application_id, step, start=True, form=form)
            )
        except ApplicationNotFound:
            return _not_found()

    return await run_in_threadpool(start)


async def start_check(request: Request, application_id: str) -> Response:
    return await _start(request, application_id, 1)


async def start_propose(request: Request, application_id: str) -> Response:
    return await _start(request, application_id, 2)


def _fact_problem(text: str) -> str:
    if not text:
        return FACT_NEEDED
    return FACT_TOO_LONG if len(text) > MAX_FACT_TEXT else ""


async def add_fact(request: Request, application_id: str) -> Response:
    form = await single_value_form(request, {"text", "requirement_id"})
    if isinstance(form, Response):
        return form
    typed = form.get("text", "").replace("\x00", "")
    text = typed.replace("\r\n", "\n").replace("\r", "\n").strip()
    requirement_id = form.get("requirement_id", "")

    def add() -> Response:
        try:
            service = _service(request)
            service.view(application_id, 1)  # the application must exist
            problem = _fact_problem(text)
            if not problem and not service.has_confirmed_fact(text):  # a double submit adds one
                try:
                    request.app.state.facts.create(FactInput(text=text))
                except FactInputInvalid as error:
                    problem = error.user_message
            if problem:
                shown = {"requirement_id": requirement_id, "text": typed, "error": problem}
                return _answer(
                    request,
                    tailoring_context(
                        request, application_id, 1, fact_form=shown, announce=problem
                    ),
                    status=422,
                )
            return _answer(
                request,
                tailoring_context(
                    request, application_id, 1, message="Fact added.", fact_added=requirement_id
                ),
            )
        except ApplicationNotFound:
            return _not_found()

    return await run_in_threadpool(add)


async def save_angle(request: Request, application_id: str) -> Response:
    form = await single_value_form(
        request, {"name", "description", "version", "reuse_angle_id", "slot"}
    )
    if isinstance(form, Response):
        return form
    version = posted_version(form, minimum=0)
    if version is None:
        return PlainTextResponse(INVALID_REQUEST, status_code=400)
    typed = {
        "slot": form.get("slot", ""),
        "name": form.get("name", "").replace("\x00", ""),
        "description": form.get("description", "").replace("\x00", ""),
        "version": version,
    }

    def refused(*, conflict: bool = False, error: str = "", field: str = "") -> Response:
        shown = {**typed, "conflict": conflict, "error": error, "field": field}
        try:
            context = tailoring_context(
                request, application_id, 2, angle_form=shown, announce=error or None
            )
        except ApplicationNotFound:
            return _not_found()
        return _answer(request, context, status=409 if conflict else 422)

    def save() -> Response:
        try:
            _service(request).save_angle(
                application_id,
                name=form.get("name", ""),
                description=form.get("description", ""),
                version=version,
                reuse_angle_id=form.get("reuse_angle_id") or None,
            )
        except ApplicationNotFound:
            return _not_found()
        except VersionConflict:
            return refused(conflict=True)
        except AngleInvalid as error:
            return refused(error=error.message, field=error.field)
        try:
            return _answer(
                request, tailoring_context(request, application_id, 2, message="Angle saved.")
            )
        except ApplicationNotFound:
            return _not_found()

    return await run_in_threadpool(save)


def _open_step(request: Request) -> int | None:
    """The step the page was showing (``step`` in the address htmx reports), so a country save
    leaves the user where they were; None (the derived step) when there is none."""
    current = request.headers.get("HX-Current-URL", "")
    try:
        found = parse_qs(urlsplit(current).query).get("step", [])
    except ValueError:
        return None
    return step_from_query(found[0]) if len(found) == 1 else None


async def save_country(request: Request, application_id: str) -> Response:
    form = await single_value_form(request, {"version", "cv_country"})
    if isinstance(form, Response):
        return form
    version = posted_version(form, minimum=0)
    if version is None or "cv_country" not in form:
        return PlainTextResponse(INVALID_REQUEST, status_code=400)

    def refused(*, conflict: bool = False, error: str = "") -> Response:
        shown = {"conflict": conflict, "error": error}
        try:
            context = tailoring_context(
                request,
                application_id,
                _open_step(request),
                country_form=shown,
                announce=error or None,
            )
        except ApplicationNotFound:
            return _not_found()
        return _answer(request, context, status=409 if conflict else 422)

    def save() -> Response:
        try:
            _service(request).save_country(
                application_id, cv_country=form["cv_country"], version=version
            )
        except ApplicationNotFound:
            return _not_found()
        except VersionConflict:
            return refused(conflict=True)
        except CountryInvalid as error:
            return refused(error=error.message)
        try:
            return _answer(
                request,
                tailoring_context(
                    request, application_id, _open_step(request), message="CV country saved."
                ),
            )
        except ApplicationNotFound:
            return _not_found()

    return await run_in_threadpool(save)
