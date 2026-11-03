"""Country CV templates: Settings > CV templates and the template unit (M6.6; ADR-106 items 3, 6
and 12; spec 3.6). None of these routes is a page, so every one needs the launch token, and every
POST also the origin. The unit (``#cv-template``) is also shown on step 3 of the Tailoring tab
(``app/api/tailoring.py`` calls ``unit_context``); both places use the same routes.

- ``GET  /settings/cv-templates``: the ``#templates-settings`` section (the same markup the
  token-free Settings page carries: a list of country, code and status; no personal data).
- ``GET  /settings/cv-templates/{code}``: the unit alone. Read only. A poll of its own task
  (``HX-Trigger: cv-template``) announces "Template draft ready." once the task has succeeded; the
  Settings list's button opens it and moves focus to its heading.
- ``POST /settings/cv-templates/{code}/draft`` (``pages``, ``photo``, ``note`` and the budget
  dialog's fields): ask the AI for the country's template through the budget gate; one task per
  country (a running one is returned). A refused style or note is a 422 tied to its field.
- ``POST /settings/cv-templates/{code}`` (``version``, ``action`` = ``save`` or ``approve`` and the
  twelve section slots and the other spec fields): the review form. A stale version is a 409 with
  the reload prompt, a refused value a 422 tied to its control; nothing is stored then.

A code outside the country list is a plain-text 404. An unknown or repeated field, an ``action``
that is neither, and a missing or non-numeric ``version`` are a plain-text 400. Answers announce
with ``HX-Trigger`` (``announce``); a change to a template also sends ``templates-changed`` so the
Settings list refreshes itself. Entered text is escaped and never echoed in a message.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.budget_gate import dialog_context, reason_context, refusal_message
from app.api.errors import INVALID_REQUEST
from app.api.forms import posted_version, read_form, single_value_form
from app.api.templating import templates
from app.domain.errors import ProviderNotConfigured, VersionConflict, message_for_kind
from app.services.budget_gate import Start, confirmation_from_form
from app.services.cv_import import tier_from_form
from app.services.cv_templates import (
    CHECKBOXES,
    COUNTRY_NAMES,
    DEFAULT_PAGES,
    DEFAULT_PHOTO,
    DETAIL_IDS,
    PAGE_CHOICES,
    PAGE_SIZES,
    PHOTO_CHOICES,
    REVIEW_FIELDS,
    SECTION_IDS,
    SLOTS,
    CvTemplateService,
    ReviewInvalid,
    StyleInvalid,
    TemplateBusy,
    TemplateNotFound,
    TemplateView,
    clean_note,
    shown_from_spec,
    shown_with_typed,
    style_from_form,
)
from app.services.job_fit import PRACTICE_NOTICE
from app.services.template_ai import MAX_NOTE_CHARS

BASE: Final = "/settings/cv-templates"
UNIT_FRAGMENT: Final = "fragments/settings/_template_unit.html"
SECTION_FRAGMENT: Final = "fragments/settings/_templates.html"
SETTINGS_PATH: Final = "/settings"  # Providers and AI: where a provider is added
AREA_PATH: Final = "/settings/your-details"  # the page that holds the section (M9.2)
STARTED: Final = "Template draft started."
READY: Final = "Template draft ready."
SAVED: Final = "Template saved. Approve it to use it."
APPROVED: Final = "Template approved."
POLL_TRIGGER: Final = "cv-template"  # the id htmx sends for the unit's own refresh
REVIEW_BUTTON_PREFIX: Final = "template-review-"
CHANGED_EVENT: Final = "templates-changed"
REASON_ID: Final = "template-reason"
START_BUTTON: Final = "template-draft-start"
STEP_LEVEL: Final = 5  # under the workspace's step heading
SETTINGS_LEVEL: Final = 3  # under Settings' section heading
_GATE_FIELDS: Final = frozenset(
    {"tier", "budget_service", "budget_threshold", "budget_cycle_start", "budget_chosen_tier"}
)
_DRAFT_FIELDS: Final = frozenset({"pages", "photo", "note", *_GATE_FIELDS})
_REVIEW_ALLOWED: Final = frozenset({"version", "action", *REVIEW_FIELDS})
_NOT_FOUND: Final = "Not found"
SECTION_LABELS: Final = tuple((name, name.replace("_", " ").capitalize()) for name in SECTION_IDS)
DETAIL_LABELS: Final = tuple((name, f"Show {name}") for name in DETAIL_IDS)
PAGE_LABELS: Final = tuple(
    (value, f"{value} page" + ("s" if value != "1" else "")) for value in PAGE_CHOICES
)
PHOTO_LABELS: Final = (
    ("none", "No photo"),
    ("optional", "Optional photo"),
    ("expected", "Photo expected"),
)
DATE_LABELS: Final = (
    ("month_year", "Month and year (Mar 2024)"),
    ("numeric", "Numeric (03/2024)"),
    ("iso", "ISO (2024-03)"),
    ("year", "Year only (2024)"),
)
SIZE_LABELS: Final = tuple((name, name) for name in PAGE_SIZES)


@dataclass(frozen=True)
class DraftState:
    """The draft form as shown: the choices, the note and the refused fields."""

    pages: str = DEFAULT_PAGES
    photo: str = DEFAULT_PHOTO
    note: str = ""
    errors: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewState:
    """What a refused or stale review form had: what was typed, the refused controls, the
    version the form was based on, whether the template changed elsewhere."""

    typed: Mapping[str, str] | None = None
    errors: Mapping[str, str] = field(default_factory=dict)
    version: int | None = None
    conflict: bool = False
    busy: bool = False  # an approval refused because a draft is queued or running


@dataclass
class _Gate:
    blocked: bool = False
    reason: dict[str, Any] | None = None
    dialog: dict[str, Any] | None = None
    status: int = 200
    started: bool = False


def register_template_routes(app: FastAPI) -> None:
    """Add the routes directly to the app; none is a page, so all need the token."""
    app.add_api_route(BASE, show_section, methods=["GET"], response_class=HTMLResponse)
    app.add_api_route(BASE + "/{code}", show_unit, methods=["GET"], response_class=HTMLResponse)
    app.add_api_route(
        BASE + "/{code}/draft", start_draft, methods=["POST"], response_class=HTMLResponse
    )
    app.add_api_route(BASE + "/{code}", save_review, methods=["POST"], response_class=HTMLResponse)


def _service(request: Request) -> CvTemplateService:
    service: CvTemplateService = request.app.state.cv_templates
    return service


def _not_found() -> Response:
    return PlainTextResponse(_NOT_FOUND, status_code=404)


def _invalid() -> Response:
    return PlainTextResponse(INVALID_REQUEST, status_code=400)


def _fields_to_change(count: int) -> str:
    return "1 field needs a change." if count == 1 else f"{count} fields need a change."


def section_context(request: Request) -> dict[str, Any]:
    """The ``cv_templates`` template value of Settings. Blocking (reads the database)."""
    return {"rows": _service(request).listing(), "base": BASE}


def _level(request: Request) -> int:
    """Heading level: under Settings' h2 when the page that asked is Settings, else the step's."""
    try:
        path = urlsplit(request.headers.get("HX-Current-URL", "")).path
    except ValueError:
        return STEP_LEVEL
    return SETTINGS_LEVEL if path == AREA_PATH else STEP_LEVEL


def _state(view: TemplateView) -> str:
    if view.task.active:
        return "running"
    if view.spec is None:
        return "failed" if view.task.attention else "none"
    return view.status or "draft"


def _gate(
    request: Request,
    view: TemplateView,
    *,
    start: bool,
    form: Mapping[str, Sequence[str]],
    dialog_fields: Mapping[str, str],
    begin: Any,
) -> _Gate:
    """The budget gate's answer for the draft button; with ``start`` also the start itself
    (``begin(tier)``). A read only asks whether the button is blocked, which writes nothing."""
    found = _Gate()
    if view.task.active:  # one draft per country: the running one is the answer
        found.started = start
        return found
    service = _service(request)
    try:
        tier = tier_from_form(form)
        plan = service.plan_start(tier=tier)
        gate = request.app.state.budget_gate
        if not start:
            block = gate.blocked(plan.service)
            if block is not None:
                found.blocked = True
                found.reason = reason_context(block.message, reason_id=REASON_ID, focus=False)
            return found
        answer = gate.check(plan.service, tier=plan.tier, confirmation=confirmation_from_form(form))
        if isinstance(answer, Start):
            begin(tier)
            found.started = True
            return found
        fields = {**dialog_fields, **({"tier": tier} if tier else {})}
        found.dialog = dialog_context(
            answer,
            dialog_id="template-dialog",
            action=f"{BASE}/{view.code}/draft",
            target="#cv-template",
            fields=fields,
            return_focus=f"#{START_BUTTON}",
        )
        found.blocked = not answer.asks
        if found.dialog is None:
            found.reason = reason_context(refusal_message(answer), reason_id=REASON_ID, focus=True)
        found.status = 409 if found.dialog is None else 200
    except ProviderNotConfigured as error:
        found.blocked = True
        found.status = 409 if start else 200
        found.reason = reason_context(error.user_message, reason_id=REASON_ID, focus=start)
        found.reason.update({"limits_url": SETTINGS_PATH, "link": "Open Settings"})
    return found


def unit_context(
    request: Request,
    code: str,
    *,
    level: int | None = None,
    start: bool = False,
    form: Mapping[str, Sequence[str]] | None = None,
    style: tuple[Any, str] | None = None,
    draft: DraftState | None = None,
    review: ReviewState | None = None,
    message: str | None = None,
    announce: str | None = None,
    polled: bool = False,
    opened: bool = False,
) -> dict[str, Any]:
    """Everything ``fragments/settings/_template_unit.html`` shows for one country.

    ``message`` is announced and takes the heading's focus; ``announce`` is announced only;
    ``style`` is the accepted (``TemplateStyle``, note) a ``start`` begins; ``draft`` and
    ``review`` say what a refused form had. Blocking. ``TemplateNotFound`` for a code that is not
    a listed country."""
    service = _service(request)
    view = service.view(code)
    typed_draft = draft or DraftState()
    dialog_fields = {"pages": typed_draft.pages, "photo": typed_draft.photo}
    if typed_draft.note:
        dialog_fields["note"] = typed_draft.note

    def begin(tier: Any) -> None:
        if style is not None:
            service.start_draft(code, style[0], style[1], tier=tier)

    gate = _gate(
        request,
        view,
        start=start and style is not None,
        form=form or {},
        dialog_fields=dialog_fields,
        begin=begin,
    )
    if gate.started:
        view = service.view(code)
    state = review or ReviewState()
    shown = shown_from_spec(view.spec) if view.spec is not None else {}
    if view.spec is not None and state.typed is not None and not state.conflict:
        shown = shown_with_typed(view.spec, state.typed)
    errors = dict(state.errors)
    draft_errors = dict(typed_draft.errors)
    first_error = next(iter(errors), "")
    first_draft_error = next((n for n in ("pages", "photo", "note") if n in draft_errors), "")
    if start and style is not None and gate.started:
        message = message or STARTED
    finished = ""
    if polled and not view.task.active and view.task.status == "succeeded":
        finished = READY
    refused = bool(
        errors or draft_errors or state.conflict or state.busy or gate.reason or gate.dialog
    )
    context: dict[str, Any] = {
        "view": view,
        "code": code,
        "country": view.country,
        "state": _state(view),
        "level": level or _level(request),
        "base": BASE,
        "polling": view.task.active,
        "refresh_once": start and gate.started and view.task.active and gate.dialog is None,
        "error": message_for_kind(view.task.error) if view.task.attention else "",
        "practice_notice": PRACTICE_NOTICE,
        "draft": typed_draft,
        "draft_errors": draft_errors,
        "first_draft_error": first_draft_error,
        "draft_open": bool(draft_errors or gate.reason or gate.dialog),
        "max_note": MAX_NOTE_CHARS,
        "blocked": gate.blocked,
        "reason": gate.reason,
        "dialog": gate.dialog,
        "status": gate.status if start else 200,
        "shown": shown,
        "errors": errors,
        "first_error": first_error,
        "form_version": view.version if state.version is None else state.version,
        "conflict": state.conflict,
        "busy": TemplateBusy.message if state.busy else "",
        "conflict_message": VersionConflict.message,
        "settings_url": AREA_PATH,
        "slots": range(1, SLOTS + 1),
        "section_options": SECTION_LABELS,
        "detail_options": DETAIL_LABELS,
        "page_options": PAGE_LABELS,
        "photo_options": PHOTO_LABELS,
        "date_options": DATE_LABELS,
        "size_options": SIZE_LABELS,
        "notes": view.notes,
        "message": message,
    }
    context["autofocus"] = bool(message or opened) and not refused
    context["announce"] = message or announce or finished
    if opened:
        context["announce"] = f"CV template: {view.country}."
    return context


def _answer(
    request: Request, context: dict[str, Any], *, status: int = 200, changed: bool = False
) -> Response:
    response = templates.TemplateResponse(
        request, UNIT_FRAGMENT, {"unit": context}, status_code=status
    )
    triggers: dict[str, str] = {}
    if context["announce"]:
        triggers["announce"] = context["announce"]
    if changed:
        triggers[CHANGED_EVENT] = context["code"]
    if triggers:
        response.headers["HX-Trigger"] = json.dumps(triggers)
    return response


def show_section(request: Request) -> Response:
    """The section alone. A plain function: FastAPI runs it in its thread pool."""
    return templates.TemplateResponse(
        request, SECTION_FRAGMENT, {"cv_templates": section_context(request)}
    )


def show_unit(request: Request, code: str) -> Response:
    if code not in COUNTRY_NAMES:
        return _not_found()
    trigger = request.headers.get("HX-Trigger", "")
    polled = trigger == POLL_TRIGGER
    try:
        context = unit_context(
            request,
            code,
            polled=polled,
            opened=trigger.startswith(REVIEW_BUTTON_PREFIX),
        )
    except TemplateNotFound:
        return _not_found()
    finished = polled and context["announce"] == READY
    return _answer(request, context, changed=finished)


def _draft_values(form: Mapping[str, Sequence[str]]) -> dict[str, str]:
    return {
        "pages": form.get("pages", [DEFAULT_PAGES])[0],
        "photo": form.get("photo", [DEFAULT_PHOTO])[0],
        "note": form.get("note", [""])[0].replace("\x00", ""),
    }


def _draft_shown(values: Mapping[str, str], errors: Mapping[str, str]) -> DraftState:
    """The refused draft form: a choice that is in the list stays, the note stays as typed."""
    pages, photo = values["pages"], values["photo"]
    return DraftState(
        pages if pages in PAGE_CHOICES else DEFAULT_PAGES,
        photo if photo in PHOTO_CHOICES else DEFAULT_PHOTO,
        values["note"],
        errors,
    )


async def start_draft(request: Request, code: str) -> Response:
    if code not in COUNTRY_NAMES:
        return _not_found()
    form = await read_form(request)
    if form is None:
        return PlainTextResponse(INVALID_REQUEST, status_code=413)
    if set(form) - _DRAFT_FIELDS or any(len(values) != 1 for values in form.values()):
        return _invalid()
    values = _draft_values(form)

    def run() -> Response:
        try:
            style, note = style_from_form(values["pages"], values["photo"], values["note"])
        except StyleInvalid as error:
            said = f"Draft not started. {_fields_to_change(len(error.fields))}"
            try:
                context = unit_context(
                    request,
                    code,
                    draft=_draft_shown(values, error.fields),
                    announce=said,
                )
            except TemplateNotFound:
                return _not_found()
            return _answer(request, context, status=422)
        shown = DraftState(values["pages"], values["photo"], clean_note(values["note"]))
        try:
            context = unit_context(
                request, code, start=True, form=form, style=(style, note), draft=shown
            )
        except TemplateNotFound:
            return _not_found()
        return _answer(request, context, status=context["status"], changed=context["refresh_once"])

    return await run_in_threadpool(run)


async def save_review(request: Request, code: str) -> Response:
    if code not in COUNTRY_NAMES:
        return _not_found()
    form = await single_value_form(request, set(_REVIEW_ALLOWED))
    if isinstance(form, Response):
        return form
    version = posted_version(form, minimum=1)
    action = form.get("action")
    if (
        version is None
        or action not in ("save", "approve")
        or any(form.get(name, "1") != "1" for name in CHECKBOXES)
    ):
        return _invalid()
    approve = action == "approve"
    subject = "approved" if approve else "saved"
    typed = {name: value.replace("\x00", "") for name, value in form.items()}

    def refused(state: ReviewState, status: int, said: str) -> Response:
        try:
            context = unit_context(request, code, review=state, announce=said)
        except TemplateNotFound:
            return _not_found()
        return _answer(request, context, status=status)

    def run() -> Response:
        try:
            _service(request).save(code, form, version=version, approve=approve)
        except TemplateNotFound:
            return _not_found()
        except ReviewInvalid as error:
            state = ReviewState(typed=typed, errors=error.fields, version=version)
            said = f"Template not {subject}. {_fields_to_change(len(error.fields))}"
            return refused(state, 422, said)
        except VersionConflict:
            return refused(ReviewState(conflict=True), 409, VersionConflict.message)
        except TemplateBusy:
            return refused(ReviewState(busy=True), 409, TemplateBusy.message)
        try:
            context = unit_context(request, code, message=APPROVED if approve else SAVED)
        except TemplateNotFound:
            return _not_found()
        return _answer(request, context, changed=True)

    return await run_in_threadpool(run)
