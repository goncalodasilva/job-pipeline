"""Write the fictional CV fixtures used by the extraction tests (M3.1, ADR-089 item 2).

Usage:
    uv run python scripts/make_cv_fixtures.py [--out DIR]

Writes every file of ``tests/fixtures/cv/`` and its ``manifest.json``. The files are built by
hand with the standard library only (PDF objects as bytes, Word files with ``zipfile``), so the
output never depends on a rendering library, the clock or the person running the script, and no
file carries an author, a tool name or a date. Every name, employer and number is invented.

The PDF text streams are left uncompressed and each manifest marker is written as one string,
so a test can find the markers in the raw bytes without a PDF library.

Only ``oversized_entry.docx`` uses DEFLATE (64 MiB of zero bytes would not fit otherwise). Its
compressed bytes may differ between zlib builds; its member names, sizes and CRCs don't.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "tests" / "fixtures" / "cv"
MANIFEST_NAME = "manifest.json"
GENERATOR = "scripts/make_cv_fixtures.py"

OVERSIZED_ENTRY_BYTES = 64 * 1024 * 1024
ZIP_DATE = (1980, 1, 1, 0, 0, 0)

# A positioned line of text: (x, y, font size, text). ASCII only.
Line = tuple[int, int, int, str]

NAME = "Alex Example"
EMAIL = "alex@example.com"

# single_column.pdf: two pages, one column.
SINGLE_PAGE_1 = [
    NAME,
    "Operations Analyst - Lisbon, Portugal",
    EMAIL,
    "Experience",
    "Example Widgets Ltd - Operations Analyst (2021 - 2024)",
    "Cut invoice processing time by 35% across 4 warehouses.",
    "Built weekly stock reports used by 12 store managers.",
    "Sample Logistics SA - Junior Planner (2018 - 2021)",
    "Planned 60 delivery routes a week with a team of 3.",
]
SINGLE_PAGE_2 = [
    "Education",
    "BSc in Industrial Management, Example University, 2018",
    "Skills",
    "SQL, Excel, Python, Portuguese, English",
]

# two_column.pdf: a narrow left column and a wide right column whose rows share baselines.
TWO_COLUMN_LEFT = [
    NAME,
    "Contact",
    EMAIL,
    "Skills",
    "SQL and Python",
    "Stock forecasting",
    "Languages",
    "Portuguese (native)",
    "English (fluent)",
]
TWO_COLUMN_RIGHT = [
    "Profile",
    "Operations analyst with six years in retail logistics.",
    "Experience",
    "Example Widgets Ltd, Operations Analyst, 2021 - 2024",
    "Cut invoice processing time by 35% across 4 warehouses.",
    "Sample Logistics SA, Junior Planner, 2018 - 2021",
    "Planned 60 delivery routes a week with a team of 3.",
    "Education",
    "BSc in Industrial Management, Example University, 2018",
]

# table.pdf: a heading, a ruled table (one string per cell) and a closing line.
TABLE_HEADING = "Alex Example - Work history"
TABLE_ROWS = [
    ["Role", "Employer", "Years"],
    ["Operations Analyst", "Example Widgets Ltd", "2021 - 2024"],
    ["Junior Planner", "Sample Logistics SA", "2018 - 2021"],
    ["Stock Intern", "Fictional Foods Lda", "2017 - 2018"],
]
TABLE_CLOSING = "References available on request."

# with_tables.docx: paragraphs, then a table whose cell texts appear nowhere else.
DOCX_PARAGRAPHS = [
    NAME,
    "Operations Analyst - Lisbon, Portugal",
    "Profile",
    "Operations analyst with six years in retail logistics.",
]
DOCX_TABLE_ROWS = [
    ["Employer", "Role", "Result"],
    ["Example Widgets Ltd", "Operations Analyst", "Invoice time down 35% in 4 warehouses"],
    ["Sample Logistics SA", "Junior Planner", "60 delivery routes planned each week"],
]
DOCX_CLOSING = "Skills: SQL, Excel, Python"

# cv.txt: UTF-8 with accented letters.
TEXT_LINES = [
    NAME,
    "Operations Analyst - Évora, Portugal",
    EMAIL,
    "",
    "Experience",
    "Example Widgets Ltd - Operations Analyst (2021 - 2024)",
    "- Cut invoice processing time by 35% across 4 warehouses.",
    "Sample Logistics SA - Junior Planner (2018 - 2021)",
    "- Planned 60 delivery routes a week with a team of 3.",
    "",
    "Education",
    "BSc in Industrial Management, Example University, 2018",
    "",
    "Languages: Português (native), English (fluent)",
]


# --- PDF -----------------------------------------------------------------------------------

FONT = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
CATALOG = b"<< /Type /Catalog /Pages 2 0 R >>"


def _pdf_string(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return b"(" + escaped.encode("ascii") + b")"


def _text_ops(lines: list[Line]) -> bytes:
    ops = []
    for x, y, size, text in lines:
        ops.append(b"BT /F1 %d Tf 1 0 0 1 %d %d Tm %s Tj ET" % (size, x, y, _pdf_string(text)))
    return b"\n".join(ops) + b"\n"


def _stream(data: bytes, extra: bytes = b"") -> bytes:
    return b"<< /Length %d%s >>\nstream\n" % (len(data), extra) + data + b"\nendstream"


def _assemble(objects: list[bytes], trailer_extra: bytes = b"") -> bytes:
    """A PDF file from numbered objects (object 1 is the catalog). No /Info dictionary."""
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref_at = out.tell()
    out.write(b"xref\n0 %d\n" % (len(objects) + 1))
    out.write(b"0000000000 65535 f \n")
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R%s >>\n" % (len(objects) + 1, trailer_extra))
    out.write(b"startxref\n%d\n%%%%EOF\n" % xref_at)
    return out.getvalue()


def _text_pdf_objects(pages: list[bytes]) -> list[bytes]:
    """Objects for a text PDF: 1 catalog, 2 page tree, 3 font, then a page and its content
    stream for every entry of ``pages``."""
    kids = b" ".join(b"%d 0 R" % (4 + 2 * index) for index in range(len(pages)))
    objects = [
        CATALOG,
        b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(pages)),
        FONT,
    ]
    for index, content in enumerate(pages):
        content_number = 5 + 2 * index
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % content_number
        )
        objects.append(_stream(content))
    return objects


def _column(texts: list[str], x: int, top: int, step: int = 22) -> list[Line]:
    return [(x, top - step * row, 11, text) for row, text in enumerate(texts)]


def single_column_pdf() -> bytes:
    pages = [_text_ops(_column(SINGLE_PAGE_1, 72, 770)), _text_ops(_column(SINGLE_PAGE_2, 72, 770))]
    return _assemble(_text_pdf_objects(pages))


def two_column_pdf() -> bytes:
    # The whole left column is written first, then the right one, as layout tools do.
    lines = _column(TWO_COLUMN_LEFT, 50, 770) + _column(TWO_COLUMN_RIGHT, 220, 770)
    return _assemble(_text_pdf_objects([_text_ops(lines)]))


def table_pdf() -> bytes:
    column_x = [72, 232, 412]
    top, row_height = 720, 24
    ops = [_text_ops([(72, 770, 14, TABLE_HEADING)])]
    rules = [b"0.5 w"]
    for row in range(len(TABLE_ROWS) + 1):
        y = top + 16 - row_height * row
        rules.append(b"66 %d m 529 %d l S" % (y, y))
    bottom = top + 16 - row_height * len(TABLE_ROWS)
    for x in (66, 226, 406, 529):
        rules.append(b"%d %d m %d %d l S" % (x, top + 16, x, bottom))
    ops.append(b"\n".join(rules) + b"\n")
    cells: list[Line] = []
    for row, texts in enumerate(TABLE_ROWS):
        for x, text in zip(column_x, texts, strict=True):
            cells.append((x, top - row_height * row, 10, text))
    ops.append(_text_ops(cells))
    ops.append(_text_ops([(72, bottom - 30, 11, TABLE_CLOSING)]))
    return _assemble(_text_pdf_objects([b"".join(ops)]))


def truncated_pdf() -> bytes:
    """The single-column file cut inside its page tree: no page, no cross-reference table."""
    whole = single_column_pdf()
    return whole[: whole.index(b"2 0 obj") + 30]


def image_only_pdf() -> bytes:
    """One page that only draws a small grey image, as a scan does: no font, no text."""
    pixels = bytes((row * 8 + column) * 4 % 256 for row in range(8) for column in range(8))
    image = _stream(
        pixels.hex().upper().encode("ascii") + b">",
        b" /Type /XObject /Subtype /Image /Width 8 /Height 8 /ColorSpace /DeviceGray"
        b" /BitsPerComponent 8 /Filter /ASCIIHexDecode",
    )
    objects = [
        CATALOG,
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /XObject << /Im1 4 0 R >> >> /Contents 5 0 R >>",
        image,
        _stream(b"q 400 0 0 400 97 300 cm /Im1 Do Q\n"),
    ]
    return _assemble(objects)


# Standard security handler, revision 2 (40-bit RC4), from the PDF reference. This is a file
# format detail for a fixture that must be refused, not protection of anything.
_PAD = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")
_PERMISSIONS = -4
# Fixture-only passwords for a file nobody needs to open; not credentials of any kind.
_FIXTURE_USER_PHRASE = b"fixture-" + b"user"
_FIXTURE_OWNER_PHRASE = b"fixture-" + b"owner"
_FIXTURE_FILE_ID = bytes(range(16))


def _md5(data: bytes) -> bytes:
    return hashlib.md5(data, usedforsecurity=False).digest()


def _rc4(key: bytes, data: bytes) -> bytes:
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) % 256
        state[i], state[j] = state[j], state[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) % 256
        j = (j + state[i]) % 256
        state[i], state[j] = state[j], state[i]
        out.append(byte ^ state[(state[i] + state[j]) % 256])
    return bytes(out)


def _padded(phrase: bytes) -> bytes:
    return (phrase + _PAD)[:32]


def encrypted_pdf() -> bytes:
    """The single-column first page behind a user password (which the app never asks for)."""
    owner_entry = _rc4(_md5(_padded(_FIXTURE_OWNER_PHRASE))[:5], _padded(_FIXTURE_USER_PHRASE))
    permissions = (_PERMISSIONS & 0xFFFFFFFF).to_bytes(4, "little")
    file_key = _md5(_padded(_FIXTURE_USER_PHRASE) + owner_entry + permissions + _FIXTURE_FILE_ID)[
        :5
    ]
    user_entry = _rc4(file_key, _PAD)

    content_number = 5
    plain = _text_ops(_column(SINGLE_PAGE_1, 72, 770))
    object_key = _md5(file_key + content_number.to_bytes(3, "little") + b"\x00\x00")[:10]
    objects = _text_pdf_objects([_rc4(object_key, plain)])
    objects.append(
        b"<< /Filter /Standard /V 1 /R 2 /Length 40 /O <%s> /U <%s> /P %d >>"
        % (owner_entry.hex().encode("ascii"), user_entry.hex().encode("ascii"), _PERMISSIONS)
    )
    file_id = _FIXTURE_FILE_ID.hex().encode("ascii")
    trailer = b" /Encrypt %d 0 R /ID [<%s> <%s>]" % (len(objects), file_id, file_id)
    return _assemble(objects, trailer)


# --- Word and zip --------------------------------------------------------------------------

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

CONTENT_TYPES = (
    _XML + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Default Extension="png" ContentType="image/png"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
    'officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
ROOT_RELS = (
    _XML + f'<Relationships xmlns="{_PKG_REL}">'
    f'<Relationship Id="rId1" Type="{_REL}/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)
DOCUMENT_RELS_WITH_IMAGE = (
    _XML + f'<Relationships xmlns="{_PKG_REL}">'
    f'<Relationship Id="rId1" Type="{_REL}/image" Target="media/image1.png"/>'
    "</Relationships>"
)


def _xml_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _paragraph(text: str) -> str:
    return f'<w:p><w:r><w:t xml:space="preserve">{_xml_text(text)}</w:t></w:r></w:p>'


def _table(rows: list[list[str]]) -> str:
    grid = "".join('<w:gridCol w:w="3000"/>' for _ in rows[0])
    body = ""
    for row in rows:
        cells = "".join(
            f'<w:tc><w:tcPr><w:tcW w:w="3000" w:type="dxa"/></w:tcPr>{_paragraph(cell)}</w:tc>'
            for cell in row
        )
        body += f"<w:tr>{cells}</w:tr>"
    return f"<w:tbl><w:tblPr/><w:tblGrid>{grid}</w:tblGrid>{body}</w:tbl>"


def _document_xml(body: str) -> str:
    section = '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr>'
    return _XML + f'<w:document xmlns:w="{_W}"><w:body>{body}{section}</w:body></w:document>'


def _zip(members: list[tuple[str, bytes]], deflate: frozenset[str] = frozenset()) -> bytes:
    """A zip with fixed dates, modes and host system, so the bytes don't depend on who runs it."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, data in members:
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
            info.create_system = 3
            info.external_attr = 0o644 << 16
            if name in deflate:
                archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
            else:
                archive.writestr(info, data, compress_type=zipfile.ZIP_STORED)
    return out.getvalue()


def _docx(body: str, extra: list[tuple[str, bytes]] | None = None) -> bytes:
    members = [
        ("[Content_Types].xml", CONTENT_TYPES.encode("utf-8")),
        ("_rels/.rels", ROOT_RELS.encode("utf-8")),
        ("word/document.xml", _document_xml(body).encode("utf-8")),
    ]
    members.extend(extra or [])
    return _zip(members, deflate=frozenset({"word/media/image1.png"}))


def with_tables_docx() -> bytes:
    body = "".join(_paragraph(text) for text in DOCX_PARAGRAPHS)
    body += _table(DOCX_TABLE_ROWS) + _paragraph(DOCX_CLOSING)
    return _docx(body)


def empty_body_docx() -> bytes:
    """A valid Word file with no text at all."""
    return _docx("<w:p/>")


def oversized_entry_docx() -> bytes:
    """A valid, readable Word file with one related 64 MiB entry that compresses to a few
    kilobytes (a decompression bomb). It must be refused before anything is inflated."""
    body = "".join(_paragraph(text) for text in DOCX_PARAGRAPHS)
    extra = [
        ("word/_rels/document.xml.rels", DOCUMENT_RELS_WITH_IMAGE.encode("utf-8")),
        ("word/media/image1.png", bytes(OVERSIZED_ENTRY_BYTES)),
    ]
    return _docx(body, extra)


def not_word_zip() -> bytes:
    return _zip([("notes.txt", b"A fictional note. This archive is not a Word file.\n")])


def cv_text() -> bytes:
    return ("\n".join(TEXT_LINES) + "\n").encode("utf-8")


# --- Manifest ------------------------------------------------------------------------------


def _entry(
    kind: str | None, outcome: str, about: str, marker_groups: list[list[str]] | None = None
) -> dict[str, Any]:
    return {
        "kind": kind,
        "outcome": outcome,
        "about": about,
        "marker_groups": marker_groups or [],
    }


def manifest() -> dict[str, Any]:
    """What each fixture is and what reading it must give.

    ``kind`` is what content sniffing must answer (null: refused as an unsupported type).
    ``outcome`` is one of text, unreadable, encrypted, no_text, unsupported. For ``text`` files,
    every marker of every group must be in the extracted text, and the markers of one group
    must come in the order listed (whitespace runs count as one space)."""
    text_markers = [line for line in TEXT_LINES if line]
    files = {
        "single_column.pdf": _entry(
            "pdf",
            "text",
            "Two pages, one column.",
            [SINGLE_PAGE_1 + SINGLE_PAGE_2],
        ),
        "two_column.pdf": _entry(
            "pdf",
            "text",
            "A sidebar and a main column on shared baselines; each group is one column.",
            [TWO_COLUMN_LEFT, TWO_COLUMN_RIGHT],
        ),
        "table.pdf": _entry(
            "pdf",
            "text",
            "A ruled table with one string per cell; each table row is a group.",
            [[TABLE_HEADING], *TABLE_ROWS, [TABLE_CLOSING]],
        ),
        "with_tables.docx": _entry(
            "docx",
            "text",
            "Paragraphs and a table whose cell texts appear nowhere else.",
            [[*DOCX_PARAGRAPHS, DOCX_CLOSING], *DOCX_TABLE_ROWS],
        ),
        "cv.txt": _entry("text", "text", "UTF-8 plain text with accented letters.", [text_markers]),
        "truncated.pdf": _entry("pdf", "unreadable", "single_column.pdf cut in its page tree."),
        "encrypted.pdf": _entry("pdf", "encrypted", "Needs a user password (40-bit RC4)."),
        "image_only.pdf": _entry("pdf", "no_text", "One page with an image and no text."),
        "empty_body.docx": _entry("docx", "no_text", "A valid Word file with no text."),
        "oversized_entry.docx": _entry(
            "docx",
            "unreadable",
            "A readable Word file with one 64 MiB entry that compresses to kilobytes.",
        ),
        "not_word.zip": _entry(None, "unsupported", "A zip archive that is not a Word file."),
    }
    return {"generator": GENERATOR, "files": files}


def build_fixtures() -> dict[str, bytes]:
    """Every fixture file by name, the manifest included."""
    files = {
        "single_column.pdf": single_column_pdf(),
        "two_column.pdf": two_column_pdf(),
        "table.pdf": table_pdf(),
        "with_tables.docx": with_tables_docx(),
        "cv.txt": cv_text(),
        "truncated.pdf": truncated_pdf(),
        "encrypted.pdf": encrypted_pdf(),
        "image_only.pdf": image_only_pdf(),
        "empty_body.docx": empty_body_docx(),
        "oversized_entry.docx": oversized_entry_docx(),
        "not_word.zip": not_word_zip(),
    }
    listing = json.dumps(manifest(), indent=2, ensure_ascii=False) + "\n"
    files[MANIFEST_NAME] = listing.encode("utf-8")
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the fictional CV fixtures.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="folder to write into")
    args = parser.parse_args(argv)
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    for name, data in build_fixtures().items():
        (out / name).write_bytes(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
