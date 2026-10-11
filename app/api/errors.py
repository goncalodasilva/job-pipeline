"""App-wide error responses (spec 2.9).

The guide swaps 422 responses into the page so field errors show (htmx ``responseHandling``
in ``base.html``). FastAPI's own request-validation error is a JSON 422 that echoes the input
unescaped, so it is replaced by a plain 400 that htmx treats as an error and never swaps.
A refused upload (``app/api/uploads.py``) answers 413 or 400 with its friendly message.
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse

from app.domain.errors import AppError, UploadNotAccepted, UploadTooLarge

INVALID_REQUEST = "This request isn't valid."


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, _invalid_request)
    app.add_exception_handler(UploadTooLarge, _upload_too_large)
    app.add_exception_handler(UploadNotAccepted, _upload_not_accepted)


async def _invalid_request(request: Request, exc: Exception) -> PlainTextResponse:
    return PlainTextResponse(INVALID_REQUEST, status_code=400)


# The two upload handlers are a backstop: form routes catch these errors themselves and render
# the message inside the form.
async def _upload_too_large(request: Request, exc: Exception) -> PlainTextResponse:
    return PlainTextResponse(_user_message(exc), status_code=413)


async def _upload_not_accepted(request: Request, exc: Exception) -> PlainTextResponse:
    return PlainTextResponse(_user_message(exc), status_code=400)


def _user_message(exc: Exception) -> str:
    return exc.user_message if isinstance(exc, AppError) else AppError.message
