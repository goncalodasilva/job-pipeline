"""Pure image checks for the optional CV photo (M6.6; ADR-106 item 4; spec 2.9 rule 5).

Assumed interface: new pure module ``app/domain/images.py`` (no I/O, no image library, no
framework import; the existing domain-purity test covers it):

- ``PHOTO_MAX_BYTES = 5 * 1024 * 1024``, ``PHOTO_MIN_SIDE = 100``, ``PHOTO_MAX_SIDE = 4000``;
  ``PHOTO_MAX_PIXELS = 16_000_000``. ``PHOTO_MAX_SIDE`` and ``PHOTO_MAX_PIXELS`` move here from
  ``app/domain/rendering.py``, which re-exports them (so the M6.2 tests are unchanged).
- ``sniff_image(data: bytes) -> "jpeg" | "png" | None``: by content only, never by name or
  declared type. JPEG starts ``FF D8 FF``; PNG starts with the 8-byte signature. Nothing else
  is an image here (no GIF, WebP, BMP, SVG, PDF). The magic must be at the very start.
- ``photo_dimensions(data: bytes, photo_type) -> tuple[int, int] | None``: moved here from
  ``rendering.py`` unchanged (``rendering.photo_dimensions`` stays importable and is the same
  function). Reads only the PNG IHDR or the JPEG start-of-frame header; ``None`` when the
  bytes are not that type, the header is unreadable or a side is zero. It does NOT apply the
  100 to 4000 px limits (the upload service does); a huge stated size comes back as stated.
- ``strip_metadata(data: bytes, photo_type) -> bytes``: returns the picture with everything but
  the picture and its colour profile removed.
  * JPEG: keeps SOI, the picture segments (DQT, DHT, DRI, SOFn, SOS and its entropy-coded
    bytes, EOI) byte for byte, and APP2 segments that are an ICC profile (``ICC_PROFILE\\0``),
    in order. Removes every other APPn (EXIF with GPS, XMP, IPTC/Photoshop, MPF, ...), COM
    comments, and anything after EOI. APP0 JFIF and APP14 Adobe may go or stay (not asserted).
  * PNG: keeps only an allow-list of chunks: IHDR, PLTE, tRNS, IDAT, IEND, the colour chunks
    (iCCP, sRGB, gAMA, cHRM); removes tEXt, zTXt, iTXt, tIME, eXIf and every other
    ancillary chunk (including private ones), and anything after IEND. IDAT
    data is kept byte for byte.
  * Raises ``app.domain.images.InvalidImage`` (a ``ValueError`` subclass whose message is fixed
    text that never includes input bytes) for: bytes that are not the given type, a truncated
    file, a bad segment length, a PNG chunk with a bad CRC or a length past the end, a PNG
    without IHDR first or IEND last, a JPEG with no SOS or no EOI. It never decodes pixels and
    never allocates from a stated size, so a tiny file stating 65535 x 65535 is cheap.
  * The result always re-parses as a valid file of the same type (these tests re-parse it).

Every picture is built by ``tests/image_support.py``; nothing is a real photo.
"""

import importlib
import struct
import zlib
from types import ModuleType
from typing import Any

import pytest

from tests.image_support import (
    ICC_DATA,
    PIXELS,
    PNG_SIGNATURE,
    SCAN,
    TRAILER,
    TRAILER_MARK,
    comment_segment,
    contains_secret,
    exif_gps_segment,
    icc_chunk,
    icc_segments,
    iptc_segment,
    jfif_segment,
    jpeg_frame_size,
    jpeg_icc,
    jpeg_image,
    jpeg_segment,
    mpf_segment,
    png_chunk,
    png_ihdr,
    png_image,
    png_pixels,
    png_size,
    png_types,
    read_jpeg,
    read_png,
    text_chunks,
    without_trailer,
    xmp_segment,
)
from tests.render_support import jpeg_header_only, png_header_only, tiny_jpeg, tiny_png


def images() -> ModuleType:
    return importlib.import_module("app.domain.images")


def invalid_image() -> type[Exception]:
    found = images().InvalidImage
    assert isinstance(found, type) and issubclass(found, ValueError)
    return found


# --- sniffing and header sizes ------------------------------------------------------------

RIFF_WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + bytes(32)
SOF2 = jpeg_image(4000, 3000, sof_marker=0xC2)  # progressive
LONG_APP = jpeg_image(321, 123, before=(jfif_segment(), exif_gps_segment(), xmp_segment()))

# name -> (data, sniffed type, dimensions for that type)
SNIFF_TABLE: dict[str, tuple[bytes, Any, Any]] = {
    "png": (png_image(100, 120), "png", (100, 120)),
    "png_tiny": (tiny_png(), "png", (2, 2)),
    "png_header_only_wide": (png_header_only(4000, 4000), "png", (4000, 4000)),
    "png_stated_huge": (png_header_only(2**31 - 1, 2**31 - 1), "png", (2**31 - 1, 2**31 - 1)),
    "jpeg": (jpeg_image(120, 150), "jpeg", (120, 150)),
    "jpeg_tiny": (tiny_jpeg(), "jpeg", (2, 2)),
    "jpeg_progressive": (SOF2, "jpeg", (4000, 3000)),
    "jpeg_after_other_segments": (LONG_APP, "jpeg", (321, 123)),
    "jpeg_header_only": (jpeg_header_only(640, 480), "jpeg", (640, 480)),
    "jpeg_stated_huge": (jpeg_image(65535, 65535), "jpeg", (65535, 65535)),
    "jpeg_zero_side": (jpeg_image(0, 150), "jpeg", None),
    "png_zero_side": (png_header_only(100, 0), "png", None),
    "jpeg_magic_only": (b"\xff\xd8\xff", "jpeg", None),
    "jpeg_no_frame_header": (b"\xff\xd8\xff\xe0\x00\x04\x00\x00\xff\xd9", "jpeg", None),
    "jpeg_truncated_in_segment": (jpeg_image(120, 150)[:20], "jpeg", None),
    "png_signature_only": (PNG_SIGNATURE, "png", None),
    "png_no_ihdr": (PNG_SIGNATURE + png_chunk(b"IDAT", b"x"), "png", None),
    "png_truncated_ihdr": (png_image()[:20], "png", None),
    "gif": (b"GIF89a" + bytes(40), None, None),
    "webp": (RIFF_WEBP, None, None),
    "bmp": (b"BM" + bytes(60), None, None),
    "pdf": (b"%PDF-1.4\n1 0 obj\n", None, None),
    "svg": (b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200"/>', None, None),
    "html": (b"<!doctype html><script>alert(1)</script>", None, None),
    "zip": (b"PK\x03\x04" + bytes(40), None, None),
    "empty": (b"", None, None),
    "one_byte": (b"\xff", None, None),
    "jpeg_magic_wrong_third_byte": (b"\xff\xd8\x00" + jpeg_image()[3:], None, None),
    "png_signature_damaged": (b"\x89PNG\r\n\x1a\x0b" + png_image()[8:], None, None),
    "magic_not_at_start": (b"\x00" * 10 + jpeg_image(), None, None),
    "gif_then_jpeg": (b"GIF89a" + jpeg_image(), None, None),
    "text_naming_png": (b"image/png photo.png", None, None),
}


@pytest.mark.parametrize("table", [pytest.param(SNIFF_TABLE, id="table")])
def test_image_sniff_and_dimensions(table: dict[str, tuple[bytes, Any, Any]]) -> None:
    module = images()
    for name, (data, expected_type, expected_size) in table.items():
        assert module.sniff_image(data) == expected_type, name
        if expected_type is not None:
            assert module.photo_dimensions(data, expected_type) == expected_size, name
        for other in ("jpeg", "png"):
            if other != expected_type:  # the wrong type never reads a size
                assert module.photo_dimensions(data, other) is None, f"{name} as {other}"


def test_image_sniff_ignores_everything_but_content() -> None:
    module = images()
    jpeg, png = jpeg_image(), png_image()

    # There is no parameter for a name or a declared type, so a wrong one cannot matter.
    assert module.sniff_image(png) == "png"
    assert module.sniff_image(jpeg) == "jpeg"
    assert module.sniff_image(bytearray(png)) == "png"
    # A polyglot is what its first bytes say; trailing or embedded other formats change nothing.
    assert module.sniff_image(jpeg + PNG_SIGNATURE + b"<script>") == "jpeg"
    assert module.sniff_image(png + b"\xff\xd8\xff<?php") == "png"


def test_photo_limits_are_the_documented_ones() -> None:
    module = images()

    assert module.PHOTO_MAX_BYTES == 5 * 1024 * 1024
    assert (module.PHOTO_MIN_SIDE, module.PHOTO_MAX_SIDE) == (100, 4000)
    assert module.PHOTO_MAX_PIXELS == 16_000_000


def test_rendering_reexports_photo_dimensions_from_images() -> None:
    rendering = importlib.import_module("app.domain.rendering")
    module = images()

    assert rendering.photo_dimensions is module.photo_dimensions
    assert rendering.PHOTO_MAX_SIDE == module.PHOTO_MAX_SIDE
    assert rendering.PHOTO_MAX_PIXELS == module.PHOTO_MAX_PIXELS


# --- stripping ----------------------------------------------------------------------------

JPEG_WITH_METADATA = jpeg_image(
    120,
    150,
    before=(
        jfif_segment(),
        exif_gps_segment(),
        xmp_segment(),
        iptc_segment(),
        comment_segment(),
        *icc_segments(2),
        mpf_segment(),
    ),
    after_frame=(comment_segment(),),
    trailer=TRAILER,
)
PNG_WITH_METADATA = png_image(
    100,
    120,
    before_idat=(*text_chunks()[:3], icc_chunk(), png_chunk(b"gAMA", b"\x00\x01\x86\xa0")),
    after_idat=(*text_chunks()[3:], png_chunk(b"prVt", b"FICTIONAL-PRIVATE-CHUNK-payload")),
    trailer=TRAILER,
)


def _strip_jpeg() -> None:
    module = images()
    # The strict reader parses the original up to its end marker; the junk after it is checked
    # on its own: it must not survive.
    original = read_jpeg(without_trailer(JPEG_WITH_METADATA))
    assert TRAILER_MARK in JPEG_WITH_METADATA and contains_secret(JPEG_WITH_METADATA)

    stripped = module.strip_metadata(JPEG_WITH_METADATA, "jpeg")

    parts = read_jpeg(stripped)  # re-parsed: still a valid JPEG ending in EOI, nothing after
    assert TRAILER_MARK not in stripped and stripped.endswith(b"\xff\xd9"), "trailing data dropped"
    assert contains_secret(stripped) == []
    assert parts.scan == original.scan == SCAN, "the picture bytes are untouched"
    assert jpeg_frame_size(stripped) == (120, 150)
    assert module.sniff_image(stripped) == "jpeg"
    assert module.photo_dimensions(stripped, "jpeg") == (120, 150)
    for marker in (0xE1, 0xED, 0xFE):  # EXIF/XMP, IPTC, comments: gone
        assert marker not in parts.markers
    assert [m for m in parts.markers if m == 0xE2] == [0xE2, 0xE2], "only the ICC profile's two"
    assert jpeg_icc(stripped) == jpeg_icc(without_trailer(JPEG_WITH_METADATA))
    assert all(ICC_DATA in payload for payload in jpeg_icc(stripped))
    kept = [m for m in parts.markers if m not in (0xE0, 0xEE, 0xE2)]
    assert kept == [0xDB, 0xC0, 0xC4, 0xDA], "the picture's own segments, in order"
    assert dict(read_jpeg(stripped).segments)[0xDB] == dict(original.segments)[0xDB]
    assert module.strip_metadata(stripped, "jpeg") == stripped, "stripping is idempotent"


def _strip_png() -> None:
    module = images()
    assert TRAILER_MARK in PNG_WITH_METADATA and contains_secret(PNG_WITH_METADATA)
    original = without_trailer(PNG_WITH_METADATA)  # what the strict reader can parse

    stripped = module.strip_metadata(PNG_WITH_METADATA, "png")
    assert TRAILER_MARK not in stripped, "trailing data dropped"

    types = png_types(stripped)  # re-parsed: signature, CRCs, IHDR first, IEND last, no trailer
    assert contains_secret(stripped) == []
    assert png_pixels(stripped) == png_pixels(original), "the picture bytes are untouched"
    assert png_size(stripped) == (100, 120)
    assert module.sniff_image(stripped) == "png"
    assert module.photo_dimensions(stripped, "png") == (100, 120)
    for gone in (b"tEXt", b"zTXt", b"iTXt", b"tIME", b"eXIf", b"prVt"):
        assert gone not in types, gone
    assert types[0] == b"IHDR" and types[-1] == b"IEND"
    assert b"iCCP" in types and b"gAMA" in types, "the colour profile and gamma stay"
    assert dict(read_png(stripped))[b"iCCP"] == dict(read_png(original))[b"iCCP"]
    assert types.count(b"IDAT") == 2
    assert module.strip_metadata(stripped, "png") == stripped, "stripping is idempotent"


@pytest.mark.parametrize(
    "check",
    [pytest.param(_strip_jpeg, id="jpeg"), pytest.param(_strip_png, id="png")],
)
def test_strip_metadata_keeps_pixels_and_icc(check: Any) -> None:
    check()


def test_strip_metadata_leaves_a_clean_picture_valid_and_equivalent() -> None:
    module = images()

    for data, kind, size in (
        (tiny_jpeg(), "jpeg", (2, 2)),
        (tiny_png(), "png", (2, 2)),
        (jpeg_image(500, 400), "jpeg", (500, 400)),
        (png_image(500, 400), "png", (500, 400)),
    ):
        stripped = module.strip_metadata(data, kind)
        assert module.sniff_image(stripped) == kind
        assert module.photo_dimensions(stripped, kind) == size
        if kind == "png":
            assert png_pixels(stripped) == png_pixels(data)
        else:
            assert read_jpeg(stripped).scan == read_jpeg(data).scan


def test_strip_metadata_takes_the_type_from_the_caller_and_checks_it() -> None:
    module = images()
    error = invalid_image()

    with pytest.raises(error):
        module.strip_metadata(jpeg_image(), "png")
    with pytest.raises(error):
        module.strip_metadata(png_image(), "jpeg")


# Hostile or damaged input: refused with InvalidImage, never a crash or an unbounded read.
BAD_JPEGS: dict[str, bytes] = {
    "empty": b"",
    "soi_only": b"\xff\xd8\xff",
    "wrong_magic": b"GIF89a" + jpeg_image()[3:],
    "truncated_in_segment": jpeg_image()[:60],
    "truncated_in_scan_no_eoi": jpeg_image(end=False),
    "cut_before_scan": jpeg_image()[: jpeg_image().index(b"\xff\xda")],
    "segment_length_zero": b"\xff\xd8\xff\xe1\x00\x00" + jpeg_image()[10:],
    "segment_length_one": b"\xff\xd8\xff\xe1\x00\x01" + jpeg_image()[10:],
    "segment_length_past_end": b"\xff\xd8" + b"\xff\xe1\xff\xff" + b"x" * 20,
    "no_scan_header": b"\xff\xd8" + jpeg_segment(0xE1, b"Exif\x00\x00") + b"\xff\xd9",
    "not_a_marker_after_segment": jpeg_image()[:2] + jpeg_segment(0xDB, b"\x00" * 65) + b"\x00\x01",
    "png_given_as_jpeg": png_image(),
    "html_polyglot": b"<html><script>alert(1)</script>",
}
BAD_PNGS: dict[str, bytes] = {
    "empty": b"",
    "signature_only": PNG_SIGNATURE,
    "wrong_magic": b"\x89JPG\r\n\x1a\n" + png_image()[8:],
    "truncated_in_idat": png_image()[:70],
    "no_iend": png_image()[:-12],
    "bad_crc": png_image()[:45] + bytes([png_image()[45] ^ 0xFF]) + png_image()[46:],
    "length_past_end": PNG_SIGNATURE + b"\x00\x00\xff\xff" + b"IHDR" + bytes(13),
    "length_huge": PNG_SIGNATURE + b"\xff\xff\xff\xff" + b"IHDR" + bytes(13),
    "idat_before_ihdr": PNG_SIGNATURE + png_chunk(b"IDAT", b"x") + png_image()[8:],
    "no_ihdr": PNG_SIGNATURE + png_chunk(b"IDAT", b"x") + png_chunk(b"IEND", b""),
    "jpeg_given_as_png": jpeg_image(),
    "text_polyglot": b"<svg onload=alert(1)>",
}


@pytest.mark.parametrize("name", sorted(BAD_JPEGS))
def test_strip_metadata_refuses_hostile_jpegs(name: str) -> None:
    with pytest.raises(invalid_image()) as caught:
        images().strip_metadata(BAD_JPEGS[name], "jpeg")

    assert "alert" not in str(caught.value) and "Exif" not in str(caught.value)


@pytest.mark.parametrize("name", sorted(BAD_PNGS))
def test_strip_metadata_refuses_hostile_pngs(name: str) -> None:
    with pytest.raises(invalid_image()) as caught:
        images().strip_metadata(BAD_PNGS[name], "png")

    assert "alert" not in str(caught.value)


def test_strip_metadata_with_a_huge_stated_size_in_a_tiny_file_is_cheap_and_safe() -> None:
    module = images()
    error = invalid_image()
    for data, kind in (
        (png_header_only(2**31 - 1, 2**31 - 1), "png"),
        (jpeg_image(65535, 65535), "jpeg"),
    ):
        try:
            result = module.strip_metadata(data, kind)
        except error:
            continue  # refusing is fine
        assert len(result) <= len(data)  # nothing grew with the stated size
        assert module.photo_dimensions(result, kind) == module.photo_dimensions(data, kind)


def test_strip_metadata_drops_trailing_data_after_the_end_marker() -> None:
    module = images()
    trailer = b"PK\x03\x04<?php echo 1;?>" + jpeg_image()
    jpeg = module.strip_metadata(jpeg_image(trailer=trailer), "jpeg")
    png = module.strip_metadata(png_image(trailer=trailer), "png")

    assert jpeg.endswith(b"\xff\xd9") and jpeg.count(b"\xff\xd8") == 1 and b"PK" not in jpeg
    assert contains_secret(jpeg) == [] and b"<?php" not in jpeg
    assert png.endswith(png_chunk(b"IEND", b"")) and b"<?php" not in png and b"PK" not in png
    read_jpeg(jpeg)
    read_png(png)


def test_strip_metadata_does_not_mutate_or_alias_its_input() -> None:
    module = images()
    original = bytes(JPEG_WITH_METADATA)
    buffer = bytearray(original)

    result = module.strip_metadata(bytes(buffer), "jpeg")

    assert bytes(buffer) == original
    assert isinstance(result, bytes)


# --- colour hints, profiles and pixel streams cut to what is needed -------------------------


def _segment(marker: int, stripped: bytes) -> bytes:
    found = [payload for m, payload in read_jpeg(stripped).segments if m == marker]
    assert len(found) == 1, f"expected one 0x{marker:02X} segment"
    return found[0]


def test_strip_metadata_cuts_a_jfif_thumbnail_to_the_fixed_header() -> None:
    thumbnail = jpeg_segment(
        0xE0,
        b"JFIF\x00\x01\x02\x01\x00\x48\x00\x48\x02\x02" + b"\x10\x20\x30" * 4 + comment_marker(),
    )
    data = jpeg_image(before=(thumbnail,))
    assert TRAILER_MARK not in data and comment_marker() in data

    stripped = images().strip_metadata(data, "jpeg")

    assert _segment(0xE0, stripped) == b"JFIF\x00\x01\x02\x01\x00\x48\x00\x48\x00\x00"
    assert comment_marker() not in stripped
    assert images().strip_metadata(stripped, "jpeg") == stripped, "idempotent"
    short = jpeg_image(before=(jpeg_segment(0xE0, b"JFIF\x00\x01"),))
    assert 0xE0 not in read_jpeg(images().strip_metadata(short, "jpeg")).markers


def comment_marker() -> bytes:
    return b"FICTIONAL-THUMBNAIL-TEXT-marker"


def test_strip_metadata_cuts_an_adobe_segment_to_its_first_twelve_bytes() -> None:
    adobe = b"Adobe\x00\x64\x80\x00\x00\x00\x02"
    assert len(adobe) == 12
    data = jpeg_image(before=(jpeg_segment(0xEE, adobe + comment_marker()),))

    stripped = images().strip_metadata(data, "jpeg")

    assert _segment(0xEE, stripped) == adobe
    assert comment_marker() not in stripped
    assert images().strip_metadata(stripped, "jpeg") == stripped
    short = jpeg_image(before=(jpeg_segment(0xEE, b"Adobe\x00\x64"),))
    assert 0xEE not in read_jpeg(images().strip_metadata(short, "jpeg")).markers


def test_strip_metadata_keeps_only_the_first_jfif_and_adobe_segment() -> None:
    adobe = jpeg_segment(0xEE, b"Adobe\x00\x64\x80\x00\x00\x00\x00\x01")
    data = jpeg_image(before=(jfif_segment(), jfif_segment(), adobe, adobe))

    markers = read_jpeg(images().strip_metadata(data, "jpeg")).markers

    assert markers.count(0xE0) == 1 and markers.count(0xEE) == 1


def test_strip_metadata_drops_reserved_and_extension_segments() -> None:
    reserved = tuple(
        jpeg_segment(marker, comment_marker()) for marker in (0xC8, 0xF0, 0xF7, 0xFD, 0x05, 0x7F)
    )
    data = jpeg_image(before=reserved)
    assert data.count(comment_marker()) == len(reserved)

    stripped = images().strip_metadata(data, "jpeg")

    assert comment_marker() not in stripped
    assert not {0xC8, 0xF0, 0xF7, 0xFD, 0x05, 0x7F} & set(read_jpeg(stripped).markers)
    assert read_jpeg(stripped).scan == SCAN


def test_strip_metadata_drops_a_colour_profile_above_the_cap_whole() -> None:
    module = images()
    cap = module.ICC_MAX_BYTES
    assert cap == 64 * 1024

    def profile(*sizes: int) -> bytes:
        parts = tuple(
            jpeg_segment(0xE2, b"ICC_PROFILE\x00" + bytes([n, len(sizes)]) + bytes(size))
            for n, size in enumerate(sizes, 1)
        )
        return jpeg_image(before=parts)

    overhead = len(b"ICC_PROFILE\x00") + 2
    fits = profile(cap // 2 - overhead, cap // 2 - overhead)
    too_big = profile(cap // 2, cap // 2)  # one profile in two parts, together above the cap
    huge = profile(60_000, 60_000, 60_000)

    assert len(jpeg_icc(module.strip_metadata(fits, "jpeg"))) == 2
    for data in (too_big, huge):
        stripped = module.strip_metadata(data, "jpeg")
        assert jpeg_icc(stripped) == [], "no part of an oversized profile is left"
        assert read_jpeg(stripped).scan == SCAN and len(stripped) < 1000


def test_strip_metadata_refuses_a_jpeg_with_a_second_frame_header() -> None:
    second = jpeg_segment(0xC2, bytes([8]) + b"\x00\x64\x00\x64" + b"\x01\x11\x00")

    with pytest.raises(invalid_image()):
        images().strip_metadata(jpeg_image(after_frame=(second,)), "jpeg")


def _png_with_stream(stream: bytes, width: int = 100, height: int = 120) -> bytes:
    return (
        PNG_SIGNATURE
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + png_chunk(b"IDAT", stream)
        + png_chunk(b"IEND", b"")
    )


def test_strip_metadata_refuses_an_undecodable_png_pixel_stream() -> None:
    whole = zlib.compress(PIXELS * 3)
    for stream in (b"this is not a zlib stream", whole[:-6], b"\x78\x9c" + b"\xff" * 20, b""):
        with pytest.raises(invalid_image()):
            images().strip_metadata(_png_with_stream(stream), "png")
    split = (
        PNG_SIGNATURE
        + png_ihdr(100, 120)
        + png_chunk(b"IDAT", whole[:10])
        + png_chunk(b"IDAT", whole[10:])
        + png_chunk(b"IEND", b"")
    )
    assert images().strip_metadata(split, "png") == split, "a stream split over chunks is fine"


def test_strip_metadata_refuses_a_png_stream_far_bigger_than_its_header_states() -> None:
    bomb = zlib.compress(bytes(30_000_000), 9)  # a few dozen KB that inflate to 30 MB
    assert len(bomb) < 100_000

    with pytest.raises(invalid_image()):
        images().strip_metadata(_png_with_stream(bomb, 100, 120), "png")
    # The same stream is fine for a picture whose header allows that much.
    big = _png_with_stream(zlib.compress(bytes(1_000_000)), 400, 400)
    assert images().strip_metadata(big, "png") == big
    interlaced_rows = zlib.compress(bytes(100 * (1 + 100 * 8) + 100))
    assert images().strip_metadata(_png_with_stream(interlaced_rows, 100, 100), "png")


def test_strip_metadata_drops_a_png_colour_profile_above_the_cap_and_a_second_one() -> None:
    module = images()
    cap = module.ICC_MAX_BYTES
    just_fits = png_chunk(b"iCCP", b"p\x00\x00" + bytes(cap - 3))
    too_big = png_chunk(b"iCCP", b"p\x00\x00" + bytes(cap - 2))

    assert b"iCCP" in png_types(module.strip_metadata(png_image(before_idat=(just_fits,)), "png"))
    assert b"iCCP" not in png_types(module.strip_metadata(png_image(before_idat=(too_big,)), "png"))
    twice = png_image(before_idat=(icc_chunk(), icc_chunk()))
    assert png_types(module.strip_metadata(twice, "png")).count(b"iCCP") == 1
