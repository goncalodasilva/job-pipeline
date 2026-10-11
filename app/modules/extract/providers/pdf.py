"""PDF text through pypdf (ADR-089 item 2). The only module that imports pypdf.

No OCR: a scanned PDF has no text and raises ``NoTextFound`` (ADR-089 item 11).
"""

from pathlib import Path
from typing import Final

from pypdf import PdfReader

from app.domain.errors import AppError, EncryptedDocument, NoTextFound, UnreadableDocument


class PdfExtractor:
    kind: Final = "pdf"

    def extract(self, path: Path) -> str:
        try:
            text = _read(path)
        except AppError:
            raise
        except Exception as error:
            # pypdf raises many types on damaged files, and their text can quote the document:
            # only the type name is kept.
            raise UnreadableDocument(detail=type(error).__name__) from None
        if not text.strip():
            raise NoTextFound()
        return text


def _read(path: Path) -> str:
    reader = PdfReader(path)
    if reader.is_encrypted:
        # Refused even when the password is empty: nothing is decrypted here.
        raise EncryptedDocument()
    pages = [page.extract_text() for page in reader.pages]
    if not pages:
        raise UnreadableDocument(detail="the PDF has no pages")
    return "\n".join(pages)
