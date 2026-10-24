"""What a rendered CV or letter contains, independent of the file format (spec 2.4; ADR-106).

Pure data: a renderer turns a ``RenderDocument`` into a file and a reader must find
``plain_text()`` in that file. All text is untrusted (spec 3.3); ``clean_render_text`` is the one
place that removes control characters, and renderers and ``plain_text`` both use it.
"""

import unicodedata
from dataclasses import dataclass
from typing import Final, Literal

PageSize = Literal["A4", "Letter"]
PhotoType = Literal["jpeg", "png"]

PAGE_SIZES: Final = ("A4", "Letter")
PHOTO_TYPES: Final = ("jpeg", "png")


def _is_kept(character: str) -> bool:
    if character in "\t\n":
        return True
    if unicodedata.category(character) in ("Cc", "Cs"):  # controls and lone surrogates
        return False
    return character not in "\ufffe\uffff"  # the noncharacters XML 1.0 forbids


def clean_render_text(text: str) -> str:
    """Remove what a file cannot hold: control characters (Cc) except tab and newline, lone
    surrogates (Cs) and the characters U+FFFE and U+FFFF.

    A carriage return is a control character, so a Windows line ending becomes one newline.
    """
    return "".join(c for c in text if _is_kept(c))


@dataclass(frozen=True)
class RenderEntry:
    heading: str
    lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class RenderSection:
    heading: str
    lines: tuple[str, ...] = ()
    entries: tuple[RenderEntry, ...] = ()


@dataclass(frozen=True)
class RenderDocument:
    title: str
    author: str
    page_size: PageSize = "A4"
    header_lines: tuple[str, ...] = ()
    sections: tuple[RenderSection, ...] = ()
    photo: bytes | None = None
    photo_type: PhotoType | None = None

    def __post_init__(self) -> None:
        if self.page_size not in PAGE_SIZES:
            raise ValueError(f"unsupported page size: {self.page_size!r}")
        if (self.photo is None) != (self.photo_type is None):
            raise ValueError("a photo needs both its bytes and its type")
        if self.photo_type is not None and self.photo_type not in PHOTO_TYPES:
            raise ValueError(f"unsupported photo type: {self.photo_type!r}")

    def plain_text(self) -> str:
        """The text a reader must find in a rendered file, one piece per line."""
        pieces: list[str] = list(self.header_lines)
        for section in self.sections:
            pieces.append(section.heading)
            pieces.extend(section.lines)
            for entry in section.entries:
                pieces.append(entry.heading)
                pieces.extend(entry.lines)
        cleaned = (clean_render_text(piece) for piece in pieces)
        return "\n".join(piece for piece in cleaned if piece)
