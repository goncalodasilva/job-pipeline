"""Shared helpers for the document-renderer tests (M6.1; spec 2.4, 4.1).

Fictional documents, two tiny valid pictures built in code (no image files, no image library) and
readers that open a rendered Word file with python-docx *in the test*. The new app names are
imported inside the functions, so a missing module fails the test that needs it.
"""

import importlib
import io
import struct
import unicodedata
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


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))


def tiny_png(width: int = 2, height: int = 2) -> bytes:
    chunk = _png_chunk
    rows = b"".join(b"\x00" + b"\x80\x40\xc0" * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


# A real 2x2 baseline JPEG (a decoder, unlike python-docx, rejects a header with no image data;
# M6.2). Made once with Pillow from a flat fictional colour; no image library is used by the tests.
_TINY_JPEG_HEX = (
    "ffd8ffe000104a46494600010100000100010000ffdb004300100b0c0e0c0a100e0d0e1211101318281a181616183123"
    "251d283a333d3c3933383740485c4e404457453738506d51575f626768673e4d71797064785c656763ffdb0043011112"
    "121815182f1a1a2f63423842636363636363636363636363636363636363636363636363636363636363636363636363"
    "6363636363636363636363636363ffc00011080002000203012200021101031101ffc4001f0000010501010101010100"
    "000000000000000102030405060708090a0bffc400b5100002010303020403050504040000017d010203000411051221"
    "31410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a"
    "434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a9293949596979899"
    "9aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1"
    "f2f3f4f5f6f7f8f9faffc4001f0100030101010101010101010000000000000102030405060708090a0bffc400b51100"
    "020102040403040705040400010277000102031104052131061241510761711322328108144291a1b1c109233352f015"
    "6272d10a162434e125f11718191a262728292a35363738393a434445464748494a535455565758595a63646566676869"
    "6a737475767778797a82838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4"
    "c5c6c7c8c9cad2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8f9faffda000c03010002110311003f00a1"
    "451457b2751fffd9"
)


def tiny_jpeg() -> bytes:
    return bytes.fromhex(_TINY_JPEG_HEX)


PHOTOS = {"png": tiny_png(), "jpeg": tiny_jpeg()}


def png_header_only(width: int, height: int) -> bytes:
    """A few dozen bytes whose IHDR states ``width`` x ``height`` (a tiny IDAT and an IEND follow,
    so the file looks complete). Not a decodable picture of that size: only the header is real."""
    return (
        tiny_png()[:8]
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + (_png_chunk(b"IDAT", zlib.compress(b"\x00\x80\x40\xc0")) + _png_chunk(b"IEND", b""))
    )


def jpeg_header_only(width: int, height: int) -> bytes:
    """Start of image, a JFIF segment, and a baseline start-of-frame stating ``width`` x
    ``height``, then the end of image. Not decodable; only the header is real."""
    jfif = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    frame = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + jfif + frame + b"\xff\xd9"


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


# ---- PDF: building the renderer and reading a rendered file back with pypdf (M6.2) -----------
#
# Normalisation (the comparison documented once, here): pypdf extracts text per line as it was
# typeset, so line breaks, spacing and zero-width spaces (U+200B, inserted so a very long word
# can wrap) are layout, not content. ``pdf_normalise`` applies NFKC, drops bidi override and
# isolate controls and zero-width characters, and collapses every run of whitespace to one
# space. ``pdf_squash`` also removes the spaces, for text the renderer may wrap in the middle of
# a word. The PDF renderer strips U+202A-U+202E and U+2066-U+2069 before typesetting (a PDF has
# no safe way to keep a reordering override; Word keeps them), so the expected text drops them
# too. Everything else must survive, in order.

PAGE_POINTS_PER_MM = 72.0 / 25.4
BIDI_CONTROLS = "".join(chr(code) for code in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))
ZERO_WIDTH = "​⁠﻿"


def pdf_module() -> Any:
    return importlib.import_module("app.render.providers.pdf")


def pdf_renderer(clock: Any, **limits: Any) -> Any:
    """``PdfRenderer(clock, **limits)``: the constructor the M6.2 tests assume."""
    return pdf_module().PdfRenderer(clock, **limits)


def pdf_normalise(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text)
    kept = "".join(c for c in folded if c not in BIDI_CONTROLS and c not in ZERO_WIDTH)
    return " ".join(kept.split())


def pdf_squash(text: str) -> str:
    return "".join(pdf_normalise(text).split())


def _pdf_reader(content: bytes) -> Any:
    return importlib.import_module("pypdf").PdfReader(io.BytesIO(content))


def pdf_text(content: bytes) -> str:
    """The text of every page, raw as pypdf gives it, joined with newlines."""
    return "\n".join(page.extract_text() for page in _pdf_reader(content).pages)


def pdf_page_lines(content: bytes) -> list[list[str]]:
    """Per page, the non-empty text lines in reading order."""
    pages = []
    for page in _pdf_reader(content).pages:
        pages.append([line.strip() for line in page.extract_text().splitlines() if line.strip()])
    return pages


def pdf_page_count(content: bytes) -> int:
    return len(_pdf_reader(content).pages)


def pdf_page_size_mm(content: bytes) -> tuple[float, float]:
    box = _pdf_reader(content).pages[0].mediabox
    return (float(box.width) / PAGE_POINTS_PER_MM, float(box.height) / PAGE_POINTS_PER_MM)


def pdf_image_count(content: bytes) -> int:
    return sum(len(page.images) for page in _pdf_reader(content).pages)


def pdf_title_author(content: bytes) -> tuple[str, str]:
    metadata = _pdf_reader(content).metadata
    assert metadata is not None, "the PDF has no document information"
    return metadata.title, metadata.author


def pdf_dates(content: bytes) -> tuple[datetime, datetime]:
    metadata = _pdf_reader(content).metadata
    assert metadata is not None, "the PDF has no document information"
    return as_utc(metadata.creation_date), as_utc(metadata.modification_date)


def pdf_info(content: bytes) -> dict[str, str]:
    """The document information dictionary as plain strings (``/Producer`` and so on)."""
    metadata = _pdf_reader(content).metadata
    return {} if metadata is None else {str(key): str(value) for key, value in metadata.items()}


def pdf_metadata_text(content: bytes) -> str:
    """The information dictionary and the XMP packet (if any) as text, for tool-name searches."""
    reader = _pdf_reader(content)
    parts = [f"{key} {value}" for key, value in pdf_info(content).items()]
    root = reader.trailer["/Root"]
    if "/Metadata" in root:
        parts.append(root["/Metadata"].get_object().get_data().decode("utf-8", "replace"))
    return "\n".join(parts)


def pdf_xref_is_valid(content: bytes) -> bool:
    """True when the cross-reference table is consistent: ``startxref`` points at ``xref`` and every
    in-use entry's offset points at ``<number> 0 obj``. Library-neutral; for the classic table."""
    marker = content.rfind(b"startxref")
    start = int(content[marker + len(b"startxref") :].split()[0])
    if not content[start:].startswith(b"xref"):
        return False
    lines = content[start:].split(b"\n")
    first, count = (int(part) for part in lines[1].split())
    body = content[start + len(b"xref\n") + len(lines[1]) + 1 :]
    for index in range(count):
        entry = body[index * 20 : index * 20 + 20]
        offset, _, state = entry[:10], entry[11:16], entry[17:18]
        if state == b"n" and not content[int(offset) :].startswith(b"%d 0 obj" % (first + index)):
            return False
    return True
