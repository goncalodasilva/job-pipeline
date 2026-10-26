"""``read_upload``: one uploaded file or pasted text, read with a hard size cap (M3.1; AC-M3.7;
spec 2.9 rule 5; ADR-089 item 2).

``app.api.uploads.read_upload(request)`` streams the multipart body itself. It counts the bytes
it really receives, whatever ``Content-Length`` says, and stops reading once more than
``MAX_UPLOAD_BYTES`` (10 MB) have arrived, raising ``UploadTooLarge``. It returns an ``Upload``
(``data`` bytes and ``source`` "file" or "text") for exactly one file part named ``file`` or a
``text`` field, raises ``UploadNotAccepted`` otherwise, and never touches the disk with a name
the client chose. ``register_error_handlers`` answers the two errors with 413 and 400 and the
friendly message as plain text.

The tests call a small app as a server would (raw ASGI messages), so they can count how many
bytes the app asked for. The new names are imported inside the tests.
"""

import dataclasses
import importlib
import json
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import FormData

from app.api.errors import register_error_handlers
from tests.upload_bodies import (
    BOUNDARY,
    CONTENT_TYPE,
    FieldPart,
    FilePart,
    closing,
    file_part_head,
    multipart_body,
)

MIB = 1024 * 1024
TEN_MB = 10 * MIB
CHUNK = 64 * 1024
# How far past the cap the app may read before it stops (a few network chunks).
SLACK = MIB
PDF_BYTES = b"%PDF-1.4\nfictional bytes that only look like a PDF\n"
CV_TEXT = "Alex Example\nOperations Analyst at Example Widgets Ltd, Évora\n"


def _uploads() -> Any:
    return importlib.import_module("app.api.uploads")


def _message(name: str) -> str:
    errors = importlib.import_module("app.domain.errors")
    assert hasattr(errors, name), f"app.domain.errors.{name} doesn't exist yet"
    text: str = getattr(errors, name)().user_message
    return text


@dataclass
class Probe:
    """A one-route app around ``read_upload`` and what the route saw."""

    app: FastAPI
    uploads: list[Any] = field(default_factory=list)


def _probe(max_bytes: int | None = None) -> Probe:
    read_upload = _uploads().read_upload
    extra = {} if max_bytes is None else {"max_bytes": max_bytes}
    app = FastAPI()
    register_error_handlers(app)
    probe = Probe(app=app)

    async def receive_upload(request: Request) -> JSONResponse:
        upload = await read_upload(request, **extra)
        probe.uploads.append(upload)
        return JSONResponse({"source": upload.source, "size": len(upload.data)})

    app.add_api_route("/probe/upload", receive_upload, methods=["POST"])
    return probe


@dataclass
class Answer:
    status: int
    content_type: str
    body: bytes
    delivered: int  # request body bytes the app actually asked for

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")


async def _post(
    app: FastAPI,
    chunks: Iterable[bytes],
    *,
    content_type: str | None = CONTENT_TYPE,
    content_length: int | None = None,
) -> Answer:
    """Send a POST as a server would: one ``http.request`` message per chunk, only when the
    app asks for it."""
    headers = [(b"host", b"127.0.0.1:8000")]
    if content_type is not None:
        headers.append((b"content-type", content_type.encode("ascii")))
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("ascii")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/probe/upload",
        "raw_path": b"/probe/upload",
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
    }
    remaining: Iterator[bytes] = iter(chunks)
    state = {"delivered": 0, "ended": False}
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if state["ended"]:
            return {"type": "http.disconnect"}
        chunk = next(remaining, None)
        if chunk is None:
            state["ended"] = True
            return {"type": "http.request", "body": b"", "more_body": False}
        state["delivered"] += len(chunk)
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)

    start = next(message for message in sent if message["type"] == "http.response.start")
    response_headers = {name.lower(): value for name, value in start["headers"]}
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return Answer(
        status=start["status"],
        content_type=response_headers.get(b"content-type", b"").decode("ascii"),
        body=body,
        delivered=int(state["delivered"]),
    )


def _in_chunks(body: bytes) -> Iterator[bytes]:
    for start in range(0, len(body), CHUNK):
        yield body[start : start + CHUNK]


def _endless_file(total: int) -> Iterator[bytes]:
    """A file part of ``total`` bytes, made chunk by chunk so nothing large is held."""
    yield file_part_head(FilePart("cv.pdf", b""))
    for _ in range(total // CHUNK):
        yield b"x" * CHUNK
    yield closing()


def _assert_refused(answer: Answer, status: int, error: str, probe: Probe) -> None:
    assert answer.status == status
    assert answer.content_type.startswith("text/plain")
    assert answer.text == _message(error)
    assert probe.uploads == [], "a refused upload reached the route's own code"


# --- size cap ------------------------------------------------------------------------------


def test_upload_cap_is_10_mb() -> None:
    assert _uploads().MAX_UPLOAD_BYTES == TEN_MB


async def test_upload_over_10mb_stops_reading_and_413() -> None:
    probe = _probe()
    total = 40 * MIB

    answer = await _post(probe.app, _endless_file(total), content_length=total + 200)

    _assert_refused(answer, 413, "UploadTooLarge", probe)
    assert answer.delivered <= TEN_MB + SLACK, (
        f"the app read {answer.delivered} bytes of an oversized upload instead of stopping"
    )


@pytest.mark.parametrize(
    "declared",
    [pytest.param(1024, id="understated"), pytest.param(None, id="absent")],
)
async def test_lying_content_length_still_capped(declared: int | None) -> None:
    probe = _probe()

    answer = await _post(probe.app, _endless_file(40 * MIB), content_length=declared)

    _assert_refused(answer, 413, "UploadTooLarge", probe)
    assert answer.delivered > TEN_MB - SLACK, "refused by the header alone, before counting"
    assert answer.delivered <= TEN_MB + SLACK


async def test_oversized_text_field_is_refused_and_reading_stops() -> None:
    probe = _probe()
    head = f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="text"\r\n\r\n'.encode()

    def chunks() -> Iterator[bytes]:
        yield head
        for _ in range(30 * MIB // CHUNK):
            yield b"a" * CHUNK
        yield closing()

    answer = await _post(probe.app, chunks())

    # A pasted text this long is refused either as too large or, by a stricter limit for form
    # fields, as not accepted. Either way it is friendly and the app stops reading.
    if answer.status == 413:
        _assert_refused(answer, 413, "UploadTooLarge", probe)
    else:
        _assert_refused(answer, 400, "UploadNotAccepted", probe)
    assert answer.delivered <= TEN_MB + SLACK


async def test_file_under_the_cap_is_read_whole() -> None:
    probe = _probe()
    data = PDF_BYTES + b"y" * (9 * MIB)
    body = multipart_body(FilePart("cv.pdf", data))

    answer = await _post(probe.app, _in_chunks(body), content_length=len(body))

    assert answer.status == 200, answer.text
    assert probe.uploads[0].data == data
    assert probe.uploads[0].source == "file"


async def test_file_of_exactly_the_cap_is_accepted() -> None:
    probe = _probe()
    data = b"z" * TEN_MB
    body = multipart_body(FilePart("cv.pdf", data))

    answer = await _post(probe.app, _in_chunks(body), content_length=len(body))

    assert answer.status == 200, answer.text
    assert len(probe.uploads[0].data) == TEN_MB
    assert probe.uploads[0].data == data


async def test_file_one_byte_over_the_cap_is_413_by_its_own_size() -> None:
    probe = _probe()
    body = multipart_body(FilePart("cv.pdf", b"z" * (TEN_MB + 1)))

    answer = await _post(probe.app, _in_chunks(body), content_length=len(body))

    _assert_refused(answer, 413, "UploadTooLarge", probe)
    # The whole body was read: the file's own size was checked, not only the stream's.
    assert answer.delivered == len(body)


async def test_file_at_the_cap_is_held_in_memory_only(monkeypatch: pytest.MonkeyPatch) -> None:
    rollovers: list[object] = []
    monkeypatch.setattr(
        tempfile.SpooledTemporaryFile, "rollover", lambda spooled: rollovers.append(spooled)
    )
    probe = _probe()
    body = multipart_body(FilePart("cv.pdf", b"z" * TEN_MB))

    answer = await _post(probe.app, _in_chunks(body), content_length=len(body))

    assert answer.status == 200, answer.text
    assert rollovers == [], "the upload was moved to a temporary file on disk"


FIVE_MB = 5 * MIB


async def test_a_route_can_pass_a_lower_cap_and_the_stream_stops_there() -> None:
    probe = _probe(FIVE_MB)
    total = 40 * MIB

    answer = await _post(probe.app, _endless_file(total), content_length=total + 200)

    _assert_refused(answer, 413, "UploadTooLarge", probe)
    assert answer.delivered <= FIVE_MB + SLACK, (
        f"the app read {answer.delivered} bytes past a 5 MB route cap"
    )


async def test_a_lower_cap_accepts_a_file_of_exactly_the_cap_and_refuses_one_byte_more() -> None:
    probe = _probe(FIVE_MB)
    exact = multipart_body(FilePart("p.jpg", b"z" * FIVE_MB))
    over = multipart_body(FilePart("p.jpg", b"z" * (FIVE_MB + 1)))

    accepted = await _post(probe.app, _in_chunks(exact), content_length=len(exact))
    refused = await _post(probe.app, _in_chunks(over), content_length=len(over))

    assert accepted.status == 200, accepted.text
    assert len(probe.uploads) == 1 and len(probe.uploads[0].data) == FIVE_MB
    assert refused.status == 413 and refused.text == _message("UploadTooLarge")


# --- what is accepted ------------------------------------------------------------------------


async def test_one_file_part_is_accepted() -> None:
    probe = _probe()
    body = multipart_body(FilePart("cv.pdf", PDF_BYTES, content_type="application/pdf"))

    answer = await _post(probe.app, _in_chunks(body), content_length=len(body))

    assert answer.status == 200, answer.text
    assert json.loads(answer.text) == {"source": "file", "size": len(PDF_BYTES)}
    assert probe.uploads[0].data == PDF_BYTES


async def test_text_field_is_accepted_as_utf8_bytes() -> None:
    probe = _probe()
    body = multipart_body(FieldPart(CV_TEXT))

    answer = await _post(probe.app, _in_chunks(body), content_length=len(body))

    assert answer.status == 200, answer.text
    assert probe.uploads[0].source == "text"
    assert probe.uploads[0].data == CV_TEXT.encode("utf-8")


async def test_browser_form_with_no_file_chosen_uses_the_text() -> None:
    # A browser sends the empty file input as a part with an empty file name.
    probe = _probe()
    body = multipart_body(FilePart("", b""), FieldPart(CV_TEXT))

    answer = await _post(probe.app, _in_chunks(body), content_length=len(body))

    assert answer.status == 200, answer.text
    assert probe.uploads[0].source == "text"
    assert probe.uploads[0].data == CV_TEXT.encode("utf-8")


async def test_file_with_blank_text_field_uses_the_file() -> None:
    probe = _probe()
    body = multipart_body(FilePart("cv.pdf", PDF_BYTES), FieldPart("  \r\n "))

    answer = await _post(probe.app, _in_chunks(body), content_length=len(body))

    assert answer.status == 200, answer.text
    assert probe.uploads[0].source == "file"
    assert probe.uploads[0].data == PDF_BYTES


@pytest.mark.parametrize(
    "second_name",
    [pytest.param("file", id="same_field"), pytest.param("other", id="another_field")],
)
async def test_two_files_refused(second_name: str) -> None:
    probe = _probe()
    body = multipart_body(
        FilePart("cv.pdf", PDF_BYTES),
        FilePart("second.pdf", PDF_BYTES + b"second", name=second_name),
    )

    answer = await _post(probe.app, _in_chunks(body), content_length=len(body))

    _assert_refused(answer, 400, "UploadNotAccepted", probe)


async def test_file_and_text_together_refused() -> None:
    # Which one the user meant can't be known, so neither is taken.
    probe = _probe()
    body = multipart_body(FilePart("cv.pdf", PDF_BYTES), FieldPart(CV_TEXT))

    answer = await _post(probe.app, _in_chunks(body), content_length=len(body))

    _assert_refused(answer, 400, "UploadNotAccepted", probe)


@pytest.mark.parametrize(
    "parts",
    [
        pytest.param((), id="no_parts"),
        pytest.param((FilePart("", b""), FieldPart("   ")), id="empty_form"),
        pytest.param((FieldPart("unrelated", name="other"),), id="unknown_field_only"),
    ],
)
async def test_nothing_submitted_refused(parts: tuple[FilePart | FieldPart, ...]) -> None:
    probe = _probe()
    body = multipart_body(*parts)

    answer = await _post(probe.app, _in_chunks(body), content_length=len(body))

    _assert_refused(answer, 400, "UploadNotAccepted", probe)


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        pytest.param("multipart/form-data", multipart_body(FieldPart(CV_TEXT)), id="no_boundary"),
        pytest.param(CONTENT_TYPE, b"this is not a multipart body at all", id="garbage_body"),
        pytest.param("application/json", b'{"text": "Alex Example"}', id="json"),
        pytest.param(None, PDF_BYTES, id="no_content_type"),
    ],
)
async def test_malformed_upload_is_refused_with_400_not_500(
    content_type: str | None, body: bytes
) -> None:
    probe = _probe()

    answer = await _post(
        probe.app, _in_chunks(body), content_type=content_type, content_length=len(body)
    )

    _assert_refused(answer, 400, "UploadNotAccepted", probe)


async def test_file_in_an_unexpected_field_refused() -> None:
    probe = _probe()
    body = multipart_body(FilePart("cv.pdf", PDF_BYTES, name="attachment"))

    answer = await _post(probe.app, _in_chunks(body), content_length=len(body))

    _assert_refused(answer, 400, "UploadNotAccepted", probe)


async def test_named_but_empty_file_refused() -> None:
    probe = _probe()
    body = multipart_body(FilePart("cv.pdf", b""))

    answer = await _post(probe.app, _in_chunks(body), content_length=len(body))

    _assert_refused(answer, 400, "UploadNotAccepted", probe)


# --- the client's charset --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("charset", "value"),
    [
        pytest.param("unicode_escape", b"\\ud800", id="unicode_escape_lone_surrogate"),
        pytest.param("utf-7", b"+2AA-", id="utf_7_lone_surrogate"),
        pytest.param("utf-7", b"Alex Example", id="utf_7_plain"),
        pytest.param("latin-1", b"Alex Example", id="latin_1"),
        pytest.param('"unicode_escape"', b"\\ud800", id="quoted"),
    ],
)
async def test_charset_other_than_utf8_is_refused_with_400_not_500(
    charset: str, value: bytes
) -> None:
    probe = _probe()
    body = (
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="text"\r\n\r\n'.encode()
        + value
        + closing()
    )

    answer = await _post(
        probe.app,
        _in_chunks(body),
        content_type=f"{CONTENT_TYPE}; charset={charset}",
        content_length=len(body),
    )

    _assert_refused(answer, 400, "UploadNotAccepted", probe)


@pytest.mark.parametrize("charset", ["utf-8", "UTF-8", '"utf-8"'])
async def test_charset_utf8_is_accepted(charset: str) -> None:
    probe = _probe()
    body = multipart_body(FieldPart(CV_TEXT))

    answer = await _post(
        probe.app,
        _in_chunks(body),
        content_type=f"{CONTENT_TYPE}; charset={charset}",
        content_length=len(body),
    )

    assert answer.status == 200, answer.text
    assert probe.uploads[0].data == CV_TEXT.encode("utf-8")


async def test_text_that_cannot_be_utf8_is_not_accepted() -> None:
    # A lone surrogate can't be written as UTF-8. Whatever let it through, it is refused.
    errors = importlib.import_module("app.domain.errors")
    form = FormData([("text", "Alex \ud800 Example")])

    with pytest.raises(errors.UploadNotAccepted):
        await _uploads()._chosen(form)


# --- the client's file name ------------------------------------------------------------------


def _files_under(folder: Path) -> list[str]:
    return sorted(str(path.relative_to(folder)) for path in folder.rglob("*") if path.is_file())


@pytest.mark.parametrize(
    "client_name",
    [
        pytest.param("../../escaped.pdf", id="parent_folders"),
        pytest.param("nested/dir/cv.pdf", id="sub_folders"),
        pytest.param("..\\\\..\\\\escaped.pdf", id="windows_separators"),
        pytest.param("ABSOLUTE", id="absolute_path"),
        pytest.param("cv.pdf", id="plain_name"),
    ],
)
async def test_filename_with_path_parts_never_used(
    client_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    working = tmp_path / "working" / "deeper"
    working.mkdir(parents=True)
    monkeypatch.chdir(working)
    target = tmp_path / "absolute_target.pdf"
    if client_name == "ABSOLUTE":
        client_name = str(target)
    probe = _probe()
    body = multipart_body(FilePart(client_name, PDF_BYTES))

    answer = await _post(probe.app, _in_chunks(body), content_length=len(body))

    assert answer.status == 200, answer.text
    upload = probe.uploads[0]
    assert upload.data == PDF_BYTES
    # Nothing was written anywhere the client's name could point to.
    assert _files_under(tmp_path) == []
    assert not target.exists()
    # The result carries no path, and no text a caller could mistake for one.
    assert dataclasses.is_dataclass(upload)
    for item in dataclasses.fields(upload):
        value = getattr(upload, item.name)
        assert not isinstance(value, Path), item.name
        if isinstance(value, str):
            assert "/" not in value and "\\" not in value and ".." not in value, item.name
