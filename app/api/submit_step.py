"""Protected Submit actions and read-only export downloads. Routes delegate work to services."""

import json

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.errors import INVALID_REQUEST
from app.api.forms import posted_version, single_value_form
from app.api.templating import templates
from app.domain.errors import AppError, ApplicationNotFound, StatusNotAllowed, VersionConflict
from app.services.exports import MEDIA, ExportRefused, ExportService

DOWNLOAD_NOT_FOUND = "That file wasn't found."


def service(request: Request) -> ExportService:
    result: ExportService = request.app.state.exports
    return result


def answer(
    request: Request,
    aid: str,
    *,
    message: str = "",
    error: str = "",
    conflict: bool = False,
    unavailable: str = "",
    status: int = 200,
) -> Response:
    from app.api.tailoring import tailoring_context

    context = tailoring_context(request, aid, 5)
    context["autofocus"] = False
    context["submit"].update(
        message=message,
        error=error,
        conflict=conflict,
        unavailable=unavailable,
        focus=bool(message or error),
    )
    response = templates.TemplateResponse(
        request, "fragments/tailoring/_tailoring.html", {"tailoring": context}, status_code=status
    )
    if message or error:
        response.headers["HX-Trigger"] = json.dumps({"announce": message or error})
    return response


async def export(request: Request, application_id: str) -> Response:
    form = await single_value_form(request, set())
    if isinstance(form, Response):
        return form
    try:
        unavailable = await service(request).export(application_id)
        return await run_in_threadpool(
            answer,
            request,
            application_id,
            message=unavailable or "Files exported.",
            unavailable=unavailable,
        )
    except ApplicationNotFound:
        return PlainTextResponse(ApplicationNotFound.message, status_code=404)
    except VersionConflict:
        return await run_in_threadpool(answer, request, application_id, conflict=True, status=409)
    except (ExportRefused, AppError) as error:
        words = error.user_message if isinstance(error, AppError) else str(error)
        return await run_in_threadpool(answer, request, application_id, error=words, status=409)


async def applied(request: Request, application_id: str) -> Response:
    form = await single_value_form(request, {"version"})
    if isinstance(form, Response):
        return form
    version = posted_version(form, minimum=1)
    if version is None:
        return PlainTextResponse(INVALID_REQUEST, status_code=400)

    def move() -> Response:
        try:
            request.app.state.applications.mark_submitted(application_id, expected_version=version)
            return answer(request, application_id, message="Moved to Applied.")
        except ApplicationNotFound:
            return PlainTextResponse(ApplicationNotFound.message, status_code=404)
        except VersionConflict:
            return answer(request, application_id, conflict=True, status=409)
        except StatusNotAllowed as error:
            return answer(request, application_id, error=error.user_message, status=409)

    return await run_in_threadpool(move)


def download(request: Request, application_id: str, export_id: str) -> Response:
    try:
        found = service(request).download(application_id, export_id)
    except ApplicationNotFound:
        found = None
    if found is None:
        return PlainTextResponse(DOWNLOAD_NOT_FOUND, status_code=404)
    row, content = found
    return Response(
        content,
        media_type=MEDIA[row.format],
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{row.stored_name}"',
        },
    )


def register_submit_routes(app: FastAPI) -> None:
    base = "/applications/{application_id}"
    app.add_api_route(base + "/tailoring/5/export", export, methods=["POST"])
    app.add_api_route(base + "/tailoring/5/applied", applied, methods=["POST"])
    app.add_api_route(base + "/exports/{export_id}", download, methods=["GET"])
