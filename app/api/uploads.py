"""Read one uploaded file or a pasted text, with a hard size cap (spec 2.9 rule 5).

The body is streamed and counted as it arrives, whatever ``Content-Length`` says, and reading
stops once the cap is passed. The parts are held in memory only, and the client's file name is
never kept or used. What the bytes are is decided later, by content, behind a service.
"""

from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from typing import Final, Literal

from fastapi import Request
from python_multipart.multipart import parse_options_header
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

from app.domain.errors import UploadNotAccepted, UploadTooLarge

MAX_UPLOAD_BYTES: Final = 10 * 1024 * 1024
FILE_FIELD: Final = "file"
TEXT_FIELD: Final = "text"
# Room for the multipart boundaries and part headers around a file of exactly the cap.
_ENVELOPE_BYTES: Final = 64 * 1024
_MAX_BODY_BYTES: Final = MAX_UPLOAD_BYTES + _ENVELOPE_BYTES
# A browser sends the file input and the text field; a few more parts are tolerated.
_MAX_FILE_PARTS: Final = 2
_MAX_FIELD_PARTS: Final = 8
# The parser decodes field values with the charset the client names; only UTF-8 is taken.
_CHARSETS: Final = (b"utf-8", b"utf8")
# The detail of the refusal when one file and a pasted text came together: a page can then
# say "choose one" (the user sees the error's message or the page's words, never a detail).
BOTH_GIVEN: Final = "a file and a text"


@dataclass(frozen=True)
class Upload:
    """What the user sent: the bytes, and whether they came as a file or as pasted text."""

    data: bytes
    source: Literal["file", "text"]


class _InMemoryParser(MultiPartParser):
    # Above the body cap, so a file part never rolls over to a temporary file on disk.
    spool_max_size = _MAX_BODY_BYTES + 1


async def read_upload(
    request: Request,
    *,
    fields: dict[str, list[str]] | None = None,
    max_bytes: int = MAX_UPLOAD_BYTES,
) -> Upload:
    """The one file part named ``file``, or the ``text`` field. Raises ``UploadTooLarge`` past
    the cap and ``UploadNotAccepted`` for anything else (two files, both, neither, malformed).

    A route whose form has other plain fields (a version) passes ``fields``: every field that is
    not ``text`` is added to it, name to values, once the upload has been accepted.

    ``max_bytes`` (at most ``MAX_UPLOAD_BYTES``) is the route's own cap on the file or text.
    The stream is counted against it plus the multipart envelope, so a smaller cap stops the
    reading sooner (the photo route passes 5 MB).

    A route using this must take only ``Request``: a ``Form()`` or ``UploadFile`` parameter
    would make FastAPI parse the body first, uncapped."""
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise UploadNotAccepted(detail="not a multipart form")
    if _charset(content_type) not in _CHARSETS:
        raise UploadNotAccepted(detail="a charset other than UTF-8")
    max_bytes = min(max_bytes, MAX_UPLOAD_BYTES)
    body_cap = max_bytes + _ENVELOPE_BYTES
    parser = _InMemoryParser(
        request.headers,
        _capped(request.stream(), body_cap),
        max_files=_MAX_FILE_PARTS,
        max_fields=_MAX_FIELD_PARTS,
        max_part_size=body_cap,
    )
    try:
        form = await parser.parse()
    except (MultiPartException, ValueError, ClientDisconnect) as error:
        raise UploadNotAccepted(detail=type(error).__name__) from None
    try:
        return await _chosen(form, fields, max_bytes)
    finally:
        await form.close()


def _charset(content_type: str) -> bytes:
    """The charset the parser would decode with, read from the header the way it reads it."""
    _, options = parse_options_header(content_type)
    return options.get(b"charset", b"utf-8").lower()


async def _capped(stream: AsyncIterator[bytes], cap: int) -> AsyncGenerator[bytes, None]:
    received = 0
    async for chunk in stream:
        received += len(chunk)
        if received > cap:
            raise UploadTooLarge(detail="the request body passed the cap")
        yield chunk


async def _chosen(
    form: FormData,
    fields: dict[str, list[str]] | None = None,
    max_bytes: int = MAX_UPLOAD_BYTES,
) -> Upload:
    files: list[tuple[str, UploadFile]] = []
    texts: list[str] = []
    others: dict[str, list[str]] = {}
    for name, value in form.multi_items():
        if isinstance(value, str):
            if name == TEXT_FIELD:
                if value.strip():
                    texts.append(value)
            else:
                others.setdefault(name, []).append(value)
        elif value.filename or value.size:  # a browser's empty file input is no file
            files.append((name, value))
    if len(files) == 1 and len(texts) == 1:
        raise UploadNotAccepted(detail=BOTH_GIVEN)
    if len(files) + len(texts) != 1:
        raise UploadNotAccepted(detail="expected one file or a text")
    if texts:
        upload = _upload(_utf8(texts[0]), "text", max_bytes)
    else:
        name, file = files[0]
        if name != FILE_FIELD:
            raise UploadNotAccepted(detail="a file in an unexpected field")
        upload = _upload(await file.read(), "file", max_bytes)
    if fields is not None:
        fields.update(others)
    return upload


def _utf8(text: str) -> bytes:
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate, which no UTF-8 body can hold
        raise UploadNotAccepted(detail="a text that is not UTF-8") from None


def _upload(data: bytes, source: Literal["file", "text"], max_bytes: int) -> Upload:
    if not data:
        raise UploadNotAccepted(detail="an empty file")
    if len(data) > max_bytes:
        raise UploadTooLarge(detail="the content passed the cap")
    return Upload(data=data, source=source)
