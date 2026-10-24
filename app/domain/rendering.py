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


PHOTO_MAX_SIDE: Final = 4000
PHOTO_MAX_PIXELS: Final = 16_000_000

_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
# JPEG start-of-frame markers: the ones that carry the picture size (not DHT, JPG or DAC).
_JPEG_FRAMES: Final = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)
_JPEG_STANDALONE: Final = frozenset({0x01, *range(0xD0, 0xDA)})  # TEM, RSTn, SOI, EOI


def photo_dimensions(photo: bytes, photo_type: PhotoType) -> tuple[int, int] | None:
    """The (width, height) in pixels that the photo's own header states, or ``None`` when the
    bytes do not start like ``photo_type`` or the header is unreadable or states a zero side.

    Reads only the PNG IHDR chunk or the JPEG start-of-frame segment: no image is decoded and no
    image library is used. A caller refuses a stated size above ``PHOTO_MAX_SIDE`` /
    ``PHOTO_MAX_PIXELS`` before anything decodes the picture. (M6.6's upload validator should
    reuse or merge this helper.)
    """
    size = _png_size(photo) if photo_type == "png" else _jpeg_size(photo)
    if size is None or size[0] == 0 or size[1] == 0:
        return None
    return size


def _png_size(photo: bytes) -> tuple[int, int] | None:
    if not photo.startswith(_PNG_SIGNATURE) or photo[12:16] != b"IHDR" or len(photo) < 24:
        return None
    return int.from_bytes(photo[16:20], "big"), int.from_bytes(photo[20:24], "big")


def _jpeg_size(photo: bytes) -> tuple[int, int] | None:
    if not photo.startswith(b"\xff\xd8\xff"):
        return None
    position = 2
    while position + 1 < len(photo):
        if photo[position] != 0xFF:
            return None
        marker = photo[position + 1]
        if marker == 0xFF:  # fill byte
            position += 1
            continue
        position += 2
        if marker in _JPEG_STANDALONE:
            continue
        if marker == 0xDA or position + 2 > len(photo):  # picture data before any frame header
            return None
        length = int.from_bytes(photo[position : position + 2], "big")
        if marker in _JPEG_FRAMES:
            if length < 8 or position + 7 > len(photo):
                return None
            height = int.from_bytes(photo[position + 3 : position + 5], "big")
            width = int.from_bytes(photo[position + 5 : position + 7], "big")
            return width, height
        if length < 2:
            return None
        position += length
    return None


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
