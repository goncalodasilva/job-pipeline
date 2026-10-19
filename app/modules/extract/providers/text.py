"""Plain text: UTF-8, with or without a byte order mark."""

from pathlib import Path
from typing import Final

from app.domain.errors import NoTextFound, UnreadableDocument


class PlainTextExtractor:
    kind: Final = "text"

    def extract(self, path: Path) -> str:
        try:
            text = path.read_bytes().decode("utf-8-sig")
        except (OSError, UnicodeDecodeError) as error:
            raise UnreadableDocument(detail=type(error).__name__) from None
        if not text.strip():
            raise NoTextFound()
        return text
