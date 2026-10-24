"""Pure render document and text cleaning (M6.1; spec 2.4; ADR-106 item 2).

The names under test are imported inside the tests; API notes are at the top of
``tests/contract/test_document_renderer_contract.py``.
"""

import dataclasses
import unicodedata

import pytest

from tests.render_support import (
    PHOTOS,
    jpeg_header_only,
    png_header_only,
    rendering,
    sample_document,
)


def test_clean_render_text_strips_controls_keeps_tabs_and_newlines() -> None:
    clean = rendering().clean_render_text

    assert clean("a\x00b\x07c\x1bd\x7fe\x85f\x0bg\x0ch") == "abcdefgh"
    assert clean("tab\there\nnew line") == "tab\there\nnew line"
    assert clean("windows\r\nending\rold mac") == "windows\nendingold mac"
    # Not control characters: accents, euro, emoji, bidi marks and zero-width characters stay.
    kept = "café € \U0001f600 ‮‬ ​­ é"
    assert clean(kept) == kept
    assert clean("") == ""
    assert clean("plain") == "plain"
    assert not [c for c in clean("\x00\x01\x02\x1f\x7f\x80\x9f") if unicodedata.category(c) == "Cc"]


def test_clean_render_text_drops_what_xml_cannot_hold() -> None:
    clean = rendering().clean_render_text

    assert clean(f"a{chr(0xD800)}b{chr(0xDFFF)}c{chr(0xD83D)}{chr(0xDE00)}d") == "abcd"
    assert clean(f"a{chr(0xFFFE)}b{chr(0xFFFF)}c") == "abc"
    # Allowed in XML 1.0, so kept: the replacement character, private use, other noncharacters.
    kept = "".join(chr(code) for code in (0xFFFD, 0xE000, 0x10FFFF, 0xFDD0, 0x1FFFE, 0x20))
    assert clean(kept) == kept


def test_plain_text_lists_header_headings_entries_and_lines_in_order() -> None:
    document = sample_document(
        header_lines=("Alex Example", "", "Lisboa\x00"),
        sections=(
            rendering().RenderSection(
                heading="Skills",
                lines=("Python",),
                entries=(rendering().RenderEntry(heading="Role", lines=("One", "\x07", "Two")),),
            ),
            rendering().RenderSection(heading="Empty"),
        ),
        title="Not in the body",
        author="Nor this",
    )

    assert document.plain_text() == "Alex Example\nLisboa\nSkills\nPython\nRole\nOne\nTwo\nEmpty"


def test_render_document_defaults_and_immutability() -> None:
    module = rendering()
    document = module.RenderDocument(title="CV", author="Alex Example")

    assert document.page_size == "A4"
    assert document.header_lines == ()
    assert document.sections == ()
    assert document.photo is None and document.photo_type is None
    assert document.plain_text() == ""
    with pytest.raises(dataclasses.FrozenInstanceError):
        document.title = "Other"


def test_render_document_rejects_an_unknown_page_size() -> None:
    with pytest.raises(ValueError):
        sample_document(page_size="A3")


@pytest.mark.parametrize(
    "values",
    [
        {"photo": PHOTOS["png"]},  # bytes without a type
        {"photo_type": "png"},  # a type without bytes
        {"photo": PHOTOS["png"], "photo_type": "gif"},  # only JPEG and PNG
    ],
    ids=["bytes-only", "type-only", "unsupported-type"],
)
def test_render_document_photo_needs_bytes_and_a_jpeg_or_png_type(
    values: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        sample_document(**values)


# ---- photo_dimensions: the size a photo's own header states, read without an image library ----


def test_photo_dimensions_reads_png_and_jpeg_headers() -> None:
    dimensions = rendering().photo_dimensions

    assert dimensions(PHOTOS["png"], "png") == (2, 2)
    assert dimensions(PHOTOS["jpeg"], "jpeg") == (2, 2)
    assert dimensions(png_header_only(5000, 7), "png") == (5000, 7)
    assert dimensions(png_header_only(1, 65_535), "png") == (1, 65_535)
    assert dimensions(jpeg_header_only(8000, 6), "jpeg") == (8000, 6)
    assert dimensions(jpeg_header_only(65_535, 65_535), "jpeg") == (65_535, 65_535)


def test_photo_dimensions_png_big_endian_and_wide_values() -> None:
    assert rendering().photo_dimensions(png_header_only(0x01020304, 5), "png") == (0x01020304, 5)


def test_photo_dimensions_is_none_when_the_bytes_are_not_the_type() -> None:
    dimensions = rendering().photo_dimensions

    assert dimensions(PHOTOS["jpeg"], "png") is None
    assert dimensions(PHOTOS["png"], "jpeg") is None
    assert dimensions(b"GIF89a" + b"\x00" * 30, "png") is None
    assert dimensions(b"", "png") is None
    assert dimensions(b"", "jpeg") is None


def test_photo_dimensions_is_none_for_truncated_or_odd_headers() -> None:
    dimensions = rendering().photo_dimensions
    png = png_header_only(10, 10)
    jpeg = jpeg_header_only(10, 10)

    for cut in range(len(png[:24])):
        assert dimensions(png[:cut], "png") is None, cut
    assert dimensions(png[:12] + b"IDAT" + png[16:], "png") is None, "the first chunk is not IHDR"
    for cut in range(len(jpeg[:30])):
        result = dimensions(jpeg[:cut], "jpeg")
        assert result is None or cut >= 29, cut  # only a complete frame header gives a size
    assert dimensions(png_header_only(0, 10), "png") is None
    assert dimensions(jpeg_header_only(10, 0), "jpeg") is None
    # Picture data (SOS) before any frame header, a bad marker, and a segment shorter than itself.
    assert dimensions(b"\xff\xd8\xff\xda\x00\x02", "jpeg") is None
    assert dimensions(b"\xff\xd8\xff\xe0\x00\x00" + b"\x00" * 20, "jpeg") is None
    assert dimensions(b"\xff\xd8\xff\xe0\x00\x04ab\x00\xc0\x00\x0b", "jpeg") is None


def test_photo_dimensions_skips_other_segments_and_fill_bytes() -> None:
    comment = b"\xff\xfe" + (2 + 4).to_bytes(2, "big") + b"\xff\xc0\x00\x00"  # a fake frame inside
    frame = jpeg_header_only(321, 123)[2:]
    photo = b"\xff\xd8" + comment + b"\xff\xff" + frame

    assert rendering().photo_dimensions(photo, "jpeg") == (321, 123)
