"""Word documents through python-docx (ADR-106). The only render module that imports ``docx``.

Styles are set in code, not taken from the library's template. The clock is the only source of
the file's dates, including the zip entry times, so the same document and clock give the same
bytes. Document text goes in as text runs only (never as markup), after control characters are
removed. The template's application properties, thumbnail and custom XML are replaced or dropped
so the file says nothing about the tool or the template.
"""

import io
import zipfile
from datetime import UTC, datetime
from typing import Final

from docx import Document
from docx.document import Document as WordDocument
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml.ns import qn
from docx.shared import Mm, Pt, RGBColor
from docx.styles.style import ParagraphStyle
from docx.text.paragraph import Paragraph

from app.domain.clock import Clock
from app.domain.rendering import RenderDocument, clean_render_text
from app.render import RenderedFile

MEDIA_TYPE: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
FONT: Final = "Arial"
PAGE_MM: Final = {"A4": (210.0, 297.0), "Letter": (215.9, 279.4)}
MARGIN_MM: Final = 20.0
PHOTO_WIDTH_MM: Final = 30.0
# Style name, size in points, bold, space before, space after (points).
STYLES: Final = (
    ("Normal", 10.5, False, 0.0, 3.0),
    ("Heading 1", 13.0, True, 12.0, 4.0),
    ("Heading 2", 11.0, True, 6.0, 2.0),
)
THEME_FONT_ATTRIBUTES: Final = ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme")
PHOTO_ALT_TEXT: Final = "Photo"
EXTENDED_PROPERTIES_XML: Final = (
    b"<?xml version='1.0' encoding='UTF-8' standalone='yes'?>\n"
    b'<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/'
    b'extended-properties"/>'
)
RELATIONSHIP_BASE: Final = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
EXTENDED_PROPERTIES: Final = RELATIONSHIP_BASE + "extended-properties"
CUSTOM_XML: Final = RELATIONSHIP_BASE + "customXml"
THUMBNAIL: Final = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail"


class WordRenderer:
    format: Final = "docx"
    media_type: Final = MEDIA_TYPE

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def render(self, document: RenderDocument) -> RenderedFile:
        now = self._clock.now()
        word = Document()
        _set_page(word, document.page_size)
        _set_styles(word)
        _set_properties(word, document, now)
        _strip_template_parts(word)
        if document.photo is not None:
            picture = word.add_picture(io.BytesIO(document.photo), width=Mm(PHOTO_WIDTH_MM))
            picture._inline.docPr.set("descr", PHOTO_ALT_TEXT)
        _add_body(word, document)
        buffer = io.BytesIO()
        word.save(buffer)
        return RenderedFile(content=_restamp(buffer.getvalue(), now), page_count=None)


def _set_page(word: WordDocument, page_size: str) -> None:
    width, height = PAGE_MM[page_size]
    section = word.sections[0]
    section.page_width, section.page_height = Mm(width), Mm(height)
    section.left_margin = section.right_margin = Mm(MARGIN_MM)
    section.top_margin = section.bottom_margin = Mm(MARGIN_MM)


def _set_styles(word: WordDocument) -> None:
    for name, size, bold, before, after in STYLES:
        style = word.styles[name]
        if style.type != WD_STYLE_TYPE.PARAGRAPH or not isinstance(style, ParagraphStyle):
            raise TypeError(f"{name} is not a paragraph style")
        style.font.name = FONT
        style.font.size = Pt(size)
        style.font.bold = bold
        style.font.italic = False
        style.font.color.rgb = RGBColor(0, 0, 0)
        _drop_theme_fonts(style)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.keep_with_next = name.startswith("Heading")


def _drop_theme_fonts(style: ParagraphStyle) -> None:
    """The template's headings use theme fonts, which override the name set above."""
    fonts = style.element.rPr.rFonts if style.element.rPr is not None else None
    if fonts is None:
        return
    for attribute in THEME_FONT_ATTRIBUTES:
        fonts.attrib.pop(qn(attribute), None)


def _set_properties(word: WordDocument, document: RenderDocument, now: datetime) -> None:
    properties = word.core_properties
    properties.title = clean_render_text(document.title)
    properties.author = clean_render_text(document.author)
    properties.last_modified_by = ""
    properties.comments = ""
    properties.subject = ""
    properties.keywords = ""
    properties.category = ""
    properties.created = now
    properties.modified = now
    properties.last_printed = now
    properties.revision = 1


def _strip_template_parts(word: WordDocument) -> None:
    """Neutralise the template's application properties; drop its thumbnail and custom XML."""
    package = word.part.package
    for rel_id, rel in list(package.rels.items()):
        if rel.reltype == THUMBNAIL:
            del package.rels[rel_id]
        elif rel.reltype == EXTENDED_PROPERTIES:
            rel.target_part._blob = EXTENDED_PROPERTIES_XML
    for rel_id, rel in list(word.part.rels.items()):
        if rel.reltype == CUSTOM_XML:
            del word.part.rels[rel_id]


def _add_body(word: WordDocument, document: RenderDocument) -> None:
    for line in document.header_lines:
        _add(word, line)
    for section in document.sections:
        _add(word, section.heading, "Heading 1")
        for line in section.lines:
            _add(word, line)
        for entry in section.entries:
            _add(word, entry.heading, "Heading 2")
            for line in entry.lines:
                _add(word, line)


def _add(word: WordDocument, text: str, style: str | None = None) -> Paragraph | None:
    cleaned = clean_render_text(text)
    if not cleaned:
        return None
    return word.add_paragraph(cleaned, style=style)


def _restamp(content: bytes, moment: datetime) -> bytes:
    """Rewrite the archive with the clock's time on every entry (the library stamps its own)."""
    utc = moment.astimezone(UTC)
    stamp = (utc.year, utc.month, utc.day, utc.hour, utc.minute, utc.second)
    output = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(content)) as source,
        zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for entry in source.infolist():
            info = zipfile.ZipInfo(entry.filename, stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            target.writestr(info, source.read(entry.filename))
    return output.getvalue()
