"""Word text through python-docx (ADR-089 item 2). The only module that imports ``docx``.

A Word file is a zip archive, so its entry sizes are checked before python-docx opens it: a
small upload can declare an entry that unpacks to gigabytes.
"""

import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Final

from docx import Document
from docx.document import Document as WordDocument
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph

from app.domain.errors import AppError, NoTextFound, UnreadableDocument

# Unpacked sizes as the archive declares them; zipfile never reads past a declared size.
MAX_ENTRY_BYTES: Final = 16 * 1024 * 1024
MAX_TOTAL_BYTES: Final = 32 * 1024 * 1024


class DocxExtractor:
    kind: Final = "docx"

    def extract(self, path: Path) -> str:
        try:
            _check_entry_sizes(path)
            text = "\n".join(_lines(Document(str(path))))
        except AppError:
            raise
        except Exception as error:
            # Error text from the library can quote the document: only the type name is kept.
            raise UnreadableDocument(detail=type(error).__name__) from None
        if not text.strip():
            raise NoTextFound()
        return text


def _check_entry_sizes(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        sizes = [entry.file_size for entry in archive.infolist()]
    if any(size > MAX_ENTRY_BYTES for size in sizes) or sum(sizes) > MAX_TOTAL_BYTES:
        raise UnreadableDocument(detail="a Word file entry is too large")


def _lines(container: WordDocument | _Cell) -> Iterator[str]:
    """Paragraphs and table cells in document order, one line each."""
    for block in container.iter_inner_content():
        if isinstance(block, Paragraph):
            yield block.text
        else:
            yield from _table_lines(block)


def _table_lines(table: Table) -> Iterator[str]:
    # A merged cell is listed once per grid position it covers; its text is taken once.
    seen: list[object] = []
    for row in table.rows:
        for cell in row.cells:
            if any(cell._tc is element for element in seen):
                continue
            seen.append(cell._tc)
            yield from _lines(cell)
