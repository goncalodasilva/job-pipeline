"""Decide a document's type from its bytes (spec 2.9 rule 5).

A file name or a browser's content type is never consulted: neither can be passed in.
"""

import io
import zipfile

from app.domain.errors import UnsupportedDocumentType
from app.modules.extract.registry import DocumentKind

_PDF_SIGNATURE = b"%PDF-"
_ZIP_SIGNATURE = b"PK\x03\x04"
_UTF8_BOM = b"\xef\xbb\xbf"
# The two entries every Word file has.
_WORD_ENTRIES = frozenset({"[Content_Types].xml", "word/document.xml"})
_TEXT_CONTROLS = frozenset("\t\n\r\f\v")


def sniff_type(data: bytes) -> DocumentKind:
    """Answer "pdf", "docx" or "text", or raise ``UnsupportedDocumentType``.

    A damaged, encrypted or oversized file still has a type; reading it fails later."""
    if data.startswith(_PDF_SIGNATURE):
        return "pdf"
    if data.startswith(_ZIP_SIGNATURE):
        if _is_word(data):
            return "docx"
        raise UnsupportedDocumentType(detail="a zip archive that is not a Word file")
    if _is_text(data):
        return "text"
    raise UnsupportedDocumentType(detail="not a PDF, a Word file or plain text")


def _is_word(data: bytes) -> bool:
    # Only the archive's listing is read; no entry is decompressed here.
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError, ValueError, NotImplementedError):
        return False
    return names >= _WORD_ENTRIES


def _is_text(data: bytes) -> bool:
    try:
        text = data.removeprefix(_UTF8_BOM).decode("utf-8")
    except UnicodeDecodeError:
        return False
    if not text:
        return False
    return not any(_is_control(char) for char in text)


def _is_control(char: str) -> bool:
    return (char < " " and char not in _TEXT_CONTROLS) or char == "\x7f"
