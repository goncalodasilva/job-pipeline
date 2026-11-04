"""Form helpers shared by the guide's and Settings' routes (M1.21).

Bodies are URL-encoded forms parsed with the standard library and capped in size. A refused
form shows back only the fields its handler names (``echo_fields``, one value each;
``echo_multi_fields``, every value), never anything else such as a credential.
"""

from collections.abc import Sequence
from typing import Any
from urllib.parse import parse_qs

from fastapi import Request
from fastapi.responses import PlainTextResponse, Response

from app.api.errors import INVALID_REQUEST
from app.onboarding.registry import Form

MAX_FORM_BYTES = 64 * 1024  # these forms are a few short fields
# A prompt editor form holds two sections of up to 256 KiB between them (URL-encoded, a byte can
# take three): the cap is a generous bound on that, not a limit on the prompt (M9.3).
MAX_PROMPT_FORM_BYTES = 1024 * 1024
MAX_VERSION = 2**63 - 1


async def read_form(request: Request, *, limit: int = MAX_FORM_BYTES) -> Form | None:
    """The URL-encoded body as lists of values, or None when it is above ``limit`` bytes."""
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return None
    parsed: dict[str, list[str]] = parse_qs(
        body.decode("utf-8", errors="replace"), keep_blank_values=True
    )
    fields: dict[str, Sequence[str]] = dict(parsed)
    return fields


def echo_submitted(form: dict[str, Any], handler: object, submitted: Form) -> None:
    """Show the submitted values back, but only the fields the handler names (autoescaped;
    NUL can't appear in HTML): ``echo_fields`` hold one value, ``echo_multi_fields`` every
    value (checkboxes; none ticked means an empty list). None by default."""
    echo_fields: tuple[str, ...] = getattr(handler, "echo_fields", ())
    for name in echo_fields:
        values = submitted.get(name)
        if values:
            form[name] = values[0].replace("\x00", "")
    multi_fields: tuple[str, ...] = getattr(handler, "echo_multi_fields", ())
    for name in multi_fields:
        form[name] = [value.replace("\x00", "") for value in submitted.get(name, ())]


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


async def single_value_form(
    request: Request, allowed: set[str], *, limit: int = MAX_FORM_BYTES
) -> dict[str, str] | Response:
    """The form's fields, each given once; anything else is a 400, a huge body a 413."""
    form = await read_form(request, limit=limit)
    if form is None:
        return PlainTextResponse(INVALID_REQUEST, status_code=413)
    if set(form) - allowed or any(len(values) != 1 for values in form.values()):
        return PlainTextResponse(INVALID_REQUEST, status_code=400)
    return {name: values[0] for name, values in form.items()}


def posted_version(form: dict[str, str], *, minimum: int) -> int | None:
    """The form's ``version`` as a whole number from ``minimum`` up, else None."""
    try:
        value = int(form.get("version", ""))
    except ValueError:
        return None
    return value if minimum <= value <= MAX_VERSION else None
