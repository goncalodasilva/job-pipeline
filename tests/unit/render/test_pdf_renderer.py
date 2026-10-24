"""PDF renderer behaviour beyond the shared contract, through the public API only (M6.2;
AC-M6.5, AC-M6.6; ADR-107, ADR-108).

The shared suite (``tests/contract/test_document_renderer_contract.py``) runs for ``"pdf"`` too.
This file covers what only a PDF has: pages, headings kept with their first line, caps, metadata
and threads. It is library-neutral: nothing here names the PDF library or reaches into it. What
depends on typst (the lazy loader, compiler keywords, the time limit, the in-flight limit, a
missing library, fonts) is in ``test_typst_renderer.py`` and ``test_typst_isolation.py``.

API assumed by these tests (the implementer follows it; change it only with the test author):

* ``app.render.providers.pdf.PdfRenderer(clock, *, max_units=MAX_UNITS,
  max_characters=MAX_CHARACTERS, max_photo_bytes=MAX_PHOTO_BYTES, max_photo_side=...,
  max_photo_pixels=..., time_limit_seconds=...)``; ``render`` is a plain synchronous method (the
  service calls it with ``asyncio.to_thread``) and is thread-safe.
* Units are the text pieces of the document: header lines, section headings and lines, entry
  headings and lines (counted before cleaning). Characters are the sum of their lengths plus the
  title and the author. Over a cap, ``render`` raises ``app.domain.errors.RenderTooLarge`` (an
  ``AppError``); a photo whose header contradicts its type raises ``RenderFailed``. Neither message
  contains document text.
* Constants: ``MAX_UNITS``, ``MAX_CHARACTERS``, ``MAX_PHOTO_BYTES``, ``TIME_LIMIT_SECONDS``.
"""

import asyncio
import inspect
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.domain.errors import AppError, RenderFailed, RenderTooLarge
from tests.fakes.clock import FrozenClock
from tests.render_support import (
    BIDI_CONTROLS,
    NOW,
    PHOTOS,
    jpeg_header_only,
    pdf_dates,
    pdf_info,
    pdf_metadata_text,
    pdf_module,
    pdf_normalise,
    pdf_page_count,
    pdf_page_lines,
    pdf_renderer,
    pdf_squash,
    pdf_text,
    pdf_title_author,
    pdf_xref_is_valid,
    png_header_only,
    rendering,
    sample_document,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


def _pieces(document: Any) -> list[str]:
    found = list(document.header_lines)
    for section in document.sections:
        found.append(section.heading)
        found.extend(section.lines)
        for entry in section.entries:
            found.append(entry.heading)
            found.extend(entry.lines)
    return found


def _many_lines(count: int, page_size: str = "A4") -> Any:
    module = rendering()
    lines = tuple(f"Filler line {index:03d}" for index in range(count))
    return sample_document(
        page_size=page_size,
        sections=(module.RenderSection(heading="Experience", lines=lines),),
    )


# ---- Shape ------------------------------------------------------------------------------------


def test_pdf_renderer_identity_and_sync_render(clock: FrozenClock) -> None:
    renderer = pdf_renderer(clock)

    assert renderer.format == "pdf"
    assert renderer.media_type == "application/pdf"
    assert not inspect.iscoroutinefunction(renderer.render), "the service uses to_thread"
    assert not inspect.isawaitable(renderer.render(sample_document()))


def test_pdf_render_runs_off_the_event_loop_thread(clock: FrozenClock) -> None:
    renderer = pdf_renderer(clock)
    expected = renderer.render(sample_document()).content

    async def run() -> bytes:
        return (await asyncio.to_thread(renderer.render, sample_document())).content

    assert asyncio.run(run()) == expected


# ---- Pages ------------------------------------------------------------------------------------


def test_pdf_page_count_reported(clock: FrozenClock) -> None:
    renderer = pdf_renderer(clock)

    one = renderer.render(sample_document())
    many = renderer.render(_many_lines(150))
    letter = renderer.render(_many_lines(150, "Letter"))

    assert isinstance(one.page_count, int) and one.page_count == 1
    assert one.page_count == pdf_page_count(one.content)
    assert isinstance(many.page_count, int) and many.page_count >= 3
    assert many.page_count == pdf_page_count(many.content)
    assert letter.page_count == pdf_page_count(letter.content)
    assert letter.page_count >= many.page_count, "Letter is shorter than A4, so never fewer pages"


def _filler_cv(count: int, page_size: str) -> Any:
    module = rendering()
    return sample_document(
        page_size=page_size,
        header_lines=("Alex Example",),
        sections=(
            module.RenderSection(
                heading="Experience",
                entries=(
                    module.RenderEntry(
                        heading="Earlier role, Example Rail",
                        lines=tuple(f"Filler line {index:03d}" for index in range(count)),
                    ),
                    module.RenderEntry(
                        heading="ROLE-HEADING-MARK", lines=("ROLE-FIRST-LINE", "second line")
                    ),
                ),
            ),
            module.RenderSection(heading="SECTION-HEADING-MARK", lines=("SECTION-FIRST-LINE",)),
        ),
    )


def _page_of(pages: list[list[str]], line: str) -> int:
    found = [index for index, lines in enumerate(pages) if line in lines]
    assert len(found) == 1, f"{line!r} should be on exactly one page, found on {found}"
    return found[0]


def test_pdf_keeps_role_heading_with_first_line(clock: FrozenClock) -> None:
    """A role (entry) heading and a section heading never end a page: they move with the line
    after them. The sweep moves the headings through both page ends, on A4 and Letter. The CV has
    no header or footer on later pages, so the last text line of a page is body text."""
    renderer = pdf_renderer(clock)
    pairs = (
        ("ROLE-HEADING-MARK", "ROLE-FIRST-LINE"),
        ("SECTION-HEADING-MARK", "SECTION-FIRST-LINE"),
    )
    sweeps = (("A4", range(0, 115)), ("Letter", range(30, 100)))
    heading_pages: set[int] = set()
    first_line_ends_a_page = 0

    for page_size, counts in sweeps:
        for count in counts:
            rendered = renderer.render(_filler_cv(count, page_size))
            pages = pdf_page_lines(rendered.content)
            for heading, first in pairs:
                page = _page_of(pages, heading)
                assert _page_of(pages, first) == page, (
                    f"{heading} is on page {page + 1} but its first line is not "
                    f"({page_size}, {count} filler lines)"
                )
                assert pages[page][-1] != heading, (
                    f"{heading} ends page {page + 1} ({page_size}, {count} filler lines)"
                )
                heading_pages.add(page)
                if pages[page][-1] == first:
                    first_line_ends_a_page += 1

    # The sweep is not vacuous: headings were seen on the first and later pages, and a heading
    # with its first line was seen at the very bottom of a page (where an orphan would have been).
    assert {0, 1} <= heading_pages
    assert first_line_ends_a_page >= 1


# ---- Metadata and determinism -----------------------------------------------------------------


def test_pdf_metadata_deterministic_from_clock(clock: FrozenClock) -> None:
    renderer = pdf_renderer(clock)
    document = sample_document()

    first = renderer.render(document).content
    second = renderer.render(document).content
    assert first == second, "same document and clock, same bytes"
    assert pdf_dates(first) == (NOW, NOW)

    info = pdf_info(first)
    assert info["/Title"] == document.title
    assert info["/Author"] == document.author
    for key in ("/Producer", "/Creator"):
        assert "typst" not in info.get(key, "").lower(), f"{key} names the tool: {info.get(key)!r}"
    assert "typst" not in pdf_metadata_text(first).lower()

    clock.set(datetime(2027, 3, 2, 8, 0, 10, tzinfo=UTC))
    later = renderer.render(document).content
    assert pdf_dates(later) == (clock.now(), clock.now())
    assert later != first

    clock.set(NOW)
    assert renderer.render(document).content == first, "back to the first moment, the first bytes"


def test_pdf_bytes_are_the_same_in_another_process(clock: FrozenClock) -> None:
    code = (
        "import sys\n"
        "from tests.fakes.clock import FrozenClock\n"
        "from tests.render_support import NOW, pdf_renderer, sample_document\n"
        "sys.stdout.write(pdf_renderer(FrozenClock(NOW)).render(sample_document()).content.hex())\n"
    )
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONHASHSEED": "7"}
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=REPO_ROOT,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert bytes.fromhex(result.stdout) == pdf_renderer(clock).render(sample_document()).content


def test_pdf_text_with_bidi_controls_is_stripped_before_typesetting(clock: FrozenClock) -> None:
    module = rendering()
    controls = "".join(BIDI_CONTROLS)
    document = sample_document(
        header_lines=(f"Alex{controls} Example",),
        sections=(module.RenderSection(heading="Skills", lines=(f"a‮b⁦c⁩d{controls}",)),),
    )

    extracted = pdf_text(pdf_renderer(clock).render(document).content)

    assert not [c for c in extracted if c in BIDI_CONTROLS]
    assert pdf_squash("Alex Example") in pdf_squash(extracted)
    assert pdf_squash("abcd") in pdf_squash(extracted), "the letters stay, in their order"


def test_pdf_long_word_wraps_instead_of_overflowing(clock: FrozenClock) -> None:
    long_word = "W" * 200
    module = rendering()
    document = sample_document(
        sections=(module.RenderSection(heading="Skills", lines=(long_word,)),)
    )

    extracted = pdf_text(pdf_renderer(clock).render(document).content)

    assert long_word in pdf_squash(extracted)
    assert max(len(line) for line in extracted.splitlines()) < 120, "it wrapped onto several lines"


# ---- Caps -----------------------------------------------------------------------------------


def test_pdf_default_caps_are_set_and_the_sample_fits(clock: FrozenClock) -> None:
    pdf = pdf_module()
    document = sample_document()

    assert 12 <= pdf.MAX_UNITS <= 10_000
    assert sum(len(piece) for piece in _pieces(document)) < pdf.MAX_CHARACTERS <= 5_000_000
    assert len(PHOTOS["png"]) <= pdf.MAX_PHOTO_BYTES <= 20 * 1024 * 1024
    assert 0 < pdf.TIME_LIMIT_SECONDS <= 60
    assert pdf_renderer(clock).render(document).page_count == 1


def test_pdf_caps_are_exact_at_the_boundary(clock: FrozenClock) -> None:
    document = sample_document()
    units = len(_pieces(document))
    characters = sum(len(piece) for piece in _pieces(document))
    characters += len(document.title) + len(document.author)

    for limits in ({"max_units": units}, {"max_characters": characters}):
        assert pdf_renderer(clock, **limits).render(document).page_count == 1, limits
    for limits in ({"max_units": units - 1}, {"max_characters": characters - 1}):
        with pytest.raises(RenderTooLarge) as caught:
            pdf_renderer(clock, **limits).render(document)
        assert isinstance(caught.value, AppError)
        assert "Example" not in caught.value.user_message, "the message holds no document text"


def test_pdf_title_and_author_count_toward_the_character_cap_in_the_default_limit(
    clock: FrozenClock,
) -> None:
    pdf = pdf_module()
    renderer = pdf_renderer(clock)

    with pytest.raises(RenderTooLarge):
        renderer.render(sample_document(title="T" * (pdf.MAX_CHARACTERS + 1)))
    with pytest.raises(RenderTooLarge):
        renderer.render(sample_document(author="A" * (pdf.MAX_CHARACTERS + 1)))


@pytest.mark.parametrize(
    ("photo", "photo_type"),
    [
        (png_header_only(4001, 1), "png"),
        (png_header_only(1, 4001), "png"),
        (jpeg_header_only(4001, 1), "jpeg"),
        (jpeg_header_only(1, 4001), "jpeg"),
    ],
)
def test_pdf_photo_stating_a_huge_size_is_refused(
    photo: bytes, photo_type: str, clock: FrozenClock
) -> None:
    assert len(photo) < 1_000, "a small file that only claims to be big"

    with pytest.raises(RenderTooLarge):
        pdf_renderer(clock).render(sample_document(photo=photo, photo_type=photo_type))


def test_pdf_photo_pixel_cap_and_side_cap_are_separate(clock: FrozenClock) -> None:
    document = sample_document(photo=PHOTOS["png"], photo_type="png")  # 2 x 2

    assert pdf_renderer(clock, max_photo_pixels=4, max_photo_side=2).render(document).content
    with pytest.raises(RenderTooLarge):
        pdf_renderer(clock, max_photo_pixels=3).render(document)
    with pytest.raises(RenderTooLarge):
        pdf_renderer(clock, max_photo_side=1).render(document)


@pytest.mark.parametrize(
    ("photo", "photo_type"), [(PHOTOS["jpeg"], "png"), (PHOTOS["png"], "jpeg")]
)
def test_pdf_photo_not_matching_its_type_fails(
    photo: bytes, photo_type: str, clock: FrozenClock
) -> None:
    with pytest.raises(RenderFailed) as caught:
        pdf_renderer(clock).render(sample_document(photo=photo, photo_type=photo_type))

    assert caught.value.user_message == "The document couldn't be turned into a PDF."


def test_pdf_small_photos_of_both_types_still_render(clock: FrozenClock) -> None:
    for photo_type in ("png", "jpeg"):
        document = sample_document(photo=PHOTOS[photo_type], photo_type=photo_type)

        assert pdf_renderer(clock).render(document).page_count == 1


# ---- Threads ----------------------------------------------------------------------------------


def test_pdf_renders_from_worker_threads_with_identical_bytes(clock: FrozenClock) -> None:
    renderer = pdf_renderer(clock)
    document = sample_document()
    expected = renderer.render(document).content
    barrier = threading.Barrier(4)

    def work(_: int) -> list[bytes]:
        barrier.wait(timeout=30)
        return [renderer.render(document).content for _ in range(2)]

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(work, range(4)))

    assert [content for run in results for content in run] == [expected] * 8


# ---- Metadata that is the user's own text -----------------------------------------------------

HOSTILE_TITLE = "/Producer (typst 0.15) x /Creator (Typst 0.15.0)"
HOSTILE_AUTHOR = "(a) /Producer (Typst 0.15.0) b\\ c)"


def test_pdf_title_and_author_that_look_like_tool_names_come_back_unchanged(
    clock: FrozenClock,
) -> None:
    renderer = pdf_renderer(clock)
    document = sample_document(title=HOSTILE_TITLE, author=HOSTILE_AUTHOR)

    content = renderer.render(document).content

    assert pdf_title_author(content) == (HOSTILE_TITLE, HOSTILE_AUTHOR)
    info = pdf_info(content)
    assert "/Producer" not in info and "/Creator" not in info, "the tool names are still gone"
    assert "CreatorTool" not in pdf_metadata_text(content)
    assert pdf_xref_is_valid(content), "the cross-reference table still points at the objects"
    assert pdf_page_count(content) == 1
    assert pdf_squash(document.plain_text()) in pdf_squash(pdf_text(content))
    plain = renderer.render(sample_document()).content
    assert len(plain) != 0 and pdf_xref_is_valid(plain)


def test_pdf_metadata_text_with_pdf_syntax_is_kept_as_text(clock: FrozenClock) -> None:
    title = "x >> /Creator (y) << [ ( \\) endobj trailer /Info 1 0 R"
    author = "<4142> % /Producer (z)"

    content = pdf_renderer(clock).render(sample_document(title=title, author=author)).content

    assert pdf_title_author(content) == (title, author)
    assert "/Producer" not in pdf_info(content) and "/Creator" not in pdf_info(content)
    assert pdf_xref_is_valid(content)


def test_pdf_normalise_helper_documented_behaviour() -> None:
    """The comparison helper itself (so a loosened helper can't hide a renderer fault)."""
    assert pdf_normalise("a\u200bb \u202ec\u2069\n d\u00a0e") == "ab c d e"
    assert pdf_squash("a b\nc") == "abc"
    assert pdf_normalise("ﬁne") == "fine"
