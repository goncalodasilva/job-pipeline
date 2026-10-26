"""The optional CV photo, checked and cleaned without an image library (M6.6; ADR-106 item 4;
spec 2.9 rule 5). Pure: bytes in, bytes out.

``sniff_image`` decides the type by content only. ``photo_dimensions`` reads the size the
header states; the upload service applies the limits below. ``strip_metadata`` keeps the picture
and its colour profile and nothing else (EXIF with GPS, XMP, IPTC, comments, PNG text and time
chunks, private chunks, anything after the end marker). The JPEG colour hints are cut to their
fixed headers (a JFIF thumbnail and the tail of an Adobe segment can hold data), a colour
profile above ``ICC_MAX_BYTES`` is dropped whole, and a PNG's pixel stream is inflated once,
under a bound derived from its header, to prove it is a stream of that size (no pixel is kept
or decoded further). It never allocates from a stated size, so a tiny file that states a huge
picture is cheap. All input is
untrusted: ``InvalidImage`` carries fixed text and never any input bytes.
"""

import zlib
from typing import Final, Literal

PhotoType = Literal["jpeg", "png"]

PHOTO_MAX_BYTES: Final = 5 * 1024 * 1024
PHOTO_MIN_SIDE: Final = 100
PHOTO_MAX_SIDE: Final = 4000
PHOTO_MAX_PIXELS: Final = 16_000_000

_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC: Final = b"\xff\xd8\xff"
# JPEG start-of-frame markers: the ones that carry the picture size (not DHT, JPG or DAC).
_JPEG_FRAMES: Final = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)
_JPEG_STANDALONE: Final = frozenset({0x01, *range(0xD0, 0xDA)})  # TEM, RSTn, SOI, EOI
_JPEG_RESTARTS: Final = frozenset(range(0xD0, 0xD8))
_SOI, _EOI, _SOS, _DNL, _APP0, _APP2, _APP14 = 0xD8, 0xD9, 0xDA, 0xDC, 0xE0, 0xE2, 0xEE
# Segments that are the picture itself: tables, frame headers, restart interval, line count.
# 0xC8 (JPG) is reserved for extensions and is not part of it.
_JPEG_PICTURE: Final = frozenset({*range(0xC0, 0xD0), 0xDB, 0xDD, _DNL} - {0xC8})
_ICC_PREFIX: Final = b"ICC_PROFILE\x00"
_JFIF_PREFIX: Final = b"JFIF\x00"
_ADOBE_PREFIX: Final = b"Adobe"  # tells a decoder how to read CMYK/YCCK colours
_PNG_KEPT: Final = frozenset(
    {b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND", b"iCCP", b"sRGB", b"gAMA", b"cHRM"}
)
ICC_MAX_BYTES: Final = 64 * 1024  # of colour profile kept; a larger one is dropped whole
_JFIF_HEADER: Final = 12  # "JFIF\0", version, units, densities; the thumbnail size follows
_ADOBE_HEADER: Final = 12  # "Adobe", version, two flag words, the colour transform
_INFLATE_STEP: Final = 64 * 1024
_PNG_IHDR_LENGTH: Final = 13
_PNG_CHUNK_OVERHEAD: Final = 12  # length, type and CRC


class InvalidImage(ValueError):
    """The bytes are not a well-formed image of the given type. The text is fixed."""

    def __init__(self, reason: str = "The picture is damaged or not of that type.") -> None:
        super().__init__(reason)


def sniff_image(data: bytes) -> PhotoType | None:
    """``"jpeg"`` or ``"png"`` by the very first bytes, else None. No name, no declared type."""
    if data.startswith(_JPEG_MAGIC):
        return "jpeg"
    if data.startswith(_PNG_SIGNATURE):
        return "png"
    return None


def photo_dimensions(photo: bytes, photo_type: PhotoType) -> tuple[int, int] | None:
    """The (width, height) in pixels that the photo's own header states, or ``None`` when the
    bytes do not start like ``photo_type`` or the header is unreadable or states a zero side.

    Reads only the PNG IHDR chunk or the JPEG start-of-frame segment: no image is decoded and no
    image library is used. A caller refuses a stated size above ``PHOTO_MAX_SIDE`` /
    ``PHOTO_MAX_PIXELS`` before anything decodes the picture.
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
    if not photo.startswith(_JPEG_MAGIC):
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
        if marker == _SOS or position + 2 > len(photo):  # picture data before any frame header
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


def strip_metadata(data: bytes, photo_type: PhotoType) -> bytes:
    """The picture with everything but the picture and its colour profile removed.

    JPEG keeps the picture's own segments and scans byte for byte, ICC profile segments and the
    JFIF and Adobe colour hints; PNG keeps an allow-list of chunks. Anything after the end
    marker goes. Raises ``InvalidImage`` for bytes that are not well formed ``photo_type``.
    """
    if photo_type == "png":
        return _strip_png(bytes(data))
    if photo_type == "jpeg":
        return _strip_jpeg(bytes(data))
    raise InvalidImage()


def _strip_png(data: bytes) -> bytes:
    if not data.startswith(_PNG_SIGNATURE):
        raise InvalidImage()
    kept = [_PNG_SIGNATURE]
    position, count, has_profile = len(_PNG_SIGNATURE), 0, False
    pixels: list[bytes] = []
    sides = (0, 0)
    while position + _PNG_CHUNK_OVERHEAD <= len(data):
        length = int.from_bytes(data[position : position + 4], "big")
        end = position + 8 + length  # of the chunk's data
        if length > len(data) - position - _PNG_CHUNK_OVERHEAD:
            raise InvalidImage()
        kind = data[position + 4 : position + 8]
        crc = int.from_bytes(data[end : end + 4], "big")
        if not kind.isalpha() or crc != zlib.crc32(data[position + 4 : end]):
            raise InvalidImage()
        if (count == 0) != (kind == b"IHDR") or (kind == b"IHDR" and length != _PNG_IHDR_LENGTH):
            raise InvalidImage()
        count += 1
        if kind == b"IHDR":
            sides = (
                int.from_bytes(data[position + 8 : position + 12], "big"),
                int.from_bytes(data[position + 12 : position + 16], "big"),
            )
        elif kind == b"IDAT":
            pixels.append(data[position + 8 : end])
        keep = kind in _PNG_KEPT
        if kind == b"iCCP":  # one colour profile, and not a large one
            keep = not has_profile and length <= ICC_MAX_BYTES
            has_profile = True
        if keep:
            kept.append(data[position : end + 4])
        if kind == b"IEND":
            if length != 0 or not pixels:
                raise InvalidImage()
            _check_pixel_stream(b"".join(pixels), *sides)
            return b"".join(kept)  # whatever follows IEND is dropped
        position = end + 4
    raise InvalidImage()


def _check_pixel_stream(stream: bytes, width: int, height: int) -> None:
    """The IDAT data must be one complete zlib stream that inflates to no more than the header
    allows (at most 8 bytes a pixel, a filter byte a row, twice over for interlacing, plus
    slack). Inflated in steps and never kept, so a small file claiming a huge stream stops at the
    bound. ``InvalidImage`` for anything else."""
    bound = height * (2 + width * 8) + 1024
    inflater = zlib.decompressobj()
    total, rest = 0, stream
    try:
        while not inflater.eof:
            produced = inflater.decompress(rest, _INFLATE_STEP)
            total += len(produced)
            rest = inflater.unconsumed_tail
            if total > bound or not (produced or rest):
                break
    except zlib.error:
        raise InvalidImage() from None
    if not inflater.eof or total > bound:
        raise InvalidImage()


def _colour_hint(marker: int, payload: bytes) -> bytes | None:
    """The JFIF or Adobe segment rebuilt from its fixed header (a JFIF one with no thumbnail),
    or ``None`` when it is neither or too short to hold the header."""
    if marker == _APP0 and payload.startswith(_JFIF_PREFIX) and len(payload) >= _JFIF_HEADER:
        header = payload[:_JFIF_HEADER] + b"\x00\x00"
    elif marker == _APP14 and payload.startswith(_ADOBE_PREFIX) and len(payload) >= _ADOBE_HEADER:
        header = payload[:_ADOBE_HEADER]
    else:
        return None
    return b"\xff" + bytes([marker]) + (len(header) + 2).to_bytes(2, "big") + header


def _next_marker(data: bytes, position: int) -> tuple[int, int]:
    """(position of the marker's 0xFF, marker) of the first marker at or after ``position``,
    skipping fill bytes. ``InvalidImage`` when the data ends or does not hold a marker."""
    if position + 2 > len(data) or data[position] != 0xFF:
        raise InvalidImage()
    while data[position + 1] == 0xFF:  # fill bytes
        position += 1
        if position + 2 > len(data):
            raise InvalidImage()
    return position, data[position + 1]


def _scan_end(data: bytes, position: int) -> int:
    """Where the entropy-coded data that starts at ``position`` ends: at the first marker that
    is not a stuffed 0xFF00 or a restart."""
    while True:
        found = data.find(b"\xff", position)
        if found < 0 or found + 2 > len(data):
            raise InvalidImage()
        following = data[found + 1]
        if following == 0xFF:  # fill byte before a marker
            position = found + 1
            continue
        if following == 0 or following in _JPEG_RESTARTS:
            position = found + 2
            continue
        return found


def _strip_jpeg(data: bytes) -> bytes:
    if not data.startswith(_JPEG_MAGIC):
        raise InvalidImage()
    kept = [data[:2]]
    profile: list[int] = []  # where the colour profile segments sit in ``kept``
    profile_size, hints = 0, set[int]()
    position, framed, scanned = 2, False, False
    while True:
        at, marker = _next_marker(data, position)
        if marker == _EOI:
            if not (framed and scanned):
                raise InvalidImage()
            if profile_size > ICC_MAX_BYTES:  # a profile too big goes whole, never cut
                for index in reversed(profile):
                    del kept[index]
            kept.append(b"\xff\xd9")  # whatever follows is dropped
            return b"".join(kept)
        position = at + 2
        if marker in _JPEG_STANDALONE:  # TEM, RSTn: nothing to keep before a scan
            if marker == _SOI:
                raise InvalidImage()
            continue
        if position + 2 > len(data):
            raise InvalidImage()
        length = int.from_bytes(data[position : position + 2], "big")
        if length < 2 or position + length > len(data):
            raise InvalidImage()
        payload = data[position + 2 : position + length]
        if marker in _JPEG_FRAMES:
            if framed:  # a second frame header: the file states two pictures
                raise InvalidImage()
            framed = True
        if marker == _APP2 and payload.startswith(_ICC_PREFIX):
            profile.append(len(kept))
            profile_size += len(payload)
            kept.append(data[at : position + length])
        elif marker in (_APP0, _APP14):
            hint = _colour_hint(marker, payload)
            if hint is not None and marker not in hints:  # the first of each only
                hints.add(marker)
                kept.append(hint)
        elif marker in _JPEG_PICTURE or marker == _SOS:
            kept.append(data[at : position + length])
        position += length
        if marker == _SOS:
            if not framed:
                raise InvalidImage()
            scanned = True
            end = _scan_end(data, position)
            kept.append(data[position:end])
            position = end
