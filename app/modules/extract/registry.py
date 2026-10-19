"""The ``TextExtractor`` protocol and the extractor for each document kind."""

from pathlib import Path
from typing import Final, Literal, Protocol

from app.domain.errors import UnsupportedDocumentType

DocumentKind = Literal["pdf", "docx", "text"]
KINDS: Final[tuple[DocumentKind, ...]] = ("pdf", "docx", "text")


class TextExtractor(Protocol):
    """Reads one format. ``extract`` returns the text, or raises ``UnreadableDocument`` for a
    damaged file and ``NoTextFound`` for one with nothing to read. It never changes the file."""

    @property
    def kind(self) -> str: ...

    def extract(self, path: Path) -> str: ...


def extractor_for(kind: str) -> TextExtractor:
    """The extractor for ``kind``. Imported on use, so only the process that parses a format
    loads its library."""
    if kind == "pdf":
        from app.modules.extract.providers.pdf import PdfExtractor

        return PdfExtractor()
    if kind == "docx":
        from app.modules.extract.providers.docx import DocxExtractor

        return DocxExtractor()
    if kind == "text":
        from app.modules.extract.providers.text import PlainTextExtractor

        return PlainTextExtractor()
    raise UnsupportedDocumentType(detail="unknown document kind")
