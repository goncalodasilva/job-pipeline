"""Form helpers shared by the guide's and Settings' routes (M1.21).

Bodies are URL-encoded forms parsed with the standard library and capped in size. A refused
form shows back only the fields its handler names (``echo_fields``, one value each;
``echo_multi_fields``, every value), never anything else such as a credential.
"""

import re
from collections.abc import Sequence
from typing import Any
from urllib.parse import parse_qs

from fastapi import Request

from app.onboarding.registry import Form

MAX_FORM_BYTES = 64 * 1024  # these forms are a few short fields
# ASCII digits only, short enough to be stored and shown as typed.
_WHOLE_NUMBER = re.compile(r"-?[0-9]{1,12}")


async def read_form(request: Request) -> Form | None:
    """The URL-encoded body as lists of values, or None when it is too large."""
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
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


def first_value(form: Form, name: str) -> str:
    """The field's first value without NUL (which can't appear in HTML); "" when absent."""
    values = form.get(name)
    return values[0].replace("\x00", "") if values else ""


def whole_number(text: str) -> int | str | None:
    """What a number field holds: the whole number, None when nothing was typed, or the text
    as typed when it isn't a whole number (the rule that refuses it lives in the domain)."""
    typed = text.strip()
    if not typed:
        return None
    return int(typed) if _WHOLE_NUMBER.fullmatch(typed) else text


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"
