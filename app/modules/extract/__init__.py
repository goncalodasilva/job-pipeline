"""Text extraction interface (spec 2.4, 2.9 rule 5, ADR-089 item 2).

``sniff_type`` decides what an upload is from its content. ``extract_text`` reads a stored
upload in a child process that is killed at a deadline. The parsing libraries are imported only
in ``providers/pdf.py`` and ``providers/docx.py``. Extracted text is untrusted content
(spec 3.3) and is never logged.
"""

from app.modules.extract.registry import KINDS, DocumentKind, TextExtractor, extractor_for
from app.modules.extract.runner import extract_text
from app.modules.extract.sniff import sniff_type

__all__ = [
    "KINDS",
    "DocumentKind",
    "TextExtractor",
    "extract_text",
    "extractor_for",
    "sniff_type",
]
