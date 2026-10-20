"""Job page and protected drawer fragments; local edits use optimistic concurrency."""

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.inbox import _form, _not_found, _perform, _reload_prompt, _version
from app.api.job_summary import summary_context
from app.api.pages import AREAS, area
from app.api.security import PAGE_TAG
from app.api.templating import render, templates
from app.domain.errors import JobNotFound, JobTriageInputInvalid, TriageNotAllowed, VersionConflict
from app.domain.job_edit import EDIT_FIELDS, JobEditInvalid
from app.services.job_detail import JobDetailService


def register_job_detail_routes(app: FastAPI) -> None:
    app.add_api_route(
        "/jobs/{job_id}",
        job_page,
        methods=["GET", "HEAD"],
        tags=[PAGE_TAG],
        response_class=HTMLResponse,
        name="page_job",
    )
    app.add_api_route("/inbox/jobs/{job_id}/detail", drawer, methods=["GET"])
    app.add_api_route("/inbox/jobs/{job_id}/header", header, methods=["GET"])
    app.add_api_route("/inbox/jobs/{job_id}/edit", edit, methods=["POST"])
    app.add_api_route("/inbox/jobs/{job_id}/detail-triage", triage, methods=["POST"])
    app.add_api_route("/inbox/jobs/{job_id}/split", split, methods=["POST"])


def _service(request: Request) -> JobDetailService:
    service: JobDetailService = request.app.state.job_detail
    return service


def _context(request: Request, job_id: str) -> dict[str, Any]:
    shown = _service(request).view(job_id)
    return {
        **shown,
        "job": shown["card"]["job"],
        "area": area("inbox"),
        "areas": AREAS,
        "summary": summary_context(request, job_id),
        "saved": False,
        "errors": {},
        "submitted": {},
        "conflict": False,
        "message": "",
        "triage_error": None,
        "triage_submitted": {},
    }


def job_page(request: Request, job_id: str) -> Response:
    try:
        return render(
            request,
            page="pages/job_detail.html",
            fragment="fragments/inbox/_detail.html",
            ctx=_context(request, job_id),
        )
    except JobNotFound:
        return _not_found()


def drawer(request: Request, job_id: str) -> Response:
    try:
        return templates.TemplateResponse(
            request, "fragments/inbox/_drawer.html", _context(request, job_id)
        )
    except JobNotFound:
        return _not_found()


def header(request: Request, job_id: str) -> Response:
    try:
        shown = _service(request).view(job_id)
        return templates.TemplateResponse(
            request,
            "fragments/inbox/_detail_header.html",
            {
                **shown,
                "job": shown["card"]["job"],
                "message": "",
                "triage_error": None,
                "triage_submitted": {},
            },
        )
    except JobNotFound:
        return _not_found()


def _changed(response: Response, job_id: str, message: str, **details: Any) -> Response:
    response.headers["HX-Trigger-After-Settle"] = json.dumps(
        {"announce": message, "job:changed": {"id": job_id, **details}}
    )
    return response


async def edit(request: Request, job_id: str) -> Response:
    form = await _form(request, {"version", *EDIT_FIELDS})
    if isinstance(form, Response):
        return form
    version = _version(form, minimum=1)
    if version is None:
        return PlainTextResponse("Invalid request.", status_code=400)

    def save() -> Response:
        errors, conflict, saved = {}, False, False
        try:
            _service(request).edit(job_id, form, expected_version=version)
            saved = True
        except JobEditInvalid as error:
            errors = error.errors
        except VersionConflict:
            conflict = True
        except JobNotFound:
            return _not_found()
        try:
            shown = _service(request).view(job_id)
        except JobNotFound:
            return _not_found()
        ctx = {
            **shown,
            "job": shown["card"]["job"],
            "errors": errors,
            "conflict": conflict,
            "saved": saved,
            "submitted": {k: v.replace("\x00", "") for k, v in form.items()}
            if errors or conflict
            else {},
        }
        response = templates.TemplateResponse(
            request,
            "fragments/inbox/_job_edit.html",
            ctx,
            status_code=409 if conflict else 422 if errors else 200,
        )
        return _changed(response, job_id, "Saved.", edited=True) if saved else response

    return await run_in_threadpool(save)


async def triage(request: Request, job_id: str) -> Response:
    form = await _form(request, {"version", "action", "reason", "text", "choice", "picked"})
    if isinstance(form, Response):
        return form
    version = _version(form, minimum=1)
    if version is None or form.get("action") not in (
        "remove",
        "to_apply",
        "snooze",
        "undo",
        "restore",
        "show",
    ):
        return PlainTextResponse("Invalid request.", status_code=400)

    def move() -> Response:
        try:
            committed = (
                request.app.state.job_triage.mark_seen(job_id, expected_version=version)
                if form["action"] == "show"
                else _perform(request.app.state.job_triage, job_id, form, version)
            )
        except JobNotFound:
            return _not_found()
        except JobTriageInputInvalid as error:
            shown = _service(request).view(job_id)
            return templates.TemplateResponse(
                request,
                "fragments/inbox/_detail_header.html",
                {
                    **shown,
                    "job": shown["card"]["job"],
                    "message": "",
                    "triage_error": error,
                    "triage_submitted": {k: v.replace("\x00", "") for k, v in form.items()},
                },
                status_code=422,
            )
        except (VersionConflict, TriageNotAllowed):
            return _reload_prompt(
                request, detail="Your last change was not saved.", url=f"/jobs/{job_id}"
            )
        shown = _service(request).view(job_id)
        message = {
            "remove": "Removed. Restore is available.",
            "to_apply": "Marked to apply. You can put it back in the queue.",
            "snooze": "Snoozed. You can put it back in the queue.",
            "undo": "Back in the queue.",
            "restore": "Restored. The job is back in the queue.",
            "show": "Marked seen.",
        }[form["action"]]
        response = templates.TemplateResponse(
            request,
            "fragments/inbox/_detail_header.html",
            {
                **shown,
                "job": shown["card"]["job"],
                "message": message,
                "triage_error": None,
                "triage_submitted": {},
            },
        )
        return _changed(
            response,
            job_id,
            message,
            previous_version=version,
            version=committed.version,
        )

    return await run_in_threadpool(move)


async def split(request: Request, job_id: str) -> Response:
    form = await _form(request, {"root"})
    if isinstance(form, Response):
        return form

    def separate() -> Response:
        try:
            _service(request).view(form.get("root", ""))
            request.app.state.job_intake.not_duplicate(job_id)
            shown = _service(request).view(form["root"])
        except JobNotFound:
            return _not_found()
        response = templates.TemplateResponse(
            request,
            "fragments/inbox/_job_sources.html",
            {**shown, "job": shown["card"]["job"], "saved": True},
        )
        return _changed(response, form["root"], "Job restored as its own card.")

    return await run_in_threadpool(separate)
