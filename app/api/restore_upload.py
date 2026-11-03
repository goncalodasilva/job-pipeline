"""Stream one uploaded export file to disk for a restore (M9.1 part D; TD-033; spec 2.9 rule 5).

The body is parsed as it arrives, with the standard multipart parser, and the file part's bytes
go straight to a file in a folder the caller made: nothing is held whole in memory, so a
200 MB export costs a few chunks of it. The body is counted as it arrives, whatever
``Content-Length`` says, and reading stops once the cap is passed. Each wait for the next chunk
has its own timeout, so a client that goes silent is answered instead of waited for, and the
whole upload has a deadline too, so one that trickles bytes cannot hold the request for ever. A
disk that cannot take the file (``OSError``) is a refusal of its own; the caller logs the
exception's class only. The client's file name is never read, kept or used, and the file's
content is not looked at here.

The form has exactly one file part, named ``file``; anything else is refused. A caller that
names ``text_fields`` also accepts each of those as one short text part (``TEXT_BYTES`` at most)
before the file part, for a form that says where the file goes (setup's restore sends the data
folder). The directory may then be a coroutine function, called with the text fields as soon as
the file part's headers have arrived and before any of its bytes are written, so the caller can
open the folder the text names. Anything it raises ends the upload as it is. The caller removes
the folder in every outcome.

A refusal carries the text fields read before it (``UploadRefused.fields``), so the caller can
show them back. A part's header name and value are each held in memory only up to
``HEADER_BYTES``; past that the upload is refused, whatever the parser library allows.
"""

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, BinaryIO, Final, Literal

from fastapi import Request
from python_multipart import MultipartParser
from python_multipart.multipart import parse_options_header
from starlette.requests import ClientDisconnect

FILE_FIELD: Final = "file"
UPLOAD_NAME: Final = "upload.json"
# Room for the boundary lines and part headers around a file of exactly the cap.
ENVELOPE_BYTES: Final = 64 * 1024
TEXT_BYTES: Final = 4096  # a text part (a folder path) is at most this long
HEADER_BYTES: Final = 8 * 1024  # a part header's name, and its value, are each at most this long

Reason = Literal["form", "too_large", "stalled", "too_slow", "storage"]
# The folder to write into, from the text parts that came before the file part.
DirectoryFor = Callable[[Mapping[str, str]], Awaitable[Path]]


class UploadRefused(Exception):
    """The upload was not taken: ``form`` (not exactly one file part, or cut short),
    ``too_large`` (past the cap), ``stalled`` (no data within the read timeout), ``too_slow``
    (not finished within the overall deadline) or ``storage`` (the disk refused the file;
    ``__cause__`` holds the ``OSError`` when there is one). ``fields`` are the text parts read
    before the refusal (set by ``save_upload``)."""

    def __init__(self, reason: Reason) -> None:
        super().__init__(reason)
        self.reason: Reason = reason
        self.fields: dict[str, str] = {}


def boundary_of(content_type: str) -> bytes | None:
    """The multipart boundary named by the header, else None."""
    kind, options = parse_options_header(content_type)
    if kind.lower() != b"multipart/form-data":
        return None
    boundary = options.get(b"boundary")
    return boundary if boundary else None


class _Sink:
    """Parser callbacks: check each part's headers, count and collect the file part's bytes."""

    def __init__(self, path: Path | None, max_bytes: int, text_fields: frozenset[str]) -> None:
        self.path = path  # None until the directory is known
        self._max = max_bytes
        self._text_names = {name.encode(): name for name in text_fields}
        self._field = b""
        self._value = b""
        self._name = b""  # the current part's name
        self._text_field: str | None = None  # the text part being read
        self._text = b""
        self._file_seen = False
        self._nothing_chosen = False
        self.fields: dict[str, str] = {}
        self.awaiting_directory = False  # the file part began and the directory is not known yet
        self.written = 0
        self.part_ended = False
        self._pending: list[bytes] = []
        self._out: BinaryIO | None = None
        self.close_failed = False

    def callbacks(self) -> dict[str, Any]:
        return {
            "on_part_begin": self._part_begin,
            "on_header_field": self._header_field,
            "on_header_value": self._header_value,
            "on_header_end": self._header_end,
            "on_headers_finished": self._headers_finished,
            "on_part_data": self._part_data,
            "on_part_end": self._part_end,
        }

    def _part_begin(self) -> None:
        self._field = self._value = self._name = b""
        self._nothing_chosen = False
        self._text_field = None

    def _header_field(self, data: bytes, start: int, end: int) -> None:
        self._field += data[start:end]
        if len(self._field) > HEADER_BYTES:
            raise UploadRefused("form")

    def _header_value(self, data: bytes, start: int, end: int) -> None:
        self._value += data[start:end]
        if len(self._value) > HEADER_BYTES:
            raise UploadRefused("form")

    def _header_end(self) -> None:
        if self._field.lower() == b"content-disposition":
            _, options = parse_options_header(self._value)
            self._name = options.get(b"name") or b""
            # A browser sends an empty file field as a part with ``filename=""``: nothing chosen.
            self._nothing_chosen = options.get(b"filename") == b""
        self._field = self._value = b""

    def _headers_finished(self) -> None:
        if self._name == FILE_FIELD.encode():
            if self._file_seen or self._nothing_chosen:
                raise UploadRefused("form")
            self._file_seen = True
            if self.path is None:
                self.awaiting_directory = True
            else:
                self._open(self.path)
        elif self._name in self._text_names and not self._file_seen:
            name = self._text_names[self._name]
            if name in self.fields:
                raise UploadRefused("form")
            self._text_field, self._text = name, b""
        else:
            raise UploadRefused("form")

    def _open(self, path: Path) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self._out = os.fdopen(fd, "wb")

    def begin(self, path: Path) -> None:
        """Blocking: the directory is known; create the file the pending bytes go to."""
        self.path = path
        self.awaiting_directory = False
        self._open(path)

    def _part_data(self, data: bytes, start: int, end: int) -> None:
        if self._text_field is not None:
            self._text += data[start:end]
            if len(self._text) > TEXT_BYTES:
                raise UploadRefused("form")
            return
        self.written += end - start
        if self.written > self._max:
            raise UploadRefused("too_large")
        self._pending.append(data[start:end])

    def _part_end(self) -> None:
        if self._text_field is not None:
            self.fields[self._text_field] = self._text.decode("utf-8", errors="replace")
            self._text_field = None
        else:
            self.part_ended = True

    def write_pending(self) -> None:
        """Blocking: write what the parser has handed over so far."""
        chunks, self._pending = self._pending, []
        if self._out is not None:
            for chunk in chunks:
                self._out.write(chunk)

    def close(self) -> None:
        """Blocking: close the file. A failing close (the last buffered bytes could not be
        written) is recorded in ``close_failed``, never raised."""
        out, self._out = self._out, None
        if out is not None:
            try:
                out.close()
            except OSError:
                self.close_failed = True


async def save_upload(
    request: Request,
    directory: Path | DirectoryFor,
    *,
    max_bytes: int,
    read_timeout: float,
    total_timeout: float,
    text_fields: frozenset[str] = frozenset(),
) -> Path:
    """Write the request's one file part to ``directory/upload.json`` and return that path
    (``directory`` is a folder, or a coroutine function that returns one; see the module's
    docstring). Each wait for a chunk is at most ``read_timeout`` seconds and the whole upload
    at most ``total_timeout``. Raises ``UploadRefused``. Disk writes run in a worker thread."""
    boundary = boundary_of(request.headers.get("content-type", ""))
    if boundary is None:
        raise UploadRefused("form")
    resolve = None if isinstance(directory, Path) else directory
    sink = _Sink(
        directory / UPLOAD_NAME if isinstance(directory, Path) else None, max_bytes, text_fields
    )
    parser = MultipartParser(boundary, sink.callbacks())  # type: ignore[arg-type]
    try:
        return await _receive(
            request, sink, parser, resolve, max_bytes, read_timeout, total_timeout
        )
    except UploadRefused as refusal:
        refusal.fields = dict(sink.fields)  # the text parts read before it, to show back
        raise


async def _receive(
    request: Request,
    sink: _Sink,
    parser: MultipartParser,
    resolve: DirectoryFor | None,
    max_bytes: int,
    read_timeout: float,
    total_timeout: float,
) -> Path:
    received = 0
    stream = request.stream().__aiter__()
    clock = asyncio.get_running_loop().time
    deadline = clock() + total_timeout
    try:
        while True:
            remaining = deadline - clock()
            if remaining <= 0:
                raise UploadRefused("too_slow")
            try:
                chunk = await asyncio.wait_for(stream.__anext__(), min(read_timeout, remaining))
            except StopAsyncIteration:
                break
            except TimeoutError:
                raise UploadRefused("too_slow" if clock() >= deadline else "stalled") from None
            except ClientDisconnect:
                raise UploadRefused("form") from None
            received += len(chunk)
            if received > max_bytes + ENVELOPE_BYTES:
                raise UploadRefused("too_large")
            try:
                parser.write(chunk)
            except ValueError:  # the parser's own errors: a body that is not a valid form
                raise UploadRefused("form") from None
            except OSError as error:  # the file could not be created
                raise UploadRefused("storage") from error
            try:
                if sink.awaiting_directory and resolve is not None:
                    folder = await resolve(dict(sink.fields))
                    await asyncio.to_thread(sink.begin, folder / UPLOAD_NAME)
                await asyncio.to_thread(sink.write_pending)
            except OSError as error:  # a full disk, a folder that went away
                raise UploadRefused("storage") from error
        if not sink.part_ended or sink.written == 0:
            raise UploadRefused("form")
    finally:
        await asyncio.shield(asyncio.to_thread(sink.close))
    if sink.close_failed:
        raise UploadRefused("storage")
    if sink.path is None:  # pragma: no cover - a finished file part always has a file
        raise UploadRefused("form")
    return sink.path
