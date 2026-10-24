"""Shared helpers for the document-renderer tests (M6.1; spec 2.4, 4.1).

Fictional documents, two tiny valid pictures built in code (no image files, no image library) and
readers that open a rendered Word file with python-docx *in the test*. The new app names are
imported inside the functions, so a missing module fails the test that needs it.
"""

import importlib
import io
import struct
import zipfile
import zlib
from datetime import UTC, datetime
from typing import Any

# Even seconds: zip entries store the time in two-second steps.
NOW = datetime(2026, 10, 24, 10, 30, 40, tzinfo=UTC)

A4_MM = (210.0, 297.0)
LETTER_MM = (215.9, 279.4)
EMU_PER_MM = 36000


def rendering() -> Any:
    return importlib.import_module("app.domain.rendering")


def word_renderer(clock: Any) -> Any:
    """``WordRenderer(clock)``: the constructor the M6.1 tests assume (see the contract test)."""
    module = importlib.import_module("app.render.providers.word")
    return module.WordRenderer(clock)


def tiny_png(width: int = 2, height: int = 2) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\x00" + b"\x80\x40\xc0" * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def tiny_jpeg(width: int = 2, height: int = 2) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    frame = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + frame + b"\xff\xd9"


PHOTOS = {"png": tiny_png(), "jpeg": tiny_jpeg()}


def sample_document(**changes: Any) -> Any:
    """A fictional two-section CV for Alex Example, with accents and a euro sign."""
    module = rendering()
    values: dict[str, Any] = {
        "title": "Alex Example - CV",
        "author": "Alex Example",
        "page_size": "A4",
        "header_lines": ("Alex Example", "alex@example.invalid | Lisboa, Portugal"),
        "sections": (
            module.RenderSection(
                heading="Experience",
                entries=(
                    module.RenderEntry(
                        heading="Platform engineer, Example Rail",
                        lines=(
                            "2021 - 2026",
                            "Cut the monthly infrastructure bill by 12 000 €.",
                            "Rebuilt the déploiement pipeline for 40 teams.",
                        ),
                    ),
                    module.RenderEntry(
                        heading="Developer, Fictional Labs",
                        lines=("2018 - 2021", "Wrote the São João booking service."),
                    ),
                ),
            ),
            module.RenderSection(
                heading="Skills",
                lines=("Python, SQL, Ångström-level patience",),
            ),
        ),
    }
    values.update(changes)
    return module.RenderDocument(**values)


# ---- Reading a Word file back, the way a user's Word would ----------------------------------


def _docx_module() -> Any:
    return importlib.import_module("docx")


def docx_text(content: bytes) -> str:
    """Non-empty paragraphs and table cells in document order, joined with newlines."""
    document = _docx_module().Document(io.BytesIO(content))
    return "\n".join(line for line in _lines(document) if line)


def _lines(container: Any) -> list[str]:
    paragraph_type = importlib.import_module("docx.text.paragraph").Paragraph
    found: list[str] = []
    for block in container.iter_inner_content():
        if isinstance(block, paragraph_type):
            found.append(block.text)
        else:
            for row in block.rows:
                seen: list[object] = []
                for cell in row.cells:
                    if any(cell._tc is element for element in seen):
                        continue
                    seen.append(cell._tc)
                    found.extend(_lines(cell))
    return found


def docx_paragraphs(content: bytes) -> list[Any]:
    return list(_docx_module().Document(io.BytesIO(content)).paragraphs)


def docx_page_size_mm(content: bytes) -> tuple[float, float]:
    sections = _docx_module().Document(io.BytesIO(content)).sections
    assert len(sections) == 1
    return (sections[0].page_width / EMU_PER_MM, sections[0].page_height / EMU_PER_MM)


def docx_media(content: bytes) -> list[bytes]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = sorted(name for name in archive.namelist() if name.startswith("word/media/"))
        return [archive.read(name) for name in names]


def docx_package_parts(content: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return archive.namelist()


def docx_picture_descriptions(content: bytes) -> list[str]:
    """The alt text (``descr``) of every inline picture."""
    document = _docx_module().Document(io.BytesIO(content))
    drawing = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}docPr"
    return [element.get("descr") for element in document.element.body.iter(drawing)]


def docx_core_properties(content: bytes) -> Any:
    return _docx_module().Document(io.BytesIO(content)).core_properties


def docx_metadata_xml(content: bytes) -> str:
    """Every ``docProps`` part as text, for tool-name searches."""
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = [name for name in archive.namelist() if name.startswith("docProps/")]
        return "\n".join(archive.read(name).decode("utf-8", "replace") for name in names)


def as_utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
