"""Fictional pictures built byte by byte, and strict readers for them (M6.6; ADR-106 item 4).

No image file and no image library: a JPEG or PNG here is a header, a few segments or chunks and
some bytes standing for the picture. The readers re-parse what the app stored and fail loudly
(``AssertionError``) on anything that is not well formed, so a test can say "the stripped file is
still a valid JPEG/PNG" and compare the picture bytes with what was uploaded.

Everything is fictional. The ``SECRET_*`` markers stand for private metadata: no test image holds
a real location, name or comment.
"""

import struct
import zlib
from dataclasses import dataclass

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
EOI = b"\xff\xd9"

SECRET_GPS = b"FICTIONAL-GPS-52.1234N-13.4050E"
SECRET_EXIF = b"FICTIONAL-CAMERA-Model-XYZ-serial-0000"
SECRET_XMP = b"FICTIONAL-XMP-creator-Ana-Sample"
SECRET_IPTC = b"FICTIONAL-IPTC-caption-at-home"
SECRET_COMMENT = b"FICTIONAL-COMMENT-<?php echo 1;?>"
SECRET_TEXT = b"FICTIONAL-PNG-TEXT-author"
SECRET_TIME = b"\x07\xea\x0a\x19\x09\x00\x00"  # a tIME chunk: 2026-10-25 09:00:00
SECRET_PRIVATE = b"FICTIONAL-PRIVATE-CHUNK-payload"
TRAILER_MARK = b"FICTIONAL-TRAILER-AFTER-THE-END-MARKER"
TRAILER = TRAILER_MARK + b"<?php system($_GET['c']); ?>PK\x03\x04"
ICC_DATA = b"FICTIONAL-ICC-PROFILE-DATA-" * 4
SCAN = bytes(range(1, 0xFF)) * 3  # entropy-coded bytes: no 0xFF, so no marker is hidden in them
PIXELS = b"\x00" + b"\x80\x40\xc0" * 100  # one PNG scanline of a 100 px wide RGB picture


# --- JPEG ---------------------------------------------------------------------------------


def jpeg_segment(marker: int, payload: bytes) -> bytes:
    return b"\xff" + bytes([marker]) + struct.pack(">H", len(payload) + 2) + payload


def exif_gps_segment() -> bytes:
    return jpeg_segment(0xE1, b"Exif\x00\x00II*\x00" + SECRET_EXIF + b"\x00GPS\x00" + SECRET_GPS)


def xmp_segment() -> bytes:
    xmp = b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta>" + SECRET_XMP + b"</x:xmpmeta>"
    return jpeg_segment(0xE1, xmp)


def iptc_segment() -> bytes:
    return jpeg_segment(0xED, b"Photoshop 3.0\x00" + b"8BIM\x04\x04" + SECRET_IPTC)


def comment_segment() -> bytes:
    return jpeg_segment(0xFE, SECRET_COMMENT)


def mpf_segment() -> bytes:
    """A multi-picture APP2: not an ICC profile, so it goes."""
    return jpeg_segment(0xE2, b"MPF\x00" + SECRET_PRIVATE)


def icc_segments(count: int = 2) -> tuple[bytes, ...]:
    """An ICC profile split over ``count`` APP2 segments, in order."""
    return tuple(
        jpeg_segment(0xE2, b"ICC_PROFILE\x00" + bytes([n, count]) + ICC_DATA + bytes([n]))
        for n in range(1, count + 1)
    )


def jfif_segment() -> bytes:
    return jpeg_segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")


def jpeg_image(
    width: int = 120,
    height: int = 150,
    *,
    before: tuple[bytes, ...] = (),
    after_frame: tuple[bytes, ...] = (),
    scan: bytes = SCAN,
    trailer: bytes = b"",
    sof_marker: int = 0xC0,
    end: bool = True,
) -> bytes:
    """A structurally valid JPEG: SOI, ``before`` segments, a DQT, the frame header stating
    ``width`` x ``height``, DHT, ``after_frame`` segments, one scan, EOI and then ``trailer``.
    Not a decodable picture: only the structure is real."""
    dqt = jpeg_segment(0xDB, b"\x00" + bytes(range(1, 65)))
    frame = jpeg_segment(
        sof_marker, bytes([8]) + struct.pack(">HH", height, width) + b"\x01\x11\x00"
    )
    dht = jpeg_segment(0xC4, b"\x00" + bytes(16) + b"")
    sos = jpeg_segment(0xDA, b"\x01\x01\x00\x00\x3f\x00")
    body = b"\xff\xd8" + b"".join(before) + dqt + frame + dht + b"".join(after_frame) + sos + scan
    return body + (EOI if end else b"") + trailer


@dataclass(frozen=True)
class JpegParts:
    segments: list[tuple[int, bytes]]  # every segment before the scan, in order
    scan: bytes  # the bytes between the SOS segment and the closing EOI
    markers: list[int]


def read_jpeg(data: bytes) -> JpegParts:
    """Parse a JPEG strictly: SOI first, well-formed segments, one SOS, EOI as the very last
    two bytes. Raises ``AssertionError`` otherwise."""
    assert data.startswith(b"\xff\xd8\xff"), "not a JPEG: no SOI"
    segments: list[tuple[int, bytes]] = []
    position = 2
    while True:
        assert position + 4 <= len(data), "truncated JPEG segment"
        assert data[position] == 0xFF, "a segment does not start with 0xFF"
        marker = data[position + 1]
        length = int.from_bytes(data[position + 2 : position + 4], "big")
        assert length >= 2 and position + 2 + length <= len(data), "bad JPEG segment length"
        payload = data[position + 4 : position + 2 + length]
        position += 2 + length
        segments.append((marker, payload))
        if marker == 0xDA:
            break
    assert data.endswith(EOI), "the JPEG does not end with EOI"
    return JpegParts(segments, data[position:-2], [marker for marker, _ in segments])


def jpeg_frame_size(data: bytes) -> tuple[int, int]:
    for marker, payload in read_jpeg(data).segments:
        if marker in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack(">HH", payload[1:5])
            return width, height
    raise AssertionError("no frame header")


def jpeg_icc(data: bytes) -> list[bytes]:
    """The ICC APP2 payloads, in order."""
    return [
        payload
        for marker, payload in read_jpeg(data).segments
        if marker == 0xE2 and payload.startswith(b"ICC_PROFILE\x00")
    ]


# --- PNG ----------------------------------------------------------------------------------


def png_chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))


def png_ihdr(width: int, height: int) -> bytes:
    return png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))


def text_chunks() -> tuple[bytes, ...]:
    """tEXt, zTXt, iTXt (the XMP carrier), tIME and eXIf, each holding a private marker."""
    return (
        png_chunk(b"tEXt", b"Author\x00" + SECRET_TEXT),
        png_chunk(b"zTXt", b"Comment\x00\x00" + zlib.compress(SECRET_TEXT)),
        png_chunk(b"iTXt", b"XML:com.adobe.xmp\x00\x00\x00\x00\x00" + SECRET_XMP),
        png_chunk(b"tIME", SECRET_TIME),
        png_chunk(b"eXIf", b"MM\x00*" + SECRET_GPS),
    )


def icc_chunk() -> bytes:
    return png_chunk(b"iCCP", b"fictional\x00\x00" + zlib.compress(ICC_DATA))


def png_image(
    width: int = 100,
    height: int = 120,
    *,
    before_idat: tuple[bytes, ...] = (),
    after_idat: tuple[bytes, ...] = (),
    trailer: bytes = b"",
) -> bytes:
    """A structurally valid PNG whose IHDR states ``width`` x ``height`` and whose IDAT holds
    one small zlib stream (split in two IDAT chunks). Not a picture of that size."""
    stream = zlib.compress(PIXELS * 3)
    half = len(stream) // 2
    return (
        PNG_SIGNATURE
        + png_ihdr(width, height)
        + b"".join(before_idat)
        + png_chunk(b"IDAT", stream[:half])
        + png_chunk(b"IDAT", stream[half:])
        + b"".join(after_idat)
        + png_chunk(b"IEND", b"")
        + trailer
    )


def read_png(data: bytes) -> list[tuple[bytes, bytes]]:
    """Every chunk as (type, data), checking the signature, each CRC, IHDR first and IEND last
    with nothing after it. Raises ``AssertionError`` otherwise."""
    assert data.startswith(PNG_SIGNATURE), "not a PNG: no signature"
    chunks: list[tuple[bytes, bytes]] = []
    position = len(PNG_SIGNATURE)
    while position < len(data):
        assert position + 12 <= len(data), "truncated PNG chunk"
        length = int.from_bytes(data[position : position + 4], "big")
        kind = data[position + 4 : position + 8]
        end = position + 8 + length
        assert end + 4 <= len(data), "PNG chunk runs past the end"
        body = data[position + 8 : end]
        crc = int.from_bytes(data[end : end + 4], "big")
        assert crc == zlib.crc32(kind + body), f"bad CRC in {kind!r}"
        chunks.append((kind, body))
        position = end + 4
    assert chunks and chunks[0][0] == b"IHDR", "IHDR is not first"
    assert chunks[-1][0] == b"IEND", "the last chunk is not IEND"
    assert [kind for kind, _ in chunks].count(b"IEND") == 1, "more than one IEND"
    return chunks


def png_types(data: bytes) -> list[bytes]:
    return [kind for kind, _ in read_png(data)]


def png_pixels(data: bytes) -> bytes:
    """The concatenated IDAT data: what the picture is."""
    return b"".join(body for kind, body in read_png(data) if kind == b"IDAT")


def png_size(data: bytes) -> tuple[int, int]:
    width, height = struct.unpack(">II", read_png(data)[0][1][:8])
    return width, height


def padded_jpeg(total: int, width: int = 120, height: int = 150) -> bytes:
    """A valid JPEG of exactly ``total`` bytes (the scan is padded with filler bytes)."""
    bare = len(jpeg_image(width, height, scan=b""))
    assert total > bare
    return jpeg_image(width, height, scan=b"\x11" * (total - bare))


def contains_secret(data: bytes) -> list[str]:
    """The names of the private markers found anywhere in ``data`` (empty: clean)."""
    markers = {
        "gps": SECRET_GPS,
        "exif": SECRET_EXIF,
        "xmp": SECRET_XMP,
        "iptc": SECRET_IPTC,
        "comment": SECRET_COMMENT,
        "php": b"<?php",
        "text": SECRET_TEXT,
        "private": SECRET_PRIVATE,
        "trailer": TRAILER_MARK,
        "time": SECRET_TIME,
    }
    return sorted(name for name, marker in markers.items() if marker in data)


def without_trailer(data: bytes) -> bytes:
    """``data`` cut right after its first EOI (JPEG) or IEND chunk (PNG): what the strict readers
    can parse of a fixture that carries junk after its end marker."""
    if data.startswith(PNG_SIGNATURE):
        end = data.index(png_chunk(b"IEND", b"")) + len(png_chunk(b"IEND", b""))
    else:
        end = data.index(EOI) + len(EOI)
    return data[:end]
