"""Token/origin-protected job intake and duplicate split (M4.3)."""

import json
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.errors import INVALID_REQUEST
from app.api.forms import is_htmx
from app.api.pages import AREAS, area
from app.api.templating import render
from app.domain.errors import (
    FetchRefused,
    JobLinkInvalid,
    JobNotFound,
    JobTextNotFound,
    JobTextTooLong,
    NetworkUnavailable,
)
from app.services.job_intake import JobIntakeService

PASTE_MESSAGE = "This page couldn't be fetched. Paste the job text instead."
MAX_INTAKE_BODY = 640 * 1024  # 50,000 four-byte codepoints, percent encoded, plus the link


def register_job_intake_routes(app: FastAPI) -> None:
    app.add_api_route("/inbox/intake", intake, methods=["POST"])
    app.add_api_route("/inbox/jobs/{job_id}/not-duplicate", not_duplicate, methods=["POST"])


def _service(request: Request) -> JobIntakeService:
    service: JobIntakeService = request.app.state.job_intake
    return service


def _answer(
    request: Request,
    *,
    url: str = "",
    text: str = "",
    error: str = "",
    field: str = "",
    said: str = "",
    job_id: str = "",
    source_job_id: str = "",
    merged: bool = False,
    status: int = 200,
) -> Response:
    response = render(
        request,
        page="pages/job_intake.html",
        fragment="fragments/inbox/_intake.html",
        ctx={
            "url": url.replace("\x00", ""),
            "text": text.replace("\x00", ""),
            "error": error,
            "error_field": field,
            "said": said,
            "job_id": job_id,
            "source_job_id": source_job_id,
            "merged": merged,
            "area": area("inbox"),
            "areas": AREAS,
            "title": "Add job",
        },
    )
    response.status_code = status
    response.headers["HX-Trigger"] = json.dumps({"announce": error or said})
    return response


async def intake(request: Request) -> Response:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_INTAKE_BODY:
            return PlainTextResponse(JobTextTooLong.message, status_code=413)
        body.extend(chunk)
    try:
        form = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=4)
    except (UnicodeError, ValueError):
        return PlainTextResponse(INVALID_REQUEST, status_code=400)
    if any(len(values) != 1 for values in form.values()) or set(form) - {"url", "text"}:
        return PlainTextResponse(INVALID_REQUEST, status_code=400)
    url = form.get("url", [""])[0].strip()
    text = form.get("text", [""])[0]
    try:
        service = _service(request)
        result = (
            await run_in_threadpool(service.add_text, text, link=url or None)
            if text.strip()
            else await service.add_url(url)
        )
    except JobTextTooLong as error:
        return PlainTextResponse(error.user_message, status_code=413)
    except (FetchRefused, NetworkUnavailable, JobTextNotFound):
        return await run_in_threadpool(
            _answer, request, url=url, text=text, error=PASTE_MESSAGE, field="text", status=422
        )
    except JobLinkInvalid as error:
        return await run_in_threadpool(
            _answer, request, url=url, text=text, error=error.user_message, field="url", status=422
        )
    said = (
        "Job saved. Another source was added to the existing card."
        if result.merged
        else "Job saved."
    )
    return await run_in_threadpool(
        _answer,
        request,
        said=said,
        job_id=result.job_id,
        source_job_id=result.source_job_id,
        merged=result.merged,
    )


async def not_duplicate(request: Request, job_id: str) -> Response:
    try:
        await run_in_threadpool(_service(request).not_duplicate, job_id)
    except JobNotFound as error:
        return PlainTextResponse(error.user_message, status_code=404)
    if not is_htmx(request):
        return RedirectResponse("/inbox", status_code=303)
    return await run_in_threadpool(
        _answer, request, said="Job restored as its own card.", job_id=job_id
    )
