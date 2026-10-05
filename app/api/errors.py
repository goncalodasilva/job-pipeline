"""App-wide error responses (spec 2.9).

The guide swaps 422 responses into the page so field errors show (htmx ``responseHandling``
in ``base.html``). FastAPI's own request-validation error is a JSON 422 that echoes the input
unescaped, so it is replaced by a plain 400 that htmx treats as an error and never swaps.
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse

INVALID_REQUEST = "This request isn't valid."


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, _invalid_request)


async def _invalid_request(request: Request, exc: Exception) -> PlainTextResponse:
    return PlainTextResponse(INVALID_REQUEST, status_code=400)
