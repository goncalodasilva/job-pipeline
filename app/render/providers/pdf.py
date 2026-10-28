"""PDF documents (ADR-107, ADR-108). The only render module that imports the PDF library.

The library is loaded on the first render, so the app starts and Word export works without it.
Each render is isolated from the machine:

* the typesetting source is the static ``TEMPLATE_SOURCE``; the document's words reach it only as
  JSON in ``sys_inputs``, so no document text is ever code;
* the compiler's root is a fresh, empty temporary folder, removed afterwards; the only thing that
  can ever be in it is the in-memory photo, written under a fixed name;
* fonts come from the library's own bundled set (``FONT_SOURCE``), never from the machine, so the
  output does not depend on the OS;
* the date is the injected clock's, and the tool name is scrubbed from the metadata, so the same
  document and clock give the same bytes.

Caps on text and photo are checked before the library loads: pieces of text, characters (title
and author included), the photo's bytes, and the size its header states (read without an image
library, so a small file that claims a huge picture is refused).

The compile runs on a worker thread that the caller waits for up to the time limit. A compile
cannot be cancelled, so a render that times out leaves its thread running until the library ends
it by itself (it can read nothing except the photo). To keep stuck compiles from piling up, at
most ``MAX_IN_FLIGHT`` compiles run at once, and a timed-out one still counts until its thread
finishes: the permit is released by the worker thread, not by the caller. A new render that finds
no free permit answers ``RenderTimedOut`` at once. The trade-off is that a stuck compile holds its
permit for as long as it runs; a subprocess would be killable and free it on timeout, at the cost
of starting a process per render.
"""

import json
import re
import shutil
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import Future
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Final

from app.domain.clock import Clock
from app.domain.errors import RenderFailed, RenderTimedOut, RenderTooLarge
from app.domain.rendering import (
    PHOTO_MAX_PIXELS,
    PHOTO_MAX_SIDE,
    PhotoType,
    RenderDocument,
    RenderEntry,
    RenderSection,
    clean_render_text,
    photo_dimensions,
)
from app.render import RenderedFile, RendererUnavailable

LIBRARY_NAME: Final = "typst"  # the one place the library is named for users
UNAVAILABLE_MESSAGE: Final = (
    f"PDF export needs {LIBRARY_NAME}. Word export still works; see the README."
)
MEDIA_TYPE: Final = "application/pdf"

MAX_UNITS: Final = 2_000
MAX_CHARACTERS: Final = 200_000
MAX_PHOTO_BYTES: Final = 5 * 1024 * 1024
MAX_IN_FLIGHT: Final = 4
# Measured in the spike: a one-page CV renders in about 10 ms. The limit is far above that, so it
# only ever stops a document that is stuck or enormous.
TIME_LIMIT_SECONDS: Final = 10.0

# Fonts: the library's bundled Libertinus Serif (SIL OFL) covers Latin extended and the euro
# sign, so nothing is vendored and no system font is used.
FONT_SOURCE: Final = "wheel"
FONT_FAMILY: Final = "Libertinus Serif"

PAPER: Final = {"A4": "a4", "Letter": "us-letter"}
PHOTO_FILE: Final = {"jpeg": "photo.jpg", "png": "photo.png"}
PHOTO_ALT_TEXT: Final = "Photo"
LONG_WORD: Final = re.compile(r"\S{51,}")
WORD_CHUNK: Final = 25
ZERO_WIDTH_SPACE: Final = "​"
# Right-to-left and left-to-right embeddings and overrides, and the isolates.
BIDI_CONTROLS: Final = dict.fromkeys([*range(0x202A, 0x202F), *range(0x2066, 0x206A)])

# One static source for every document. No file, package or network access is written here, and
# the photo is opened by the fixed name the renderer chose, not by any text of the document.
TEMPLATE_SOURCE: Final = """\
#let doc = json(bytes(sys.inputs.doc))
#set document(
  title: doc.title,
  author: if doc.author.len() == 0 { () } else { doc.author },
)
#set page(paper: doc.paper, margin: 20mm)
#set text(
  font: "Libertinus Serif",
  size: 10.5pt,
  lang: "en",
  hyphenate: false,
  features: (liga: 0, clig: 0, dlig: 0),
)
#set par(leading: 0.55em, spacing: 0.6em, justify: false, linebreaks: "simple")
#show heading: it => block(
  sticky: true,
  above: if it.level == 1 { 12pt } else { 7pt },
  below: if it.level == 1 { 4pt } else { 2pt },
  text(size: if it.level == 1 { 13pt } else { 11pt }, weight: "bold", it.body),
)

#let paragraphs(lines) = for line in lines [#par(line)]
#let head = stack(spacing: 0.4em, ..doc.header.enumerate().map(
  pair => if pair.at(0) == 0 {
    text(size: 15pt, weight: "bold", pair.at(1))
  } else {
    pair.at(1)
  }
))
#if doc.photo == none {
  head
} else {
  grid(
    columns: (1fr, 30mm),
    column-gutter: 8mm,
    head,
    image(doc.photo, width: 30mm, alt: "Photo"),
  )
}

#for section in doc.sections {
  heading(level: 1, section.heading)
  paragraphs(section.lines)
  for entry in section.entries {
    heading(level: 2, entry.heading)
    paragraphs(entry.lines)
  }
}
"""

# Compiles running at once, timed-out ones included (see the module docstring).
_IN_FLIGHT: Final = threading.BoundedSemaphore(MAX_IN_FLIGHT)

_PAGES_NODE: Final = re.compile(rb"<<[^<>]*?/Type\s*/Pages\b[^<>]*>>")
_COUNT: Final = re.compile(rb"/Count\s+(\d+)")
# What the library writes about itself: /Creator and /Producer in the information dictionary and
# the CreatorTool / Producer elements of the XMP packet. Only those places are touched; the title
# and author are user text and are never searched for tool names.
_XMP_TOOL_ELEMENT: Final = re.compile(rb"<(xmp:CreatorTool|pdf:Producer)>[^<]*</\1>")
_TOOL_KEYS: Final = (b"Creator", b"Producer")
_PDF_SPACE: Final = b"\x00\t\n\x0c\r "
_PDF_DELIMITERS: Final = b"()<>[]{}/%"


def _load_typst() -> ModuleType:
    import typst

    return typst


class PdfRenderer:
    format: Final = "pdf"
    media_type: Final = MEDIA_TYPE

    def __init__(
        self,
        clock: Clock,
        *,
        max_units: int = MAX_UNITS,
        max_characters: int = MAX_CHARACTERS,
        max_photo_bytes: int = MAX_PHOTO_BYTES,
        max_photo_side: int = PHOTO_MAX_SIDE,
        max_photo_pixels: int = PHOTO_MAX_PIXELS,
        time_limit_seconds: float = TIME_LIMIT_SECONDS,
    ) -> None:
        self._clock = clock
        self._max_units = max_units
        self._max_characters = max_characters
        self._max_photo_bytes = max_photo_bytes
        self._max_photo_side = max_photo_side
        self._max_photo_pixels = max_photo_pixels
        self._time_limit = time_limit_seconds

    def render(self, document: RenderDocument) -> RenderedFile:
        self._check_caps(document)
        try:
            library = _load_typst()
        except (ImportError, OSError):
            raise RendererUnavailable(UNAVAILABLE_MESSAGE) from None
        now = self._clock.now().astimezone(UTC).replace(microsecond=0)
        data = _template_data(document)
        root = Path(tempfile.mkdtemp(prefix="jobpipeline-render-"))
        try:
            if document.photo is not None and document.photo_type is not None:
                (root / PHOTO_FILE[document.photo_type]).write_bytes(document.photo)
            content = self._compile(library, root, data, now)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        content = _scrub_tool_name(content)
        return RenderedFile(content=content, page_count=_page_count(content))

    def _check_caps(self, document: RenderDocument) -> None:
        pieces = _pieces(document)
        characters = (
            sum(len(piece) for piece in pieces) + len(document.title) + len(document.author)
        )
        if len(pieces) > self._max_units or characters > self._max_characters:
            raise RenderTooLarge
        if document.photo is not None and document.photo_type is not None:
            self._check_photo(document.photo, document.photo_type)

    def _check_photo(self, photo: bytes, photo_type: PhotoType) -> None:
        if len(photo) > self._max_photo_bytes:
            raise RenderTooLarge
        size = photo_dimensions(photo, photo_type)
        if size is None:
            raise RenderFailed(detail="the photo does not match its stated type")
        width, height = size
        if max(width, height) > self._max_photo_side or width * height > self._max_photo_pixels:
            raise RenderTooLarge

    def _compile(
        self, library: ModuleType, root: Path, data: dict[str, object], now: datetime
    ) -> bytes:
        def work() -> bytes:
            try:
                compiler = library.Compiler(root=root, font_paths=[], ignore_system_fonts=True)
                compiled = compiler.compile(
                    input=TEMPLATE_SOURCE.encode("utf-8"),
                    format="pdf",
                    sys_inputs={"doc": json.dumps(data, ensure_ascii=False)},
                    timestamp=now,
                )
            except Exception as error:  # the library's own error types stay in this file
                raise RenderFailed(detail=f"the compiler failed ({type(error).__name__})") from None
            if not isinstance(compiled, bytes):
                raise RenderFailed(detail="the compiler returned something other than one PDF")
            return compiled

        if not _IN_FLIGHT.acquire(blocking=False):
            raise RenderTimedOut(detail="too many compiles are still running")
        return _run_within(work, self._time_limit, _IN_FLIGHT)


def _run_within(
    work: Callable[[], bytes], seconds: float, slot: threading.BoundedSemaphore
) -> bytes:
    """Run ``work`` on a worker thread and wait up to ``seconds`` for it. The caller has acquired
    ``slot``; the worker releases it when ``work`` ends, even if the caller stopped waiting."""
    outcome: Future[bytes] = Future()

    def target() -> None:
        try:
            outcome.set_result(work())
        except BaseException as error:  # handed to the caller, which re-raises it
            outcome.set_exception(error)
        finally:
            slot.release()

    thread = threading.Thread(target=target, name="pdf-render", daemon=True)
    try:
        thread.start()
    except BaseException:
        slot.release()
        raise
    thread.join(seconds)
    if thread.is_alive():
        raise RenderTimedOut
    return outcome.result()


def _pieces(document: RenderDocument) -> list[str]:
    found = list(document.header_lines)
    for section in document.sections:
        found.append(section.heading)
        found.extend(section.lines)
        for entry in section.entries:
            found.append(entry.heading)
            found.extend(entry.lines)
    return found


def _typeset_text(text: str) -> str:
    """Text as the page should hold it: no control characters, no bidi overrides, and long words
    given places to wrap."""
    cleaned = clean_render_text(text).translate(BIDI_CONTROLS)
    return LONG_WORD.sub(_wrappable, cleaned)


def _wrappable(match: re.Match[str]) -> str:
    word = match.group()
    return ZERO_WIDTH_SPACE.join(word[i : i + WORD_CHUNK] for i in range(0, len(word), WORD_CHUNK))


def _heading(text: str) -> str:
    return " ".join(_typeset_text(text).split())


def _lines(texts: tuple[str, ...]) -> list[str]:
    """Each piece is a paragraph; a line break inside it starts another."""
    found: list[str] = []
    for text in texts:
        found.extend(line for line in _typeset_text(text).split("\n") if line.strip())
    return found


def _section_data(section: RenderSection) -> dict[str, object]:
    return {
        "heading": _heading(section.heading),
        "lines": _lines(section.lines),
        "entries": [_entry_data(entry) for entry in section.entries],
    }


def _entry_data(entry: RenderEntry) -> dict[str, object]:
    return {"heading": _heading(entry.heading), "lines": _lines(entry.lines)}


def _template_data(document: RenderDocument) -> dict[str, object]:
    photo = None if document.photo_type is None else PHOTO_FILE[document.photo_type]
    return {
        "title": " ".join(clean_render_text(document.title).translate(BIDI_CONTROLS).split()),
        "author": " ".join(clean_render_text(document.author).translate(BIDI_CONTROLS).split()),
        "paper": PAPER[document.page_size],
        "header": _lines(document.header_lines),
        "sections": [_section_data(section) for section in document.sections],
        "photo": photo,
    }


def _scrub_tool_name(content: bytes) -> bytes:
    """Blank the library's name and version in the metadata. Only the information dictionary's own
    /Creator and /Producer entries and the XMP packet's tool elements are found, by walking the
    file structure from the trailer, so the title and author (user text, which may say anything)
    are never touched. Each span becomes spaces of the same length, so every offset stays valid."""
    spans = [*_info_tool_spans(content), *_xmp_tool_spans(content)]
    if not spans:
        return content
    scrubbed = bytearray(content)
    for start, end in spans:
        scrubbed[start:end] = b" " * (end - start)
    return bytes(scrubbed)


def _object_start(content: bytes, number: int) -> int | None:
    """Where the body of indirect object ``number`` begins (after ``obj``), or None."""
    found = re.search(rb"(?:^|[\r\n])%d\s+0\s+obj\b" % number, content)
    return None if found is None else found.end()


def _reference(content: bytes, key: bytes, start: int, end: int) -> int | None:
    """The object number that ``/key N 0 R`` points to, searched only in content[start:end]."""
    found = re.search(rb"/" + key + rb"\s+(\d+)\s+\d+\s+R", content[start:end])
    return None if found is None else int(found.group(1))


def _trailer_start(content: bytes) -> int:
    return max(content.rfind(b"trailer"), 0)


def _object_end(content: bytes, start: int) -> int:
    found = content.find(b"endobj", start)
    return len(content) if found < 0 else found


def _skip_string(data: bytes, start: int) -> int:
    """The end of the literal string ``( ... )`` that starts at ``start`` (balanced brackets and
    backslash escapes), or of the hexadecimal string ``< ... >``."""
    if data[start : start + 1] == b"<":
        found = data.find(b">", start)
        return len(data) if found < 0 else found + 1
    depth = 0
    position = start
    while position < len(data):
        byte = data[position]
        if byte == 0x5C:  # a backslash escapes the next byte
            position += 2
            continue
        depth += byte == 0x28
        depth -= byte == 0x29
        position += 1
        if depth == 0:
            return position
    return len(data)


def _info_tool_spans(content: bytes) -> list[tuple[int, int]]:
    """Spans of the /Creator and /Producer entries (key and string) at the top level of the
    information dictionary named by the trailer."""
    trailer = _trailer_start(content)
    info = _reference(content, b"Info", trailer, len(content))
    body = None if info is None else _object_start(content, info)
    if body is None:
        return []
    start = content.find(b"<<", body)
    if start < 0:
        return []
    return _top_level_tool_entries(content, start, len(content))


def _line_end(data: bytes, start: int, end: int) -> int:
    ends = [i for i in (data.find(b"\n", start, end), data.find(b"\r", start, end)) if i >= 0]
    return min(ends, default=end)


def _top_level_tool_entries(data: bytes, start: int, end: int) -> list[tuple[int, int]]:
    """Walk the dictionary that starts at ``start``, skipping strings, nested dictionaries and
    arrays, and return the spans of its own /Creator and /Producer entries with a string value."""
    spans: list[tuple[int, int]] = []
    depth = 0
    position = start
    while position < end:
        two = data[position : position + 2]
        byte = data[position : position + 1]
        if two == b"<<":
            depth += 1
            position += 2
        elif two == b">>":
            depth -= 1
            position += 2
            if depth == 0:
                break
        elif byte in (b"(", b"<"):
            position = _skip_string(data, position)
        elif byte in (b"[", b"]"):
            depth += 1 if byte == b"[" else -1
            position += 1
        elif byte == b"%":
            position = _line_end(data, position, end)
        elif byte == b"/":
            name_end = position + 1
            while name_end < end and data[name_end] not in _PDF_SPACE + _PDF_DELIMITERS:
                name_end += 1
            value = name_end
            while value < end and data[value] in _PDF_SPACE:
                value += 1
            is_tool = data[position + 1 : name_end] in _TOOL_KEYS
            is_string = data[value : value + 1] == b"(" or (
                data[value : value + 1] == b"<" and data[value : value + 2] != b"<<"
            )
            if depth == 1 and is_tool and is_string:
                string_end = _skip_string(data, value)
                spans.append((position, string_end))
                position = string_end
            else:
                position = name_end
        else:
            position += 1
    return spans


def _xmp_tool_spans(content: bytes) -> list[tuple[int, int]]:
    """Spans of the tool elements inside the metadata stream that the catalog points to. The
    stream is cut out by its declared /Length, and the user's text in it is XML-escaped, so it
    cannot contain these elements."""
    trailer = _trailer_start(content)
    root = _reference(content, b"Root", trailer, len(content))
    catalog = None if root is None else _object_start(content, root)
    if catalog is None:
        return []
    metadata = _reference(content, b"Metadata", catalog, _object_end(content, catalog))
    body = None if metadata is None else _object_start(content, metadata)
    if body is None:
        return []
    keyword = content.find(b"stream", body)
    length = re.search(rb"/Length\s+(\d+)", content[body : max(keyword, body)])
    if keyword < 0 or length is None:
        return []
    begin = keyword + len(b"stream")
    begin += 2 if content[begin : begin + 2] == b"\r\n" else 1
    end = min(begin + int(length.group(1)), len(content))
    return [
        (begin + found.start(), begin + found.end())
        for found in _XMP_TOOL_ELEMENT.finditer(content[begin:end])
    ]


def _page_count(content: bytes) -> int:
    counts = [
        int(count.group(1))
        for node in _PAGES_NODE.finditer(content)
        for count in _COUNT.finditer(node.group())
    ]
    if not counts:
        raise RenderFailed(detail="the PDF has no page count")
    return max(counts)
