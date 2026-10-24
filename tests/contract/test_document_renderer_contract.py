"""Shared ``DocumentRenderer`` contract (M6.1; AC-M6.5, AC-M6.6 groundwork; spec 2.4, 4.1).

API assumed by these tests (the implementer follows it; change it only with the test author):

* ``app.domain.rendering`` (pure):
  ``RenderEntry(heading: str, lines: tuple[str, ...] = ())``,
  ``RenderSection(heading: str, lines: tuple[str, ...] = (),
  entries: tuple[RenderEntry, ...] = ())``,
  ``RenderDocument(title: str, author: str, page_size: "A4" | "Letter" = "A4",
  header_lines: tuple[str, ...] = (), sections: tuple[RenderSection, ...] = (),
  photo: bytes | None = None, photo_type: "jpeg" | "png" | None = None)``, all frozen
  dataclasses, and ``clean_render_text(text) -> str``.
  ``RenderDocument.plain_text() -> str`` is the text a reader must find in a rendered file: the
  header lines, then for each section its heading, its own lines, then each entry's heading and
  lines, every piece cleaned with ``clean_render_text``, pieces that are empty after cleaning
  dropped, joined with "\\n". Title and author are metadata and are not part of it.
* ``app.render``: ``DocumentRenderer`` protocol (``format``, ``media_type``,
  ``render(document) -> RenderedFile``), ``RenderedFile(content: bytes, page_count: int | None)``
  (``None`` where the format has no fixed pages, as for Word) and ``RendererUnavailable``
  (an ``Exception``). Importing ``app.render`` imports no vendor library.
* ``app.render.providers.word.WordRenderer(clock)``: ``format == "docx"``; the clock is the only
  source of the file's dates, including the zip entry times, so the same document and clock give
  the same bytes.

M6.2 adds ``"pdf"`` to ``RENDERERS`` and to ``READERS``; every sub-check below then runs for it.
"""

import importlib
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from typing import Any

import pytest

from tests.fakes.clock import FrozenClock
from tests.render_support import (
    A4_MM,
    LETTER_MM,
    NOW,
    PHOTOS,
    as_utc,
    docx_core_properties,
    docx_media,
    docx_metadata_xml,
    docx_package_parts,
    docx_page_size_mm,
    docx_paragraphs,
    docx_picture_descriptions,
    docx_text,
    rendering,
    sample_document,
    word_renderer,
)

pytestmark = pytest.mark.contract

RENDERERS = ["word"]
TOOL_NAMES = ("python-docx", "docx", "lxml", "python", "claude", "reportlab", "weasyprint")


@dataclass(frozen=True)
class Reader:
    """Opens a rendered file of one format, the way a user's program would."""

    text: Any  # bytes -> str: non-empty lines in order, joined with "\n"
    page_size_mm: Any  # bytes -> (width, height) of the first page
    image_count: Any  # bytes -> number of embedded pictures
    title_and_author: Any  # bytes -> (title, author)
    created_modified: Any  # bytes -> (created, modified) as aware datetimes
    metadata_text: Any  # bytes -> all metadata as text, for tool-name searches


def _word_dates(content: bytes) -> tuple[Any, Any]:
    properties = docx_core_properties(content)
    return as_utc(properties.created), as_utc(properties.modified)


def _word_title_author(content: bytes) -> tuple[str, str]:
    properties = docx_core_properties(content)
    return properties.title, properties.author


READERS = {
    "word": Reader(
        text=docx_text,
        page_size_mm=docx_page_size_mm,
        image_count=lambda content: len(docx_media(content)),
        title_and_author=_word_title_author,
        created_modified=_word_dates,
        metadata_text=docx_metadata_xml,
    )
}


def make_renderer(name: str, clock: FrozenClock) -> Any:
    if name == "word":
        return word_renderer(clock)
    raise AssertionError(f"unknown renderer {name}")


@pytest.fixture(params=RENDERERS)
def kind(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


def _control_characters(text: str) -> list[str]:
    return [c for c in text if unicodedata.category(c) == "Cc" and c not in "\t\n"]


@pytest.mark.parametrize("name", RENDERERS)
def test_document_renderer_contract(name: str, clock: FrozenClock) -> None:
    render_module = importlib.import_module("app.render")
    renderer = make_renderer(name, clock)

    assert isinstance(renderer.format, str) and renderer.format
    assert isinstance(renderer.media_type, str) and "/" in renderer.media_type
    if name == "word":
        assert renderer.format == "docx"
        assert (
            renderer.media_type
            == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )

    document = sample_document()
    rendered = renderer.render(document)

    assert isinstance(rendered, render_module.RenderedFile)
    assert isinstance(rendered.content, bytes) and rendered.content
    assert rendered.page_count is None or rendered.page_count >= 1
    assert renderer.render(document).content == rendered.content, "same input, same bytes"
    assert document == sample_document(), "rendering leaves the document alone"

    assert issubclass(render_module.RendererUnavailable, Exception)
    assert hasattr(render_module, "DocumentRenderer")


def test_render_package_imports_no_vendor_library() -> None:
    """``app.render`` is the interface; python-docx stays in the provider package."""
    code = (
        "import sys, app.render\n"
        "loaded = sorted(m for m in ('docx', 'lxml', 'weasyprint', 'reportlab', 'fpdf', 'pypdf')"
        " if m in sys.modules)\n"
        "assert not loaded, loaded\n"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_round_trip_text_equals_document_text(kind: str, clock: FrozenClock) -> None:
    document = sample_document()
    rendered = make_renderer(kind, clock).render(document)

    extracted = READERS[kind].text(rendered.content)

    assert extracted == document.plain_text()
    # Spot checks that the comparison isn't vacuous: accents, the euro sign and a long number.
    assert "déploiement" in extracted
    assert "12 000 €" in extracted
    assert extracted.index("Experience") < extracted.index("Skills")


def test_hostile_text_rendered_literally(kind: str, clock: FrozenClock) -> None:
    module = rendering()
    long_word = "W" * 200
    hostile = (
        "<script>alert(1)</script>",
        "</w:t></w:p><w:p><w:t>injected",
        "{{ 7 * 7 }} {% for x in y %}",
        "&amp; &lt;b&gt; &#x41;",
        "bidi ‮evil‬ ⁦x⁩",
        long_word,
    )
    document = sample_document(
        title="<script>t</script>",
        author="{{ author }} &amp;",
        header_lines=hostile[:2],
        sections=(
            module.RenderSection(
                heading=hostile[2],
                lines=(hostile[3],),
                entries=(module.RenderEntry(heading=hostile[4], lines=(hostile[5],)),),
            ),
        ),
    )

    rendered = make_renderer(kind, clock).render(document)
    extracted = READERS[kind].text(rendered.content)

    assert extracted == document.plain_text()
    for text in hostile:
        assert text in extracted, f"{text[:20]!r} was changed"
    assert long_word in extracted, "a 200-character word is neither cut nor split"
    assert READERS[kind].title_and_author(rendered.content) == (
        "<script>t</script>",
        "{{ author }} &amp;",
    )
    if kind == "word":
        # The injected markup stays text: no element came from the document's own words.
        import zipfile
        from io import BytesIO

        with zipfile.ZipFile(BytesIO(rendered.content)) as archive:
            body = archive.read("word/document.xml").decode("utf-8")
        assert "<script>" not in body
        assert "<w:t>injected" not in body and '<w:t xml:space="preserve">injected' not in body
        assert "&lt;/w:t&gt;" in body, "the closing tag in the text is escaped, not interpreted"


def test_control_characters_removed(kind: str, clock: FrozenClock) -> None:
    module = rendering()
    document = sample_document(
        title="Al\x00ex CV",
        author="Alex\x07 Example",
        header_lines=("Ali\x00ce\x1b[31m Example", "\x0bVertical\x0c tab", "Bell\x07"),
        sections=(
            module.RenderSection(
                heading="Exp\x7ferience",
                lines=("tab\there", "line\nbreak", "windows\r\nending", "c1\x85control"),
            ),
        ),
    )

    rendered = make_renderer(kind, clock).render(document)
    extracted = READERS[kind].text(rendered.content)

    assert _control_characters(extracted) == []
    assert extracted == document.plain_text()
    assert "Alice[31m Example" in extracted
    assert "Experience" in extracted
    assert "tab\there" in extracted and "line\nbreak" in extracted
    assert "windows\nending" in extracted
    title, author = READERS[kind].title_and_author(rendered.content)
    assert (title, author) == ("Alex CV", "Alex Example")


def test_characters_xml_cannot_hold_never_break_rendering(kind: str, clock: FrozenClock) -> None:
    """Lone surrogates and U+FFFE/U+FFFF in any field are dropped, not raised as errors."""
    module = rendering()
    bad = chr(0xD800) + chr(0xDFFF) + chr(0xFFFE) + chr(0xFFFF)
    document = sample_document(
        title=f"Ti{bad}tle",
        author=f"Au{bad}thor",
        header_lines=(f"Head{bad}er", bad),
        sections=(
            module.RenderSection(
                heading=f"Sec{bad}tion",
                lines=(f"li{bad}ne",),
                entries=(module.RenderEntry(heading=f"En{bad}try", lines=(f"en{bad}d",)),),
            ),
        ),
    )

    rendered = make_renderer(kind, clock).render(document)
    extracted = READERS[kind].text(rendered.content)

    assert extracted == "Header\nSection\nline\nEntry\nend"
    assert extracted == document.plain_text()
    assert READERS[kind].title_and_author(rendered.content) == ("Title", "Author")


@pytest.mark.parametrize("photo_type", ["png", "jpeg"])
def test_photo_only_when_given(kind: str, photo_type: str, clock: FrozenClock) -> None:
    renderer = make_renderer(kind, clock)

    without = renderer.render(sample_document())
    with_photo = renderer.render(sample_document(photo=PHOTOS[photo_type], photo_type=photo_type))

    assert READERS[kind].image_count(without.content) == 0
    assert READERS[kind].image_count(with_photo.content) == 1
    assert READERS[kind].text(with_photo.content) == sample_document().plain_text(), (
        "the photo adds no text"
    )
    document = sample_document(photo=PHOTOS[photo_type], photo_type=photo_type)
    assert renderer.render(document).content == renderer.render(document).content, (
        "same document with a photo, same bytes"
    )
    if kind == "word":
        assert docx_media(with_photo.content) == [PHOTOS[photo_type]], "stored as given"
        assert docx_picture_descriptions(with_photo.content) == ["Photo"], "neutral alt text"


@pytest.mark.parametrize(
    ("page_size", "expected"), [("A4", A4_MM), ("Letter", LETTER_MM)], ids=["A4", "Letter"]
)
def test_page_size_a4_and_letter(
    kind: str, page_size: str, expected: tuple[float, float], clock: FrozenClock
) -> None:
    rendered = make_renderer(kind, clock).render(sample_document(page_size=page_size))

    width, height = READERS[kind].page_size_mm(rendered.content)

    assert width == pytest.approx(expected[0], abs=1.0)
    assert height == pytest.approx(expected[1], abs=1.0)
    assert READERS[kind].text(rendered.content) == sample_document().plain_text()


def test_metadata_from_document_and_clock_only(kind: str, clock: FrozenClock) -> None:
    renderer = make_renderer(kind, clock)
    document = sample_document()

    rendered = renderer.render(document)
    reader = READERS[kind]

    assert reader.title_and_author(rendered.content) == (document.title, document.author)
    assert reader.created_modified(rendered.content) == (NOW, NOW)
    metadata = reader.metadata_text(rendered.content).lower()
    for tool in TOOL_NAMES:
        assert tool not in metadata, f"{tool!r} found in the file's metadata"
    if kind == "word":
        # The library's template says it was made by Word on a Mac, with counts that are not ours.
        for leftover in ("application", "template", "microsoft", "normal.dotm", "appversion"):
            assert leftover not in metadata, f"{leftover!r} found in the file's metadata"
        parts = docx_package_parts(rendered.content)
        assert not [p for p in parts if "thumbnail" in p or p.startswith("customXml/")], parts

    # The date moves with the injected clock and with nothing else.
    clock.set(NOW.replace(year=2027, month=3, day=2, hour=8, minute=0, second=10))
    later = renderer.render(document)
    assert reader.created_modified(later.content) == (clock.now(), clock.now())
    assert later.content != rendered.content


def test_word_zip_entries_carry_the_clock_time_only(clock: FrozenClock) -> None:
    import zipfile
    from io import BytesIO

    content = make_renderer("word", clock).render(sample_document()).content

    with zipfile.ZipFile(BytesIO(content)) as archive:
        stamps = {entry.date_time for entry in archive.infolist()}
    assert stamps == {(2026, 10, 24, 10, 30, 40)}


def test_word_headings_use_heading_styles_and_stay_with_the_next_line(
    clock: FrozenClock,
) -> None:
    """Code-owned styles: section headings are real headings kept with the line after them."""
    document = sample_document()
    content = make_renderer("word", clock).render(document).content

    by_text = {p.text: p for p in docx_paragraphs(content)}
    headings = [(section.heading, "Heading 1") for section in document.sections]
    for section in document.sections:
        headings.extend((entry.heading, "Heading 2") for entry in section.entries)
    assert len(headings) == 4
    for text, style in headings:
        heading = by_text[text]
        assert heading.style.name == style, heading.style.name
        assert (
            heading.paragraph_format.keep_with_next or heading.style.paragraph_format.keep_with_next
        )
